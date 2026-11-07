"""Clean-room read-only companion data source for ~/.rarvis-lighthouse.

Enforces the ternary read-only contract:
  - describe() -> DataSourceDescriptor
  - refresh() -> DataSourceDescriptor
  - snapshot(expected_revision=None) -> DataSourceSnapshot

Strict guardrails:
  1. Never creates directories or SQLite files (connects exclusively with ?mode=ro).
  2. Never runs PRAGMA journal_mode=WAL or schema migrations.
  3. Honestly reports MISSING_LEDGER, UNVERSIONED_SCHEMA, UNSUPPORTED_SCHEMA,
     CORRUPTED_LEDGER, and active WAL (-wal / -shm) sidecar presence.
  4. Never splices across different transactions or revisions; raises
     RevisionMismatchError if expected_revision does not match the atomic snapshot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any

from gcl.core.regular_file import sha256_regular
from gcl.core.store import SCHEMA_VERSION, completion_awaiting_confirmation, completion_state
from gcl.runtime.files import home_path, live_controller, mission_dir, process_start, read_json

FRESHNESS_STATES = ("FRESH", "STALE", "UNKNOWN")
LEDGER_STATES = (
    "READY",
    "MISSING_HOME",
    "MISSING_LEDGER",
    "UNVERSIONED_SCHEMA",
    "UNSUPPORTED_SCHEMA",
    "CORRUPTED_LEDGER",
    "UNREADABLE_LEDGER",
)

_REQUIRED_TABLES = frozenset({
    "missions",
    "beacons",
    "mutable_fields",
    "rollovers",
    "owner_messages",
    "owner_outbox",
    "effects",
    "events",
})


class CompanionDataSourceError(RuntimeError):
    """Base error for read-only companion data source failures."""


class RevisionMismatchError(CompanionDataSourceError):
    """Raised when expected_revision does not match the current atomic snapshot revision."""

    def __init__(self, expected_revision: str, actual_revision: str | None):
        super().__init__(
            f"snapshot revision mismatch: expected {expected_revision!r}, actual {actual_revision!r}"
        )
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class DataSourceDescriptor:
    contract_version: str
    home_path: str
    ledger_path: str
    ledger_state: str
    freshness: str
    freshness_reason: str
    schema_version: int | None
    supported_schema_version: int
    revision: str | None
    active_wal: bool
    wal_size_bytes: int
    shm_present: bool
    read_only_enforced: bool
    enabled: bool
    hook_errors_present: bool
    mission_count: int
    max_event_id: int
    observed_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataSourceSnapshot:
    contract_version: str
    descriptor: DataSourceDescriptor
    revision: str | None
    freshness: str
    ledger_state: str
    missions: tuple[dict[str, Any], ...]
    beta_summary: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["missions"] = list(self.missions)
        return payload


class LighthouseReadOnlyDataSource:
    """Strictly read-only adapter over ~/.rarvis-lighthouse or synthetic fixtures."""

    CONTRACT_VERSION = "rlh.companion.datasource.v0.2"

    def __init__(
        self,
        home: str | os.PathLike | None = None,
        *,
        stale_after_s: float = 20.0,
        now_fn=time.time,
    ):
        self._explicit_home = os.fspath(home) if home is not None else None
        self.stale_after_s = float(stale_after_s)
        self._now_fn = now_fn
        self._cached_snapshot: DataSourceSnapshot | None = None

    @property
    def home(self) -> Path:
        if self._explicit_home:
            return Path(self._explicit_home).expanduser().resolve()
        explicit_env = os.environ.get("RLH_HOME") or os.environ.get("GCL_HOME")
        if explicit_env:
            return Path(explicit_env).expanduser().resolve()
        return Path("~/.rarvis-lighthouse").expanduser().resolve()

    @property
    def ledger_path(self) -> Path:
        return self.home / "state.sqlite"

    @classmethod
    def from_synthetic_bundle(
        cls,
        bundle: dict[str, Any],
        *,
        stale_after_s: float = 20.0,
        now_fn=time.time,
    ) -> "SyntheticReadOnlyDataSource":
        return SyntheticReadOnlyDataSource(bundle, stale_after_s=stale_after_s, now_fn=now_fn)

    def describe(self) -> DataSourceDescriptor:
        if self._cached_snapshot is None:
            return self.refresh()
        return self._cached_snapshot.descriptor

    def refresh(self) -> DataSourceDescriptor:
        self._cached_snapshot = self._capture_atomic_snapshot()
        return self._cached_snapshot.descriptor

    def snapshot(self, expected_revision: str | None = None) -> DataSourceSnapshot:
        current = self._capture_atomic_snapshot()
        self._cached_snapshot = current
        if expected_revision is not None and current.revision != expected_revision:
            raise RevisionMismatchError(expected_revision, current.revision)
        return current

    def _sidecar_status(self) -> tuple[bool, int, bool]:
        wal_path = Path(os.fspath(self.ledger_path) + "-wal")
        shm_path = Path(os.fspath(self.ledger_path) + "-shm")
        wal_size = 0
        wal_exists = False
        try:
            st = os.lstat(wal_path)
            if stat.S_ISREG(st.st_mode):
                wal_exists = True
                wal_size = int(st.st_size)
        except OSError:
            pass
        shm_exists = False
        try:
            st_shm = os.lstat(shm_path)
            shm_exists = stat.S_ISREG(st_shm.st_mode)
        except OSError:
            pass
        active_wal = wal_exists and wal_size > 0
        return active_wal, wal_size, shm_exists

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = self.ledger_path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db

    def _capture_atomic_snapshot(self) -> DataSourceSnapshot:
        now = float(self._now_fn())
        home = self.home
        ledger = self.ledger_path
        active_wal, wal_size, shm_present = self._sidecar_status()
        enabled = not (home / "OFF").exists() and os.environ.get("GCL_MODE", "").upper() != "OFF"
        hook_errors = (home / "native" / "hook-errors.jsonl").is_file()

        if not home.exists():
            return self._empty_snapshot(
                ledger_state="MISSING_HOME",
                freshness="UNKNOWN",
                freshness_reason="state_directory_missing",
                schema_version=None,
                active_wal=active_wal,
                wal_size=wal_size,
                shm_present=shm_present,
                enabled=enabled,
                hook_errors=hook_errors,
                now=now,
            )

        try:
            st = os.lstat(ledger)
        except OSError:
            return self._empty_snapshot(
                ledger_state="MISSING_LEDGER",
                freshness="UNKNOWN",
                freshness_reason="ledger_file_missing",
                schema_version=None,
                active_wal=active_wal,
                wal_size=wal_size,
                shm_present=shm_present,
                enabled=enabled,
                hook_errors=hook_errors,
                now=now,
            )

        if not stat.S_ISREG(st.st_mode):
            return self._empty_snapshot(
                ledger_state="UNREADABLE_LEDGER",
                freshness="UNKNOWN",
                freshness_reason="ledger_not_regular_file",
                schema_version=None,
                active_wal=active_wal,
                wal_size=wal_size,
                shm_present=shm_present,
                enabled=enabled,
                hook_errors=hook_errors,
                now=now,
            )

        try:
            db = self._connect_readonly()
        except sqlite3.OperationalError as exc:
            return self._empty_snapshot(
                ledger_state="UNREADABLE_LEDGER",
                freshness="UNKNOWN",
                freshness_reason=f"sqlite_open_failed:{exc}",
                schema_version=None,
                active_wal=active_wal,
                wal_size=wal_size,
                shm_present=shm_present,
                enabled=enabled,
                hook_errors=hook_errors,
                now=now,
            )
        except sqlite3.DatabaseError as exc:
            return self._empty_snapshot(
                ledger_state="CORRUPTED_LEDGER",
                freshness="UNKNOWN",
                freshness_reason=f"sqlite_corrupted:{exc}",
                schema_version=None,
                active_wal=active_wal,
                wal_size=wal_size,
                shm_present=shm_present,
                enabled=enabled,
                hook_errors=hook_errors,
                now=now,
            )

        try:
            # Start a single read transaction so all queries observe one atomic snapshot.
            db.execute("BEGIN")
            schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if schema_version == 0 or not (_REQUIRED_TABLES <= tables):
                return self._empty_snapshot(
                    ledger_state="UNVERSIONED_SCHEMA",
                    freshness="UNKNOWN",
                    freshness_reason=(
                        f"unversioned_or_incomplete_schema:v{schema_version}"
                    ),
                    schema_version=schema_version,
                    active_wal=active_wal,
                    wal_size=wal_size,
                    shm_present=shm_present,
                    enabled=enabled,
                    hook_errors=hook_errors,
                    now=now,
                )
            if schema_version > SCHEMA_VERSION or schema_version < 0:
                return self._empty_snapshot(
                    ledger_state="UNSUPPORTED_SCHEMA",
                    freshness="UNKNOWN",
                    freshness_reason=(
                        f"unsupported_schema_version:{schema_version}!=supported:{SCHEMA_VERSION}"
                    ),
                    schema_version=schema_version,
                    active_wal=active_wal,
                    wal_size=wal_size,
                    shm_present=shm_present,
                    enabled=enabled,
                    hook_errors=hook_errors,
                    now=now,
                )

            outbox_cols = {row[1] for row in db.execute("PRAGMA table_info(owner_outbox)")}
            rollover_cols = {row[1] for row in db.execute("PRAGMA table_info(rollovers)")}
            if not ({"beacon_version", "beacon_hash"} <= outbox_cols) or (
                "successor_session_id" not in rollover_cols
            ):
                return self._empty_snapshot(
                    ledger_state="UNVERSIONED_SCHEMA",
                    freshness="UNKNOWN",
                    freshness_reason="missing_v1_migrated_columns",
                    schema_version=schema_version,
                    active_wal=active_wal,
                    wal_size=wal_size,
                    shm_present=shm_present,
                    enabled=enabled,
                    hook_errors=hook_errors,
                    now=now,
                )

            max_event_row = db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
            max_event_id = int(max_event_row[0]) if max_event_row else 0
            mission_rows = db.execute("SELECT * FROM missions ORDER BY rowid").fetchall()

            missions: list[dict[str, Any]] = []
            for m_row in mission_rows:
                missions.append(self._project_mission(db, m_row, now))

            beta_summary = self._project_beta_summary(db)
        except sqlite3.DatabaseError as exc:
            return self._empty_snapshot(
                ledger_state="CORRUPTED_LEDGER",
                freshness="UNKNOWN",
                freshness_reason=f"sqlite_read_error:{exc}",
                schema_version=None,
                active_wal=active_wal,
                wal_size=wal_size,
                shm_present=shm_present,
                enabled=enabled,
                hook_errors=hook_errors,
                now=now,
            )
        finally:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            db.close()

        active_wal_after, wal_size_after, shm_after = self._sidecar_status()
        active_wal = active_wal or active_wal_after
        wal_size = max(wal_size, wal_size_after)
        shm_present = shm_present or shm_after

        freshness, freshness_reason = self._evaluate_overall_freshness(
            missions=missions,
            active_wal=active_wal,
            enabled=enabled,
        )
        revision = self._compute_revision(
            schema_version=schema_version,
            max_event_id=max_event_id,
            missions=missions,
            active_wal=active_wal,
            wal_size=wal_size,
            enabled=enabled,
        )
        descriptor = DataSourceDescriptor(
            contract_version=self.CONTRACT_VERSION,
            home_path=str(home),
            ledger_path=str(ledger),
            ledger_state="READY",
            freshness=freshness,
            freshness_reason=freshness_reason,
            schema_version=schema_version,
            supported_schema_version=SCHEMA_VERSION,
            revision=revision,
            active_wal=active_wal,
            wal_size_bytes=wal_size,
            shm_present=shm_present,
            read_only_enforced=True,
            enabled=enabled,
            hook_errors_present=hook_errors,
            mission_count=len(missions),
            max_event_id=max_event_id,
            observed_at=now,
        )
        return DataSourceSnapshot(
            contract_version=self.CONTRACT_VERSION,
            descriptor=descriptor,
            revision=revision,
            freshness=freshness,
            ledger_state="READY",
            missions=tuple(missions),
            beta_summary=beta_summary,
        )

    def _empty_snapshot(
        self,
        *,
        ledger_state: str,
        freshness: str,
        freshness_reason: str,
        schema_version: int | None,
        active_wal: bool,
        wal_size: int,
        shm_present: bool,
        enabled: bool,
        hook_errors: bool,
        now: float,
    ) -> DataSourceSnapshot:
        descriptor = DataSourceDescriptor(
            contract_version=self.CONTRACT_VERSION,
            home_path=str(self.home),
            ledger_path=str(self.ledger_path),
            ledger_state=ledger_state,
            freshness=freshness,
            freshness_reason=freshness_reason,
            schema_version=schema_version,
            supported_schema_version=SCHEMA_VERSION,
            revision=None,
            active_wal=active_wal,
            wal_size_bytes=wal_size,
            shm_present=shm_present,
            read_only_enforced=True,
            enabled=enabled,
            hook_errors_present=hook_errors,
            mission_count=0,
            max_event_id=0,
            observed_at=now,
        )
        return DataSourceSnapshot(
            contract_version=self.CONTRACT_VERSION,
            descriptor=descriptor,
            revision=None,
            freshness=freshness,
            ledger_state=ledger_state,
            missions=(),
            beta_summary=None,
        )

    def _project_mission(
        self,
        db: sqlite3.Connection,
        m_row: sqlite3.Row,
        now: float,
    ) -> dict[str, Any]:
        mid = m_row["id"]
        version = int(m_row["version"])
        epoch = int(m_row["epoch"])
        session_id = m_row["session_id"]
        mode = m_row["mode"]
        cancelled = bool(m_row["cancelled"])
        owner_cursor = int(m_row["owner_cursor"])

        beacon_row = db.execute(
            "SELECT body, hash FROM beacons WHERE mission_id=? AND version=?",
            (mid, version),
        ).fetchone()
        beacon_body = json.loads(beacon_row["body"]) if beacon_row else {}
        beacon_hash = beacon_row["hash"] if beacon_row else "UNKNOWN"
        mutable_row = db.execute(
            "SELECT body FROM mutable_fields WHERE mission_id=?",
            (mid,),
        ).fetchone()
        if mutable_row:
            beacon_body.update(json.loads(mutable_row["body"]))
        beacon = dict(beacon_body, version=version, hash=beacon_hash)

        events = [
            dict(row, data=json.loads(row["data"]))
            for row in db.execute(
                "SELECT * FROM events WHERE mission_id=? ORDER BY id",
                (mid,),
            ).fetchall()
        ]
        pending_messages = [
            dict(row)
            for row in db.execute(
                "SELECT seq, text, actor FROM owner_messages WHERE mission_id=? AND seq>? ORDER BY seq",
                (mid, owner_cursor),
            ).fetchall()
        ]
        rollovers = [
            dict(row)
            for row in db.execute(
                "SELECT id, state, epoch, reason, checkpoint, checkpoint_hash, successor_session_id "
                "FROM rollovers WHERE mission_id=? ORDER BY rowid",
                (mid,),
            ).fetchall()
        ]
        latest_rollover = rollovers[-1] if rollovers else None
        effects = [
            dict(
                row,
                receipt=json.loads(row["receipt"]) if row["receipt"] else None,
            )
            for row in db.execute(
                "SELECT key, kind, state, receipt FROM effects WHERE mission_id=? ORDER BY rowid",
                (mid,),
            ).fetchall()
        ]
        unknown_outbox = db.execute(
            "SELECT * FROM owner_outbox WHERE mission_id=? AND state='UNKNOWN' ORDER BY rowid LIMIT 1",
            (mid,),
        ).fetchone()
        active_outbox = (
            unknown_outbox
            or db.execute(
                "SELECT * FROM owner_outbox WHERE mission_id=? AND state!='ACKED' ORDER BY rowid LIMIT 1",
                (mid,),
            ).fetchone()
        )
        delivery = None
        if active_outbox:
            delivery = dict(active_outbox)
            delivery["messages"] = json.loads(delivery["messages"])

        config = next((e["data"] for e in events if e["type"] == "runtime.config"), {})
        thread_policy = next(
            (
                e["data"]
                for e in reversed(events)
                if e["type"] == "runtime.thread_policy" and e["data"].get("session_id") == session_id
            ),
            {},
        )
        decision_event = next(
            (e for e in reversed(events) if e["type"] == "runtime.monitor_decision"),
            None,
        )
        decision = decision_event["data"] if decision_event else None
        recovery = next(
            (
                e
                for e in reversed(events)
                if e["type"] in ("mission.recovery_required", "mission.recovered")
            ),
            None,
        )
        telemetry = next(
            (e["data"] for e in reversed(events) if e["type"] == "runtime.telemetry"),
            None,
        )
        completion = completion_state(events)
        completed = completion["completed"]
        awaiting_owner = completion_awaiting_confirmation(
            completion,
            cancelled=cancelled,
            recovery_required=bool(recovery and recovery["type"] == "mission.recovery_required"),
            owner_delivery_unknown=unknown_outbox is not None,
        )

        try:
            m_dir = mission_dir(self.home, mid)
            info = read_json(m_dir / "supervisor.json")
            heartbeat = read_json(m_dir / "heartbeat.json")
            pid = info.get("pid")
            hb_at = heartbeat.get("at")
            if not isinstance(pid, int):
                live, live_reason = False, "supervisor_identity_missing"
            else:
                expected_start = info.get("process_start")
                actual_start = process_start(pid) if expected_start else None
                # When ps is available, verify process_start; if ps is blocked by sandbox (None),
                # fall back to verifying heartbeat PID and recency.
                if expected_start and actual_start is not None and actual_start != expected_start:
                    live, live_reason = False, "supervisor_dead"
                elif (
                    heartbeat.get("pid") != pid
                    or not isinstance(hb_at, (int, float))
                    or (now - float(hb_at)) > self.stale_after_s
                ):
                    live, live_reason = False, "heartbeat_stale"
                else:
                    live, live_reason = True, "fresh"
        except ValueError:
            live, live_reason = False, "invalid_mission_id"

        delivery_recovery = None
        if delivery and delivery["state"] == "UNKNOWN":
            delivery_recovery = {
                "delivery_id": delivery["id"],
                "session_id": delivery["session_id"],
                "epoch": delivery["epoch"],
                "automatic_retry": False,
                "instruction": (
                    "Reconcile this delivery ID and session against authoritative executor "
                    "evidence. Do not retry or alter the batch while the outcome is unknown."
                ),
            }

        telemetry_age = (
            (now - float(telemetry["at"]))
            if (telemetry and isinstance(telemetry.get("at"), (int, float)))
            else None
        )
        telemetry_stale = telemetry_age is None or telemetry_age > self.stale_after_s

        if delivery_recovery:
            health = "RECOVERY_REQUIRED"
        elif completed:
            health = "COMPLETED"
        elif cancelled:
            health = "DEGRADED:cancelled"
        elif mode == "OFF":
            health = "DEGRADED:off"
        elif any(e["type"] == "runtime.hook_error" for e in events):
            health = "DEGRADED:hook_error"
        elif config.get("native"):
            health = "OBSERVE_ONLY:hooks"
        elif not live:
            health = "DEGRADED:" + live_reason
        elif telemetry_stale:
            health = "DEGRADED:telemetry_stale"
        else:
            health = "PROTECTED"

        latest_block = next(
            (
                e
                for e in reversed(events)
                if e["type"] in ("runtime.blocked", "runtime.rollover_trigger", "mission.recovered")
            ),
            None,
        )
        if delivery_recovery:
            runtime_status, block_reason = "RECOVERY_REQUIRED", "OWNER_DELIVERY_UNKNOWN"
        elif completed:
            runtime_status, block_reason = "COMPLETED", None
        elif cancelled:
            runtime_status, block_reason = "CANCELLED", None
        elif recovery and recovery["type"] == "mission.recovery_required":
            runtime_status, block_reason = "RECOVERY_REQUIRED", recovery["data"].get("reason")
        elif awaiting_owner:
            runtime_status, block_reason = "AWAITING_CONFIRMATION", "completion_reported"
        elif decision and decision.get("action") == "BLOCK":
            runtime_status, block_reason = "BLOCKED", ",".join(decision.get("reasons", []))
        elif (
            latest_block
            and latest_block["type"] == "runtime.blocked"
            and (decision_event is None or latest_block["id"] > decision_event["id"])
        ):
            runtime_status, block_reason = "BLOCKED", latest_block["data"].get("reason")
        else:
            runtime_status, block_reason = "ACTIVE", None

        if completed or cancelled:
            mission_freshness = "FRESH"
            mission_freshness_reason = "terminal_mission_state"
        elif delivery_recovery:
            mission_freshness = "UNKNOWN"
            mission_freshness_reason = "owner_delivery_unknown"
        elif mode == "OFF":
            mission_freshness = "STALE"
            mission_freshness_reason = "mission_mode_off"
        elif config.get("native"):
            if telemetry_age is not None and telemetry_age <= self.stale_after_s:
                mission_freshness = "FRESH"
                mission_freshness_reason = "native_hook_recent"
            elif telemetry_age is not None:
                mission_freshness = "STALE"
                mission_freshness_reason = "native_hook_stale"
            else:
                mission_freshness = "UNKNOWN"
                mission_freshness_reason = "native_telemetry_missing"
        elif not live:
            mission_freshness = "STALE"
            mission_freshness_reason = live_reason
        elif telemetry_stale:
            mission_freshness = "STALE" if telemetry_age is not None else "UNKNOWN"
            mission_freshness_reason = "telemetry_stale" if telemetry_age is not None else "telemetry_missing"
        else:
            mission_freshness = "FRESH"
            mission_freshness_reason = "live_controller_and_telemetry"

        evidence_records = []
        for e in events:
            if e["type"] == "runtime.evidence":
                data = dict(e["data"])
                ev_path = data.get("evidence_path")
                file_verified = False
                if isinstance(ev_path, str) and ev_path:
                    try:
                        file_verified = sha256_regular(ev_path) == data.get("sha256")
                    except OSError:
                        file_verified = False
                data["file_verified"] = file_verified
                data["event_id"] = e["id"]
                evidence_records.append(data)

        return {
            "mission": mid,
            "mode": mode,
            "epoch": epoch,
            "session_id": session_id,
            "executor": config.get("executor"),
            "cwd": config.get("cwd"),
            "done_check": config.get("done_check"),
            "self_complete": bool(config.get("self_complete")),
            "beacon": beacon,
            "rollover": (
                {"id": latest_rollover["id"], "state": latest_rollover["state"], "epoch": latest_rollover["epoch"], "reason": latest_rollover["reason"]}
                if latest_rollover
                else None
            ),
            "rollovers": rollovers,
            "sandbox": (
                thread_policy.get("sandbox", config.get("codex_sandbox", "inherit"))
                if config.get("executor") == "codex"
                else None
            ),
            "approval_policy": (
                thread_policy.get("approval_policy", config.get("approval_policy", "inherit"))
                if config.get("executor") == "codex"
                else None
            ),
            "permission_mode": (
                config.get("permission_mode", "inherit")
                if config.get("executor") == "claude"
                else None
            ),
            "monitor": decision,
            "controller": health,
            "freshness": mission_freshness,
            "freshness_reason": mission_freshness_reason,
            "pending_owner_messages": len(pending_messages),
            "pending_owner_message_details": pending_messages,
            "owner_delivery": delivery,
            "owner_delivery_recovery": delivery_recovery,
            "effects": effects,
            "evidence": evidence_records,
            "completion_state": completion,
            "cancelled": cancelled,
            "runtime_status": runtime_status,
            "reason": block_reason,
            "events": events,
            "last_event_id": events[-1]["id"] if events else 0,
        }

    def _project_beta_summary(self, db: sqlite3.Connection) -> dict[str, Any]:
        from datetime import datetime, timezone

        missions = int(db.execute("SELECT COUNT(*) FROM missions").fetchone()[0])
        completed = int(
            db.execute(
                "SELECT COUNT(*) FROM missions m WHERE EXISTS "
                "(SELECT 1 FROM events e WHERE e.mission_id=m.id "
                "AND e.type IN ('mission.completed','mission.succeeded'))"
            ).fetchone()[0]
        )
        rollovers = int(db.execute("SELECT COUNT(*) FROM rollovers").fetchone()[0])
        timestamps = db.execute(
            "SELECT e.created_at FROM events e JOIN missions m ON m.id=e.mission_id"
        ).fetchall()
        utc_dates = set()
        for (timestamp,) in timestamps:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            utc_dates.add(parsed.astimezone(timezone.utc).date().isoformat())
        days = len(utc_dates)
        return {
            "schema_version": "gcl.beta_summary.v1",
            "missions_total": missions,
            "missions_completed": completed,
            "rollovers_total": rollovers,
            "active_days_count": days,
            "repeat_use_qualified": days >= 2 or missions >= 2,
            "privacy_mode": "STATS_ONLY_NO_CONTENT_NO_PATHS",
        }

    @staticmethod
    def _evaluate_overall_freshness(
        *,
        missions: list[dict[str, Any]],
        active_wal: bool,
        enabled: bool,
    ) -> tuple[str, str]:
        if not enabled:
            return "STALE", "global_switch_off"
        if not missions:
            if active_wal:
                return "UNKNOWN", "active_wal_without_committed_missions"
            return "FRESH", "empty_ready_ledger"
        mission_states = [m["freshness"] for m in missions]
        if "UNKNOWN" in mission_states:
            return "UNKNOWN", "mission_state_unknown"
        if "STALE" in mission_states:
            return "STALE", "mission_telemetry_or_controller_stale"
        if active_wal:
            return "FRESH", "ready_with_active_wal"
        return "FRESH", "all_missions_fresh"

    @staticmethod
    def _compute_revision(
        *,
        schema_version: int,
        max_event_id: int,
        missions: list[dict[str, Any]],
        active_wal: bool,
        wal_size: int,
        enabled: bool,
    ) -> str:
        digest_input = {
            "schema_version": schema_version,
            "max_event_id": max_event_id,
            "enabled": enabled,
            "active_wal": active_wal,
            "wal_size": wal_size,
            "missions": [
                {
                    "mission": m["mission"],
                    "epoch": m["epoch"],
                    "session_id": m["session_id"],
                    "mode": m["mode"],
                    "runtime_status": m["runtime_status"],
                    "reason": m["reason"],
                    "beacon_version": m["beacon"].get("version"),
                    "beacon_hash": m["beacon"].get("hash"),
                    "last_event_id": m["last_event_id"],
                    "pending_owner_messages": m["pending_owner_messages"],
                    "effects": [(eff["key"], eff["state"]) for eff in m["effects"]],
                }
                for m in missions
            ],
        }
        digest = hashlib.sha256(_canonical_json(digest_input).encode("utf-8")).hexdigest()[:16]
        return f"rev-v{schema_version}-e{max_event_id}-{digest}"


class SyntheticReadOnlyDataSource:
    """In-memory synthetic fixture adapter honoring the exact describe/refresh/snapshot contract."""

    CONTRACT_VERSION = LighthouseReadOnlyDataSource.CONTRACT_VERSION

    def __init__(
        self,
        bundle: dict[str, Any],
        *,
        stale_after_s: float = 20.0,
        now_fn=time.time,
    ):
        self._bundle = json.loads(_canonical_json(bundle))
        self.stale_after_s = float(stale_after_s)
        self._now_fn = now_fn
        self._cached_snapshot: DataSourceSnapshot | None = None

    def describe(self) -> DataSourceDescriptor:
        if self._cached_snapshot is None:
            return self.refresh()
        return self._cached_snapshot.descriptor

    def refresh(self) -> DataSourceDescriptor:
        self._cached_snapshot = self._build_snapshot()
        return self._cached_snapshot.descriptor

    def snapshot(self, expected_revision: str | None = None) -> DataSourceSnapshot:
        snap = self._build_snapshot()
        self._cached_snapshot = snap
        if expected_revision is not None and snap.revision != expected_revision:
            raise RevisionMismatchError(expected_revision, snap.revision)
        return snap

    def _build_snapshot(self) -> DataSourceSnapshot:
        now = float(self._now_fn())
        ledger_state = self._bundle.get("ledger_state", "READY")
        schema_version = self._bundle.get("schema_version", SCHEMA_VERSION if ledger_state == "READY" else None)
        active_wal = bool(self._bundle.get("active_wal", False))
        wal_size = int(self._bundle.get("wal_size_bytes", 4096 if active_wal else 0))
        shm_present = bool(self._bundle.get("shm_present", active_wal))
        enabled = bool(self._bundle.get("enabled", True))
        hook_errors = bool(self._bundle.get("hook_errors_present", False))

        if ledger_state != "READY":
            descriptor = DataSourceDescriptor(
                contract_version=self.CONTRACT_VERSION,
                home_path=self._bundle.get("home_path", "<SYNTHETIC_RLH_HOME>"),
                ledger_path=self._bundle.get("ledger_path", "<SYNTHETIC_RLH_HOME>/state.sqlite"),
                ledger_state=ledger_state,
                freshness="UNKNOWN",
                freshness_reason=self._bundle.get("freshness_reason", f"synthetic_{ledger_state.lower()}"),
                schema_version=schema_version,
                supported_schema_version=SCHEMA_VERSION,
                revision=None,
                active_wal=active_wal,
                wal_size_bytes=wal_size,
                shm_present=shm_present,
                read_only_enforced=True,
                enabled=enabled,
                hook_errors_present=hook_errors,
                mission_count=0,
                max_event_id=0,
                observed_at=now,
            )
            return DataSourceSnapshot(
                contract_version=self.CONTRACT_VERSION,
                descriptor=descriptor,
                revision=None,
                freshness="UNKNOWN",
                ledger_state=ledger_state,
                missions=(),
                beta_summary=None,
            )

        missions = list(self._bundle.get("missions", []))
        max_event_id = int(
            self._bundle.get(
                "max_event_id",
                max((m.get("last_event_id", 0) for m in missions), default=0),
            )
        )
        freshness = self._bundle.get("freshness")
        freshness_reason = self._bundle.get("freshness_reason", "synthetic_bundle")
        if freshness not in FRESHNESS_STATES:
            freshness, freshness_reason = LighthouseReadOnlyDataSource._evaluate_overall_freshness(
                missions=missions,
                active_wal=active_wal,
                enabled=enabled,
            )
        revision = self._bundle.get("revision") or LighthouseReadOnlyDataSource._compute_revision(
            schema_version=schema_version or SCHEMA_VERSION,
            max_event_id=max_event_id,
            missions=missions,
            active_wal=active_wal,
            wal_size=wal_size,
            enabled=enabled,
        )
        descriptor = DataSourceDescriptor(
            contract_version=self.CONTRACT_VERSION,
            home_path=self._bundle.get("home_path", "<SYNTHETIC_RLH_HOME>"),
            ledger_path=self._bundle.get("ledger_path", "<SYNTHETIC_RLH_HOME>/state.sqlite"),
            ledger_state="READY",
            freshness=freshness,
            freshness_reason=freshness_reason,
            schema_version=schema_version,
            supported_schema_version=SCHEMA_VERSION,
            revision=revision,
            active_wal=active_wal,
            wal_size_bytes=wal_size,
            shm_present=shm_present,
            read_only_enforced=True,
            enabled=enabled,
            hook_errors_present=hook_errors,
            mission_count=len(missions),
            max_event_id=max_event_id,
            observed_at=now,
        )
        return DataSourceSnapshot(
            contract_version=self.CONTRACT_VERSION,
            descriptor=descriptor,
            revision=revision,
            freshness=freshness,
            ledger_state="READY",
            missions=tuple(missions),
            beta_summary=self._bundle.get("beta_summary"),
        )

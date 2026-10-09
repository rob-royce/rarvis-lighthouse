"""Durable, process-safe standalone mission authority."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager

from . import beacon as beacon_api, checkpoint as checkpoint_api
from .errors import (CheckpointIncomplete, GoalChangeUnauthorized, IllegalTransition, MissionCancelled,
                     SchemaTooNew, StaleEpoch, StateMoved)

_NEXT = {
    "ROLLOVER_REQUESTED": "QUIESCING",
    "QUIESCING": "CHECKPOINT_READY",
    "CHECKPOINT_READY": "SUCCESSOR_PREPARED_READ_ONLY",
    "SUCCESSOR_PREPARED_READ_ONLY": "OWNERSHIP_TRANSFERRED",
    "OWNERSHIP_TRANSFERRED": "SUCCESSOR_ACTIVE",
    "SUCCESSOR_ACTIVE": "PREDECESSOR_RETIRED",
}
_OPEN = tuple(_NEXT)
_MODES = ("OFF", "OBSERVE_ONLY", "CONTROLLED_AUTO")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _schema_version(db) -> int:
    return db.execute("PRAGMA user_version").fetchone()[0]


def completion_state(events: list[dict]) -> dict:
    """Project durable completion events; an owner message supersedes even a confirmation."""
    declined = next((e for e in reversed(events) if e["type"] == "runtime.completion_declined"), None)
    boundary = declined["id"] if declined else 0
    report = next((e for e in reversed(events) if e["type"] == "runtime.completion_reported"
                   and e["id"] > boundary), None)
    confirmation = next((e for e in reversed(events) if report and
                         e["type"] == "mission.completion_confirmed" and e["id"] > report["id"] and
                         e["data"].get("report_id") == report["id"]), None)
    invalidated = bool(report and any(e["type"] == "owner.message" and e["id"] > report["id"]
                                      for e in events))
    completed = next((e for e in reversed(events) if e["type"] == "mission.completed"), None)
    return {"report": report, "confirmation": confirmation, "invalidated": invalidated,
            "declined": declined, "completed": completed}


def completion_awaiting_confirmation(state: dict, *, cancelled: bool, recovery_required: bool,
                                     owner_delivery_unknown: bool) -> bool:
    """Use the same completion eligibility gates for status and transactional confirmation."""
    return bool(state["report"] and not (cancelled or recovery_required or owner_delivery_unknown or
                                         state["completed"] or state["confirmation"] or state["invalidated"]))


# The schema as it stood when versioning began (B5). Unversioned ledgers report version 0.
_BASELINE = (
    """CREATE TABLE IF NOT EXISTS missions (
        id TEXT PRIMARY KEY, version INTEGER NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
        session_id TEXT, mode TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
        owner_cursor INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS beacons (
        mission_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
        hash TEXT NOT NULL, PRIMARY KEY(mission_id, version))""",
    """CREATE TABLE IF NOT EXISTS mutable_fields (
        mission_id TEXT PRIMARY KEY, body TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS rollovers (
        id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, state TEXT NOT NULL,
        epoch INTEGER NOT NULL, reason TEXT NOT NULL, checkpoint TEXT, checkpoint_hash TEXT,
        successor_session_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS owner_messages (
        mission_id TEXT NOT NULL, seq INTEGER NOT NULL, text TEXT NOT NULL,
        actor TEXT NOT NULL, PRIMARY KEY(mission_id, seq))""",
    """CREATE TABLE IF NOT EXISTS owner_outbox (
        id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, session_id TEXT NOT NULL,
        epoch INTEGER NOT NULL, state TEXT NOT NULL, messages TEXT NOT NULL,
        prefix TEXT NOT NULL, kind TEXT NOT NULL, through_seq INTEGER NOT NULL, receipt TEXT)""",
    """CREATE TABLE IF NOT EXISTS effects (
        key TEXT PRIMARY KEY, mission_id TEXT NOT NULL, kind TEXT NOT NULL,
        state TEXT NOT NULL, receipt TEXT)""",
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL,
        type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS events_by_type ON events(mission_id, type, id)",
    """CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN
        SELECT RAISE(ABORT, 'events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN
        SELECT RAISE(ABORT, 'events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS beacons_no_update BEFORE UPDATE ON beacons BEGIN
        SELECT RAISE(ABORT, 'beacons are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS beacons_no_delete BEFORE DELETE ON beacons BEGIN
        SELECT RAISE(ABORT, 'beacons are append-only'); END""",
)


def _migrate_to_1(db) -> None:
    """Bring any unversioned ledger to the baseline. Older ledgers lack later additive columns."""
    for statement in _BASELINE:
        db.execute(statement)
    columns = {row[1] for row in db.execute("PRAGMA table_info(owner_outbox)")}
    for name, sql_type in (("beacon_version", "INTEGER"), ("beacon_hash", "TEXT")):
        if name not in columns:
            db.execute(f"ALTER TABLE owner_outbox ADD COLUMN {name} {sql_type}")
    if "successor_session_id" not in {row[1] for row in db.execute("PRAGMA table_info(rollovers)")}:
        db.execute("ALTER TABLE rollovers ADD COLUMN successor_session_id TEXT")


# Entry n brings a ledger from version n to n + 1. Migrations are forward-only and additive,
# run in order on open inside one transaction, and never change once released: add a new entry.
_MIGRATIONS = (_migrate_to_1,)
SCHEMA_VERSION = len(_MIGRATIONS)


class LocalStore:
    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), mode=0o700, exist_ok=True)
        if not os.path.exists(self.path):
            # SQLite gives -wal/-shm files the main database's permission bits.
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=30000")
            if _schema_version(db) != SCHEMA_VERSION:
                # Serialize migration when several processes open a ledger at once.
                db.execute("BEGIN IMMEDIATE")
                try:
                    version = _schema_version(db)
                    if version > SCHEMA_VERSION:
                        raise SchemaTooNew(f"ledger schema version {version} is newer than this release "
                                           f"supports ({SCHEMA_VERSION}); upgrade before opening it")
                    for migrate in _MIGRATIONS[version:]:
                        migrate(db)
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        finally:
            db.close()

    def schema_version(self) -> int:
        with self._read() as db:
            return _schema_version(db)

    @contextmanager
    def _tx(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def _read(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    def _event(self, db, mission_id, kind, data=None):
        db.execute("INSERT INTO events(mission_id,type,data) VALUES(?,?,?)",
                   (mission_id, kind, _json(data or {})))

    def _mission(self, db, mission_id):
        row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            raise KeyError(mission_id)
        return row

    def _rollover(self, db, rollover_id):
        row = db.execute("SELECT * FROM rollovers WHERE id=?", (rollover_id,)).fetchone()
        if row is None:
            raise KeyError(rollover_id)
        return row

    def _writer(self, db, mission_id, epoch, session_id=None):
        row = self._mission(db, mission_id)
        if row["cancelled"]:
            raise MissionCancelled(mission_id)
        if row["epoch"] != epoch or (session_id is not None and row["session_id"] != session_id):
            raise StaleEpoch(f"expected current epoch {epoch}")
        return row

    def create_mission(self, beacon: dict, actor: str, mode: str = "OBSERVE_ONLY") -> str:
        if mode not in _MODES:
            raise ValueError(mode)
        mid = beacon.get("mission_id") or uuid.uuid4().hex
        body = dict(beacon, mission_id=mid)
        controlled = {key: body[key] for key in beacon_api.CONTROLLED_FIELDS if key in body}
        mutable = {key: body[key] for key in beacon_api.MUTABLE_FIELDS if key in body}
        digest = beacon_api.canonical_hash(body)
        with self._tx() as db:
            db.execute("INSERT INTO missions(id,version,mode) VALUES(?,1,?)", (mid, mode))
            db.execute("INSERT INTO beacons VALUES(?,?,?,?)", (mid, 1, _json(controlled), digest))
            db.execute("INSERT INTO mutable_fields VALUES(?,?)", (mid, _json(mutable)))
            self._event(db, mid, "mission.created", {"actor": actor})
        return mid

    def get_beacon(self, mission_id: str, version: int | None = None) -> dict:
        with self._read() as db:
            mission = self._mission(db, mission_id)
            selected = mission["version"] if version is None else version
            row = db.execute("SELECT * FROM beacons WHERE mission_id=? AND version=?",
                             (mission_id, selected)).fetchone()
            if row is None:
                raise KeyError((mission_id, selected))
            body = json.loads(row["body"])
            if selected == mission["version"]:
                extra = db.execute("SELECT body FROM mutable_fields WHERE mission_id=?", (mission_id,)).fetchone()
                body.update(json.loads(extra["body"]))
            return dict(body, version=selected, hash=row["hash"])

    def change_goal(self, mission_id: str, changes: dict, actor: str, authority_ref: str | None) -> int:
        unknown = set(changes) - set(beacon_api.CONTROLLED_FIELDS) - set(beacon_api.MUTABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown beacon fields: {sorted(unknown)}")
        if "mission_id" in changes and changes["mission_id"] != mission_id:
            raise ValueError("mission_id is immutable")
        rejected = False
        with self._tx() as db:
            mission = self._mission(db, mission_id)
            row = db.execute("SELECT body FROM beacons WHERE mission_id=? AND version=?",
                             (mission_id, mission["version"])).fetchone()
            controlled = json.loads(row["body"])
            if set(changes) & set(beacon_api.CONTROLLED_FIELDS) and (actor != controlled.get("owner") or not authority_ref):
                self._event(db, mission_id, "goal.change_rejected", {"actor": actor, "fields": sorted(changes)})
                rejected = True
            elif set(changes) & set(beacon_api.CONTROLLED_FIELDS):
                controlled.update({k: v for k, v in changes.items() if k in beacon_api.CONTROLLED_FIELDS})
                new_version = mission["version"] + 1
                db.execute("INSERT INTO beacons VALUES(?,?,?,?)", (mission_id, new_version,
                           _json(controlled), beacon_api.canonical_hash(controlled)))
                db.execute("UPDATE missions SET version=? WHERE id=? AND version=?",
                           (new_version, mission_id, mission["version"]))
                self._event(db, mission_id, "goal.changed", {"actor": actor, "version": new_version, "authority_ref": authority_ref})
            else:
                new_version = mission["version"]
            if not rejected and set(changes) & set(beacon_api.MUTABLE_FIELDS):
                mutable = json.loads(db.execute("SELECT body FROM mutable_fields WHERE mission_id=?",
                                                (mission_id,)).fetchone()["body"])
                mutable.update({k: v for k, v in changes.items() if k in beacon_api.MUTABLE_FIELDS})
                db.execute("UPDATE mutable_fields SET body=? WHERE mission_id=?", (_json(mutable), mission_id))
                self._event(db, mission_id, "goal.plan_changed", {"actor": actor})
        if rejected:
            raise GoalChangeUnauthorized(actor)
        return new_version

    def get_mode(self, mission_id):
        with self._read() as db:
            return self._mission(db, mission_id)["mode"]

    def set_mode(self, mission_id, mode: str, actor: str) -> None:
        if mode not in _MODES:
            raise ValueError(mode)
        with self._tx() as db:
            self._mission(db, mission_id)
            db.execute("UPDATE missions SET mode=? WHERE id=?", (mode, mission_id))
            self._event(db, mission_id, "mission.mode_changed", {"mode": mode, "actor": actor})

    def activate_initial(self, mission_id: str, session_id: str, executor: str) -> int:
        with self._tx() as db:
            self._mission(db, mission_id)
            cur = db.execute("UPDATE missions SET epoch=1,session_id=? WHERE id=? AND epoch=0 AND cancelled=0",
                             (session_id, mission_id))
            if cur.rowcount != 1:
                raise StaleEpoch(mission_id)
            self._event(db, mission_id, "writer.activated", {"epoch": 1, "session_id": session_id, "executor": executor})
        return 1

    def current_writer(self, mission_id: str) -> tuple[int, str]:
        with self._read() as db:
            row = self._mission(db, mission_id)
            return row["epoch"], row["session_id"]

    def check_writer(self, mission_id: str, session_id: str, epoch: int) -> bool:
        with self._read() as db:
            row = self._mission(db, mission_id)
            return not row["cancelled"] and row["epoch"] == epoch and row["session_id"] == session_id

    def request_rollover(self, mission_id: str, reason: str, requested_by_epoch: int) -> str:
        with self._tx() as db:
            self._writer(db, mission_id, requested_by_epoch)
            existing = db.execute("SELECT id FROM rollovers WHERE mission_id=? AND state IN (" +
                                  ",".join("?" for _ in _OPEN) + ") ORDER BY rowid DESC LIMIT 1",
                                  (mission_id, *_OPEN)).fetchone()
            if existing:
                active = self._rollover(db, existing["id"])
                if active["epoch"] != requested_by_epoch:
                    raise IllegalTransition("previous rollover must retire before another epoch may roll over")
                return existing["id"]
            rid = uuid.uuid4().hex
            db.execute("INSERT INTO rollovers(id,mission_id,state,epoch,reason) VALUES(?,?,?,?,?)",
                       (rid, mission_id, "ROLLOVER_REQUESTED", requested_by_epoch, reason))
            self._event(db, mission_id, "rollover.requested", {"id": rid, "reason": reason})
            return rid

    def rollover_state(self, rollover_id: str) -> str:
        with self._read() as db:
            return self._rollover(db, rollover_id)["state"]

    def advance(self, rollover_id: str, expected_state: str, to_state: str, **data) -> None:
        # These edges carry additional atomic invariants: a validated checkpoint
        # and an epoch transfer. They cannot be entered through generic advance.
        if (_NEXT.get(expected_state) != to_state
                or to_state in ("CHECKPOINT_READY", "OWNERSHIP_TRANSFERRED")):
            raise IllegalTransition(f"{expected_state} -> {to_state}")
        if to_state == "SUCCESSOR_PREPARED_READ_ONLY" and "successor_session_id" in data:
            if not isinstance(data["successor_session_id"], str) or not data["successor_session_id"]:
                raise IllegalTransition("successor_session_id must be nonempty")
        with self._tx() as db:
            row = self._rollover(db, rollover_id)
            if row["state"] != expected_state:
                raise StateMoved(f"actual state {row['state']}")
            self._writer(db, row["mission_id"], row["epoch"] if expected_state in ("ROLLOVER_REQUESTED", "QUIESCING", "CHECKPOINT_READY") else row["epoch"] + 1)
            # The prepared identity is durable before ownership changes. Older
            # round-1 callers may omit it, but runtimes must provide it.
            if to_state == "SUCCESSOR_PREPARED_READ_ONLY" and data.get("successor_session_id"):
                db.execute("UPDATE rollovers SET state=?,successor_session_id=? WHERE id=? AND state=?",
                           (to_state, data["successor_session_id"], rollover_id, expected_state))
            else:
                db.execute("UPDATE rollovers SET state=? WHERE id=? AND state=?", (to_state, rollover_id, expected_state))
            self._event(db, row["mission_id"], "rollover.advanced", {"id": rollover_id, "state": to_state, **data})

    def save_checkpoint(self, rollover_id: str, checkpoint: dict) -> str:
        checkpoint_api.validate(checkpoint)
        digest = hashlib.sha256(_json(checkpoint).encode()).hexdigest()
        with self._tx() as db:
            row = self._rollover(db, rollover_id)
            if row["state"] != "QUIESCING":
                raise IllegalTransition(row["state"])
            self._writer(db, row["mission_id"], row["epoch"], checkpoint["predecessor_session_id"])
            if checkpoint["mission_id"] != row["mission_id"]:
                raise CheckpointIncomplete("checkpoint mission mismatch")
            db.execute("UPDATE rollovers SET checkpoint=?,checkpoint_hash=?,state='CHECKPOINT_READY' WHERE id=? AND state='QUIESCING'",
                       (_json(checkpoint), digest, rollover_id))
            self._event(db, row["mission_id"], "checkpoint.saved", {"id": rollover_id, "sha256": digest})
        return digest

    def get_checkpoint(self, rollover_id: str) -> dict | None:
        with self._read() as db:
            body = self._rollover(db, rollover_id)["checkpoint"]
            return json.loads(body) if body else None

    def transfer_ownership(self, rollover_id: str, successor_session_id: str, expected_epoch: int) -> int:
        with self._tx() as db:
            row = self._rollover(db, rollover_id)
            if row["state"] == "OWNERSHIP_TRANSFERRED" or row["epoch"] != expected_epoch:
                raise StaleEpoch(rollover_id)
            if row["state"] != "SUCCESSOR_PREPARED_READ_ONLY":
                raise IllegalTransition(row["state"])
            if row["successor_session_id"] and row["successor_session_id"] != successor_session_id:
                raise IllegalTransition("successor differs from prepared identity")
            self._writer(db, row["mission_id"], expected_epoch)
            cur = db.execute("UPDATE missions SET epoch=epoch+1,session_id=? WHERE id=? AND epoch=? AND cancelled=0",
                             (successor_session_id, row["mission_id"], expected_epoch))
            if cur.rowcount != 1:
                raise StaleEpoch(rollover_id)
            db.execute("UPDATE rollovers SET state='OWNERSHIP_TRANSFERRED' WHERE id=? AND state='SUCCESSOR_PREPARED_READ_ONLY'", (rollover_id,))
            db.execute("UPDATE effects SET state='OUTCOME_UNKNOWN' WHERE mission_id=? AND state='IN_FLIGHT'",
                       (row["mission_id"],))
            self._event(db, row["mission_id"], "writer.transferred", {"id": rollover_id, "epoch": expected_epoch + 1, "session_id": successor_session_id})
            return expected_epoch + 1

    def abort_rollover(self, rollover_id: str, reason: str) -> str:
        with self._tx() as db:
            row = self._rollover(db, rollover_id)
            if row["state"] not in _OPEN or row["state"] == "OWNERSHIP_TRANSFERRED":
                raise IllegalTransition(row["state"])
            self._writer(db, row["mission_id"], row["epoch"])
            db.execute("UPDATE rollovers SET state='ABORTED' WHERE id=? AND state=?", (rollover_id, row["state"]))
            self._event(db, row["mission_id"], "rollover.aborted", {"id": rollover_id, "reason": reason})
        return "ABORTED"

    def post_owner_message(self, mission_id: str, text: str, actor: str) -> int:
        with self._tx() as db:
            self._mission(db, mission_id)
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM owner_messages WHERE mission_id=?", (mission_id,)).fetchone()[0]
            db.execute("INSERT INTO owner_messages VALUES(?,?,?,?)", (mission_id, seq, text, actor))
            self._event(db, mission_id, "owner.message", {"seq": seq, "actor": actor})
            return seq

    def owner_delivery(self, mission_id: str) -> dict | None:
        with self._read() as db:
            row = db.execute("SELECT * FROM owner_outbox WHERE mission_id=? AND state!='ACKED' ORDER BY rowid LIMIT 1",
                             (mission_id,)).fetchone()
            return self._delivery(row) if row else None

    @staticmethod
    def _delivery(row):
        result = dict(row)
        result['messages'] = json.loads(result['messages'])
        return result

    def prepare_owner_delivery(self, mission_id, session_id, epoch, prefix='', kind='owner'):
        """Stage/refresh an unsent batch against the current controlled contract.

        A prefix factory receives the exact beacon snapshot bound to the batch.
        It must only render text, without store writes or external side effects.
        UNKNOWN is immutable until authoritative reconciliation.
        """
        with self._tx() as db:
            mission = self._writer(db, mission_id, epoch, session_id)
            held = db.execute("SELECT 1 FROM rollovers WHERE mission_id=? AND epoch=? AND state IN "
                              "('QUIESCING','CHECKPOINT_READY','SUCCESSOR_PREPARED_READ_ONLY')",
                              (mission_id, epoch)).fetchone()
            if held:
                return None
            row = db.execute("SELECT * FROM owner_outbox WHERE mission_id=? AND state!='ACKED' ORDER BY rowid LIMIT 1",
                             (mission_id,)).fetchone()
            if row and row['state'] != 'PENDING':
                return self._delivery(row)
            if row is None and kind == 'successor':
                acknowledged = db.execute("SELECT * FROM owner_outbox WHERE mission_id=? AND epoch=? AND kind=? AND state='ACKED'",
                                          (mission_id, epoch, kind)).fetchone()
                if acknowledged:
                    return self._delivery(acknowledged)
            contract = db.execute("SELECT body,hash FROM beacons WHERE mission_id=? AND version=?",
                                  (mission_id, mission['version'])).fetchone()
            current = dict(json.loads(contract['body']), version=mission['version'], hash=contract['hash'])
            rendered = prefix(current) if callable(prefix) else prefix
            if not isinstance(rendered, str):
                raise TypeError('delivery prefix must render text')
            if row:
                # Includes the same writer after a crash: its old prefix may
                # predate an owner goal change. Never reuse that unsent contract.
                db.execute("UPDATE owner_outbox SET session_id=?,epoch=?,prefix=?,kind=?,beacon_version=?,beacon_hash=? WHERE id=?",
                           (session_id, epoch, rendered, kind, current['version'], current['hash'], row['id']))
                self._event(db, mission_id, 'owner.delivery_refreshed',
                            {'delivery_id': row['id'], 'beacon_version': current['version'], 'beacon_hash': current['hash']})
                return self._delivery(db.execute("SELECT * FROM owner_outbox WHERE id=?", (row['id'],)).fetchone())
            messages = [dict(row) for row in db.execute(
                "SELECT seq,text,actor FROM owner_messages WHERE mission_id=? AND seq>? ORDER BY seq",
                (mission_id, mission['owner_cursor']))]
            if not messages and kind != 'successor':
                return None
            delivery_id = uuid.uuid4().hex
            db.execute("INSERT INTO owner_outbox(id,mission_id,session_id,epoch,state,messages,prefix,kind,through_seq,beacon_version,beacon_hash) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (delivery_id, mission_id, session_id, epoch, 'PENDING', _json(messages), rendered, kind,
                        messages[-1]['seq'] if messages else mission['owner_cursor'], current['version'], current['hash']))
            self._event(db, mission_id, 'owner.delivery_pending', {'delivery_id': delivery_id})
            return self._delivery(db.execute("SELECT * FROM owner_outbox WHERE id=?", (delivery_id,)).fetchone())

    def claim_owner_delivery(self, mission_id, delivery_id, session_id, epoch, *,
                             beacon_version=None, beacon_hash=None):
        with self._tx() as db:
            mission = self._writer(db, mission_id, epoch, session_id)
            current_hash = db.execute("SELECT hash FROM beacons WHERE mission_id=? AND version=?",
                                      (mission_id, mission['version'])).fetchone()[0]
            if ((beacon_version is not None and beacon_version != mission['version']) or
                    (beacon_hash is not None and beacon_hash != current_hash)):
                return False
            if db.execute("SELECT 1 FROM rollovers WHERE mission_id=? AND epoch=? AND state IN "
                          "('QUIESCING','CHECKPOINT_READY','SUCCESSOR_PREPARED_READ_ONLY')",
                          (mission_id, epoch)).fetchone():
                return False
            # UNKNOWN is durable *before* crossing the RPC boundary. A crash in
            # this small window requires reconciliation, never a blind retry.
            changed = db.execute("UPDATE owner_outbox SET state='UNKNOWN' WHERE id=? AND mission_id=? "
                                 "AND session_id=? AND epoch=? AND state='PENDING' AND beacon_version=? AND beacon_hash=?",
                                 (delivery_id, mission_id, session_id, epoch, mission['version'], current_hash)).rowcount
            if changed:
                self._event(db, mission_id, 'owner.delivery_unknown', {'delivery_id': delivery_id})
            return bool(changed)

    def _ack_owner_delivery(self, db, row, turn_id):
        if not turn_id:
            raise ValueError('accepted delivery requires a turn receipt')
        if row['state'] == 'ACKED':
            return
        if row['state'] != 'UNKNOWN':
            raise IllegalTransition(row['state'])
        db.execute("UPDATE owner_outbox SET state='ACKED',receipt=? WHERE id=?", (turn_id, row['id']))
        db.execute("UPDATE missions SET owner_cursor=MAX(owner_cursor,?) WHERE id=?",
                   (row['through_seq'], row['mission_id']))
        data = {'delivery_id': row['id'], 'epoch': row['epoch'], 'session_id': row['session_id'],
                'kind': row['kind'], 'seq': row['through_seq'], 'turn_id': turn_id}
        self._event(db, row['mission_id'], 'runtime.owner_delivered', data)
        if row['kind'] == 'successor':
            self._event(db, row['mission_id'], 'runtime.turn_started', data)

    def ack_owner_delivery(self, mission_id, delivery_id, turn_id):
        with self._tx() as db:
            row = db.execute("SELECT * FROM owner_outbox WHERE id=? AND mission_id=?", (delivery_id, mission_id)).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            self._ack_owner_delivery(db, row, turn_id)

    def resolve_owner_delivery(self, mission_id, delivery_id, *, accepted, evidence, turn_id=None):
        """Reconcile ONLY against authoritative executor evidence, not absence of a local event.

        An explicit negative receipt permits retry using the same delivery ID.
        Unknown/unavailable evidence must leave the batch UNKNOWN.
        """
        if not isinstance(accepted, bool) or not evidence or not evidence.strip():
            raise ValueError('explicit executor outcome and evidence required')
        with self._tx() as db:
            row = db.execute("SELECT * FROM owner_outbox WHERE id=? AND mission_id=?", (delivery_id, mission_id)).fetchone()
            if row is None or row['state'] != 'UNKNOWN':
                raise IllegalTransition('delivery is not UNKNOWN')
            if accepted:
                self._ack_owner_delivery(db, row, turn_id)
            else:
                db.execute("UPDATE owner_outbox SET state='PENDING' WHERE id=?", (delivery_id,))
            self._event(db, mission_id, 'owner.delivery_reconciled',
                        {'delivery_id': delivery_id, 'accepted': accepted, 'evidence': evidence})

    def consume_owner_messages(self, mission_id: str, session_id: str, epoch: int) -> list[dict]:
        with self._tx() as db:
            mission = self._writer(db, mission_id, epoch, session_id)
            # An outstanding RPC batch owns the cursor until a confirmed outcome.
            if db.execute("SELECT 1 FROM owner_outbox WHERE mission_id=? AND state!='ACKED'",
                          (mission_id,)).fetchone():
                return []
            held = db.execute("SELECT 1 FROM rollovers WHERE mission_id=? AND epoch=? AND state IN "
                              "('QUIESCING','CHECKPOINT_READY','SUCCESSOR_PREPARED_READ_ONLY') LIMIT 1",
                              (mission_id, epoch)).fetchone()
            if held:
                return []
            rows = db.execute("SELECT seq,text,actor FROM owner_messages WHERE mission_id=? AND seq>? ORDER BY seq",
                              (mission_id, mission["owner_cursor"])).fetchall()
            result = [dict(row) for row in rows]
            if result:
                db.execute("UPDATE missions SET owner_cursor=? WHERE id=? AND epoch=?",
                           (result[-1]["seq"], mission_id, epoch))
                self._event(db, mission_id, "owner.messages_consumed", {"through": result[-1]["seq"], "epoch": epoch})
            return result

    def cancel(self, mission_id: str, actor: str) -> None:
        with self._tx() as db:
            self._mission(db, mission_id)
            if db.execute("UPDATE missions SET cancelled=1 WHERE id=? AND cancelled=0", (mission_id,)).rowcount:
                self._event(db, mission_id, "mission.cancelled", {"actor": actor})

    def record_effect(self, mission_id: str, key: str, kind: str, epoch: int) -> str:
        with self._tx() as db:
            self._writer(db, mission_id, epoch)
            existing = db.execute("SELECT mission_id,kind,state FROM effects WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["mission_id"] != mission_id or existing["kind"] != kind:
                    raise ValueError("effect key collision")
                return existing["state"]
            db.execute("INSERT INTO effects(key,mission_id,kind,state) VALUES(?,?,?,'NOT_STARTED')", (key, mission_id, kind))
            self._event(db, mission_id, "effect.recorded", {"key": key, "kind": kind})
            return "NOT_STARTED"

    def mark_effect(self, key: str, state: str, epoch: int, receipt: dict | None = None) -> None:
        edges = {"NOT_STARTED": {"IN_FLIGHT", "FAILED"}, "IN_FLIGHT": {"CONFIRMED_DONE", "FAILED"},
                 "FAILED": {"IN_FLIGHT"}, "OUTCOME_UNKNOWN": {"CONFIRMED_DONE", "FAILED"}}
        with self._tx() as db:
            row = db.execute("SELECT * FROM effects WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            self._writer(db, row["mission_id"], epoch)
            if state not in edges.get(row["state"], set()):
                raise IllegalTransition(f"effect {row['state']} -> {state}")
            db.execute("UPDATE effects SET state=?,receipt=? WHERE key=? AND state=?",
                       (state, _json(receipt) if receipt is not None else None, key, row["state"]))
            self._event(db, row["mission_id"], "effect.changed", {"key": key, "state": state})

    def effect_state(self, key: str) -> str:
        with self._read() as db:
            row = db.execute("SELECT state FROM effects WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            return row["state"]

    def _events(self, db, mission_id: str) -> list[dict]:
        return [dict(row, data=json.loads(row["data"])) for row in db.execute(
            "SELECT * FROM events WHERE mission_id=? ORDER BY id", (mission_id,))]

    def events(self, mission_id: str) -> list[dict]:
        with self._read() as db:
            return self._events(db, mission_id)

    def confirm_completion(self, mission_id: str, actor: str, at: float) -> None:
        """Bind confirmation to an eligible report under the same lock as owner messages."""
        with self._tx() as db:
            mission = self._mission(db, mission_id)
            events = self._events(db, mission_id)
            state = completion_state(events)
            recovery = next((e["type"] for e in reversed(events)
                             if e["type"] in ("mission.recovery_required", "mission.recovered")), None)
            owner_delivery_unknown = db.execute(
                "SELECT 1 FROM owner_outbox WHERE mission_id=? AND state='UNKNOWN' LIMIT 1",
                (mission_id,)).fetchone() is not None
            if not completion_awaiting_confirmation(
                    state, cancelled=bool(mission["cancelled"]),
                    recovery_required=recovery == "mission.recovery_required",
                    owner_delivery_unknown=owner_delivery_unknown):
                raise ValueError("no reported completion is awaiting the owner's confirmation")
            self._event(db, mission_id, "mission.completion_confirmed",
                        {"actor": actor, "at": at, "report_id": state["report"]["id"]})

    def resolve_reported_completion(self, mission_id: str, epoch: int, session_id: str, *,
                                    goal_status: str | None, reset_goal: bool, at: float,
                                    reporting_turn_id: str | None = None) -> str | None:
        """Resolve saved reports before vendor signals, atomically with any terminal event.

        A decline is durable before the caller resets the vendor goal. Until an explicit
        active observation is recorded, a restart must retry that reset and ignore stale
        native completion. Vendor RPCs run outside this transaction.
        """
        with self._tx() as db:
            self._writer(db, mission_id, epoch, session_id)
            events = self._events(db, mission_id)
            state = completion_state(events)
            if state["completed"]:
                return "completed"
            report = state["report"]
            data = {"epoch": epoch, "session_id": session_id, "at": at}
            if report:
                data["report_id"] = report["id"]
                if state["invalidated"]:
                    self._event(db, mission_id, "runtime.completion_declined",
                                dict(data, reason="owner_message"))
                    return "reset" if reset_goal else None
                if state["confirmation"]:
                    self._event(db, mission_id, "mission.completed",
                                dict(data, done_check=None, confirmed_by="owner"))
                    return "completed"
                return "awaiting"

            declined = state["declined"]
            boundary = declined["id"] if declined else 0
            reset_pending = bool(reset_goal and declined and
                                 declined["data"].get("epoch") == epoch and
                                 declined["data"].get("session_id") == session_id and not any(
                                     e["type"] == "runtime.completion_reset_observed" and
                                     e["id"] > boundary and e["data"].get("decline_id") == boundary
                                     for e in events))
            if reset_pending and goal_status == "active":
                self._event(db, mission_id, "runtime.completion_reset_observed",
                            dict(data, decline_id=boundary))
                reset_pending = False
            worker_done = any(e["type"] == "runtime.worker_done" and e["id"] > boundary and
                              e["data"].get("epoch") == epoch and
                              e["data"].get("session_id") == session_id for e in events)
            goal_complete = goal_status == "complete" and not reset_pending
            if not goal_complete and not worker_done:
                return "reset" if reset_pending else None
            self._event(db, mission_id, "runtime.completion_reported",
                        dict(data, source="goal_status" if goal_complete else "worker_done",
                             turn_id=reporting_turn_id))
            return "awaiting"

    def latest_event(self, mission_id: str, type: str, **matches) -> dict | None:
        """Newest event data of one type whose top-level fields equal ``matches`` (indexed)."""
        sql = "SELECT data FROM events WHERE mission_id=? AND type=?"
        params = [mission_id, type]
        for key, value in matches.items():
            if not key.isidentifier():
                raise ValueError(key)
            sql += f" AND json_extract(data, '$.{key}') IS ?"
            params.append(value)
        with self._read() as db:
            row = db.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return json.loads(row["data"]) if row else None

    def first_event(self, mission_id: str, type: str) -> dict | None:
        with self._read() as db:
            row = db.execute("SELECT data FROM events WHERE mission_id=? AND type=? ORDER BY id LIMIT 1",
                             (mission_id, type)).fetchone()
        return json.loads(row["data"]) if row else None

    def note_event(self, mission_id: str, type: str, data: dict) -> None:
        """Append runtime observations without changing core mission state."""
        self.note_events(mission_id, [(type, data)])

    def note_events(self, mission_id: str, events: list[tuple[str, dict]]) -> None:
        """Append related observations in one transaction: a reader sees all of them or none."""
        if not events or any(not kind.startswith(("mission.", "runtime.")) or not isinstance(data, dict)
                             for kind, data in events):
            raise ValueError("only mission.* or runtime.* dictionary events are allowed")
        with self._tx() as db:
            self._mission(db, mission_id)
            if any(kind in ("runtime.note", "runtime.evidence", "runtime.worker_done") for kind, _ in events):
                state = completion_state(self._events(db, mission_id))
                if state["report"] and not state["completed"]:
                    raise PermissionError("completion report is held; wait for the controller to process owner input")
            for kind, data in events:
                self._event(db, mission_id, kind, data)

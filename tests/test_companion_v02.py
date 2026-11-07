"""Synthetic tests for B1 (LighthouseReadOnlyDataSource & Tauri 2.0 shell security) and B2 (StepSummaryCard, v0.2 Result Card, Redacted Handoff Exporter, and CLI-only Pending Action Cards)."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import socket
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from gcl.companion import (
    EXECUTION_STATES,
    FRESHNESS_STATES,
    INDEPENDENT_REVIEW_STATES,
    LEDGER_STATES,
    OWNER_CONFIRMATION_STATES,
    VERIFICATION_STATES,
    LighthouseReadOnlyDataSource,
    RevisionMismatchError,
    audit_companion_shell_security,
    audit_third_party_notices,
    build_companion_view_model,
    build_done_check_result_card,
    build_pending_action_cards,
    build_step_summary_cards,
    default_tauri_security_config,
    export_redacted_handoff,
    redact_public_text,
    render_companion_html,
)
from gcl.companion.shell_security import load_repo_tauri_security_config
from gcl.core.store import SCHEMA_VERSION, LocalStore
from gcl.runtime.files import identity, mission_dir, write_json


def _seed_mission(
    home: Path,
    *,
    done_check: str | None = "test -f proof.txt",
    self_complete: bool = False,
    mode: str = "CONTROLLED_AUTO",
    now: float = 1_762_000_000.0,
) -> tuple[LocalStore, str]:
    store = LocalStore(home / "state.sqlite")
    beacon = {
        "owner": "human:owner",
        "owner_instruction": "Verify filter and empty state",
        "objective": "Click Unshipped and show only unshipped orders",
        "done_when": ["Filter unshipped orders", "Restore all orders intact"],
        "non_goals": ["No live payment gateway"],
        "constraints": ["Zero external network"],
        "route_boundaries": ["src/ui/**"],
        "acceptance": ["Empty state shown when 0 unshipped"],
        "decision_refs": ["D-26"],
    }
    mid = store.create_mission(beacon, "human:owner", mode)
    store.activate_initial(mid, "sess-1", "codex")
    store.note_event(
        mid,
        "runtime.config",
        {
            "executor": "codex",
            "cwd": str(home / "repo"),
            "done_check": done_check,
            "self_complete": self_complete,
        },
    )
    m_dir = mission_dir(home, mid)
    write_json(m_dir / "supervisor.json", identity())
    write_json(m_dir / "heartbeat.json", {"pid": identity()["pid"], "at": now})
    store.note_event(
        mid,
        "runtime.telemetry",
        {"at": now, "epoch": 1, "session_id": "sess-1", "context_tokens": 12000, "context_window": 200000},
    )
    return store, mid


def test_missing_home_and_missing_ledger_never_create_files(tmp_path, monkeypatch):
    blocked_net = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(socket, "socket", blocked_net)
    monkeypatch.setattr(socket, "create_connection", blocked_net)

    nonexistent = tmp_path / "does-not-exist"
    ds = LighthouseReadOnlyDataSource(nonexistent)
    desc = ds.describe()
    assert desc.ledger_state == "MISSING_HOME"
    assert desc.freshness == "UNKNOWN"
    assert desc.revision is None
    assert desc.read_only_enforced is True
    assert not nonexistent.exists()

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    ds_empty = LighthouseReadOnlyDataSource(empty_home)
    snap = ds_empty.snapshot()
    assert snap.ledger_state == "MISSING_LEDGER"
    assert snap.freshness == "UNKNOWN"
    assert snap.missions == ()
    assert not (empty_home / "state.sqlite").exists()
    assert list(empty_home.iterdir()) == []
    blocked_net.assert_not_called()


def test_unversioned_and_unsupported_schema_reported_without_migration(tmp_path):
    home = tmp_path / "unversioned-home"
    home.mkdir()
    ledger = home / "state.sqlite"
    with closing(sqlite3.connect(ledger)) as db:
        db.execute("CREATE TABLE custom (id TEXT)")
        db.execute("PRAGMA user_version=0")

    before_bytes = ledger.read_bytes()
    ds = LighthouseReadOnlyDataSource(home)
    desc = ds.refresh()
    assert desc.ledger_state == "UNVERSIONED_SCHEMA"
    assert desc.freshness == "UNKNOWN"
    assert desc.schema_version == 0
    assert ledger.read_bytes() == before_bytes
    assert not Path(str(ledger) + "-wal").exists()

    future_home = tmp_path / "future-home"
    store = LocalStore(future_home / "state.sqlite")
    store.create_mission({"owner": "human:test", "objective": "future"}, "human:test")
    with closing(sqlite3.connect(future_home / "state.sqlite")) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 5}")

    ds_future = LighthouseReadOnlyDataSource(future_home)
    snap_future = ds_future.snapshot()
    assert snap_future.ledger_state == "UNSUPPORTED_SCHEMA"
    assert snap_future.descriptor.schema_version == SCHEMA_VERSION + 5
    assert snap_future.freshness == "UNKNOWN"
    assert snap_future.missions == ()


def test_describe_refresh_snapshot_ternary_contract_and_revision_fence(tmp_path):
    now = 1_762_000_000.0
    home = tmp_path / "live-home"
    store, mid = _seed_mission(home, now=now)

    ds = LighthouseReadOnlyDataSource(home, now_fn=lambda: now + 2.0)
    d1 = ds.describe()
    assert d1.ledger_state == "READY"
    assert d1.freshness == "FRESH"
    assert d1.mission_count == 1
    assert d1.revision is not None

    snap1 = ds.snapshot(expected_revision=d1.revision)
    assert snap1.revision == d1.revision
    assert len(snap1.missions) == 1
    assert snap1.missions[0]["mission"] == mid

    # Advance the ledger in a writer process; snapshot with old expected_revision must fail-closed
    store.note_event(mid, "runtime.note", {"epoch": 1, "text": "new progress note"})
    with pytest.raises(RevisionMismatchError) as exc_info:
        ds.snapshot(expected_revision=d1.revision)
    assert exc_info.value.expected_revision == d1.revision
    assert exc_info.value.actual_revision != d1.revision

    # Refresh updates descriptor to the new atomic revision
    d2 = ds.refresh()
    assert d2.revision == exc_info.value.actual_revision
    snap2 = ds.snapshot(expected_revision=d2.revision)
    assert snap2.revision == d2.revision


def test_active_wal_detection_and_stale_telemetry_classification(tmp_path):
    now = 1_762_000_000.0
    home = tmp_path / "wal-home"
    store, mid = _seed_mission(home, now=now)
    wal_path = Path(str(home / "state.sqlite") + "-wal")
    wal_path.write_bytes(b"synthetic-uncheckpointed-wal-frames")

    ds_fresh = LighthouseReadOnlyDataSource(home, stale_after_s=20.0, now_fn=lambda: now + 5.0)
    desc_fresh = ds_fresh.refresh()
    assert desc_fresh.active_wal is True
    assert desc_fresh.wal_size_bytes > 0
    assert desc_fresh.freshness == "FRESH"
    assert desc_fresh.freshness_reason == "ready_with_active_wal"

    # Advance clock past stale_after_s
    ds_stale = LighthouseReadOnlyDataSource(home, stale_after_s=20.0, now_fn=lambda: now + 60.0)
    desc_stale = ds_stale.refresh()
    assert desc_stale.freshness == "STALE"


def test_synthetic_bundle_adapter_honors_ternary_contract():
    bundle = {
        "ledger_state": "READY",
        "schema_version": SCHEMA_VERSION,
        "active_wal": True,
        "wal_size_bytes": 8192,
        "missions": [
            {
                "mission": "a" * 32,
                "mode": "CONTROLLED_AUTO",
                "epoch": 2,
                "session_id": "syn-sess-2",
                "runtime_status": "ACTIVE",
                "reason": None,
                "freshness": "FRESH",
                "beacon": {"version": 1, "hash": "h123", "objective": "Synthetic objective"},
                "last_event_id": 9,
                "pending_owner_messages": 0,
                "effects": [],
                "events": [],
                "evidence": [],
            }
        ],
    }
    ds = LighthouseReadOnlyDataSource.from_synthetic_bundle(bundle)
    desc = ds.describe()
    assert desc.ledger_state == "READY"
    assert desc.active_wal is True
    assert desc.wal_size_bytes == 8192
    snap = ds.snapshot(expected_revision=desc.revision)
    assert snap.missions[0]["mission"] == "a" * 32
    with pytest.raises(RevisionMismatchError):
        ds.snapshot(expected_revision="rev-stale-0000")


def test_v02_result_card_four_states_and_missing_evidence_unknown(tmp_path):
    now = 1_762_000_000.0
    home = tmp_path / "card-home"
    store, mid = _seed_mission(home, done_check=None, self_complete=False, now=now)

    # Record verified evidence ONLY for the first criterion, leaving the 2nd & 3rd missing
    proof = tmp_path / "proof.txt"
    proof.write_text("filter verified\n")
    store.note_event(
        mid,
        "runtime.evidence",
        {
            "epoch": 1,
            "claim": "Filter unshipped orders",
            "evidence_path": str(proof),
            "sha256": hashlib.sha256(proof.read_bytes()).hexdigest(),
            "observed_at": now,
        },
    )
    store.resolve_reported_completion(
        mid, 1, "sess-1", goal_status="complete", reset_goal=False, at=now + 1.0
    )

    ds = LighthouseReadOnlyDataSource(home, now_fn=lambda: now + 2.0)
    snap = ds.snapshot()
    mission = snap.missions[0]

    card = build_done_check_result_card(mission, home_override="/Users/private/rlh-home")
    assert card.schema_version == "rlh.companion.result_card.v0.2"
    assert card.execution_state in EXECUTION_STATES
    assert card.verification_state in VERIFICATION_STATES
    assert card.independent_review_state in INDEPENDENT_REVIEW_STATES
    assert card.owner_confirmation_state in OWNER_CONFIRMATION_STATES

    assert card.execution_state == "COMPLETED"
    # Because 2 of 3 criteria lack evidence and no done_check ran, overall verification must be UNKNOWN
    assert card.verification_state == "UNKNOWN"
    assert card.independent_review_state == "UNKNOWN"
    assert card.owner_confirmation_state == "AWAITING_CONFIRMATION"
    assert card.overall_status == "AWAITING_CONFIRMATION"

    # Item 1 has valid file evidence -> VERIFIED, but pending independent review
    assert card.items[0].verification_state == "VERIFIED"
    assert card.items[0].overall_verdict == "PASS_PENDING_INDEPENDENT_REVIEW"
    # Items 2 and 3 have missing evidence -> MUST be marked UNKNOWN
    assert card.items[1].verification_state == "UNKNOWN"
    assert card.items[1].overall_verdict == "UNKNOWN"
    assert card.items[2].verification_state == "UNKNOWN"
    assert card.items[2].overall_verdict == "UNKNOWN"

    # Pending action must be CLI-only with direct_write_enabled=False
    assert len(card.pending_actions) == 1
    action = card.pending_actions[0]
    assert action.action_kind == "CONFIRM_COMPLETION"
    assert action.direct_write_enabled is False
    assert f"confirm {mid}" in action.cli_command
    assert action.receipt_binding["mission_id"] == mid
    assert action.receipt_binding["report_id"] is not None


def test_independent_review_and_unknown_delivery_recovery_actions(tmp_path):
    now = 1_762_000_000.0
    home = tmp_path / "review-home"
    store, mid = _seed_mission(home, done_check="test -f proof.txt", mode="OBSERVE_ONLY", now=now)

    # 1. OBSERVE_ONLY active mission offers CLI adopt preview
    snap_obs = LighthouseReadOnlyDataSource(home, now_fn=lambda: now + 1.0).snapshot()
    actions_obs = build_pending_action_cards(snap_obs.missions[0])
    assert any(a.action_kind == "ADOPT_CONTROLLED_AUTO" and not a.direct_write_enabled for a in actions_obs)

    # 2. Independent review APPROVED + done_check completed -> VERIFIED_COMPLETE
    store.note_event(
        mid,
        "runtime.verification",
        {"rollover_id": "r1", "fresh_ok": True, "checkpoint": {"verdict": "TRUSTED"}},
    )
    store.note_event(
        mid,
        "mission.completed",
        {"epoch": 1, "session_id": "sess-1", "done_check": "test -f proof.txt", "at": now + 2.0},
    )
    snap_done = LighthouseReadOnlyDataSource(home, now_fn=lambda: now + 3.0).snapshot()
    card_done = build_done_check_result_card(snap_done.missions[0])
    assert card_done.execution_state == "COMPLETED"
    assert card_done.verification_state == "VERIFIED"
    assert card_done.independent_review_state == "APPROVED"
    assert card_done.owner_confirmation_state == "NOT_REQUIRED"
    assert card_done.overall_status == "VERIFIED_COMPLETE"
    assert all(item.overall_verdict == "VERIFIED_AND_REVIEWED" for item in card_done.items)

    # 3. OWNER_DELIVERY_UNKNOWN surfaces CLI delivery resolve action with direct_write_enabled=False
    home_unk = tmp_path / "unk-home"
    store_unk, mid_unk = _seed_mission(home_unk, done_check=None, now=now)
    store_unk.post_owner_message(mid_unk, "check delivery", "human:owner")
    delivery = store_unk.prepare_owner_delivery(mid_unk, "sess-1", 1)
    assert store_unk.claim_owner_delivery(mid_unk, delivery["id"], "sess-1", 1)
    snap_unk = LighthouseReadOnlyDataSource(home_unk, now_fn=lambda: now + 1.0).snapshot()
    assert snap_unk.freshness == "UNKNOWN"
    card_unk = build_done_check_result_card(snap_unk.missions[0])
    assert card_unk.execution_state == "BLOCKED"
    assert any(
        a.action_kind == "RESOLVE_UNKNOWN_DELIVERY"
        and delivery["id"] in a.cli_command
        and a.direct_write_enabled is False
        for a in card_unk.pending_actions
    )


def test_step_summary_cards_and_redacted_exporter_strip_sensitive_data(tmp_path):
    now = 1_762_000_000.0
    home = tmp_path / "steps-home"
    store, mid = _seed_mission(home, done_check=None, now=now)

    store.note_event(
        mid,
        "runtime.goal_boundary_red",
        {"reason": "foreign_commit_worktree", "path": "/Users/synthetic-owner/secret-worktree"},
    )
    store.note_event(
        mid,
        "runtime.monitor_decision",
        {
            "epoch": 1,
            "level": "YELLOW",
            "action": "ROLLOVER",
            "reasons": ["no_progress"],
            "metrics": {"occupancy": 0.65},
        },
    )
    store.note_event(
        mid,
        "runtime.done_check_failed",
        {"error": "failed connecting to 100.64.10.25:19999 with sk-proj-1234567890abcdef"},
    )
    store.note_event(
        mid,
        "runtime.note",
        {"epoch": 1, "text": "Checked synthetic-node.example.ts.net:19998 and /Users/synthetic-owner/repo/file.py"},
    )

    snap = LighthouseReadOnlyDataSource(home, now_fn=lambda: now + 1.0).snapshot()
    mission = snap.missions[0]
    steps = build_step_summary_cards(mission)
    categories = [s.category for s in steps]
    assert "OUT_OF_SCOPE_EDIT" in categories
    assert "EVIDENCE_GAP" in categories
    assert "REPEATED_ERROR" in categories
    assert "EXECUTION_NOTE" in categories

    # Alert cards expand by default; routine notes collapse by default
    by_cat = {s.category: s for s in steps}
    assert by_cat["OUT_OF_SCOPE_EDIT"].collapsed_by_default is False
    assert by_cat["EVIDENCE_GAP"].collapsed_by_default is False
    assert by_cat["EXECUTION_NOTE"].collapsed_by_default is True

    handoff = export_redacted_handoff(mission, snapshot_revision=snap.revision)
    serialized = json.dumps(handoff, ensure_ascii=False)
    for forbidden in (
        "/Users/synthetic-owner",
        "100.64.10.25",
        "19999",
        "synthetic-node.example.ts.net",
        "sk-proj-1234567890abcdef",
        str(tmp_path),
    ):
        assert forbidden not in serialized
    assert "<REDACTED_" in serialized
    assert handoff["privacy_mode"] == "REDACTED_PUBLIC_SAFE_OD14"
    assert len(handoff["handoff_sha256"]) == 64

    view_model = build_companion_view_model(snap)
    html_doc = render_companion_html(view_model)
    assert "connect-src 'none'" in html_doc
    assert 'data-read-only="true"' in html_doc


def test_tauri_security_shell_and_clean_room_license_audit():
    repo_root = Path(__file__).resolve().parents[1]
    config = load_repo_tauri_security_config(repo_root)
    assert config == default_tauri_security_config()

    audit = audit_companion_shell_security(config)
    assert audit == {"ok": True, "violations": []}

    # Tampered CSP or direct write buttons must fail-closed
    bad_csp = json.loads(json.dumps(config))
    bad_csp["app"]["security"]["csp"] = "default-src 'self'; connect-src https://api.example.com"
    bad_csp["companionGuardrails"]["directDbWriteButtons"] = True
    bad_csp["companionGuardrails"]["allowedIpcCommands"].append("sql_write_mutation")
    failed = audit_companion_shell_security(bad_csp)
    assert failed["ok"] is False
    assert len(failed["violations"]) >= 3

    notices_audit = audit_third_party_notices(repo_root / "THIRD_PARTY_NOTICES.md")
    assert notices_audit == {"ok": True, "violations": []}

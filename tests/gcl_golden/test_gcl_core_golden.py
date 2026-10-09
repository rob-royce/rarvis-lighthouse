"""GCL core golden tests (normative, written by CC; implementers must not edit).

Contract: docs/gcl/ADR-GCL-001.md §4. If a golden test contradicts the ADR,
stop and report instead of working around it.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import threading
import time

import pytest

from gcl.core import beacon, checkpoint, monitor, sideeffects, snapshot, verify
from gcl.core.errors import (
    CheckpointIncomplete,
    GoalChangeUnauthorized,
    IllegalTransition,
    MissionCancelled,
    StaleEpoch,
)
from gcl.core.store import LocalStore

OWNER = "human:owner"


def _beacon(**over):
    b = {
        "owner": OWNER,
        "owner_instruction": {"text": "Add a --json flag to tool X", "source": "cli:start"},
        "objective": "tool X supports --json output",
        "done_when": ["tests/test_x.py passes", "README documents --json"],
        "non_goals": ["no new dependencies"],
        "constraints": ["no network"],
        "route_boundaries": ["only tools/x.py and tests/test_x.py"],
        "acceptance": [{"id": "A1", "text": "json output parses", "evidence": "pytest"}],
        "decision_refs": [],
        "plan": ["read x.py", "add flag"],
        "assumptions": ["python 3.11"],
    }
    b.update(over)
    return b


@pytest.fixture()
def st(tmp_path):
    return LocalStore(tmp_path / "gcl.sqlite")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    (r / "bin.dat").write_bytes(bytes(range(256)))
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "base")
    return r


def _full_checkpoint(st, mid, sid, repo_dir, snap, **over):
    b = st.get_beacon(mid)
    ck = {
        "mission_id": mid,
        "predecessor_session_id": sid,
        "beacon_version": b["version"],
        "beacon_hash": b["hash"],
        "phase": "implement",
        "next_step": {"acceptance_id": "A1", "action": "write test"},
        "verified_results": [],
        "blockers": [],
        "unverified_claims": [],
        "abandoned_hypotheses": [],
        "repo": {"path": str(repo_dir), "head": _git(repo_dir, "rev-parse", "HEAD").strip()},
        "snapshot": snap,
        "active_jobs": [],
        "pending_effects": [],
        "owner_cursor": 0,
        "resume_method": "fresh session with beacon brief + checkpoint",
    }
    ck.update(over)
    return ck


def _roll_to_prepared(st, mid, epoch, repo_dir, tmp_path, sid="s1"):
    rid = st.request_rollover(mid, "context_pressure", requested_by_epoch=epoch)
    st.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    snap = snapshot.capture(repo_dir, tmp_path / f"snap-{rid}")
    st.save_checkpoint(rid, _full_checkpoint(st, mid, sid, repo_dir, snap))
    assert st.rollover_state(rid) == "CHECKPOINT_READY"
    st.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY")
    return rid


# ---------------------------------------------------------------- boundary

def test_core_imports_no_platform_packages():
    core = pathlib.Path(__file__).resolve().parents[2] / "gcl" / "core"
    banned = {"bridge", "blackboard", "adapters", "router", "providers", "integrations"}
    for py in core.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] not in banned, f"{py.name} imports {n}"


# ---------------------------------------------------------------- beacon

def test_hash_is_canonical_and_ignores_mutable_fields():
    a = _beacon()
    b = dict(reversed(list(_beacon().items())))
    assert beacon.canonical_hash(a) == beacon.canonical_hash(b)
    c = _beacon(plan=["totally different plan"], assumptions=[])
    assert beacon.canonical_hash(a) == beacon.canonical_hash(c)
    d = _beacon(done_when=["tests/test_x.py passes"])
    assert beacon.canonical_hash(a) != beacon.canonical_hash(d)


def test_brief_contains_anchor_fields():
    txt = beacon.render_brief({**_beacon(mission_id="m1"), "version": 3, "hash": "abc123"}, max_chars=4000)
    for needle in ("tool X supports --json output", "tests/test_x.py passes", "no new dependencies", "abc123"):
        assert needle in txt
    with pytest.raises(ValueError, match="contract"):
        beacon.render_brief({**_beacon(mission_id="m1", objective="x" * 20000), "version": 1, "hash": "h"}, max_chars=4000)


def test_executor_cannot_change_goal_but_owner_can(st):
    mid = st.create_mission(_beacon(), actor=OWNER)
    v1 = st.get_beacon(mid)
    assert v1["version"] == 1 and v1["mission_id"] == mid
    with pytest.raises(GoalChangeUnauthorized):
        st.change_goal(mid, {"done_when": ["anything"]}, actor="session:abc", authority_ref="self")
    with pytest.raises(GoalChangeUnauthorized):
        st.change_goal(mid, {"objective": "bigger scope"}, actor=OWNER, authority_ref=None)
    assert any(e["type"] == "goal.change_rejected" for e in st.events(mid))
    assert st.get_beacon(mid)["hash"] == v1["hash"]
    # legitimate plan adjustment by executor: no version bump
    assert st.change_goal(mid, {"plan": ["new plan"]}, actor="session:abc", authority_ref=None) == 1
    assert st.get_beacon(mid)["hash"] == v1["hash"]
    # owner goal change with authority: new version, old retained
    v = st.change_goal(mid, {"done_when": ["tests/test_x.py passes"]}, actor=OWNER, authority_ref="chat:2026-09-29T10:00")
    assert v == 2
    assert st.get_beacon(mid, version=1)["hash"] == v1["hash"]
    assert st.get_beacon(mid)["hash"] != v1["hash"]


# ---------------------------------------------------------------- rollover / single writer

def test_state_machine_forward_only(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    e1 = st.activate_initial(mid, "s1", "codex")
    assert e1 == 1 and st.current_writer(mid) == (1, "s1")
    rid = st.request_rollover(mid, "context_pressure", requested_by_epoch=1)
    assert st.request_rollover(mid, "loop", requested_by_epoch=1) == rid  # dedupe
    with pytest.raises(IllegalTransition):
        st.advance(rid, "ROLLOVER_REQUESTED", "SUCCESSOR_ACTIVE")
    with pytest.raises(IllegalTransition):
        st.advance(rid, "QUIESCING", "CHECKPOINT_READY")  # expected_state mismatch
    st.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    with pytest.raises(IllegalTransition):
        st.advance(rid, "QUIESCING", "ROLLOVER_REQUESTED")  # no regression
    snap = snapshot.capture(repo, tmp_path / "snap")
    st.save_checkpoint(rid, _full_checkpoint(st, mid, "s1", repo, snap))
    st.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY")
    # successor prepared but not yet writer
    assert not st.check_writer(mid, "s2", 2)
    assert st.check_writer(mid, "s1", 1)
    e2 = st.transfer_ownership(rid, "s2", expected_epoch=1)
    assert e2 == 2 and st.rollover_state(rid) == "OWNERSHIP_TRANSFERRED"
    assert st.check_writer(mid, "s2", 2) and not st.check_writer(mid, "s1", 1)
    st.advance(rid, "OWNERSHIP_TRANSFERRED", "SUCCESSOR_ACTIVE")
    st.advance(rid, "SUCCESSOR_ACTIVE", "PREDECESSOR_RETIRED")
    with pytest.raises(IllegalTransition):
        st.abort_rollover(rid, "late")
    # retired predecessor cannot request another rollover or write
    with pytest.raises(StaleEpoch):
        st.request_rollover(mid, "late callback", requested_by_epoch=1)


def test_concurrent_transfer_has_exactly_one_winner(tmp_path, repo):
    path = tmp_path / "gcl.sqlite"
    st = LocalStore(path)
    mid = st.create_mission(_beacon(), actor=OWNER)
    st.activate_initial(mid, "s1", "claude")
    rid = _roll_to_prepared(st, mid, 1, repo, tmp_path)
    results, errors = [], []

    def go(name):
        try:
            results.append(LocalStore(path).transfer_ownership(rid, name, expected_epoch=1))
        except StaleEpoch as exc:  # the loser
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(f"succ{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [2] and len(errors) == 7
    epoch, sid = st.current_writer(mid)
    assert epoch == 2 and sid.startswith("succ")


def test_abort_before_transfer_keeps_predecessor(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    st.activate_initial(mid, "s1", "codex")
    rid = _roll_to_prepared(st, mid, 1, repo, tmp_path)
    assert st.abort_rollover(rid, "successor failed to start") == "ABORTED"
    assert st.current_writer(mid) == (1, "s1")
    rid2 = st.request_rollover(mid, "retry", requested_by_epoch=1)
    assert rid2 != rid


def test_incomplete_checkpoint_rejected_and_state_kept(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    st.activate_initial(mid, "s1", "codex")
    rid = st.request_rollover(mid, "context_pressure", requested_by_epoch=1)
    st.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    snap = snapshot.capture(repo, tmp_path / "snap")
    ck = _full_checkpoint(st, mid, "s1", repo, snap)
    del ck["pending_effects"]
    with pytest.raises(CheckpointIncomplete):
        st.save_checkpoint(rid, ck)
    assert st.rollover_state(rid) == "QUIESCING"
    assert st.get_checkpoint(rid) is None
    # controller restart: a new store instance sees the same durable state
    st2 = LocalStore(st.path)
    assert st2.rollover_state(rid) == "QUIESCING"
    assert set(checkpoint.REQUIRED_FIELDS) <= set(_full_checkpoint(st, mid, "s1", repo, snap))


def test_cancel_during_rollover_blocks_successor(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    st.activate_initial(mid, "s1", "codex")
    rid = _roll_to_prepared(st, mid, 1, repo, tmp_path)
    st.cancel(mid, actor=OWNER)
    with pytest.raises(MissionCancelled):
        st.transfer_ownership(rid, "s2", expected_epoch=1)
    assert not st.check_writer(mid, "s2", 2)


# ---------------------------------------------------------------- owner input

def test_owner_messages_survive_rollover_and_go_to_current_writer_once(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    st.activate_initial(mid, "s1", "claude")
    st.post_owner_message(mid, "first", actor=OWNER)
    assert [m["text"] for m in st.consume_owner_messages(mid, "s1", 1)] == ["first"]
    rid = _roll_to_prepared(st, mid, 1, repo, tmp_path)
    st.post_owner_message(mid, "during handoff", actor=OWNER)
    st.transfer_ownership(rid, "s2", expected_epoch=1)
    with pytest.raises(StaleEpoch):
        st.consume_owner_messages(mid, "s1", 1)
    assert [m["text"] for m in st.consume_owner_messages(mid, "s2", 2)] == ["during handoff"]
    assert st.consume_owner_messages(mid, "s2", 2) == []


# ---------------------------------------------------------------- side effects

def test_in_flight_effect_becomes_unknown_and_is_never_blindly_reissued(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    st.activate_initial(mid, "s1", "codex")
    st.record_effect(mid, "fx-1", "local_submit", epoch=1)
    st.record_effect(mid, "fx-2", "local_submit", epoch=1)
    st.mark_effect("fx-1", "IN_FLIGHT", epoch=1)
    st.mark_effect("fx-2", "IN_FLIGHT", epoch=1)
    st.mark_effect("fx-2", "CONFIRMED_DONE", epoch=1, receipt={"id": "r2"})
    rid = _roll_to_prepared(st, mid, 1, repo, tmp_path)
    st.transfer_ownership(rid, "s2", expected_epoch=1)
    assert st.effect_state("fx-1") == "OUTCOME_UNKNOWN"
    assert st.effect_state("fx-2") == "CONFIRMED_DONE"
    with pytest.raises(StaleEpoch):
        st.mark_effect("fx-1", "CONFIRMED_DONE", epoch=1, receipt={"late": True})  # late callback from old writer
    assert not sideeffects.may_execute("OUTCOME_UNKNOWN", has_idempotent_query=True)
    assert not sideeffects.may_execute("OUTCOME_UNKNOWN", has_idempotent_query=False)
    assert sideeffects.may_query("OUTCOME_UNKNOWN", has_idempotent_query=True)
    assert not sideeffects.may_query("OUTCOME_UNKNOWN", has_idempotent_query=False)
    assert not sideeffects.may_execute("CONFIRMED_DONE", has_idempotent_query=True)
    assert not sideeffects.may_execute("IN_FLIGHT", has_idempotent_query=True)
    assert sideeffects.may_execute("NOT_STARTED", has_idempotent_query=False)


# ---------------------------------------------------------------- snapshot

def test_snapshot_preserves_uncommitted_and_untracked_without_touching_repo(repo, tmp_path):
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "bin.dat").write_bytes(bytes(reversed(range(256))))
    (repo / "new_dir").mkdir()
    (repo / "new_dir" / "u.txt").write_text("untracked\n")
    before = (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "stash", "list"),
        _git(repo, "reflog"),
        _git(repo, "status", "--porcelain"),
        (repo / ".git" / "index").read_bytes(),
    )
    man = snapshot.capture(repo, tmp_path / "snap")
    after = (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "stash", "list"),
        _git(repo, "reflog"),
        _git(repo, "status", "--porcelain"),
        (repo / ".git" / "index").read_bytes(),
    )
    assert before == after
    assert (repo / "new_dir" / "u.txt").read_text() == "untracked\n"
    assert snapshot.verify(man, repo)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(repo), str(clone))
    snapshot.restore(man, clone)
    assert (clone / "a.txt").read_text() == "one\ntwo\n"
    assert (clone / "bin.dat").read_bytes() == bytes(reversed(range(256)))
    assert (clone / "new_dir" / "u.txt").read_text() == "untracked\n"
    assert snapshot.verify(man, clone)
    (clone / "a.txt").write_text("tampered\n")
    assert not snapshot.verify(man, clone)


# ---------------------------------------------------------------- verification

def test_verify_checkpoint_rejects_forgery_and_downgrades_stale_evidence(st, repo, tmp_path):
    mid = st.create_mission(_beacon(), actor=OWNER)
    b = st.get_beacon(mid)
    snap = snapshot.capture(repo, tmp_path / "snap")
    ev = tmp_path / "ev.txt"
    ev.write_text("pytest: 3 passed\n")
    import hashlib

    good_ev = {"claim": "A1 tests pass", "evidence_path": str(ev),
               "sha256": hashlib.sha256(ev.read_bytes()).hexdigest(), "observed_at": time.time()}
    ck = _full_checkpoint(st, mid, "s1", repo, snap, verified_results=[good_ev])
    assert verify.verify_checkpoint(ck, repo, b, now=time.time(), max_evidence_age_s=3600)["verdict"] == "TRUSTED"

    forged_hash = dict(ck, beacon_hash="0" * 64)
    assert verify.verify_checkpoint(forged_hash, repo, b, now=time.time(), max_evidence_age_s=3600)["verdict"] == "REJECTED"

    forged_head = dict(ck, repo={"path": str(repo), "head": "f" * 40})
    assert verify.verify_checkpoint(forged_head, repo, b, now=time.time(), max_evidence_age_s=3600)["verdict"] == "REJECTED"

    (repo / "a.txt").write_text("drifted after checkpoint\n")
    assert verify.verify_checkpoint(ck, repo, b, now=time.time(), max_evidence_age_s=3600)["verdict"] == "REJECTED"
    (repo / "a.txt").write_text("one\n")

    stale = dict(good_ev, observed_at=time.time() - 7200)
    missing = dict(good_ev, evidence_path=str(tmp_path / "nope.txt"))
    wrong = dict(good_ev, sha256="1" * 64)
    for bad in (stale, missing, wrong):
        rep = verify.verify_checkpoint(dict(ck, verified_results=[bad]), repo, b, now=time.time(), max_evidence_age_s=3600)
        assert rep["verdict"] == "PARTIAL", bad
        assert any(not i["ok"] for i in rep["items"])


def test_verify_fresh_rejects_resume_and_fork():
    ok, _ = verify.verify_fresh({"executor": "codex", "session_meta": {"id": "t1", "session_id": "t1"},
                                 "first_token_count": {"total": 25790, "last": 25790}})
    assert ok
    for meta in ({"id": "t2", "session_id": "t2", "forked_from_id": "t1"},
                 {"id": "t2", "session_id": "t1", "parent_thread_id": "t1"},
                 {"id": "t2", "session_id": "t1"}):
        assert not verify.verify_fresh({"executor": "codex", "session_meta": meta})[0]
    assert not verify.verify_fresh({"executor": "codex", "session_meta": {"id": "t3", "session_id": "t3"},
                                    "first_token_count": {"total": 900000, "last": 25000}})[0]
    good_c = {"executor": "claude", "requested_session_id": "u1", "session_id": "u1",
              "hook_source": "startup", "transcript_preexisting": False}
    assert verify.verify_fresh(good_c)[0]
    for bad in ({"hook_source": "resume"}, {"hook_source": "fork"}, {"hook_source": "compact"},
                {"transcript_preexisting": True}, {"session_id": "other"}):
        assert not verify.verify_fresh({**good_c, **bad})[0]


# ---------------------------------------------------------------- monitor

def _sig(**over):
    now = 10_000.0
    s = {"now": now, "session_started_at": now - 3600, "last_rollover_at": None, "rollovers_last_hour": 0,
         "context_tokens": 50_000, "context_window": 258_400, "cumulative_tokens": 20_000_000,
         "repeated_error_count": 0, "turns_without_evidence": 0, "blocker_kind": None,
         "goal_hash_matches": True, "unauthorized_goal_change": False,
         "previous_rollover_reason": None, "evidence_since_last_rollover": True}
    s.update(over)
    return s


def test_monitor_uses_current_occupancy_not_cumulative():
    d = monitor.evaluate(_sig(), monitor.Policy(), "CONTROLLED_AUTO")
    assert d["level"] == "GREEN" and d["action"] == "CONTINUE"
    assert d["metrics"]["occupancy_quality"] == "MEASURED"
    assert abs(d["metrics"]["occupancy"] - 50_000 / 258_400) < 1e-9
    d = monitor.evaluate(_sig(context_tokens=230_000), monitor.Policy(), "CONTROLLED_AUTO")
    assert d["level"] == "RED" and d["action"] == "ROLLOVER"


def test_monitor_unknown_metrics_never_trigger_context_rollover():
    d = monitor.evaluate(_sig(context_tokens=None, context_window=None), monitor.Policy(), "CONTROLLED_AUTO")
    assert d["metrics"]["occupancy"] is None and d["metrics"]["occupancy_quality"] == "UNKNOWN"
    assert d["action"] != "ROLLOVER"


def test_monitor_environment_blocker_blocks_instead_of_rollover():
    for kind in ("rate_limit", "network", "auth", "quota"):
        d = monitor.evaluate(_sig(context_tokens=240_000, blocker_kind=kind), monitor.Policy(), "CONTROLLED_AUTO")
        assert d["action"] == "BLOCK", kind


def test_monitor_goal_violation_is_red_block():
    d = monitor.evaluate(_sig(unauthorized_goal_change=True), monitor.Policy(), "CONTROLLED_AUTO")
    assert d["level"] == "RED" and d["action"] == "BLOCK"
    d = monitor.evaluate(_sig(goal_hash_matches=False), monitor.Policy(), "CONTROLLED_AUTO")
    assert d["level"] == "RED" and d["action"] == "BLOCK"


def test_monitor_cooldown_budget_and_no_progress_prevent_rollover_loops():
    p = monitor.Policy()
    hot = dict(context_tokens=240_000)
    now = 10_000.0
    # within min_work after session start
    assert monitor.evaluate(_sig(session_started_at=now - 60, **hot), p, "CONTROLLED_AUTO")["action"] != "ROLLOVER"
    # within cooldown after last rollover
    assert monitor.evaluate(_sig(last_rollover_at=now - 100, **hot), p, "CONTROLLED_AUTO")["action"] != "ROLLOVER"
    # hourly cap exhausted -> BLOCK
    assert monitor.evaluate(_sig(rollovers_last_hour=3, **hot), p, "CONTROLLED_AUTO")["action"] == "BLOCK"
    # loop signal: first time rollover allowed, repeating the same reason with no new evidence -> BLOCK
    loop = dict(repeated_error_count=5)
    assert monitor.evaluate(_sig(**loop), p, "CONTROLLED_AUTO")["action"] == "ROLLOVER"
    d = monitor.evaluate(_sig(previous_rollover_reason="error_loop", evidence_since_last_rollover=False,
                              last_rollover_at=now - 2000, **loop), p, "CONTROLLED_AUTO")
    assert d["action"] == "BLOCK"


def test_monitor_modes():
    hot = _sig(context_tokens=240_000)
    assert monitor.evaluate(hot, monitor.Policy(), "OBSERVE_ONLY")["action"] == "OBSERVE"
    assert monitor.evaluate(hot, monitor.Policy(), "OBSERVE_ONLY")["level"] == "RED"
    assert monitor.evaluate(hot, monitor.Policy(), "OFF")["action"] == "NONE"

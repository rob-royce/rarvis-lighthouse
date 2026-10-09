"""GCL core golden tests round 2 (normative, CC review of T1 commit 06191bd).

Closes CC review findings GCL-T1-F1..F3 (see docs/gcl/ADR-GCL-001.md §4 addendum).
"""
from __future__ import annotations

import subprocess

import pytest

from gcl.core import snapshot
from gcl.core.errors import IllegalTransition
from gcl.core.store import LocalStore

OWNER = "human:owner"


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
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "base")
    return r


@pytest.fixture()
def st(tmp_path):
    return LocalStore(tmp_path / "gcl.sqlite")


def _mission(st):
    mid = st.create_mission({"owner": OWNER, "owner_instruction": {"text": "t", "source": "test"},
                             "objective": "o", "done_when": ["d"], "non_goals": [], "constraints": [],
                             "route_boundaries": [], "acceptance": [], "decision_refs": []}, actor=OWNER)
    st.activate_initial(mid, "s1", "codex")
    return mid


def _ck(st, mid, sid, repo, snap):
    b = st.get_beacon(mid)
    return {"mission_id": mid, "predecessor_session_id": sid, "beacon_version": b["version"],
            "beacon_hash": b["hash"], "phase": "p", "next_step": {}, "verified_results": [], "blockers": [],
            "unverified_claims": [], "abandoned_hypotheses": [],
            "repo": {"path": str(repo), "head": _git(repo, "rev-parse", "HEAD").strip()}, "snapshot": snap,
            "active_jobs": [], "pending_effects": [], "owner_cursor": 0, "resume_method": "fresh"}


def _to_checkpoint(st, mid, epoch, sid, repo, tmp_path, tag):
    rid = st.request_rollover(mid, "context_pressure", requested_by_epoch=epoch)
    st.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    st.save_checkpoint(rid, _ck(st, mid, sid, repo, snapshot.capture(repo, tmp_path / f"snap-{tag}")))
    return rid


# F1: owner input posted while a rollover is quiescing is held for the next writer, not consumed by the predecessor.
def test_f1_owner_messages_held_during_quiescing(st, repo, tmp_path):
    mid = _mission(st)
    rid = st.request_rollover(mid, "context_pressure", requested_by_epoch=1)
    st.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    st.post_owner_message(mid, "held", actor=OWNER)
    assert st.consume_owner_messages(mid, "s1", 1) == []
    st.save_checkpoint(rid, _ck(st, mid, "s1", repo, snapshot.capture(repo, tmp_path / "snap")))
    assert st.consume_owner_messages(mid, "s1", 1) == []
    # abort: predecessor keeps writing and now receives the held message exactly once
    st.abort_rollover(rid, "successor failed")
    assert [m["text"] for m in st.consume_owner_messages(mid, "s1", 1)] == ["held"]
    assert st.consume_owner_messages(mid, "s1", 1) == []


# F2: the successor identity bound at prepare time is the only one that may take ownership.
def test_f2_transfer_bound_to_prepared_successor(st, repo, tmp_path):
    mid = _mission(st)
    rid = _to_checkpoint(st, mid, 1, "s1", repo, tmp_path, "a")
    st.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY", successor_session_id="s2")
    with pytest.raises(IllegalTransition):
        st.transfer_ownership(rid, "intruder", expected_epoch=1)
    assert st.current_writer(mid) == (1, "s1")
    assert st.transfer_ownership(rid, "s2", expected_epoch=1) == 2


# F3: a new writer cannot start the next rollover until the previous one is retired, and never gets the old id.
def test_f3_next_rollover_requires_previous_retired(st, repo, tmp_path):
    mid = _mission(st)
    rid = _to_checkpoint(st, mid, 1, "s1", repo, tmp_path, "a")
    st.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY", successor_session_id="s2")
    st.transfer_ownership(rid, "s2", expected_epoch=1)
    with pytest.raises(IllegalTransition):
        st.request_rollover(mid, "context_pressure", requested_by_epoch=2)
    st.advance(rid, "OWNERSHIP_TRANSFERRED", "SUCCESSOR_ACTIVE")
    with pytest.raises(IllegalTransition):
        st.request_rollover(mid, "context_pressure", requested_by_epoch=2)
    st.advance(rid, "SUCCESSOR_ACTIVE", "PREDECESSOR_RETIRED")
    rid2 = st.request_rollover(mid, "context_pressure", requested_by_epoch=2)
    assert rid2 != rid and st.rollover_state(rid2) == "ROLLOVER_REQUESTED"

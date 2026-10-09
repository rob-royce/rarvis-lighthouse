"""Crash recovery and independent-process contract checks."""
from __future__ import annotations

from contextlib import closing
import multiprocessing
import sqlite3
import subprocess

import pytest

from gcl.core import beacon, snapshot
from gcl.core.errors import IllegalTransition, SchemaTooNew, StaleEpoch
from gcl.core.store import SCHEMA_VERSION, LocalStore


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked file.txt").write_text("before\n")
    (repo / "delete me.txt").write_text("delete\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _beacon():
    return {"owner": "human:test", "objective": "finish", "done_when": ["verified"], "non_goals": []}


def _checkpoint(store, mid, repo, manifest):
    b = store.get_beacon(mid)
    return {"mission_id": mid, "predecessor_session_id": "old", "beacon_version": b["version"],
            "beacon_hash": b["hash"], "phase": "working", "next_step": {"action": "continue"},
            "verified_results": [], "blockers": [], "unverified_claims": [], "abandoned_hypotheses": [],
            "repo": {"path": str(repo), "head": _git(repo, "rev-parse", "HEAD").decode().strip()},
            "snapshot": manifest, "active_jobs": [], "pending_effects": [], "owner_cursor": 0,
            "resume_method": "fresh"}


def _prepared(tmp_path):
    repo = _repo(tmp_path)
    path = tmp_path / "store.sqlite"
    store = LocalStore(path)
    mid = store.create_mission(_beacon(), "human:test")
    store.activate_initial(mid, "old", "codex")
    rid = store.request_rollover(mid, "context_pressure", 1)
    store.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    manifest = snapshot.capture(repo, tmp_path / "snapshot")
    store.save_checkpoint(rid, _checkpoint(store, mid, repo, manifest))
    store.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY")
    return path, mid, rid


def _transfer_worker(path, rid, successor, start, results):
    start.wait()
    try:
        epoch = LocalStore(path).transfer_ownership(rid, successor, 1)
        results.put(("winner", epoch, successor))
    except StaleEpoch:
        results.put(("stale", None, successor))
    except Exception as exc:
        results.put(("error", repr(exc), successor))


def test_controller_restarts_at_each_rollover_state(tmp_path):
    repo = _repo(tmp_path)
    path = tmp_path / "state.sqlite"
    store = LocalStore(path)
    mid = store.create_mission(_beacon(), "human:test")
    store.activate_initial(mid, "old", "codex")
    assert LocalStore(path).current_writer(mid) == (1, "old")
    rid = store.request_rollover(mid, "context_pressure", 1)
    states = ["ROLLOVER_REQUESTED", "QUIESCING", "CHECKPOINT_READY",
              "SUCCESSOR_PREPARED_READ_ONLY", "OWNERSHIP_TRANSFERRED",
              "SUCCESSOR_ACTIVE", "PREDECESSOR_RETIRED"]
    for state in states:
        store = LocalStore(path)
        assert store.rollover_state(rid) == state
        assert LocalStore(path).rollover_state(rid) == state
        if state == "ROLLOVER_REQUESTED":
            store.advance(rid, state, "QUIESCING")
        elif state == "QUIESCING":
            manifest = snapshot.capture(repo, tmp_path / "snap")
            store.save_checkpoint(rid, _checkpoint(store, mid, repo, manifest))
        elif state == "CHECKPOINT_READY":
            store.advance(rid, state, "SUCCESSOR_PREPARED_READ_ONLY")
        elif state == "SUCCESSOR_PREPARED_READ_ONLY":
            assert store.transfer_ownership(rid, "new", 1) == 2
        elif state == "OWNERSHIP_TRANSFERRED":
            store.advance(rid, state, "SUCCESSOR_ACTIVE")
        elif state == "SUCCESSOR_ACTIVE":
            store.advance(rid, state, "PREDECESSOR_RETIRED")
    assert LocalStore(path).current_writer(mid) == (2, "new")
    assert LocalStore(path).get_checkpoint(rid)["mission_id"] == mid


def test_checkpoint_ready_requires_validated_checkpoint(tmp_path):
    store = LocalStore(tmp_path / "state.sqlite")
    mid = store.create_mission(_beacon(), "human:test")
    store.activate_initial(mid, "old", "codex")
    rid = store.request_rollover(mid, "context_pressure", 1)
    store.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    with pytest.raises(IllegalTransition):
        store.advance(rid, "QUIESCING", "CHECKPOINT_READY")
    assert LocalStore(store.path).rollover_state(rid) == "QUIESCING"
    assert LocalStore(store.path).get_checkpoint(rid) is None


def test_long_objective_does_not_hide_beacon_anchors():
    brief = beacon.render_brief({"mission_id": "m1", "version": 3, "hash": "abc123",
                                 "objective": "x" * 20000, "done_when": ["done"],
                                 "non_goals": ["no network"]})
    assert "x" * 20000 in brief
    for anchor in ("objective:", "version: 3", "hash: \"abc123\"", "done_when: [\"done\"]",
                   "non_goals: [\"no network\"]"):
        assert anchor in brief


def test_multiprocess_transfer_has_exactly_one_winner(tmp_path):
    path, mid, rid = _prepared(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    workers = [ctx.Process(target=_transfer_worker, args=(path, rid, f"new-{n}", start, results))
               for n in range(6)]
    for process in workers:
        process.start()
    start.set()
    observed = [results.get(timeout=30) for _ in workers]
    for process in workers:
        process.join(timeout=30)
        assert process.exitcode == 0
    winners = [entry for entry in observed if entry[0] == "winner"]
    assert len(winners) == 1, observed
    assert len([entry for entry in observed if entry[0] == "stale"]) == 5, observed
    assert LocalStore(path).current_writer(mid) == (2, winners[0][2])


def test_events_and_beacon_versions_are_append_only(tmp_path):
    path = tmp_path / "events.sqlite"
    store = LocalStore(path)
    mid = store.create_mission(_beacon(), "human:test")
    first = store.get_beacon(mid)
    store.change_goal(mid, {"objective": "finish safely"}, "human:test", "owner:approved")
    assert store.get_beacon(mid, 1)["hash"] == first["hash"]
    with store._read() as configured:
        assert configured.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT COUNT(*) FROM events WHERE mission_id=?", (mid,)).fetchone()[0] >= 2
        for statement in ("UPDATE events SET type='forged'", "DELETE FROM events",
                          "UPDATE beacons SET hash='forged'", "DELETE FROM beacons"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)


def test_grouped_events_are_written_together_or_not_at_all(tmp_path):
    store = LocalStore(tmp_path / "events.sqlite")
    mid = store.create_mission(_beacon(), "human:test")
    before = len(store.events(mid))
    with pytest.raises(ValueError):
        store.note_events(mid, [("runtime.blocked", {"reason": "x"}), ("forged.type", {})])
    assert len(store.events(mid)) == before
    store.note_events(mid, [("runtime.goal_boundary_red", {"reason": "x"}), ("runtime.blocked", {"reason": "x"})])
    assert [e["type"] for e in store.events(mid)[before:]] == ["runtime.goal_boundary_red", "runtime.blocked"]


def _columns(path, table):
    with closing(sqlite3.connect(path)) as db:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _unversioned(path):
    """Rewrite a ledger to an older, unversioned layout: rollovers had no successor_session_id."""
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE rollovers RENAME TO rollovers_current")
        db.execute("CREATE TABLE rollovers (id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, state TEXT NOT NULL, "
                   "epoch INTEGER NOT NULL, reason TEXT NOT NULL, checkpoint TEXT, checkpoint_hash TEXT)")
        db.execute("INSERT INTO rollovers SELECT id, mission_id, state, epoch, reason, checkpoint, checkpoint_hash "
                   "FROM rollovers_current")
        db.execute("DROP TABLE rollovers_current")
        db.execute("PRAGMA user_version=0")
        db.execute("COMMIT")


def test_new_ledger_carries_the_schema_version(tmp_path):
    store = LocalStore(tmp_path / "state.sqlite")
    assert SCHEMA_VERSION >= 1
    assert store.schema_version() == SCHEMA_VERSION


def test_unversioned_ledger_migrates_on_open_and_keeps_its_data(tmp_path):
    path = tmp_path / "state.sqlite"
    store = LocalStore(path)
    mid = store.create_mission(_beacon(), "human:test")
    store.activate_initial(mid, "old", "codex")
    rid = store.request_rollover(mid, "context_pressure", 1)
    events = store.events(mid)
    _unversioned(path)
    assert "successor_session_id" not in _columns(path, "rollovers")
    reopened = LocalStore(path)
    assert reopened.schema_version() == SCHEMA_VERSION
    assert "successor_session_id" in _columns(path, "rollovers")
    assert reopened.events(mid) == events
    assert reopened.rollover_state(rid) == "ROLLOVER_REQUESTED"
    assert reopened.current_writer(mid) == (1, "old")


def test_ledger_from_a_newer_release_is_refused_unchanged(tmp_path):
    path = tmp_path / "state.sqlite"
    store = LocalStore(path)
    mid = store.create_mission(_beacon(), "human:test")
    events = store.events(mid)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaTooNew):
        LocalStore(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    assert store.events(mid) == events


def _open_worker(path, start, results):
    start.wait()
    try:
        results.put(LocalStore(path).schema_version())
    except Exception as exc:
        results.put(repr(exc))


def test_concurrent_opens_migrate_an_unversioned_ledger_once(tmp_path):
    path = tmp_path / "state.sqlite"
    store = LocalStore(path)
    mid = store.create_mission(_beacon(), "human:test")
    events = store.events(mid)
    _unversioned(path)
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    results = ctx.Queue()
    workers = [ctx.Process(target=_open_worker, args=(path, start, results)) for _ in range(6)]
    for process in workers:
        process.start()
    start.set()
    observed = [results.get(timeout=30) for _ in workers]
    for process in workers:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert observed == [SCHEMA_VERSION] * len(workers), observed
    assert "successor_session_id" in _columns(path, "rollovers")
    assert LocalStore(path).events(mid) == events


def test_snapshot_deleted_tracked_and_paths_with_spaces(tmp_path):
    repo = _repo(tmp_path)
    (repo / "delete me.txt").unlink()
    (repo / "tracked file.txt").write_text("after\n")
    (repo / "untracked file.txt").write_text("new\n")
    index_before = (repo / ".git" / "index").read_bytes()
    head_before = _git(repo, "rev-parse", "HEAD")
    reflog_before = _git(repo, "reflog")
    manifest = snapshot.capture(repo, tmp_path / "saved snapshot")
    assert (repo / ".git" / "index").read_bytes() == index_before
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "reflog") == reflog_before
    assert {item["path"] for item in manifest["untracked"]} == {"untracked file.txt"}
    assert snapshot.verify(manifest, repo)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(repo), str(clone))
    snapshot.restore(manifest, clone)
    assert not (clone / "delete me.txt").exists()
    assert (clone / "tracked file.txt").read_text() == "after\n"
    assert (clone / "untracked file.txt").read_text() == "new\n"
    assert snapshot.verify(manifest, clone)

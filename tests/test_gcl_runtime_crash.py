"""Supervisor recovery at every durable handoff state: after SIGKILL or a lost transition race."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from gcl.core.store import LocalStore
from gcl.supervisor import Supervisor


ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fixtures/gcl/fake_codex.py"
CRASH = ROOT / "tests/fixtures/gcl/crash_supervisor.py"
STATES = ("ROLLOVER_REQUESTED", "QUIESCING", "CHECKPOINT_READY",
          "SUCCESSOR_PREPARED_READ_ONLY", "OWNERSHIP_TRANSFERRED",
          "SUCCESSOR_ACTIVE", "PREDECESSOR_RETIRED")
# The from-state of every generic store.advance the supervisor makes.
ADVANCED = ("ROLLOVER_REQUESTED", "CHECKPOINT_READY", "OWNERSHIP_TRANSFERRED", "SUCCESSOR_ACTIVE")


def until(predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("timed out")


def _mission(tmp_path):
    """A codex mission in a clean repository with one manual rollover requested."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "GCL"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "gcl@example.invalid"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work", "done_when": ["done"],
                                "non_goals": []}, "human:owner", "CONTROLLED_AUTO")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo), "endpoint": "own",
                                              "policy": {"red_ratio": .99, "min_work_s": 0}})
    store.note_event(mid, "runtime.manual_request", {"request_id": "once", "reason": "manual"})
    env = dict(os.environ, GCL_HOME=str(home), GCL_CODEX_BIN=str(FAKE),
               GCL_FAKE_STATE=str(tmp_path / "fake.json"), GCL_POLL_INTERVAL="0.05",
               PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    return home, store, mid, env


@pytest.mark.parametrize("state", STATES)
def test_sigkill_and_resume_each_state(tmp_path, state):
    home, store, mid, env = _mission(tmp_path)
    crashed = subprocess.run([sys.executable, "-B", str(CRASH), str(home), mid, state],
                             env=env, cwd=ROOT, capture_output=True, timeout=15)
    assert crashed.returncode == -9, (state, crashed.stderr)
    with store._read() as db:
        assert db.execute("SELECT state FROM rollovers WHERE mission_id=?", (mid,)).fetchone()[0] == state
    resumed = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", "resume", mid],
                             env=env, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert resumed.returncode == 0, resumed.stderr
    try:
        until(lambda: store.current_writer(mid)[0] == 2)
        until(lambda: store.rollover_state(next(e["data"]["id"] for e in store.events(mid)
              if e["type"] == "rollover.requested")) == "PREDECESSOR_RETIRED")
        with store._read() as db:
            assert db.execute("SELECT count(*) FROM rollovers WHERE mission_id=?", (mid,)).fetchone()[0] == 1
        fake = json.loads((tmp_path / "fake.json").read_text())
        assert len(fake["threads"]) == 2
        calls = fake["calls"]
        assert len([c for c in calls if c["method"] == "turn/start" and
                   c["params"].get("sandboxPolicy") == {"type": "readOnly"}]) == 1
        writes = [c["params"] for c in calls if c["method"] == "turn/start" and
                  c["params"].get("sandboxPolicy") != {"type": "readOnly"}]
        assert len(writes) == 2
        assert "sandboxPolicy" not in writes[0] and "approvalPolicy" not in writes[0]
        assert writes[1]["sandboxPolicy"] == {"type": "dangerFullAccess"}
        assert writes[1]["approvalPolicy"] == "on-request"
        assert store.current_writer(mid)[0] == 2
    finally:
        subprocess.run([sys.executable, "-B", "-m", "gcl.cli", "cancel", mid],
                       env=env, cwd=ROOT, capture_output=True, timeout=10, check=True)


@pytest.mark.parametrize("state", ["SUCCESSOR_PREPARED_READ_ONLY", "OWNERSHIP_TRANSFERRED"])
@pytest.mark.parametrize("receipt", ["wider", "missing"])
@pytest.mark.parametrize("unknown_delivery", [False, True])
def test_successor_policy_resume_rechecks_before_activation(tmp_path, state, receipt, unknown_delivery):
    home, store, mid, env = _mission(tmp_path)
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(tmp_path / "repo"),
        "endpoint": "own", "codex_sandbox": "workspace-write", "approval_policy": "never",
        "policy": {"red_ratio": .99, "min_work_s": 0}})
    crashed = subprocess.run([sys.executable, "-B", str(CRASH), str(home), mid, state],
                             env=env, cwd=ROOT, capture_output=True, timeout=15)
    assert crashed.returncode == -9, crashed.stderr
    row = next(e["data"] for e in store.events(mid) if e["type"] == "rollover.requested")
    before_writer = store.current_writer(mid)
    if unknown_delivery:
        # Seed a durable dispatch with no receipt at this crash boundary. No RPC is replayable.
        beacon = store.get_beacon(mid)
        with store._tx() as db:
            db.execute("INSERT INTO owner_outbox(id,mission_id,session_id,epoch,state,messages,prefix,kind,"
                       "through_seq,beacon_version,beacon_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       ("held", mid, before_writer[1], before_writer[0], "UNKNOWN", "[]", "synthetic",
                        "owner", 0, beacon["version"], beacon["hash"]))
        original_delivery = store.owner_delivery(mid)
    fake_before = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())
    env["GCL_FAKE_RESUMED_SUCCESSOR_POLICY"] = ("null" if receipt == "missing" else json.dumps({
        "sandbox": "danger-full-access", "approvalPolicy": "on-request"}))
    _cli(env, "resume", mid)
    try:
        reason = "successor_policy_unknown" if receipt == "missing" else "successor_policy_mismatch"
        def settled():
            events = store.events(mid)
            blocked = any(e["type"] == "runtime.policy_continuity_blocked" and
                          e["data"].get("rollover_id") == row["id"] and
                          e["data"].get("reason") == reason for e in events)
            recovery = any(e["type"] == "mission.recovery_required" and
                           e["data"].get("rollover_id") == row["id"] and
                           e["data"].get("reason") == reason for e in events)
            recovered = any(e["type"] == "mission.recovered" and
                            e["data"].get("rollover_id") == row["id"] for e in events)
            return events if (blocked and recovery) or recovered else None
        events = until(settled)
        blocked = [e["data"] for e in events if e["type"] == "runtime.policy_continuity_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["reason"] == reason
        assert any(e["type"] == "mission.recovery_required" and e["data"]["reason"] == reason for e in events)
        assert store.current_writer(mid) == before_writer
        assert store.rollover_state(row["id"]) == ("ABORTED" if state == "SUCCESSOR_PREPARED_READ_ONLY" else state)
        fake_after = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())
        new_calls = fake_after["calls"][len(fake_before["calls"]):]
        successor = list(fake_after["threads"])[1]
        assert not any(c["params"].get("threadId") == successor and (
            c["method"] == "turn/start" or c["method"] == "thread/goal/set" and c["params"]["status"] == "active")
            for c in new_calls)
        if unknown_delivery:
            assert store.owner_delivery(mid) == original_delivery
    finally:
        _cli(env, "cancel", mid)


@pytest.mark.parametrize("after_abort", [False, True])
def test_policy_hold_recovery_survives_crash_between_durable_events(tmp_path, monkeypatch, after_abort):
    home, store, mid, env = _mission(tmp_path)
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(tmp_path / "repo"),
        "endpoint": "own", "codex_sandbox": "workspace-write", "approval_policy": "never"})
    crashed = subprocess.run([sys.executable, "-B", str(CRASH), str(home), mid, "SUCCESSOR_PREPARED_READ_ONLY"],
                             env=env, cwd=ROOT, capture_output=True, timeout=15)
    assert crashed.returncode == -9
    for key in ("GCL_CODEX_BIN", "GCL_FAKE_STATE"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("GCL_FAKE_RESUMED_SUCCESSOR_POLICY", "null")
    controller = Supervisor(home, mid)
    controller.adapter.start()
    if after_abort:
        note_all = controller.note_all
        def crash_note(*events):
            if any(kind == "mission.recovery_required" for kind, _ in events):
                raise SystemExit("synthetic crash after abort")
            return note_all(*events)
        monkeypatch.setattr(controller, "note_all", crash_note)
    else:
        monkeypatch.setattr(controller, "_recover", lambda *args: (_ for _ in ()).throw(SystemExit("synthetic crash")))
    try:
        with pytest.raises(SystemExit):
            controller.tick()
    finally:
        controller.adapter.close()
    assert any(e["type"] == "runtime.policy_continuity_blocked" for e in store.events(mid))
    assert not any(e["type"] == "mission.recovery_required" for e in store.events(mid))
    resumed = Supervisor(home, mid)
    resumed.adapter.start()
    try:
        resumed.tick()
        resumed.tick()
    finally:
        resumed.adapter.close()
    assert store.current_writer(mid)[0] == 1
    blocked = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.policy_continuity_blocked")
    assert store.rollover_state(blocked["rollover_id"]) == "ABORTED"
    assert any(e["type"] == "mission.recovery_required" for e in store.events(mid))


@pytest.mark.parametrize("state", ADVANCED)
def test_supervisor_survives_losing_each_transition_race(tmp_path, monkeypatch, state):
    home, store, mid, env = _mission(tmp_path)
    for name in ("GCL_CODEX_BIN", "GCL_FAKE_STATE"):
        monkeypatch.setenv(name, env[name])
    controller = Supervisor(home, mid)
    rival, advance, raced = LocalStore(home / "state.sqlite"), controller.store.advance, []

    def contested(rid, expected_state, to_state, **data):
        # Another actor takes the same edge first, so the supervisor's compare-and-set loses.
        if expected_state == state and not raced:
            rival.advance(rid, expected_state, to_state, **data)
            raced.append(rid)
        return advance(rid, expected_state, to_state, **data)

    monkeypatch.setattr(controller.store, "advance", contested)
    controller.directory.mkdir(parents=True, exist_ok=True)
    controller.adapter.start()
    try:
        until(lambda: controller.tick() and raced and
              store.rollover_state(raced[0]) == "PREDECESSOR_RETIRED")
    finally:
        controller.adapter.close()
    assert store.current_writer(mid)[0] == 2
    lost = [e["data"] for e in store.events(mid) if e["type"] == "runtime.transition_lost"]
    assert len(lost) == 1 and lost[0]["rollover_id"] == raced[0]
    with store._read() as db:
        assert db.execute("SELECT count(*) FROM rollovers WHERE mission_id=?", (mid,)).fetchone()[0] == 1


def _quiescing(tmp_path):
    home, store, mid, env = _mission(tmp_path)
    result = subprocess.run([sys.executable, "-B", str(CRASH), str(home), mid, "QUIESCING"],
                            env=env, cwd=ROOT, capture_output=True, timeout=15)
    assert result.returncode == -9
    return store, mid, env


def _cli(env, *arguments):
    result = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", *arguments],
                            env=env, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_owner_message_held_during_quiescence_and_delivered_once(tmp_path):
    store, mid, env = _quiescing(tmp_path)
    assert _cli(env, "send", mid, "important owner update")["seq"] == 1
    first = store.current_writer(mid)
    assert store.consume_owner_messages(mid, first[1], first[0]) == []
    _cli(env, "resume", mid)
    try:
        until(lambda: store.current_writer(mid)[0] == 2)
        until(lambda: any(e["type"] == "runtime.turn_started" and e["data"].get("kind") == "successor"
                          for e in store.events(mid)))
        calls = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"]
        delivered = [c for c in calls if c["method"] == "turn/start" and
                     "important owner update" in c["params"]["input"][0]["text"]]
        assert len(delivered) == 1
        assert delivered[0]["params"]["threadId"] == store.current_writer(mid)[1]
        assert store.consume_owner_messages(mid, *reversed(store.current_writer(mid))) == []
    finally:
        _cli(env, "cancel", mid)


def test_cancel_during_quiescence_prevents_activation(tmp_path):
    store, mid, env = _quiescing(tmp_path)
    _cli(env, "cancel", mid)
    _cli(env, "resume", mid)
    until(lambda: _cli(env, "status", mid)["controller"].startswith("DEGRADED:"))
    assert store.current_writer(mid)[0] == 1
    assert not any(e["type"] == "writer.transferred" for e in store.events(mid))
    assert len(json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["threads"]) == 1

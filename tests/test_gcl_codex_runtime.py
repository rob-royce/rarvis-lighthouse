"""Offline app-server integration: no real Codex process or network."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from gcl.adapters.codex_appserver import CodexAppServer
from gcl.core.store import LocalStore
from gcl.supervisor import Supervisor


ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fixtures/gcl/fake_codex.py"


def command(env, *args):
    result = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", *args], cwd=ROOT,
                            env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)


def wait_for(test, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = test()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("condition did not become true")


def setup(tmp_path, monkeypatch, fail_successor=0):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "GCL"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "gcl@example.invalid"], cwd=repo, check=True)
    (repo / "README.md").write_text("baseline\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    home = tmp_path / "home"
    env = dict(os.environ, GCL_HOME=str(home), GCL_CODEX_BIN=str(FAKE),
               GCL_FAKE_STATE=str(tmp_path / "fake.json"), GCL_POLL_INTERVAL="0.05",
               GCL_FAKE_FAIL_SUCCESSOR=str(fail_successor), PYTHONDONTWRITEBYTECODE="1")
    return repo, home, env


def start(repo, env, *extra):
    return command(env, "start", "--executor", "codex", "--cwd", str(repo),
                   "--mode", "CONTROLLED_AUTO", "--policy", "red_ratio=0.01",
                   "--policy", "min_work_s=0", "--policy", "cooldown_s=0", *extra,
                   "--", "finish the task")["mission"]


def policy_rollover(tmp_path, monkeypatch, configure=None, **switches):
    repo, home, env = setup(tmp_path, monkeypatch)
    for name, value in dict(env, **switches).items():
        if name.startswith("GCL_"):
            monkeypatch.setenv(name, value)
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "synthetic work",
                                "done_when": ["done"], "non_goals": []},
                               "human:owner", "CONTROLLED_AUTO")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo),
        "endpoint": "own", "codex_sandbox": "workspace-write", "approval_policy": "never",
        "policy": {"red_ratio": .99, "min_work_s": 0}})
    store.note_event(mid, "runtime.manual_request", {"request_id": "once", "reason": "manual"})
    controller = Supervisor(home, mid)
    if configure:
        configure(controller)
    controller.directory.mkdir(parents=True, exist_ok=True)
    controller.adapter.start()
    try:
        controller.tick()
        predecessor = store.current_writer(mid)[1]
        controller.tick()
    finally:
        controller.adapter.close()
    fake = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())
    return store, mid, predecessor, fake


def assert_policy_hold(store, mid, predecessor, fake, reason, field=None):
    blocked = [e["data"] for e in store.events(mid) if e["type"] == "runtime.policy_continuity_blocked"]
    assert len(blocked) == 1
    event = blocked[0]
    assert event["reason"] == reason
    assert event["predecessor_session_id"] == predecessor and event["epoch"] == 1
    assert event["rollover_id"] and event["successor_session_id"]
    assert "predecessor_policy" in event and "successor_policy" in event and "requested_policy" in event
    if field:
        assert field in event["differing_fields"]
    assert store.current_writer(mid) == (1, predecessor)
    assert not any(e["type"] == "writer.transferred" for e in store.events(mid))
    verification = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.verification")
    assert verification["fresh_ok"] and verification["checkpoint"]["verdict"] == "TRUSTED"
    successor_calls = [c for c in fake["calls"] if c["params"].get("threadId") == event["successor_session_id"]]
    assert not any(c["method"] == "turn/start" and c["params"].get("sandboxPolicy") != {"type": "readOnly"}
                   or c["method"] == "thread/goal/set" and c["params"]["status"] == "active"
                   for c in successor_calls)


def test_successor_policy_change_does_not_transfer(tmp_path, monkeypatch):
    result = policy_rollover(tmp_path, monkeypatch, GCL_FAKE_SUCCESSOR_POLICY=json.dumps({
        "sandbox": "danger-full-access", "approvalPolicy": "on-request"}))
    assert_policy_hold(*result, "successor_policy_mismatch", "sandbox.type")


def test_successor_policy_missing_receipt_holds(tmp_path, monkeypatch):
    # A valid start receipt is recorded; the fresh resume omits both fields.
    result = policy_rollover(tmp_path, monkeypatch, GCL_FAKE_RESUMED_SUCCESSOR_POLICY="null")
    assert_policy_hold(*result, "successor_policy_unknown")


@pytest.mark.parametrize("echo", ["0", "1"])
@pytest.mark.parametrize("change,field", [
    ({"writableRoots": ["/synthetic/extra"]}, "sandbox.writable_roots"),
    ({"networkAccess": True}, "sandbox.network_access"),
    ({"excludeTmpdirEnvVar": True}, "sandbox.exclude_tmpdir_env_var"),
    ({"excludeSlashTmp": True}, "sandbox.exclude_slash_tmp"),
    ({"approvalPolicy": "on-request"}, "approval_policy"),
])
def test_successor_policy_structured_widening_holds(tmp_path, monkeypatch, change, field, echo):
    sandbox = {"type": "workspaceWrite", "writableRoots": [str(tmp_path / "repo")],
               "networkAccess": False, "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
    approval = change.get("approvalPolicy", "never")
    sandbox.update({k: v for k, v in change.items() if k != "approvalPolicy"})
    if field == "sandbox.writable_roots":
        sandbox["writableRoots"].append(str(tmp_path / "repo"))
    # With echo on, thread/resume reports the controller's read-only turn, as Codex does.
    result = policy_rollover(tmp_path, monkeypatch, GCL_FAKE_RESUME_ECHOES_TURN=echo,
                             GCL_FAKE_SUCCESSOR_POLICY=json.dumps({"sandbox": sandbox, "approvalPolicy": approval}))
    assert_policy_hold(*result, "successor_policy_mismatch", field)


def _structured_policy(tmp_path):
    home = str((tmp_path / "home").resolve())
    sandbox = {"type": "workspaceWrite", "writableRoots": [home, str(tmp_path / "repo")],
               "networkAccess": False, "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
    return json.dumps({"sandbox": sandbox, "approvalPolicy": "never"}), sandbox


@pytest.mark.parametrize("extra", [{}, {"access": {"type": "fullAccess"}}])
def test_controller_read_only_override_is_not_successor_policy(tmp_path, monkeypatch, extra):
    receipt, sandbox = _structured_policy(tmp_path)
    store, mid, predecessor, fake = policy_rollover(
        tmp_path, monkeypatch, GCL_FAKE_RESUME_ECHOES_TURN="1", GCL_FAKE_READONLY_ECHO_EXTRA=json.dumps(extra),
        GCL_FAKE_INITIAL_POLICY=receipt, GCL_FAKE_SUCCESSOR_POLICY=receipt)
    events = store.events(mid)
    assert not any(e["type"] == "runtime.policy_continuity_blocked" for e in events)
    assert store.current_writer(mid)[0] == 2
    assert any(e["type"] == "mission.recovered" for e in events)
    successor = store.current_writer(mid)[1]
    checked = [e["data"] for e in events if e["type"] == "runtime.policy_continuity_checked"]
    assert checked and all(c["controller_read_only_override"] and
                           c["successor_observed_policy"]["sandbox"]["type"] == "readOnly" and
                           c["predecessor_policy"] == c["successor_policy"] for c in checked)
    turns = [c["params"] for c in fake["calls"] if c["method"] == "turn/start" and c["params"]["threadId"] == successor]
    assert [t["sandboxPolicy"]["type"] for t in turns] == ["readOnly", "workspaceWrite"]
    writer = turns[1]
    assert writer["sandboxPolicy"] == dict(sandbox, writableRoots=sorted(sandbox["writableRoots"]))
    assert writer["approvalPolicy"] == "never"
    sent = [e["data"] for e in events if e["type"] == "runtime.turn_policy_sent" and e["data"]["session_id"] == successor]
    assert [s["read_only"] for s in sent] == [True, False]
    assert sent[1]["sandbox_policy"] == writer["sandboxPolicy"]
    assert sent[1]["approval_policy"] == writer["approvalPolicy"]


@pytest.mark.parametrize("echo", ["0", "1"])
def test_successor_policy_missing_thread_start_sandbox_holds(tmp_path, monkeypatch, echo):
    result = policy_rollover(tmp_path, monkeypatch, GCL_FAKE_RESUME_ECHOES_TURN=echo,
                             GCL_FAKE_SUCCESSOR_POLICY=json.dumps({"approvalPolicy": "never"}))
    assert_policy_hold(*result, "successor_policy_unknown")


def test_successor_resume_wider_than_controller_read_only_still_holds(tmp_path, monkeypatch):
    receipt, _ = _structured_policy(tmp_path)
    result = policy_rollover(tmp_path, monkeypatch, GCL_FAKE_INITIAL_POLICY=receipt,
                             GCL_FAKE_SUCCESSOR_POLICY=receipt,
                             GCL_FAKE_RESUMED_SUCCESSOR_POLICY=json.dumps(
                                 {"sandbox": {"type": "dangerFullAccess"}, "approvalPolicy": "never"}))
    assert_policy_hold(*result, "successor_policy_mismatch", "sandbox.type")


def test_read_only_turn_timeout_retries_one_new_successor(tmp_path, monkeypatch):
    receipt, _ = _structured_policy(tmp_path)
    store, mid, predecessor, fake = policy_rollover(
        tmp_path, monkeypatch, configure=lambda c: setattr(c, "read_only_turn_s", .5),
        GCL_FAKE_RESUME_ECHOES_TURN="1", GCL_FAKE_READONLY_STALLS="1",
        GCL_FAKE_INITIAL_POLICY=receipt, GCL_FAKE_SUCCESSOR_POLICY=receipt)
    events = store.events(mid)
    created = [e["data"] for e in events if e["type"] == "runtime.successor_created"]
    timeouts = [e["data"] for e in events if e["type"] == "runtime.read_only_turn_timeout"]
    assert [c["read_only_attempt"] for c in created] == [1, 2]
    assert len(timeouts) == 1 and timeouts[0]["session_id"] == created[0]["session_id"]
    assert timeouts[0]["attempt"] == 1 and timeouts[0]["limit_s"] == .5
    assert any(c["method"] == "turn/interrupt" and c["params"]["threadId"] == created[0]["session_id"]
               for c in fake["calls"])
    assert any(c["method"] == "thread/unsubscribe" and c["params"]["threadId"] == created[0]["session_id"]
               for c in fake["calls"])
    assert store.current_writer(mid) == (2, created[1]["session_id"])
    assert not any(e["type"] == "mission.recovery_required" for e in events)


def test_read_only_turn_timeout_twice_recovers(tmp_path, monkeypatch):
    receipt, _ = _structured_policy(tmp_path)
    store, mid, predecessor, fake = policy_rollover(
        tmp_path, monkeypatch, configure=lambda c: setattr(c, "read_only_turn_s", .5),
        GCL_FAKE_RESUME_ECHOES_TURN="1", GCL_FAKE_READONLY_STALLS="2",
        GCL_FAKE_INITIAL_POLICY=receipt, GCL_FAKE_SUCCESSOR_POLICY=receipt)
    events = store.events(mid)
    timeouts = [e["data"] for e in events if e["type"] == "runtime.read_only_turn_timeout"]
    assert [t["attempt"] for t in timeouts] == [1, 2]
    assert len({t["session_id"] for t in timeouts}) == 2
    recovery = [e["data"] for e in events if e["type"] == "mission.recovery_required"]
    assert [r["reason"] for r in recovery] == ["read_only_turn_timeout"]
    assert store.current_writer(mid) == (1, predecessor)
    assert store.rollover_state(recovery[0]["rollover_id"]) == "ABORTED"
    assert not any(c["method"] == "turn/start" and c["params"]["threadId"] != predecessor and
                   c["params"].get("sandboxPolicy") != {"type": "readOnly"} for c in fake["calls"])


@pytest.mark.parametrize("value,expected", [(None, 300), (45, 45), (5, 30), (10_000, 3600)])
def test_read_only_turn_limit_comes_from_runtime_policy(tmp_path, value, expected):
    store = LocalStore(tmp_path / "home" / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "synthetic work",
                                "done_when": ["done"], "non_goals": []}, "human:owner", "CONTROLLED_AUTO")
    runtime_policy = {} if value is None else {"read_only_turn_s": value}
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(tmp_path),
                                             "runtime_policy": runtime_policy})
    assert Supervisor(tmp_path / "home", mid).read_only_turn_s == expected


def test_successor_policy_normalized_equal_allows_transfer(tmp_path, monkeypatch):
    sandbox = {"type": "workspaceWrite", "writableRoots": [str(tmp_path / "home"), str(tmp_path / "repo")],
               "networkAccess": False, "excludeTmpdirEnvVar": False, "excludeSlashTmp": False}
    store, mid, predecessor, fake = policy_rollover(tmp_path, monkeypatch,
        GCL_FAKE_SUCCESSOR_POLICY=json.dumps({"sandbox": sandbox, "approvalPolicy": "never"}))
    events = store.events(mid)
    assert store.current_writer(mid)[0] == 2
    assert len([e for e in events if e["type"] == "writer.transferred"]) == 1
    checked = [e["data"] for e in events if e["type"] == "runtime.policy_continuity_checked"]
    assert checked and all(e["predecessor_policy"] == e["successor_policy"] == e["requested_policy"] for e in checked)
    assert not any(e["type"] == "runtime.policy_continuity_blocked" for e in events)


def test_successor_policy_rechecks_after_transfer_before_writable_turn(tmp_path, monkeypatch):
    receipts = [{"sandbox": "workspace-write", "approvalPolicy": "never"},
                {"sandbox": "danger-full-access", "approvalPolicy": "on-request"}]
    store, mid, predecessor, fake = policy_rollover(tmp_path, monkeypatch,
        GCL_FAKE_RESUMED_SUCCESSOR_POLICY=json.dumps(receipts))
    events = store.events(mid)
    assert store.current_writer(mid)[0] == 2
    assert len([e for e in events if e["type"] == "writer.transferred"]) == 1
    blocked = next(e["data"] for e in events if e["type"] == "runtime.policy_continuity_blocked")
    assert blocked["reason"] == "successor_policy_mismatch"
    assert store.rollover_state(blocked["rollover_id"]) == "OWNERSHIP_TRANSFERRED"
    assert any(e["type"] == "mission.recovery_required" for e in events)
    assert not any(c["params"].get("threadId") == blocked["successor_session_id"] and (
        c["method"] == "turn/start" and c["params"].get("sandboxPolicy") != {"type": "readOnly"} or
        c["method"] == "thread/goal/set" and c["params"]["status"] == "active") for c in fake["calls"])


def test_successor_policy_matching_predecessor_still_checks_explicit_restrictions(tmp_path, monkeypatch):
    broad = json.dumps({"sandbox": "danger-full-access", "approvalPolicy": "on-request"})
    result = policy_rollover(tmp_path, monkeypatch,
                            GCL_FAKE_INITIAL_POLICY=broad, GCL_FAKE_SUCCESSOR_POLICY=broad)
    assert_policy_hold(*result, "successor_policy_mismatch", "sandbox.type")
    event = next(e["data"] for e in result[0].events(result[1]) if e["type"] == "runtime.policy_continuity_blocked")
    assert event["predecessor_policy"] == event["successor_policy"]


@pytest.mark.parametrize("sandbox", [
    {"type": "workspaceWrite", "writableRoots": []},
    {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
     "excludeTmpdirEnvVar": False, "excludeSlashTmp": False, "futurePermission": "opaque-test-value"},
    {"type": "externalSandbox", "networkAccess": "restricted"},
])
def test_successor_policy_unknown_structured_fields_hold(tmp_path, monkeypatch, sandbox):
    result = policy_rollover(tmp_path, monkeypatch,
        GCL_FAKE_SUCCESSOR_POLICY=json.dumps({"sandbox": sandbox, "approvalPolicy": "never"}))
    assert_policy_hold(*result, "successor_policy_unknown")
    event = next(e["data"] for e in result[0].events(result[1]) if e["type"] == "runtime.policy_continuity_blocked")
    assert "opaque-test-value" not in json.dumps(event)


def test_two_rollovers_with_fresh_verified_checkpoints_and_retirement(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: len([r for r in store.events(mid) if r["type"] == "runtime.verification"]) >= 1)
        wait_for(lambda: len([r for r in store.events(mid) if r["type"] == "mission.recovered"]) >= 1)
        epoch, sid = store.current_writer(mid)
        assert epoch == 2
        evidence = repo / "proof.txt"
        evidence.write_text("work after first handoff\n")
        command(dict(env, GCL_EPOCH=str(epoch), GCL_SESSION_ID=sid), "evidence", mid,
                "--claim", "new evidence", "--path", str(evidence))
        wait_for(lambda: store.current_writer(mid)[0] == 3)
        wait_for(lambda: len([r for r in store.events(mid) if r["type"] == "mission.recovered"]) >= 2)
        with store._read() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM rollovers WHERE mission_id=? ORDER BY rowid", (mid,))]
        assert [r["state"] for r in rows] == ["PREDECESSOR_RETIRED"] * 2
        assert all(store.get_checkpoint(r["id"])["snapshot"] for r in rows)
        verifications = [e["data"] for e in store.events(mid) if e["type"] == "runtime.verification"]
        assert len(verifications) == 2
        assert all(v["fresh_ok"] and v["checkpoint"]["verdict"] == "TRUSTED" for v in verifications)
        status = command(env, "status", mid)
        assert status["sandbox"] == "danger-full-access"
        assert status["approval_policy"] == "on-request"
        config = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.config")
        assert config["codex_sandbox"] == "inherit"
        calls = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"]
        assert len([c for c in calls if c["method"] == "thread/unsubscribe"]) == 2
        assert len([c for c in calls if c["method"] == "thread/goal/set" and c["params"]["status"] == "paused"]) >= 2
        assert len([c for c in calls if c["method"] == "turn/start" and c["params"].get("sandboxPolicy") == {"type": "readOnly"}]) == 2
        turns = [c for c in calls if c["method"] == "turn/start"]
        assert len(turns) == 5
        assert "sandboxPolicy" not in turns[0]["params"]
        assert "approvalPolicy" not in turns[0]["params"]
        assert [c["params"].get("sandboxPolicy") for c in turns[1:]] == [
            {"type": "readOnly"}, {"type": "dangerFullAccess"},
            {"type": "readOnly"}, {"type": "dangerFullAccess"}]
        assert all("approvalPolicy" not in c["params"] for c in (turns[1], turns[3]))
        assert all(c["params"]["approvalPolicy"] == "on-request" for c in (turns[2], turns[4]))
        fake = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())
        assert fake.get("blocked_auto_turns", []) == []
        for successor in list(fake["threads"])[1:]:
            scoped = [(i, c) for i, c in enumerate(calls) if c["params"].get("threadId") == successor]
            verify_index = next(i for i, c in scoped if c["method"] == "turn/start" and
                                c["params"]["sandboxPolicy"]["type"] == "readOnly")
            write_index = next(i for i, c in scoped if c["method"] == "turn/start" and
                               c["params"]["sandboxPolicy"]["type"] == "dangerFullAccess")
            goal_index = next(i for i, c in scoped if c["method"] == "thread/goal/set" and
                              c["params"]["status"] == "active")
            assert verify_index < write_index < goal_index
            assert all(a["sandboxPolicy"]["type"] == "dangerFullAccess" for a in fake.get("auto_turns", [])
                       if a["threadId"] == successor)
        briefs = [c["params"]["input"][0]["text"] for c in turns
                  if c["params"].get("sandboxPolicy", {}).get("type") != "readOnly"]
        for current_epoch, brief in enumerate(briefs, 1):
            sid = next(c["params"]["threadId"] for c in turns if c["params"]["input"][0]["text"] == brief)
            for verb in ("note", "evidence", "effect", "job"):
                assert f"{ROOT / 'bin/rlh'} {verb} {mid} --session {sid} --epoch {current_epoch}" in brief
            assert f"RLH_HOME={home}" in brief
        assert {e["data"]["trigger"] for e in store.events(mid) if e["type"] == "runtime.rollover_trigger"} == {"MONITOR"}
    finally:
        command(env, "cancel", mid)


def test_explicit_codex_sandbox_and_approval_are_recorded(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    mid = command(env, "start", "--executor", "codex", "--cwd", str(repo),
                  "--mode", "OBSERVE_ONLY", "--codex-sandbox", "workspace-write",
                  "--approval-policy", "never", "--", "observe")["mission"]
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: store.current_writer(mid)[0] == 1)
        status = command(env, "status", mid)
        assert status["sandbox"] == "workspace-write"
        assert status["approval_policy"] == "never"
        config = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.config")
        assert config["codex_sandbox"] == "workspace-write"
        calls = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"]
        start_call = next(c for c in calls if c["method"] == "thread/start")
        assert start_call["params"]["sandbox"] == "workspace-write"
        assert start_call["params"]["approvalPolicy"] == "never"
        turn = next(c for c in calls if c["method"] == "turn/start")
        assert turn["params"]["sandboxPolicy"] == {
            "type": "workspaceWrite", "writableRoots": [str(repo), str(home)]}
        assert "approvalPolicy" not in turn["params"]
    finally:
        command(env, "cancel", mid)


def test_resume_restores_recorded_policy_when_response_omits_it(tmp_path, monkeypatch):
    adapter = CodexAppServer()
    adapter.recorded_policies["thread"] = {"sandbox": "danger-full-access",
                                            "approval_policy": "on-request"}
    adapter._remember({"thread": {"id": "thread", "cwd": str(tmp_path)}}, resumed=True)
    calls = []
    monkeypatch.setattr(adapter, "call", lambda method, params: calls.append((method, params)) or
                        {"turn": {"id": "turn"}})
    adapter.start_turn("thread", "continue")
    assert calls[0][0] == "turn/start"
    assert calls[0][1]["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert calls[0][1]["approvalPolicy"] == "on-request"


def test_successor_start_failure_blocks_and_keeps_predecessor(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch, fail_successor=2)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "mission.recovery_required" for e in store.events(mid)))
        assert store.current_writer(mid)[0] == 1
        with store._read() as db:
            assert db.execute("SELECT state FROM rollovers WHERE mission_id=?", (mid,)).fetchone()[0] == "ABORTED"
        assert command(env, "status", mid)["runtime_status"] == "RECOVERY_REQUIRED"
        time.sleep(.2)
        assert json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["starts"] == 3
        calls = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"]
        assert not any(c["method"] == "thread/unsubscribe" for c in calls)
        assert any(c["method"] == "thread/goal/set" and c["params"]["status"] == "active"
                   for c in calls)
    finally:
        command(env, "cancel", mid)


def test_manual_trigger_and_observe_only(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    mid = command(env, "start", "--executor", "codex", "--cwd", str(repo),
                  "--mode", "OBSERVE_ONLY", "--policy", "red_ratio=0.01", "--", "observe")["mission"]
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "runtime.monitor_decision" for e in store.events(mid)))
        assert store.current_writer(mid)[0] == 1
        assert not any(e["type"] == "rollover.requested" for e in store.events(mid))
        command(env, "mode", mid, "CONTROLLED_AUTO")
        command(env, "rollover", mid, "--reason", "manual")
        wait_for(lambda: any(e["type"] == "runtime.rollover_trigger" and e["data"]["trigger"] == "MANUAL"
                             for e in store.events(mid)))
        lines = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", "tail", mid], cwd=ROOT,
                               env=env, text=True, capture_output=True, check=True).stdout
        assert "[epoch " in lines and " · session " in lines
    finally:
        command(env, "cancel", mid)


def test_environment_errors_block_without_rollover(tmp_path, monkeypatch):
    for index, (error, kind) in enumerate((("rate limit 429", "rate_limit"),
                                           ("network timeout", "network"),
                                           ("authentication 401", "auth"),
                                           ("quota exceeded", "quota"))):
        repo, home, env = setup(tmp_path / str(index), monkeypatch)
        env["GCL_FAKE_TURN_ERROR"] = error
        mid = start(repo, env)
        store = LocalStore(home / "state.sqlite")
        try:
            wait_for(lambda: any(e["type"] == "runtime.blocked" for e in store.events(mid)))
            assert store.current_writer(mid)[0] == 1
            assert not any(e["type"] == "rollover.requested" for e in store.events(mid))
            assert any(e["type"] == "runtime.monitor_decision" and e["data"]["action"] == "BLOCK"
                       and "environment_blocker:" + kind in e["data"]["reasons"] for e in store.events(mid))
        finally:
            command(env, "cancel", mid)


def test_rejected_checkpoint_never_transfers(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    env["GCL_FAKE_MUTATE_READONLY"] = "1"
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "mission.recovery_required" for e in store.events(mid)))
        assert store.current_writer(mid)[0] == 1
        verification = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.verification")
        assert verification["checkpoint"]["verdict"] == "REJECTED"
        assert not any(e["type"] == "writer.transferred" for e in store.events(mid))
    finally:
        command(env, "cancel", mid)


def test_off_leaves_worker_and_status_degrades(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    mid = command(env, "start", "--executor", "codex", "--cwd", str(repo),
                  "--mode", "OBSERVE_ONLY", "--", "observe")["mission"]
    store = LocalStore(home / "state.sqlite")
    wait_for(lambda: store.current_writer(mid)[0] == 1)
    fake_pid = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["server_pid"]
    command(env, "mode", mid, "OFF")
    try:
        wait_for(lambda: command(env, "status", mid)["controller"].startswith("DEGRADED:"))
        os.kill(fake_pid, 0)
        assert store.current_writer(mid)[0] == 1
    finally:
        os.kill(fake_pid, signal.SIGTERM)


def test_status_never_protected_with_stale_telemetry(tmp_path, monkeypatch):
    from gcl.cli import _status

    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "observe"}, "human:owner")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(tmp_path)})
    store.note_event(mid, "runtime.telemetry", {"at": time.time() - 120})
    monkeypatch.setattr("gcl.cli.live_controller", lambda directory: (True, "fresh"))
    assert _status(store, home, mid)["controller"] == "DEGRADED:telemetry_stale"


def test_monitor_waits_for_turn_completion_below_hard_ratio(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    env["GCL_FAKE_TURN_DELAY"] = "30"
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "runtime.monitor_decision" and
                             "relay_pending_chunk_boundary" in e["data"]["reasons"] for e in store.events(mid)))
        time.sleep(.2)
        assert not any(e["type"] == "runtime.rollover_trigger" for e in store.events(mid))
        assert not any(c["method"] == "turn/interrupt" for c in json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"])
        (tmp_path / "release-turn").write_text("release")
        wait_for(lambda: any(e["type"] == "runtime.rollover_trigger" for e in store.events(mid)))
        rid = next(e["data"]["rollover_id"] for e in store.events(mid)
                   if e["type"] == "runtime.rollover_trigger")
        assert not any(c["method"] == "turn/interrupt" for c in json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"])
        wait_for(lambda: store.rollover_state(rid) == "PREDECESSOR_RETIRED")
        assert next(e["data"]["cause"] for e in store.events(mid) if e["type"] == "runtime.quiescence") == "turn_completed"
    finally:
        command(env, "cancel", mid)


def test_chunk_relay_interrupts_long_turn_only_at_clean_commit(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    env.update(GCL_FAKE_TURN_DELAY="30", GCL_FAKE_WORK_INTERVAL="1",
               GCL_FAKE_WORK_FINAL_DELAY="30",
               GCL_FAKE_WORK_STEPS=json.dumps(["commit_dirty", "commit_dirty", "commit", "commit"]))
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    def fake():
        path = Path(env["GCL_FAKE_STATE"])
        return json.loads(path.read_text()) if path.exists() else {}
    try:
        wait_for(lambda: len(fake().get("work_steps_done", [])) >= 1)
        wait_for(lambda: any(e["type"] == "runtime.rollover_trigger" for e in store.events(mid)))
        rid = next(e["data"]["rollover_id"] for e in store.events(mid)
                   if e["type"] == "runtime.rollover_trigger")
        assert store.rollover_state(rid) in ("ROLLOVER_REQUESTED", "QUIESCING")
        assert not any(c["method"] == "turn/interrupt" for c in fake()["calls"])
        assert not any(e["type"] == "mission.recovery_required" for e in store.events(mid))
        wait_for(lambda: len(fake().get("work_steps_done", [])) >= 3)
        wait_for(lambda: store.rollover_state(rid) == "PREDECESSOR_RETIRED")
        commits = fake()["work_commits"]
        assert [c["dirty"] for c in commits] == [True, True, False]
        assert [c["interrupts"] for c in commits] == [0, 0, 0]
        interrupts = [c for c in fake()["calls"] if c["method"] == "turn/interrupt"]
        assert len(interrupts) == 1
        assert fake()["work_steps_cancelled"][interrupts[0]["params"]["threadId"]] == 1
        assert not any(e["type"] == "mission.recovery_required" for e in store.events(mid))
        checkpoint = store.get_checkpoint(rid)
        assert checkpoint["repo"]["head"] == commits[2]["head"]
        assert checkpoint["repo"]["head"] == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            check=True).stdout.strip()
        time.sleep(.2)
        assert fake()["work_commits"] == commits
        assert checkpoint["repo"]["head"] == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            check=True).stdout.strip()
        assert checkpoint["snapshot"]["untracked"] == []
        assert not Path(checkpoint["snapshot"]["diff_path"]).read_bytes()
    finally:
        command(env, "cancel", mid)


def chunk_scenario(tmp_path, adapter):
    repo, home, _ = setup(tmp_path, None)
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo),
                                              "runtime_policy": {"chunk_wait_s": 5}})
    store.activate_initial(mid, "thread", "codex")
    store.note_event(mid, "runtime.session_created", {"epoch": 1, "session_id": "thread"})
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout.strip()
    store.note_event(mid, "runtime.base_head", {"path": str(repo), "head": head, "branch": branch})
    store.note_event(mid, "runtime.worktree_seen", {"path": str(repo), "head": head,
                                                     "branch": branch, "session_id": "thread"})
    rid = store.request_rollover(mid, "relay_chunk", 1)
    store.note_event(mid, "runtime.rollover_trigger", {"rollover_id": rid, "trigger": "MONITOR",
                                                       "urgency": "chunk_boundary", "at": time.time(),
                                                       "head": head, "boundary_head": head, "branch": branch})
    return repo, store, mid, rid, Supervisor(home, mid, adapter)


class FailingChunkAdapter:
    def __init__(self):
        self.interrupts = []
        self.tokens = 90

    def resume_thread(self, _):
        pass

    def signals(self, _):
        return {"active_turn": "long", "context_tokens": self.tokens, "context_window": 100}

    def quiesce(self, _, interrupt):
        self.interrupts.append(interrupt)
        return False

    def set_goal(self, *_):
        pass


def test_chunk_quiesce_short_wait_exhausts_without_recovery(tmp_path):
    adapter = FailingChunkAdapter()
    _, store, mid, rid, supervisor = chunk_scenario(tmp_path, adapter)
    supervisor.tick()
    assert store.rollover_state(rid) == "QUIESCING"
    assert adapter.interrupts == [True]
    assert not any(e["type"] == "mission.recovery_required" for e in store.events(mid))
    supervisor.chunk_wait_s = .01
    supervisor.tick()
    assert store.rollover_state(rid) == "ABORTED"
    assert any(e["type"] == "runtime.chunk_wait_exhausted" for e in store.events(mid))
    assert not any(e["type"] == "mission.recovery_required" for e in store.events(mid))


def test_chunk_hard_relay_quiesce_failure_requires_recovery(tmp_path):
    adapter = FailingChunkAdapter()
    _, store, mid, rid, supervisor = chunk_scenario(tmp_path, adapter)
    supervisor.tick()
    adapter.tokens = 96
    supervisor.tick()
    assert store.rollover_state(rid) == "ABORTED"
    assert adapter.interrupts == [True, True]
    assert any(e["type"] == "runtime.quiescence" and e["data"]["cause"] == "relay_hard"
               for e in store.events(mid))
    assert any(e["type"] == "mission.recovery_required" and
               e["data"]["reason"] == "quiescence_unproven" for e in store.events(mid))
    assert not any(e["type"] == "runtime.chunk_wait_exhausted" for e in store.events(mid))


def test_chunk_cancelled_after_quiesce_does_not_save_checkpoint(tmp_path, monkeypatch):
    cancelled = False

    class CancellingAdapter(FailingChunkAdapter):
        def quiesce(self, session_id, interrupt):
            nonlocal cancelled
            self.interrupts.append(interrupt)
            cancelled = True
            return True

    adapter = CancellingAdapter()
    repo, store, mid, rid, supervisor = chunk_scenario(tmp_path, adapter)
    monkeypatch.setattr(supervisor, "_cancelled", lambda: cancelled)
    (repo / "README.md").write_text("clean boundary\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "clean boundary"], cwd=repo, check=True)
    supervisor.tick()
    assert adapter.interrupts == [True]
    assert store.rollover_state(rid) == "ABORTED"
    assert any(e["type"] == "rollover.aborted" and e["data"] == {"id": rid, "reason": "cancelled"}
               for e in store.events(mid))
    assert store.get_checkpoint(rid) is None
    assert not any(e["type"] == "checkpoint.saved" for e in store.events(mid))


def test_chunk_retries_if_turn_changes_worktree_during_interrupt(tmp_path):
    repo, home, env = setup(tmp_path, None)
    env.update(GCL_FAKE_TURN_DELAY="30", GCL_FAKE_WORK_INTERVAL=".5",
               GCL_FAKE_WORK_STEPS=json.dumps(["commit"]), GCL_FAKE_MUTATE_ON_INTERRUPT="1")
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "runtime.chunk_boundary_moved" for e in store.events(mid)))
        rid = next(e["data"]["rollover_id"] for e in store.events(mid)
                   if e["type"] == "runtime.rollover_trigger")
        assert store.rollover_state(rid) == "QUIESCING"
        assert not any(e["type"] == "mission.recovery_required" for e in store.events(mid))
        subprocess.run(["git", "add", "late.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "next clean boundary"], cwd=repo, check=True)
        wait_for(lambda: store.rollover_state(rid) == "PREDECESSOR_RETIRED")
        assert store.get_checkpoint(rid)["repo"]["head"] == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
            check=True).stdout.strip()
    finally:
        command(env, "cancel", mid)


def test_monitor_interrupt_records_hard_ratio_or_turn_timeout(tmp_path, monkeypatch):
    for index, policy in enumerate(("hard_ratio=0.8", "max_turn_s=0.1")):
        repo, home, env = setup(tmp_path / str(index), monkeypatch)
        env["GCL_FAKE_TURN_DELAY"] = "30"
        mid = start(repo, env, "--policy", policy)
        store = LocalStore(home / "state.sqlite")
        try:
            wait_for(lambda: any(e["type"] == "runtime.quiescence" for e in store.events(mid)))
            cause = next(e["data"]["cause"] for e in store.events(mid) if e["type"] == "runtime.quiescence")
            assert cause == ("hard_ratio" if index == 0 else "max_turn_s")
            wait_for(lambda: any(c["method"] == "turn/interrupt" for c in json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"]))
        finally:
            command(env, "cancel", mid)


def test_status_block_cancel_and_repo_progress_since_checkpoint(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "mission.recovered" for e in store.events(mid)))
        wait_for(lambda: command(env, "status", mid)["runtime_status"] == "BLOCKED")
        assert "no_evidence_since_last_rollover" in command(env, "status", mid)["reason"]
        supervisor = Supervisor(home, mid, adapter=object())
        previous = supervisor._rollovers()[-1]
        epoch, sid = store.current_writer(mid)
        assert not supervisor._progress_since_checkpoint(previous, epoch)
        (repo / "README.md").write_text("changed after checkpoint\n")
        assert supervisor._progress_since_checkpoint(previous, epoch)
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "progress"], cwd=repo, check=True)
        assert supervisor._progress_since_checkpoint(previous, epoch)
        (repo / "new-proof.txt").write_text("untracked progress\n")
        assert supervisor._progress_since_checkpoint(previous, epoch)
        store.note_event(mid, "runtime.note", {"epoch": epoch, "text": "verified observation"})
        supervisor.refresh()
        assert supervisor._progress_since_checkpoint(previous, epoch)
        command(env, "cancel", mid)
        assert command(env, "status", mid)["runtime_status"] == "CANCELLED"
    finally:
        command(env, "cancel", mid)


def test_worker_brief_commands_are_runnable_and_fenced(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    mid = command(env, "start", "--executor", "codex", "--cwd", str(repo),
                  "--mode", "OBSERVE_ONLY", "--", "work")["mission"]
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: Path(env["GCL_FAKE_STATE"]).exists() and
                 any(c["method"] == "turn/start" for c in json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"]))
        sid = store.current_writer(mid)[1]
        brief = next(c["params"]["input"][0]["text"] for c in
                     json.loads(Path(env["GCL_FAKE_STATE"]).read_text())["calls"] if c["method"] == "turn/start")
        (repo / "proof.txt").write_text("verified\n")
        (repo / "job.log").write_text("job started\n")
        for line in brief.splitlines():
            if f"--session {sid}" in line and any(f" {verb} " in line for verb in
                                                      ("note", "evidence", "effect", "job")):
                good = subprocess.run(line, shell=True, cwd=repo, env=env, capture_output=True, text=True)
                assert good.returncode == 0, (line, good.stdout, good.stderr)
        stale = subprocess.run([str(ROOT / "bin/gcl"), "note", mid, "--session", "wrong", "--epoch", "1", "bad"],
                               env=env, capture_output=True, text=True)
        assert stale.returncode == 2 and "stale worker epoch" in stale.stdout
    finally:
        command(env, "cancel", mid)


def test_restarted_writer_turn_uses_gate_checked_policy_not_cached_read_only(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "home" / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "synthetic work",
                                "done_when": ["done"], "non_goals": []}, "human:owner", "CONTROLLED_AUTO")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(tmp_path)})
    checked = {"type": "workspaceWrite", "writable_roots": ["/synthetic/home", "/synthetic/repo"],
               "network_access": False, "exclude_tmpdir_env_var": False, "exclude_slash_tmp": False}
    store.note_event(mid, "runtime.policy_continuity_checked", {
        "successor_session_id": "t", "successor_policy": {"sandbox": checked, "approval_policy": "never"}})
    adapter = Supervisor(tmp_path / "home", mid).adapter
    # A resume after the read-only turn caches the override, as Codex reports it.
    adapter._remember({"thread": {"id": "t", "cwd": "/synthetic/repo"},
                       "sandbox": {"type": "readOnly"}, "approvalPolicy": "never"}, resumed=True)
    calls = []
    monkeypatch.setattr(adapter, "call", lambda method, params: calls.append(params) or {"turn": {"id": "x"}})
    adapter.start_turn("t", "continue")
    assert calls[0]["sandboxPolicy"] == {"type": "workspaceWrite",
                                         "writableRoots": ["/synthetic/home", "/synthetic/repo"],
                                         "networkAccess": False, "excludeTmpdirEnvVar": False,
                                         "excludeSlashTmp": False}
    assert calls[0]["approvalPolicy"] == "never"
    sent = [e["data"] for e in store.events(mid) if e["type"] == "runtime.turn_policy_sent"]
    assert sent == [dict(sent[0], session_id="t", read_only=False, sandbox_policy=calls[0]["sandboxPolicy"],
                         approval_policy="never")]

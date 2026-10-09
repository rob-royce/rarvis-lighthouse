"""Claude background integration against a local executable stub only."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from gcl.core.store import LocalStore
from gcl.hooks import handle, transcript_occupancy
from gcl.supervisor import Supervisor
from gcl.adapters.claude_bg import ClaudeBackground
from gcl.adapters.codex_appserver import AppServerError
from gcl.runtime.files import read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests/fixtures/gcl/fake_claude.py"


@pytest.fixture(autouse=True)
def never_invoke_real_claude(monkeypatch):
    monkeypatch.setenv("GCL_CLAUDE_BIN", str(FAKE))


def cli(env, *args, ok=True):
    result = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", *args], cwd=ROOT,
                            env=env, text=True, capture_output=True, timeout=15)
    if ok:
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)
    return result


def until(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("timed out")


def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "GCL"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "gcl@example.invalid"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"projects": {str(repo): {"hasTrustDialogAccepted": True}}}))
    home = tmp_path / "home"
    state = tmp_path / "fake.json"
    settings = tmp_path / "claude-settings.json"
    settings.write_text("{}")
    env = dict(os.environ, GCL_HOME=str(home), GCL_CLAUDE_BIN=str(FAKE),
               GCL_CLAUDE_SETTINGS=str(settings),
               GCL_CLAUDE_PROJECTS_DIR=str(tmp_path / "projects"),
               GCL_CLAUDE_TRUST_FILE=str(trust), GCL_FAKE_CLAUDE_STATE=str(state),
               GCL_HOOK_PYTHON=sys.executable, GCL_POLL_INTERVAL="0.05",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    return repo, home, state, env


def start(repo, env, *extra):
    return cli(env, "start", "--executor", "claude", "--cwd", str(repo),
               "--mode", "CONTROLLED_AUTO", "--done-when", "proof exists", *extra,
               "--", "finish the task")["mission"]


def fake(state):
    return json.loads(state.read_text())


def set_agent_state(state_path, index, **updates):
    with open(str(state_path) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        body = fake(state_path)
        body["agents"][index].update(**updates)
        replacement = state_path.with_suffix(".working")
        replacement.write_text(json.dumps(body))
        os.replace(replacement, state_path)


def hook(env, agent, event, **overrides):
    payload = {"session_id": agent["sessionId"], "transcript_path": agent["transcript_path"],
               "cwd": agent["cwd"], **overrides}
    settings = json.loads(Path(agent["settings"]).read_text())
    command = settings["hooks"][event][0]["hooks"][0]["command"]
    result = subprocess.run(command, shell=True, input=json.dumps(payload), text=True,
                            capture_output=True, env=dict(env, **settings.get("env", {})), timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bin_and_generated_hook_resist_cwd_package_shadowing(tmp_path):
    decoy = tmp_path / "other-checkout"
    package = decoy / "gcl"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("raise RuntimeError('cwd decoy imported')\n")
    (package / "hooks.py").write_text("raise RuntimeError('cwd decoy hook imported')\n")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(decoy),
               GCL_HOME=str(tmp_path / "unused-home"))
    for name in ("rlh", "gcl"):
        result = subprocess.run([str(ROOT / "bin" / name), "--help"], cwd=decoy, env=env,
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "usage: rlh" in result.stdout
    mission_root = tmp_path / "mission"
    mission_root.mkdir()
    repo, home, state, mission_env = setup(mission_root)
    mid = start(repo, mission_env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        agent = fake(state)["agents"][0]
        settings = json.loads(Path(agent["settings"]).read_text())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert " -P " in command
        payload = {"session_id": agent["sessionId"], "transcript_path": agent["transcript_path"],
                   "cwd": str(repo), "tool_name": "Read"}
        hook_env = dict(mission_env, PYTHONPATH=str(decoy))
        result = subprocess.run(command, shell=True, cwd=decoy, env=hook_env,
                                input=json.dumps(payload), text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {}
    finally:
        cli(mission_env, "cancel", mid)


def test_two_rollovers_receipts_retirement_and_attach(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env, "--permission-mode", "acceptEdits", "--allowed-tools", "Read",
                "--allowed-tools", "Bash")
    store = LocalStore(home / "state.sqlite")
    attached_process = None
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        attached_process = subprocess.Popen([sys.executable, "-B", "-m", "gcl.cli", "attach", mid,
                                             "--follow"], cwd=ROOT, env=env,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        until(lambda: ["attach", "bg-1"] in fake(state)["calls"])
        for epoch in (2, 3):
            cli(env, "rollover", mid, "--reason", "manual")
            until(lambda: store.current_writer(mid)[0] == epoch)
            until(lambda: len([e for e in store.events(mid) if e["type"] == "mission.recovered"]) == epoch - 1)
            until(lambda: ["attach", f"bg-{epoch}"] in fake(state)["calls"])
        with store._read() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM rollovers WHERE mission_id=? ORDER BY rowid", (mid,))]
        assert [r["state"] for r in rows] == ["PREDECESSOR_RETIRED"] * 2
        verifications = [e["data"] for e in store.events(mid) if e["type"] == "runtime.verification"]
        assert len(verifications) == 2
        assert all(v["fresh_ok"] and v["checkpoint"]["verdict"] == "TRUSTED" for v in verifications)
        assert all(v["fresh_receipt"]["requested_session_id"] == v["fresh_receipt"]["session_id"]
                   and v["fresh_receipt"]["hook_source"] == "startup"
                   and v["fresh_receipt"]["transcript_preexisting"] is False for v in verifications)
        events = store.events(mid)
        for rid in (r["id"] for r in rows):
            observed = next(e["id"] for e in events if e["type"] == "runtime.worker_verified"
                            and e["data"]["rollover_id"] == rid)
            checked = next(e["id"] for e in events if e["type"] == "runtime.verification"
                           and e["data"]["rollover_id"] == rid)
            transferred = next(e["id"] for e in events if e["type"] == "writer.transferred"
                               and e["data"]["id"] == rid)
            assert observed < checked < transferred
        calls = fake(state)["calls"]
        assert len([c for c in calls if c[0] == "stop"]) == 2
        for sid in ("session-2", "session-3"):
            launch = next(p.parent for p in (home / "missions" / mid / "sessions").glob("*/resolved.json")
                          if read_json(p).get("session_id") == sid)
            result = until(lambda: read_json(launch / "successor_result.json"))
            assert "WRITER GRANTED" in result["await"]
            assert result["exit_code"] == 0
            assert "Continued in same turn" in Path(fake(state)["agents"][int(sid[-1]) - 1]["transcript_path"]).read_text()
        assert not any(set(c) & {"--resume", "--continue", "--fork-session"} for c in calls)
        assert not any(c[0] == "rm" for c in calls)
        assert all('objective: "finish the task"' in a["prompt"] for a in fake(state)["agents"])
        assert all("--permission-mode" in c and "--allowedTools=Read,Bash" in c and
                   c[-2] == "--" for c in calls
                   if c and c[0] == "--bg" and "--session-id" in c)
        assert cli(env, "status", mid)["permission_mode"] == "acceptEdits"
        attached = cli(env, "attach", mid)
        assert attached["session_id"] == "session-3" and "attach bg-3" in attached["instruction"]
        subprocess.run([env["GCL_CLAUDE_BIN"], "attach", "bg-3"],
                       env=env, cwd=repo, check=True)
    finally:
        cli(env, "cancel", mid)
        if attached_process:
            attached_process.wait(timeout=15)


def test_default_permission_mode_inherits_without_flag(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env, "--runtime-policy", "quiesce_proof_s=0.5")
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        calls = [c for c in fake(state)["calls"] if c and c[0] == "--bg"]
        assert calls and all("--permission-mode" not in c for c in calls)
        assert cli(env, "status", mid)["permission_mode"] == "inherit"
        config = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.config")
        assert config["permission_mode"] == "inherit"
        assert config["runtime_policy"]["quiesce_proof_s"] == 0.5
    finally:
        cli(env, "cancel", mid)


def test_cancel_idle_stops_worker_and_records_receipt(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    until(lambda: store.current_writer(mid)[0] == 1)
    result = cli(env, "cancel", mid)
    receipt = result["worker_stop"]
    assert receipt["command"] == ["claude", "stop", "bg-1"]
    assert receipt["exit_code"] == 0
    assert receipt["state"] == "stopped"
    assert ["stop", "bg-1"] in fake(state)["calls"]
    assert not any(c[0] == "rm" for c in fake(state)["calls"])
    status = cli(env, "status", mid)
    assert status["runtime_status"] == "CANCELLED"
    assert status["worker_stop"] == receipt
    assert cli(env, "cancel", mid)["worker_stop"] == receipt
    assert [c for c in fake(state)["calls"] if c[0] == "stop"] == [["stop", "bg-1"]]


@pytest.mark.parametrize("command", [("cancel",), ("mode", "OFF", "--stop-worker")])
def test_absent_claude_worker_records_receipt(tmp_path, command):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    until(lambda: store.current_writer(mid)[0] == 1)
    with open(str(state) + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        body = fake(state)
        body["agents"] = []
        state.write_text(json.dumps(body))
    result = cli(env, command[0], mid, *command[1:])
    assert result["worker_stop"]["state"] == "absent"
    assert cli(env, "status", mid)["worker_stop"] == result["worker_stop"]
    assert not any(c[0] == "stop" for c in fake(state)["calls"])
    if command[0] == "cancel":
        assert cli(env, "status", mid)["runtime_status"] == "CANCELLED"


def test_cancel_working_defers_until_stop_hook(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    until(lambda: store.current_writer(mid)[0] == 1)
    set_agent_state(state, 0, state="working", status="busy")
    result = cli(env, "cancel", mid)
    assert result["worker_stop"]["state"] == "pending"
    assert not any(c[0] == "stop" for c in fake(state)["calls"])
    assert cli(env, "status", mid)["runtime_status"] == "CANCELLED"
    assert cli(env, "status", mid)["worker_stop"]["bg_id"] == "bg-1"
    assert hook(env, fake(state)["agents"][0], "Stop") == {}
    receipt = cli(env, "status", mid)["worker_stop"]
    assert receipt["state"] == "stopped" and receipt["exit_code"] == 0
    assert [c for c in fake(state)["calls"] if c[0] == "stop"] == [["stop", "bg-1"]]
    assert not any(c[0] == "rm" for c in fake(state)["calls"])


def test_mode_off_stops_worker_only_with_explicit_flag(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    until(lambda: store.current_writer(mid)[0] == 1)
    assert cli(env, "mode", mid, "OFF")["worker_stop"] is None
    assert not any(c[0] == "stop" for c in fake(state)["calls"])
    result = cli(env, "mode", mid, "OFF", "--stop-worker")
    assert result["worker_stop"]["command"] == ["claude", "stop", "bg-1"]
    assert cli(env, "status", mid)["worker_stop"]["state"] == "stopped"


def test_hook_fence_compact_and_fail_open(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        first = fake(state)["agents"][0]
        compact = hook(env, first, "SessionStart", source="compact")
        brief = compact["hookSpecificOutput"]["additionalContext"]
        for anchor in ("objective:", "done_when:", "version:", "hash:"):
            assert anchor in brief
        cli(env, "rollover", mid, "--reason", "manual")
        until(lambda: store.current_writer(mid)[0] == 2)
        second = fake(state)["agents"][1]
        for tool in ("Bash", "Write", "Edit"):
            assert hook(env, first, "PreToolUse", tool_name=tool)["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert hook(env, second, "PreToolUse", tool_name="Read") == {}
        # A malformed transcript is an internal Stop error, never a session denial.
        assert hook(env, second, "Stop", transcript_path=str(repo / "missing")) == {}
        assert any(e["type"] == "runtime.hook_error" for e in store.events(mid))
        missing_home = tmp_path / "broken-home"
        failure = subprocess.run([sys.executable, "-B", "-m", "gcl.hooks", "claude", "PreToolUse",
                                  "--home", str(missing_home), "--mission", mid, "--session", "missing",
                                  "--epoch", "1"], cwd=ROOT, env=env, input=json.dumps({"session_id": "x",
                                  "tool_name": "Read"}), text=True, capture_output=True, check=True)
        assert json.loads(failure.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    finally:
        cli(env, "cancel", mid)


def test_read_only_successor_and_owner_delivery(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        first = fake(state)["agents"][0]
        observations = sum(c == ["agents", "--json"] for c in fake(state)["calls"])
        set_agent_state(state, 0, state="working", status="busy")
        until(lambda: sum(c == ["agents", "--json"] for c in fake(state)["calls"]) > observations)
        cli(env, "send", mid, "while working")
        assert "while working" in hook(env, first, "Stop")["reason"]
        assert hook(env, first, "Stop") == {}
        set_agent_state(state, 0, state="blocked", status="waiting")
        cli(env, "send", mid, "while idle")
        pending_status = until(lambda: cli(env, "status", mid)
                               if cli(env, "status", mid)["pending_owner_messages"] == 1 else None)
        assert pending_status["pending_owner_message_details"][0]["text"] == "while idle"
        assert not any("--resume" in c for c in fake(state)["calls"])
        set_agent_state(state, 0, state="working", status="busy")
        # Stage a durable quiescent handoff and verify the store withholds new input.
        # The live supervisor owns the transition; advancing it here too would race it.
        rid = store.request_rollover(mid, "manual", 1)
        until(lambda: store.rollover_state(rid) == "QUIESCING")
        cli(env, "send", mid, "during quiescence")
        assert hook(env, first, "Stop") == {}
        assert store.consume_owner_messages(mid, "session-1", 1) == []
        set_agent_state(state, 0, state="blocked", status="waiting")
        until(lambda: store.current_writer(mid)[0] == 2)
        successor = next(p.parent for p in (home / "missions" / mid / "sessions").glob("*/resolved.json")
                         if read_json(p).get("session_id") == "session-2")
        result = until(lambda: read_json(successor / "successor_result.json"))
        assert "during quiescence" in result["await"]
        assert "while idle" in result["await"]
        assert not any("--resume" in c for c in fake(state)["calls"])
    finally:
        cli(env, "cancel", mid)


def test_read_only_successor_fence_before_transfer(tmp_path):
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work", "done_when": ["done"]},
                               "human:owner", "CONTROLLED_AUTO")
    store.activate_initial(mid, "old", "claude")
    rid = store.request_rollover(mid, "manual", 1)
    store.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    path = home / "missions" / mid / "sessions" / "prepared"
    path.mkdir(parents=True)
    (path / "launch.json").write_text(json.dumps({"epoch": 2, "read_only": True}))
    for sid, epoch in (("old", 1), ("new", 2)):
        store.note_event(mid, "runtime.session_capability", {"session_id": sid, "epoch": epoch, "sha256": "test"})
    for tool in ("Read", "Grep", "Glob"):
        assert handle("PreToolUse", {"session_id": "new", "tool_name": tool}, home, mid, "prepared", 2) == {}
    for tool in ("Bash", "Write", "Edit", "NotebookEdit", "mcp__server__write"):
        assert handle("PreToolUse", {"session_id": "new", "tool_name": tool}, home, mid, "prepared", 2)["hookSpecificOutput"]["permissionDecision"] == "deny"
    prefix = f"GCL_HOME={home} {ROOT / 'bin/gcl'} "
    fence = f"{mid} --session new --epoch 2"
    for command in (f"{prefix}verified {fence} --summary 'checked workspace'",
                    f"{prefix}await-writer {fence}", f"{prefix}note {fence} 'observation'"):
        assert handle("PreToolUse", {"session_id": "new", "tool_name": "Bash",
                      "tool_input": {"command": command}}, home, mid, "prepared", 2) == {}
    for command in (f"{prefix}evidence {fence} --claim proof --path /proof.txt",
                    f"{prefix}verified {fence} --summary 'ok'; touch /bad",
                    f"{prefix}done {fence}"):
        assert handle("PreToolUse", {"session_id": "new", "tool_name": "Bash",
                      "tool_input": {"command": command}}, home, mid, "prepared", 2)[
                          "hookSpecificOutput"]["permissionDecision"] == "deny"
    assert handle("PreToolUse", {"session_id": "old", "tool_name": "Bash"}, home, mid,
                  "prepared", 1)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stop_now_worker_commands_and_read_only_successor(tmp_path):
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.activate_initial(mid, "old", "claude")
    rid = store.request_rollover(mid, "manual", 1)
    store.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
    path = home / "missions" / mid / "sessions" / "prepared"
    path.mkdir(parents=True)
    (path / "launch.json").write_text(json.dumps({"epoch": 2, "read_only": True}))
    for sid, epoch in (("old", 1), ("new", 2)):
        store.note_event(mid, "runtime.session_capability", {"session_id": sid, "epoch": epoch, "sha256": "test"})
    executable = ROOT / "bin/gcl"
    prefix = f"GCL_HOME={home} {executable} "
    note = f"{prefix}note {mid} --session old --epoch 1 'handoff summary and next step'"
    evidence = f"{prefix}evidence {mid} --session old --epoch 1 --claim proof --path /proof.txt"
    def decision(session, epoch, command, launch="prepared"):
        return handle("PreToolUse", {"session_id": session, "tool_name": "Bash",
                "tool_input": {"command": command}}, home, mid, launch, epoch)
    assert decision("old", 1, note) == {}
    assert decision("old", 1, note.replace("handoff summary and next step", "handoff; next step")) == {}
    assert decision("old", 1, evidence) == {}
    assert decision("old", 1, f"{prefix}help {mid} --session old --epoch 1") == {}
    assert decision("old", 1, f"{prefix}--help") == {}
    assert decision("new", 2, note.replace("old --epoch 1", "new --epoch 2")) == {}
    assert handle("PreToolUse", {"session_id": "old", "tool_name": "Read"},
                  home, mid, "prepared", 1) == {}
    for command in ("ls", note + "; touch /bad", note[:-len("'handoff summary and next step'")] +
                    "summary;touch /bad", note.replace(mid, "other"),
                    note.replace("--epoch 1", "--epoch 2"),
                    note.replace("'handoff summary and next step'", "$(touch /bad)"),
                    note.replace("'handoff summary and next step'", "summary>/bad"),
                    f"{prefix}effect {mid} --session old --epoch 1 begin bad"):
        denied = decision("old", 1, command)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert note.replace("GCL_HOME=", "RLH_HOME=").replace("bin/gcl", "bin/rlh") in denied["hookSpecificOutput"]["permissionDecisionReason"]


def test_claude_quiesce_uses_hooks_and_quiet_transcript_not_agent_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.activate_initial(mid, "old", "claude")
    store.request_rollover(mid, "manual", 1)
    store.note_event(mid, "runtime.pre_tool_use", {"session_id": "old", "epoch": 1})
    store.note_event(mid, "runtime.turn_stop", {"session_id": "old", "epoch": 1})
    directory = home / "missions" / mid
    launch = directory / "sessions" / "launch"
    launch.mkdir(parents=True)
    (launch / "resolved.json").write_text(json.dumps({"session_id": "old", "bg_id": "bg-1"}))
    transcript = tmp_path / "transcript.jsonl"
    final = {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}}
    transcript.write_text(json.dumps(final) + "\n")
    (launch / "latest_hook.json").write_text(json.dumps({"transcript_path": str(transcript)}))
    adapter = ClaudeBackground({"runtime_policy": {"quiesce_proof_s": 0,
                                "quiesce_quiet_s": .05}}, directory)
    monkeypatch.setattr(adapter, "_agent", lambda _: {"state": "working", "status": "busy"})
    assert adapter.quiesce("old", interrupt=True) is False
    time.sleep(.06)
    assert adapter.quiesce("old", interrupt=True) is True
    assert any(e["type"] == "runtime.claude_agent_advisory" and e["data"]["state"] == "working"
               for e in store.events(mid))
    transcript.write_text(json.dumps(final) + "\n" + json.dumps(final) + "\n")
    assert adapter.quiesce("old", interrupt=True) is False  # growing transcript resets quiet period
    time.sleep(.06)
    assert adapter.quiesce("old", interrupt=True) is True
    store.note_event(mid, "runtime.pre_tool_use", {"session_id": "old", "epoch": 1})
    assert adapter.quiesce("old", interrupt=True) is False
    store.note_event(mid, "runtime.turn_stop", {"session_id": "old", "epoch": 1})
    transcript.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "id": "pending"}], "stop_reason": "tool_use"}}) + "\n")
    assert adapter.quiesce("old", interrupt=True) is False
    transcript.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "id": "pending"}], "stop_reason": "tool_use"}}) +
        "\n" + json.dumps(final) + "\n")
    assert adapter.quiesce("old", interrupt=True) is False


@pytest.mark.parametrize("fixture,extra,expected", [
    ("claude_tail_completed.jsonl", None, True),
    ("claude_tail_unresolved_tool.jsonl", None, False),
    ("claude_tail_completed.jsonl", {"type": "user", "message": {"role": "user",
         "content": [{"type": "text", "text": "continue"}]}}, False),
    ("claude_tail_completed.jsonl", {"type": "user", "message": {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "toolu_late"}]}}, False),
    ("claude_tail_completed.jsonl", {"type": "assistant", "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "sidechain"}]}}, True),
])
def test_claude_quiesce_corroborates_real_transcript_tail(tmp_path, monkeypatch, fixture, extra, expected):
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.activate_initial(mid, "old", "claude")
    store.request_rollover(mid, "manual", 1)
    store.note_event(mid, "runtime.pre_tool_use", {"session_id": "old", "epoch": 1})
    store.note_event(mid, "runtime.turn_stop", {"session_id": "old", "epoch": 1})
    directory = home / "missions" / mid
    launch = directory / "sessions" / "launch"
    launch.mkdir(parents=True)
    (launch / "resolved.json").write_text(json.dumps({"session_id": "old", "bg_id": "bg-1"}))
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text((ROOT / "tests/fixtures/gcl" / fixture).read_text() +
                          (json.dumps(extra) + "\n" if extra else ""))
    (launch / "latest_hook.json").write_text(json.dumps({"transcript_path": str(transcript)}))
    adapter = ClaudeBackground({"runtime_policy": {"quiesce_proof_s": 0,
                                "quiesce_quiet_s": 0}}, directory)
    monkeypatch.setattr(adapter, "_agent", lambda _: {"state": "working", "status": "busy"})
    assert adapter.quiesce("old", interrupt=True) is expected


def test_claude_unproven_quiescence_retries_without_new_stop_until_deadline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.note_event(mid, "runtime.config", {"executor": "claude", "cwd": str(repo), "policy": {},
                                             "runtime_policy": {"quiesce_max_s": .1}})
    store.activate_initial(mid, "old", "claude")
    rid = store.request_rollover(mid, "manual", 1)
    class Unproven:
        calls = 0
        def resume_thread(self, _):
            pass
        def quiesce(self, _, interrupt):
            self.calls += 1
            return False
        def set_goal(self, *_):
            pass
    adapter = Unproven()
    supervisor = Supervisor(home, mid, adapter)
    store.note_event(mid, "runtime.turn_stop", {"session_id": "old", "epoch": 1})
    supervisor.tick()
    assert adapter.calls == 1 and store.rollover_state(rid) == "QUIESCING"
    supervisor.tick()
    assert adapter.calls == 2 and store.rollover_state(rid) == "QUIESCING"
    time.sleep(.11)
    supervisor.tick()
    assert adapter.calls == 3
    assert store.rollover_state(rid) == "ABORTED"
    assert any(e["type"] == "mission.recovery_required" and
               e["data"]["reason"] == "quiescence_unproven" for e in store.events(mid))


def test_transcript_occupancy_dedup_and_model_window(tmp_path):
    path = tmp_path / "transcript.jsonl"
    entries = [
        {"type": "assistant", "requestId": "r1", "isSidechain": True, "message": {"model": "opus[1m]", "usage": {"input_tokens": 999}}},
        {"type": "assistant", "requestId": "r1", "message": {"model": "opus[1m]", "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30}}},
        {"type": "assistant", "requestId": "r1", "message": {"model": "opus[1m]", "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30}}},
        {"type": "assistant", "requestId": "r2", "message": {"model": "opus", "usage": {"input_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 9}}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    assert transcript_occupancy(str(path)) == {"context_tokens": 21, "context_window": 200_000,
                                               "quality": "ESTIMATED", "model": "opus", "request_id": "r2"}
    path.write_text("\n".join(json.dumps(e) for e in entries[:-1]) + "\n")
    assert transcript_occupancy(str(path))["context_window"] == 1_000_000
    assert transcript_occupancy(str(path))["context_tokens"] == 60


def test_stop_transcript_telemetry_is_reported_as_estimated(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        agent = fake(state)["agents"][0]
        Path(agent["transcript_path"]).write_text(json.dumps({"type": "assistant", "requestId": "r1",
            "message": {"model": "opus[1m]", "usage": {"input_tokens": 10,
            "cache_read_input_tokens": 20, "cache_creation_input_tokens": 30}}}) + "\n")
        hook(env, agent, "Stop")
        monitor = until(lambda: (cli(env, "status", mid)["monitor"] or {}).get("metrics", {})
                        .get("occupancy_quality") == "ESTIMATED")
        assert monitor is True
        status = cli(env, "status", mid)
        assert status["monitor"]["metrics"]["occupancy"] == 60 / 1_000_000
    finally:
        cli(env, "cancel", mid)


def test_untrusted_start_creates_no_session(tmp_path):
    repo, home, state, env = setup(tmp_path)
    Path(env["GCL_CLAUDE_TRUST_FILE"]).write_text('{"projects": {}}')
    result = cli(env, "start", "--executor", "claude", "--cwd", str(repo), "--", "work", ok=False)
    assert result.returncode != 0 and "not trusted" in result.stdout
    assert not state.exists() and not (home / "state.sqlite").exists()


def test_claude_done_and_runtime_policy(tmp_path, monkeypatch):
    repo, home, state, env = setup(tmp_path)
    monkeypatch.setenv("GCL_CLAUDE_BIN", str(FAKE))
    monkeypatch.setenv("GCL_FAKE_CLAUDE_STATE", str(state))
    mid = start(repo, env, "--runtime-policy", "hard_ratio=0.91,max_turn_s=12")
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        config = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.config")
        assert config["runtime_policy"] == {"hard_ratio": .91, "max_turn_s": 12.0}
        supervisor = Supervisor(home, mid)
        assert supervisor.hard_ratio == .91 and supervisor.max_turn_s == 12
        set_agent_state(state, 0, state="working", status="busy")
        assert isinstance(supervisor.adapter.signals("session-1")["turn_started_at"], float)
        session_env = dict(env, **json.loads(Path(fake(state)["agents"][0]["settings"]).read_text())["env"])
        reported = cli(session_env, "done", mid, "--session", "session-1", "--epoch", "1")
        assert reported["completion"].startswith("reported")
        # The agent's word is a report: the mission waits for the owner (OWNER-DECISIONS D5).
        until(lambda: cli(env, "status", mid)["runtime_status"] == "AWAITING_CONFIRMATION")
        refused = cli(session_env, "confirm", mid, ok=False)
        assert refused.returncode == 2 and "only the owner" in refused.stdout
        assert cli(env, "confirm", mid) == {"confirmed": mid}
        until(lambda: cli(env, "status", mid)["runtime_status"] == "COMPLETED")
        assert supervisor.tick() is False
        completed = next(e["data"] for e in store.events(mid) if e["type"] == "mission.completed")
        assert completed["confirmed_by"] == "owner"
    finally:
        # Completion terminates monitoring; no cancellation is needed.
        pass


def test_worktree_hook_path_drives_checkpoint_and_successor(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        worktree = repo / ".claude" / "worktrees" / "roman"
        worktree.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", "-qb", "worktree-roman", str(worktree)],
                       cwd=repo, check=True)
        agent = fake(state)["agents"][0]
        moved_transcript = tmp_path / "moved.jsonl"
        moved_transcript.write_text("")
        hook(env, agent, "PreToolUse", cwd=str(worktree), transcript_path=str(moved_transcript),
             tool_name="Read")
        (worktree / "proof.txt").write_text("worktree proof\n")
        session_env = dict(env, **json.loads(Path(agent["settings"]).read_text())["env"])
        subprocess.run([sys.executable, "-B", "-m", "gcl.cli", "evidence", mid,
                        "--session", agent["sessionId"], "--epoch", "1", "--claim", "proof",
                        "--path", "proof.txt"], cwd=worktree, env=session_env, check=True, capture_output=True)
        cli(env, "rollover", mid, "--reason", "manual")
        moved_transcript.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "Ready to hand off."}],
            "stop_reason": "end_turn"}}) + "\n")
        hook(env, agent, "Stop", cwd=str(worktree), transcript_path=str(moved_transcript))
        until(lambda: store.current_writer(mid)[0] == 2)
        rid = next(e["data"]["rollover_id"] for e in store.events(mid)
                   if e["type"] == "runtime.rollover_trigger")
        checkpoint = store.get_checkpoint(rid)
        assert checkpoint["repo"]["path"] == str(worktree)
        assert checkpoint["repo"]["branch"] == "worktree-roman"
        assert checkpoint["snapshot"]["base_head"] == checkpoint["repo"]["head"]
        assert checkpoint["verified_results"][-1]["cwd"] == str(worktree)
        assert str(worktree) in fake(state)["agents"][1]["prompt"]
        assert "worktree-roman" in fake(state)["agents"][1]["prompt"]
    finally:
        cli(env, "cancel", mid)


def test_foreign_commit_worktree_blocks_transfer(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        worktree = repo / ".claude" / "worktrees" / "foreign"
        worktree.parent.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", "-qb", "worktree-foreign", str(worktree)],
                       cwd=repo, check=True)
        (worktree / "foreign.txt").write_text("another mission\n")
        subprocess.run(["git", "add", "foreign.txt"], cwd=worktree, check=True)
        subprocess.run(["git", "commit", "-qm", "foreign"], cwd=worktree, check=True)
        agent = fake(state)["agents"][0]
        hook(env, agent, "PreToolUse", cwd=str(worktree), tool_name="Read")
        until(lambda: any(e["type"] == "runtime.goal_boundary_red" for e in store.events(mid)))
        assert store.current_writer(mid)[0] == 1
        assert not any(e["type"] == "writer.transferred" for e in store.events(mid))
        assert cli(env, "status", mid)["runtime_status"] == "BLOCKED"
    finally:
        cli(env, "cancel", mid)


def test_latest_transcript_is_polled_with_fixture_settings_window(tmp_path):
    repo, home, state, env = setup(tmp_path)
    Path(env["GCL_CLAUDE_SETTINGS"]).write_text(json.dumps({"model": "opus[1m]"}))
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        agent = fake(state)["agents"][0]
        moved = tmp_path / "moved.jsonl"
        moved.write_text(json.dumps({"type": "assistant", "requestId": "new",
            "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 100,
            "cache_read_input_tokens": 200, "cache_creation_input_tokens": 300}}}) + "\n")
        hook(env, agent, "PreToolUse", transcript_path=str(moved), tool_name="Read")
        until(lambda: (cli(env, "status", mid)["monitor"] or {}).get("metrics", {}).get("occupancy") == .0006)
        telemetry = json.loads(next((home / "missions" / mid / "sessions").glob("*/telemetry.json")).read_text())
        assert telemetry["context_window"] == 1_000_000
        assert telemetry["context_tokens"] == 600
        assert telemetry["quality"] == "ESTIMATED"
    finally:
        cli(env, "cancel", mid)


def test_start_accepts_relay_policy_flags(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env, "--policy", "relay_soft_tokens=40000,relay_hard_tokens=60000")
    try:
        store = LocalStore(home / "state.sqlite")
        config = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.config")
        assert config["policy"]["relay_soft_tokens"] == 40000
        assert config["policy"]["relay_hard_tokens"] == 60000
    finally:
        cli(env, "cancel", mid)


def test_adapter_rejects_second_turn_without_launch(tmp_path, monkeypatch):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        until(lambda: any(e["type"] == "runtime.turn_started" for e in store.events(mid)))
        monkeypatch.setenv("GCL_FAKE_CLAUDE_STATE", str(state))
        adapter = ClaudeBackground({"cwd": str(repo)}, home / "missions" / mid)
        with pytest.raises(AppServerError, match="single-launch"):
            adapter.start_turn("session-1", "next turn")
        calls = fake(state)["calls"]
        assert not any(set(call) & {"--resume", "--continue", "--fork-session"} for call in calls)
    finally:
        cli(env, "cancel", mid)


def test_fake_claude_running_resume_creates_hookless_fork(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        agent = fake(state)["agents"][0]
        set_agent_state(state, 0, state="working", status="busy")
        result = subprocess.run([str(FAKE), "--bg", "--resume", agent["sessionId"],
                                 "--settings", agent["settings"], "--", "unsafe turn"],
                                cwd=repo, env=env, text=True, capture_output=True, check=True)
        assert "backgrounded · bg-2" in result.stdout
        fork = fake(state)["agents"][1]
        assert fork["sessionId"] != agent["sessionId"]
        assert fork["source"] == "fork" and fork["settings"] is None
        subprocess.run([str(FAKE), "stop", agent["id"]], cwd=repo, env=env, check=True)
        subprocess.run([str(FAKE), "--bg", "--resume", agent["sessionId"],
                        "--settings", agent["settings"], "--", "unsafe turn"],
                       cwd=repo, env=env, check=True, capture_output=True)
        stopped_fork = fake(state)["agents"][2]
        assert stopped_fork["sessionId"] != agent["sessionId"]
        assert stopped_fork["source"] == "fork" and stopped_fork["settings"] is None
    finally:
        cli(env, "cancel", mid)


@pytest.mark.parametrize("command", ["note", "evidence", "effect", "job", "done"])
def test_worker_requires_matching_session_capability(tmp_path, command):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        agent = fake(state)["agents"][0]
        token = read_json(Path(agent["settings"]))["env"]["GCL_SESSION_TOKEN"]
        record = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.session_capability")
        assert record["sha256"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in json.dumps(store.events(mid))
        arguments = {"note": ["progress"], "evidence": ["--claim", "proof", "--path", str(repo / "README.md")],
                     "effect": ["begin", "key"], "job": ["register", "--id", "job", "--cmd", "true",
                                 "--log", str(repo / "job.log")], "done": []}[command]
        for provided in (None, "wrong-token"):
            trial_env = dict(env)
            if provided:
                trial_env["GCL_SESSION_TOKEN"] = provided
            else:
                trial_env.pop("GCL_SESSION_TOKEN", None)
            result = cli(trial_env, command, mid, "--session", agent["sessionId"], "--epoch", "1",
                         *arguments, ok=False)
            assert result.returncode != 0
        assert len([e for e in store.events(mid) if e["type"] == "runtime.impersonation_rejected"]) == 2
        if command == "note":
            cli(dict(env, GCL_SESSION_TOKEN=token), command, mid,
                "--session", agent["sessionId"], "--epoch", "1", *arguments)
    finally:
        cli(env, "cancel", mid)


@pytest.mark.parametrize("in_worktree", [False, True])
def test_unregistered_project_transcript_blocks_rollover(tmp_path, in_worktree):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        observed = repo
        if in_worktree:
            observed = Path("/short/recorded-worktree")
            store.note_event(mid, "runtime.worktree_seen",
                             {"session_id": "session-1", "path": str(observed),
                              "head": "recorded", "branch": "recorded"})
        projects = Path(env["GCL_CLAUDE_PROJECTS_DIR"])
        project = projects / re.sub(r"[^A-Za-z0-9]", "-", str(observed))
        project.mkdir(parents=True)
        unknown = project / "unmanaged-session.jsonl"
        unknown.write_text('{}\n')
        cli(env, "rollover", mid, "--reason", "manual")
        until(lambda: any(e["type"] == "runtime.unmanaged_writer_detected" for e in store.events(mid)))
        # One transaction: once detection is visible, the red boundary and block are too, adjacent.
        events = store.events(mid)
        start_at = next(i for i, e in enumerate(events) if e["type"] == "runtime.unmanaged_writer_detected")
        assert [e["type"] for e in events[start_at:start_at + 3]] == [
            "runtime.unmanaged_writer_detected", "runtime.goal_boundary_red", "runtime.blocked"]
        assert events[start_at + 1]["data"].get("reason") == "unmanaged_writer_detected"
        assert store.current_writer(mid) == (1, "session-1")
        assert not any(e["type"] == "writer.transferred" for e in store.events(mid))
        assert cli(env, "status", mid)["runtime_status"] == "BLOCKED"
    finally:
        cli(env, "cancel", mid)


def test_wait_turn_accepts_stop_after_twenty_seconds_while_agent_working(tmp_path, monkeypatch):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        monkeypatch.setenv("GCL_FAKE_CLAUDE_STATE", str(state))
        adapter = ClaudeBackground({"cwd": str(repo)}, home / "missions" / mid)
        agent = fake(state)["agents"][0]
        path = adapter._path(agent["sessionId"])
        write_json(path / "turn.json", {"id": "later-turn", "started_at": time.time(),
                                        "started_monotonic": time.monotonic()})
        set_agent_state(state, 0, state="working", status="busy")
        timer = threading.Timer(20, lambda: hook(env, fake(state)["agents"][0], "Stop"))
        timer.start()
        try:
            assert adapter.wait_turn(agent["sessionId"], "later-turn", 25)
        finally:
            timer.join(timeout=5)
        assert adapter._agent(agent["sessionId"])["state"] == "working"
        assert any(e["type"] == "runtime.turn_stop" and e["data"].get("turn_id") == "later-turn"
                   for e in store.events(mid))
    finally:
        cli(env, "cancel", mid)


def test_waiting_input_status_and_mid_turn_stop_now_handoff(tmp_path):
    repo, home, state, env = setup(tmp_path)
    mid = start(repo, env)
    store = LocalStore(home / "state.sqlite")
    try:
        until(lambda: store.current_writer(mid)[0] == 1)
        first = fake(state)["agents"][0]
        set_agent_state(state, 0, state="blocked", status="waiting", waitingFor="permission prompt")
        status = cli(env, "status", mid)
        assert status["runtime_status"] == "WAITING_INPUT"
        assert status["waitingFor"] == "permission prompt"
        assert status["controller"] != "PROTECTED"
        set_agent_state(state, 0, state="working", status="busy", waitingFor=None)
        cli(env, "rollover", mid, "--reason", "manual")
        until(lambda: any(e["type"] == "runtime.rollover_trigger" for e in store.events(mid)))
        denied = hook(env, first, "PreToolUse", tool_name="Bash")
        reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "stop tool use" in reason and "rlh note" in reason and "end this turn" in reason
        hook(env, first, "Stop")
        set_agent_state(state, 0, state="blocked", status="waiting")
        until(lambda: store.current_writer(mid)[0] == 2)
    finally:
        cli(env, "cancel", mid)

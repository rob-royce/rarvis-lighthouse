"""Permission ceilings must survive launches and controller reconstruction."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gcl.adapters.claude_bg import ClaudeBackground, resolve_rollover_permissions
from gcl.core.store import LocalStore
from gcl.hooks import _authorize_pretool
from gcl.supervisor import Supervisor
from gcl.runtime.files import read_json, write_json


def boundary(mode="default", tools=None):
    return {"permission_mode": mode, "allowed_tools": tools}


def adapter(tmp_path, config):
    obj = ClaudeBackground({"cwd": str(tmp_path), **config}, tmp_path / "missions" / "m")
    obj._run = Mock(return_value=SimpleNamespace(stdout="backgrounded · bg1 ·"))
    obj._agents = Mock(return_value=[{"id": "bg1", "cwd": str(tmp_path), "sessionId": "sid"}])
    return obj


@pytest.mark.parametrize("requested,expected", [(None, ["Read", "Grep"]),
                                               (["Grep", "Edit"], ["Grep"])])
def test_restricted_predecessor_enforced_in_argv(tmp_path, requested, expected):
    obj = adapter(tmp_path, {"allowed_tools": requested,
                            "predecessor_permissions": boundary(tools=["Read", "Grep"])})
    obj.prepare_launch(2, "continue")
    assert obj.create_thread(str(tmp_path)) == "sid"
    argv = obj._run.call_args.args
    assert "--allowedTools=" + ",".join(expected) in argv
    assert obj.resolved_permissions["allowed_tools"] == expected
    assert read_json(obj._path("sid") / "launch.json")["resolved_permissions"] == obj.resolved_permissions


@pytest.mark.parametrize("config", [
    {"allowed_tools": ["Edit"], "predecessor_permissions": boundary(tools=["Read"])},
    {"allowed_tools": []},
    {"predecessor_permissions": boundary(tools=[])},
])
def test_empty_tool_boundary_never_spawns(tmp_path, config):
    obj = adapter(tmp_path, config)
    obj.prepare_launch(1, "test")
    with pytest.raises(PermissionError, match="Empty allowed_tools intersection"):
        obj.create_thread(str(tmp_path))
    obj._run.assert_not_called()


def test_mode_tightening_and_rejection(tmp_path):
    config = {"permission_mode": "bypassPermissions", "predecessor_permissions": boundary("plan")}
    obj = adapter(tmp_path, config)
    obj.prepare_launch(2, "continue")
    obj.create_thread(str(tmp_path))
    argv = obj._run.call_args.args
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    with pytest.raises(PermissionError, match="escalation"):
        resolve_rollover_permissions({**config, "reject_permission_escalation": True})


@pytest.mark.parametrize("previous", ["UNKNOWN", {}, [], {"permission_mode": "plan"},
                                      boundary("UNKNOWN"), boundary(None), boundary(tools="Read"),
                                      {**boundary(), "quality": "UNKNOWN"}])
def test_unknown_predecessor_never_spawns(tmp_path, previous):
    obj = adapter(tmp_path, {"predecessor_permissions": previous})
    obj.prepare_launch(2, "continue")
    with pytest.raises(PermissionError, match="Predecessor permission boundary is UNKNOWN"):
        obj.create_thread(str(tmp_path))
    obj._run.assert_not_called()


@pytest.mark.parametrize("mode", ["unsupported", None, []])
def test_unsupported_modes(mode):
    with pytest.raises(ValueError, match="Unsupported permission_mode"):
        resolve_rollover_permissions({"permission_mode": mode})


def test_unrestricted_and_tuple_semantics():
    assert resolve_rollover_permissions({}) == boundary("inherit")
    assert resolve_rollover_permissions({"allowed_tools": ("Grep", "Read")},
                                        boundary(tools=("Read",))) == boundary("default", ["Read"])


def test_persisted_predecessor_survives_restart(tmp_path):
    first = adapter(tmp_path, {"permission_mode": "plan", "allowed_tools": ["Read"]})
    first.prepare_launch(1, "initial")
    sid = first.create_thread(str(tmp_path))
    successor = adapter(tmp_path, {"permission_mode": "bypassPermissions"})
    successor.resume_thread(sid)
    assert successor.resolved_permissions == boundary("plan", ["Read"])
    successor.prepare_launch(2, "continue", predecessor_session_id=sid)
    successor.create_thread(str(tmp_path))
    argv = successor._run.call_args.args
    assert "--allowedTools=Read" in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_missing_persisted_predecessor_refused(tmp_path):
    obj = adapter(tmp_path, {})
    with pytest.raises(PermissionError, match="UNKNOWN"):
        obj.prepare_launch(2, "continue", predecessor_session_id="legacy")
    obj.prepare_launch(2, "continue")
    with pytest.raises(PermissionError, match="UNKNOWN"):
        obj.create_thread(str(tmp_path))
    obj._run.assert_not_called()


@pytest.mark.parametrize("mode", ["plan", "default", "acceptEdits", "bypassPermissions"])
def test_inherit_pins_concrete_predecessor_in_argv(tmp_path, mode):
    previous = boundary(mode, ["Read"])
    assert resolve_rollover_permissions({}, previous) == previous
    obj = adapter(tmp_path, {"predecessor_permissions": previous})
    obj.prepare_launch(2, "continue")
    obj.create_thread(str(tmp_path))
    argv = obj._run.call_args.args
    assert argv[argv.index("--permission-mode") + 1] == mode


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
def test_inherit_predecessor_escalation_clamps_to_default(mode):
    assert resolve_rollover_permissions({"permission_mode": mode}, boundary("inherit")) == boundary()
    with pytest.raises(PermissionError, match="escalation"):
        resolve_rollover_permissions({"permission_mode": mode, "reject_permission_escalation": True},
                                     boundary("inherit"))


@pytest.mark.parametrize("mode", ["inherit", "plan", "default"])
def test_inherit_predecessor_preserves_or_tightens(mode):
    assert resolve_rollover_permissions({"permission_mode": mode}, boundary("inherit")) == boundary(mode)


@pytest.mark.parametrize("tools,tool,allowed", [
    (["Read"], "Edit", False), (["Read"], "Read", True),
    (["Bash(git:*)"], "Bash", True), (["Bash(git:*)"], "Bash(git:*)", True),
    (["Bash(git:*)"], "BashOther", False), (["Read"], "", False),
    ([], "Read", False), (None, "Edit", True),
])
@pytest.mark.parametrize("tool_key", ["tool_name", "tool"])
def test_writer_allowed_tools_enforced_under_bypass(tmp_path, tools, tool, allowed, tool_key):
    store = LocalStore(tmp_path / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.activate_initial(mid, "sid", "claude")
    store.note_event(mid, "runtime.session_capability", {"session_id": "sid", "epoch": 1})
    path = tmp_path / "missions" / mid / "sessions" / "launch"
    write_json(path / "launch.json", {"resolved_permissions": boundary("bypassPermissions", tools)})
    result = _authorize_pretool({"session_id": "sid", tool_key: tool}, tmp_path, mid, "launch", 1)
    if allowed:
        assert result == {}
    else:
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("after,allowed", [
    (boundary("default", ["Read"]), True), (boundary("plan", ["Read"]), True),
    (boundary("inherit", ["Read"]), False), (boundary("bypassPermissions", ["Read"]), False),
    (boundary("default", None), False), (boundary("default", ["Read", "Edit"]), False),
    (None, False), ({}, False),
])
def test_supervisor_verifies_persisted_claude_permissions(tmp_path, after, allowed):
    obj = adapter(tmp_path, {})
    for sid, permissions in (("old", boundary("default", ["Read"])), ("new", after)):
        path = obj.directory / "sessions" / sid
        write_json(path / "launch.json", {"resolved_permissions": permissions})
        obj.launches[sid] = path
    supervisor = Supervisor.__new__(Supervisor)
    supervisor.adapter = obj
    supervisor.config = {"executor": "claude"}
    supervisor.events = []
    supervisor.note = Mock()
    supervisor._recover = Mock()
    assert supervisor._policy_continuity("rid", 1, "old", "new") is allowed
    if allowed:
        kind, receipt = supervisor.note.call_args.args
        assert kind == "runtime.policy_continuity_checked"
        assert receipt["rollover_id"] == "rid"
        assert receipt["permission_continuity_verified"] is True
        assert receipt["predecessor_permissions"] == boundary("default", ["Read"])
        assert receipt["successor_permissions"] == after
        supervisor._recover.assert_not_called()
    else:
        supervisor._recover.assert_called_once()

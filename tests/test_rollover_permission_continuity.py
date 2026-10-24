"""Permission ceilings must survive launches and controller reconstruction."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gcl.adapters.claude_bg import ClaudeBackground, resolve_rollover_permissions
from gcl.runtime.files import read_json


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
                                        boundary(tools=("Read",))) == boundary("inherit", ["Read"])


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

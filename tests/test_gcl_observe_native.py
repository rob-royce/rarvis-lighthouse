"""Native observation stays lazy, non-controlling and fail-open."""
import io
import json
from pathlib import Path
import sys

from gcl.cli import _parser, _status, run
from gcl.core.store import LocalStore
from gcl.observe import handle, record_error


def mids(store):
    with store._read() as db:
        return [row[0] for row in db.execute("SELECT id FROM missions")]


def test_startup_first_prompt_only_and_compact_telemetry(tmp_path):
    home = tmp_path / "home"
    prompt = "  Keep this exact first prompt.\nSecond line  "
    for source in ("resume", "compact", "fork"):
        assert handle("claude", "SessionStart", {"session_id": source, "source": source}, home) == {}
        assert handle("claude", "UserPromptSubmit", {"session_id": source, "prompt": prompt}, home) == {}
    assert not (home / "state.sqlite").exists()
    assert handle("claude", "SessionStart", {"session_id": "fresh", "source": "startup"}, home) == {}
    assert handle("claude", "UserPromptSubmit", {"session_id": "fresh", "prompt": prompt}, home) == {}
    assert handle("claude", "UserPromptSubmit", {"session_id": "fresh", "prompt": "later"}, home) == {}
    store = LocalStore(home / "state.sqlite")
    assert len(mids(store)) == 1
    mid = mids(store)[0]
    beacon = store.get_beacon(mid)
    assert beacon["owner_instruction"] == prompt and beacon["objective"] == prompt
    assert beacon["done_when"] == beacon["non_goals"] == []
    assert all(x.startswith("ASSUMPTION:") for x in beacon["assumptions"])
    assert store.get_mode(mid) == "OBSERVE_ONLY"
    for event in ("Stop", "PreCompact", "PostCompact"):
        assert handle("claude", event, {"session_id": "fresh", "usage": {"input_tokens": 80},
                                        "context_window": 100}, home) == {}
    output = handle("claude", "SessionStart", {"session_id": "fresh", "source": "compact"}, home)
    assert beacon["hash"] in output["hookSpecificOutput"]["additionalContext"]
    assert len(mids(store)) == 1
    kinds = [e["type"] for e in store.events(mid)]
    assert kinds.count("runtime.telemetry") == 3
    decisions = [e["data"] for e in store.events(mid) if e["type"] == "runtime.monitor_decision"]
    assert len(decisions) == 3 and all(d["action"] == "OBSERVE" for d in decisions)
    assert not any(k.startswith("rollover.") or k == "runtime.worker_stop_pending" for k in kinds)
    assert _status(store, home, mid)["controller"] == "OBSERVE_ONLY:hooks"
    assert "deny" not in json.dumps(output).lower() and "block" not in json.dumps(output).lower()


def test_discriminators_off_and_managed_ledger(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for env in ({"GCL_MANAGED": "1"}, {"GCL_PLATFORM_RUN": "1"}):
        with monkeypatch.context() as patch:
            for key, value in env.items():
                patch.setenv(key, value)
            handle("codex", "SessionStart", {"session_id": str(env), "source": "startup"}, home)
    for command in (["claude", "--print"], ["codex", "exec", "--json"],
                    ["claude", "--no-session-persistence"]):
        handle("codex", "SessionStart", {"session_id": str(command), "source": "startup",
                                         "argv": command}, home)
    assert not home.exists()
    monkeypatch.setenv("GCL_MODE", "OFF")
    handle("claude", "SessionStart", {"session_id": "off", "source": "startup"}, home)
    assert not home.exists()
    monkeypatch.delenv("GCL_MODE")
    home.mkdir()
    (home / "OFF").touch()
    handle("claude", "SessionStart", {"session_id": "off", "source": "startup"}, home)
    assert not (home / "state.sqlite").exists()
    (home / "OFF").unlink()
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "managed"}, "human:owner")
    store.activate_initial(mid, "owned", "claude")
    handle("claude", "SessionStart", {"session_id": "owned", "source": "startup"}, home)
    assert len(mids(store)) == 1


def test_hook_exception_is_fail_open_and_degraded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    handle("claude", "SessionStart", {"session_id": "fresh", "source": "startup"}, home)
    handle("claude", "UserPromptSubmit", {"session_id": "fresh", "prompt": "work"}, home)
    monkeypatch.setattr("gcl.observe.evaluate", lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    payload = {"session_id": "fresh"}
    args = _parser().parse_args(["--home", str(home), "hook", "claude", "Stop", "--auto-observe"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert run(args) == {}
    store = LocalStore(home / "state.sqlite")
    mid = mids(store)[0]
    assert any(e["type"] == "runtime.hook_error" for e in store.events(mid))
    assert _status(store, home, mid)["controller"] == "DEGRADED:hook_error"


def test_adopt_requires_owner_controller(tmp_path, monkeypatch):
    home = tmp_path / "home"
    handle("claude", "SessionStart", {"session_id": "native", "source": "startup"}, home)
    handle("claude", "UserPromptSubmit", {"session_id": "native", "prompt": "work"}, home)
    store = LocalStore(home / "state.sqlite")
    mid = mids(store)[0]
    args = _parser().parse_args(["--home", str(home), "adopt", mid, "--mode", "CONTROLLED_AUTO"])
    result = run(args)
    assert result["available"] is False and result["reason"].startswith("CONTROLLED_AUTO unavailable:")
    assert store.get_mode(mid) == "OBSERVE_ONLY"
    managed = store.create_mission({"owner": "human:owner", "objective": "managed"}, "human:owner")
    store.note_event(managed, "runtime.config", {"executor": "claude", "native": False})
    store.activate_initial(managed, "bg-session", "claude")
    store.note_event(managed, "runtime.session_created", {"session_id": "bg-session", "epoch": 1})
    monkeypatch.setattr("gcl.cli.live_controller", lambda directory: (True, "fresh"))
    args = _parser().parse_args(["--home", str(home), "adopt", managed, "--mode", "CONTROLLED_AUTO"])
    assert run(args)["available"] is True
    assert store.get_mode(managed) == "CONTROLLED_AUTO"

    daemon = store.create_mission({"owner": "human:owner", "objective": "desktop"}, "human:owner")
    store.note_event(daemon, "runtime.config", {"executor": "codex", "endpoint": "daemon"})
    store.activate_initial(daemon, "desktop-thread", "codex")
    store.note_event(daemon, "runtime.session_created", {"session_id": "desktop-thread", "epoch": 1})
    args = _parser().parse_args(["--home", str(home), "adopt", daemon, "--mode", "CONTROLLED_AUTO"])
    result = run(args)
    assert result["available"] is False and "daemon-control" in result["reason"]
    assert store.get_mode(daemon) == "OBSERVE_ONLY"


def test_enable_disable_switch_is_home_scoped(tmp_path):
    home = tmp_path / "home"
    disable = _parser().parse_args(["--home", str(home), "disable"])
    enable = _parser().parse_args(["--home", str(home), "enable"])
    assert run(disable)["enabled"] is False
    assert (home / "OFF").is_file()
    assert handle("claude", "SessionStart", {"session_id": "new", "source": "startup"}, home) == {}
    assert not (home / "state.sqlite").exists()
    assert run(enable)["enabled"] is True
    assert not (home / "OFF").exists()
    assert handle("claude", "SessionStart", {"session_id": "new", "source": "startup"}, home) == {}
    assert (home / "state.sqlite").exists()

"""Server-initiated JSON-RPC and standalone sandbox regression checks."""
import json
from pathlib import Path

import pytest

from gcl.adapters.codex_appserver import CodexAppServer
from gcl.core.store import LocalStore
from test_gcl_codex_runtime import command, setup, wait_for


DECISIONS = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "abort"},
    "applyPatchApproval": {"decision": "abort"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "mcpServer/elicitation/request": {"action": "decline", "content": None, "_meta": None},
}
UNSUPPORTED = ["item/tool/requestUserInput", "item/tool/call",
               "account/chatgptAuthTokens/refresh", "attestation/generate", "future/request"]


def test_server_requests_get_replies_without_consuming_the_pending_call(tmp_path, monkeypatch):
    repo, home, env = setup(tmp_path, monkeypatch)
    methods = [*DECISIONS, *UNSUPPORTED]
    env.update(GCL_FAKE_SERVER_REQUESTS=json.dumps(methods), GCL_FAKE_STRUCTURED_SANDBOX="1")
    mid = command(env, "start", "--executor", "codex", "--cwd", str(repo),
                  "--mode", "OBSERVE_ONLY", "--codex-sandbox", "workspace-write",
                  "--", "synthetic request handling")["mission"]
    store = LocalStore(home / "state.sqlite")
    try:
        wait_for(lambda: any(e["type"] == "runtime.turn_started" for e in store.events(mid)))
        events = wait_for(lambda: [e["data"] for e in store.events(mid)
                                   if e["type"] == "runtime.server_request"])
        assert len(events) == len(methods)
        state = json.loads(Path(env["GCL_FAKE_STATE"]).read_text())
        replies = state["server_replies"]
        assert len(replies) == len(methods)
        assert isinstance(replies[0]["id"], int)  # Collided with the turn/start call.
        for method, reply in zip(methods, replies):
            if method in DECISIONS:
                assert reply["result"] == DECISIONS[method] and "error" not in reply
            else:
                assert reply["error"]["code"] == -32601 and "result" not in reply
        assert [e["method"] for e in events] == methods
        assert "opaque-test-value" not in json.dumps(events)
        assert "synthetic command" not in json.dumps(events)
        assert command(env, "status", mid)["last_server_request"]["method"] == methods[-1]
        turn = next(c for c in state["calls"] if c["method"] == "turn/start")
        assert turn["params"]["sandboxPolicy"]["writableRoots"] == [str(repo), str(home)]
    finally:
        command(env, "cancel", mid)


@pytest.mark.parametrize("sandbox", ["workspace-write", {
    "type": "workspaceWrite", "writableRoots": ["/synthetic/existing"],
    "networkAccess": False, "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}])
@pytest.mark.parametrize("resumed", [False, True])
def test_data_home_preserves_policy_for_first_and_resumed_turns(sandbox, resumed, monkeypatch):
    adapter = CodexAppServer()
    adapter.data_home = "/synthetic/ledger"
    adapter._remember({"thread": {"id": "t", "cwd": "/synthetic/repo"},
                       "sandbox": sandbox, "approvalPolicy": "on-request"}, resumed=resumed)
    calls = []
    monkeypatch.setattr(adapter, "call", lambda method, params: calls.append(params) or {"turn": {"id": "x"}})
    adapter.start_turn("t", "work")
    adapter.start_turn("t", "verify", read_only=True)
    adapter.start_turn("t", "continue")
    expected = dict(sandbox) if isinstance(sandbox, dict) else {"type": "workspaceWrite"}
    expected["writableRoots"] = [*expected.get("writableRoots", []), "/synthetic/repo", "/synthetic/ledger"]
    assert calls[0]["sandboxPolicy"] == calls[2]["sandboxPolicy"] == expected
    assert calls[1]["sandboxPolicy"] == {"type": "readOnly"}
    if isinstance(sandbox, dict):
        assert sandbox["writableRoots"] == ["/synthetic/existing"]


@pytest.mark.parametrize("sandbox", ["read-only", "danger-full-access", {"type": "readOnly"},
                                     {"type": "externalSandbox", "networkAccess": "restricted"}])
def test_data_home_does_not_widen_other_sandbox_modes(sandbox, monkeypatch):
    adapter = CodexAppServer()
    adapter.data_home = "/synthetic/ledger"
    adapter._remember({"thread": {"id": "t", "cwd": "/synthetic/repo"},
                       "sandbox": sandbox, "approvalPolicy": "never"})
    calls = []
    monkeypatch.setattr(adapter, "call", lambda method, params: calls.append(params) or {"turn": {"id": "x"}})
    adapter.start_turn("t", "work")
    assert "sandboxPolicy" not in calls[0]


def test_host_policy_is_unchanged_without_standalone_data_home(monkeypatch):
    adapter = CodexAppServer()
    adapter.writable_roots = ["/synthetic/host-workspace"]
    adapter._remember({"thread": {"id": "t", "cwd": "/synthetic/repo"}, "sandbox": "workspace-write"})
    calls = []
    monkeypatch.setattr(adapter, "call", lambda method, params: calls.append(params) or {"turn": {"id": "x"}})
    adapter.start_turn("t", "work")
    assert calls[0]["sandboxPolicy"] == {"type": "workspaceWrite", "writableRoots": adapter.writable_roots,
                                         "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}


def test_request_audit_is_bounded_and_reports_overflow(monkeypatch):
    adapter = CodexAppServer()
    monkeypatch.setattr(adapter, "_send", lambda message: None)
    for number in range(300):
        adapter._handle_server_request({"method": "item/fileChange/requestApproval", "id": number})
    events = adapter.drain_server_requests()
    assert events[0]["outcome"] == "audit_overflow" and events[0]["count"] == 44
    assert len(events) == 257 and events[-1]["request_id"] == 299
    assert adapter.drain_server_requests() == []


@pytest.mark.parametrize("recorded", [False, True])
def test_fresh_policy_receipt_exposes_cached_or_recorded_fallback(monkeypatch, recorded):
    adapter = CodexAppServer()
    response = {"thread": {"id": "t", "cwd": "/synthetic/repo"},
                "sandbox": "workspace-write", "approvalPolicy": "never"}
    adapter._remember(response)
    if recorded:
        adapter.recorded_policies["t"] = adapter.effective_policy("t")
    calls = []
    monkeypatch.setattr(adapter, "call", lambda method, params: calls.append((method, params)) or
                        {"thread": response["thread"], "approvalPolicy": "never"})
    policy = adapter.observe_policy("t")
    assert calls == [("thread/resume", {"threadId": "t"})]
    assert policy["sandbox"] == "workspace-write"
    assert policy["policy_evidence"]["source"] == "thread/resume"
    assert policy["policy_evidence"]["fields"] == {
        "sandbox": "recorded" if recorded else "cached", "approval_policy": "observed"}

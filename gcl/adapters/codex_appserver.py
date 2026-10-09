"""Codex 0.155 app-server JSON-RPC client. Only this client owns its threads."""
from __future__ import annotations

import json
from collections import deque
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from gcl.core.regular_file import open_regular


class AppServerError(RuntimeError):
    pass


class NotSentError(AppServerError):
    """The request provably never reached the app-server pipe; a retry cannot duplicate it."""


class RequestRejected(AppServerError):
    """The app-server answered with a JSON-RPC error: an authoritative negative receipt."""


_EARLY_THREADS = 32
_EARLY_PER_THREAD = 256


def classify_error(value: object) -> str | None:
    text = str(value).lower()
    for kind, words in {
        "rate_limit": ("rate limit", "rate_limit", "429"),
        "auth": ("unauthorized", "authentication", "invalid api key", "401", "403"),
        "quota": ("quota", "insufficient credits", "billing"),
        "network": ("network", "connection", "dns", "timeout", "timed out"),
    }.items():
        if any(word in text for word in words):
            return kind
    return None


class CodexAppServer:
    def __init__(self, endpoint: str = "own", binary: str | None = None, log_path: str | None = None,
                 approval_policy: str = "inherit"):
        if endpoint not in ("own", "daemon"):
            raise ValueError(endpoint)
        self.endpoint = endpoint
        self.binary = binary or os.environ.get("GCL_CODEX_BIN", "codex")
        self.log_path = log_path
        self.approval_policy = approval_policy
        self.codex_sandbox = "inherit"
        self.model: str | None = None
        self.reasoning_effort: str | None = None
        self.writable_roots: list[str] | None = None
        # Standalone ledger access; separate from a host's explicit sandbox policy.
        self.data_home: str | None = None
        self.environment: dict[str, str] | None = None
        self.wait_guard = lambda: None
        self.process = None
        self._stderr = None
        self._reader = None
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._threads: dict[str, dict] = {}
        self.recorded_policies: dict[str, dict] = {}
        # Gate-checked wire policies for successor writer turns. Such a turn never uses
        # cached thread state, which a read-only turn override may have changed.
        self.writer_policies: dict[str, dict] = {}
        # Called with the exact turn policy before every turn/start; a failure means not sent.
        self.before_turn = lambda session_id, sent: None
        # Notifications for a thread whose start/resume response has not been
        # remembered yet. Bounded: a proxy endpoint also streams foreign threads.
        self._early: dict[str, list[tuple[str, dict]]] = {}
        self._request_events = deque(maxlen=256)
        self._requests_dropped = 0

    def start(self) -> None:
        if self.process is not None:
            return
        self._stderr = open(self.log_path, "ab", buffering=0) if self.log_path else subprocess.DEVNULL
        argv = [self.binary, "app-server"] + (["proxy"] if self.endpoint == "daemon" else [])
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self._stderr, bufsize=0,
                                        env=dict(self.environment or os.environ, GCL_MANAGED="1"))
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.call("initialize", {"clientInfo": {"name": "gcl", "version": "1.0"}})
        self.notify("initialized", {})

    def close(self, detach: bool = False) -> None:
        if self.process is not None:
            if detach and self.process.poll() is None:
                subprocess.Popen([sys.executable, "-B", "-m", "gcl.runtime.drain",
                                  str(self.process.stdin.fileno()), str(self.process.stdout.fileno())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 pass_fds=(self.process.stdin.fileno(), self.process.stdout.fileno()),
                                 start_new_session=True)
            # This is our child, never a shared daemon or an unrelated worker.
            if not detach and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        if self._stderr not in (None, subprocess.DEVNULL):
            self._stderr.close()

    def _send(self, message: dict) -> None:
        if self.process is None or self.process.poll() is not None:
            raise NotSentError("app-server is not running")
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        with self._lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def call(self, method: str, params: dict, timeout: float = 30) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            inbox = queue.Queue(maxsize=1)
            self._pending[request_id] = inbox
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                answer = inbox.get(timeout=timeout)
            except queue.Empty as exc:
                raise AppServerError(f"{method} timed out") from exc
            if "error" in answer:
                raise RequestRejected(f"{method}: {answer['error']}")
            return answer.get("result") or {}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _read_loop(self) -> None:
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            # Server requests and our calls have independent ID namespaces.
            if "method" in message and "id" in message:
                self._handle_server_request(message)
            elif "id" in message:
                inbox = self._pending.get(message["id"])
                if inbox:
                    try:
                        inbox.put_nowait(message)
                    except queue.Full:
                        pass  # A duplicate reply must not stop the reader.
            elif "method" in message:
                self._handle_notification(message["method"], message.get("params") or {})

    def _handle_server_request(self, message: dict) -> None:
        """Never approve a server-initiated action or leave it waiting for a reply."""
        method = message["method"]
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            result = {"decision": "decline"}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            result = {"decision": "abort"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None, "_meta": None}
        else:
            result = None
        reply = {"id": message["id"]}
        if result is None:
            reply["error"] = {"code": -32601, "message": "GCL does not support this server request"}
        else:
            reply["result"] = result
        self._send(reply)
        params = message.get("params") or {}
        # Never persist commands, patches, elicitation content or authentication data.
        event = {"method": method, "request_id": message["id"],
                 "session_id": params.get("threadId", params.get("conversationId")),
                 "outcome": "unsupported" if result is None else "declined", "at": time.time()}
        with self._lock:
            if len(self._request_events) == self._request_events.maxlen:
                self._requests_dropped += 1
            self._request_events.append(event)

    def drain_server_requests(self) -> list[dict]:
        with self._lock:
            events = list(self._request_events)
            self._request_events.clear()
            if self._requests_dropped:
                events.insert(0, {"outcome": "audit_overflow", "count": self._requests_dropped,
                                  "at": time.time()})
                self._requests_dropped = 0
            return events

    def _handle_notification(self, method: str, params: dict) -> None:
        sid = params.get("threadId")
        if sid and sid not in self._threads:
            if sid not in self._early and len(self._early) >= _EARLY_THREADS:
                self._early.pop(next(iter(self._early)))
            early = self._early.setdefault(sid, [])
            early.append((method, params))
            del early[:-_EARLY_PER_THREAD]
        if sid and sid in self._threads:
            state = self._threads[sid]
            state["last_notification_at"] = time.time()
            if method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage") or {}
                state["usage"] = usage
                if "first_usage" not in state:
                    state["first_usage"] = usage
            elif method == "turn/started":
                turn = params.get("turn") or {}
                turn_id = turn.get("id") or params.get("turnId")
                state["active_turn"] = turn_id
                state["turn_started_at"] = time.monotonic()
                observation = {"turn_id": turn_id, "origin": turn.get("origin", params.get("origin", "unknown"))}
                policy = turn.get("sandboxPolicy", params.get("sandboxPolicy"))
                if policy is not None:
                    observation["sandbox_policy"] = policy
                state["active_turn_observation"] = observation
                with self._lock:
                    state.setdefault("turn_starts", {})[turn_id] = observation
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                turn_id = turn.get("id") or params.get("turnId")
                state["completed_turns"].add(turn_id)
                if state.get("active_turn") == turn_id:
                    state["active_turn"] = None
                    state["turn_started_at"] = None
                if turn.get("error"):
                    state.setdefault("turn_errors", {})[turn_id] = turn["error"]
                    state["blocker_kind"] = classify_error(turn["error"])
            elif method in ("error", "thread/error"):
                state["blocker_kind"] = classify_error(params)
            elif method == "thread/goal/updated":
                state["goal_status"] = (params.get("goal") or {}).get("status")
            elif method in ("item/completed", "item/updated"):
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    state["last_message"] = item.get("text", "")

    def _remember(self, response: dict, resumed: bool = False) -> str:
        thread = response.get("thread", response)
        sid = thread["id"]
        self._threads.setdefault(sid, {"thread": thread, "completed_turns": set(),
                                       "active_turn": None, "last_notification_at": time.time(),
                                       "turns_started": resumed})
        state = self._threads[sid]
        state["thread"] = thread
        recorded = self.recorded_policies.get(sid, {})
        provenance = {}
        for wire, key in (("sandbox", "sandbox"), ("approvalPolicy", "approval_policy")):
            if wire in response or wire in thread:
                state[key] = response[wire] if wire in response else thread[wire]
                provenance[key] = "observed"
            elif key in recorded:
                state[key] = recorded[key]
                provenance[key] = "recorded"
            else:
                provenance[key] = "cached" if key in state else "missing"
        state["policy_evidence"] = {"source": "thread/resume" if resumed else "thread/start",
                                    "observed_at": time.time(), "fields": provenance}
        for method, params in self._early.pop(sid, []):
            self._handle_notification(method, params)
        for turn in thread.get("turns") or []:
            if turn.get("status") == "inProgress" and turn.get("id") not in state["completed_turns"]:
                self._handle_notification("turn/started", {"threadId": sid, "turn": turn})
        return sid

    def drain_turn_starts(self, session_id: str) -> list[dict]:
        """Include short-lived turns; resolve controller origin after turn/start returns."""
        state = self._threads[session_id]
        with self._lock:
            starts = list(state.setdefault("turn_starts", {}).values())
            state["turn_starts"].clear()
        for turn in starts:
            if turn["turn_id"] in state.get("controller_turns", set()):
                turn["origin"] = "controller"
            elif turn["origin"] not in ("vendor", "unknown"):
                turn["origin"] = "unknown"
        return starts

    def create_thread(self, cwd: str) -> str:
        params = {"cwd": cwd}
        if self.codex_sandbox != "inherit":
            params["sandbox"] = self.codex_sandbox
        if self.approval_policy != "inherit":
            params["approvalPolicy"] = self.approval_policy
        if self.model is not None:
            params["model"] = self.model
        config = {}
        if self.reasoning_effort is not None:
            config["model_reasoning_effort"] = self.reasoning_effort
        if self.writable_roots is not None:
            params["sandbox"] = "workspace-write"
            config["sandbox_workspace_write"] = {
                "writable_roots": self.writable_roots,
                "exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}
        if config:
            params["config"] = config
        return self._remember(self.call("thread/start", params))

    def resume_thread(self, session_id: str) -> None:
        if session_id in self._threads:
            return
        response = self.call("thread/resume", {"threadId": session_id})
        self._remember(response, resumed=True)

    def effective_policy(self, session_id: str) -> dict:
        state = self._threads[session_id]
        return {"sandbox": state.get("sandbox"), "approval_policy": state.get("approval_policy"),
                "cwd": state["thread"].get("cwd"), "policy_evidence": state["policy_evidence"]}

    def observe_policy(self, session_id: str) -> dict:
        """Obtain a new wire receipt even for a thread already cached in this process."""
        response = self.call("thread/resume", {"threadId": session_id})
        if not isinstance(response, dict):
            raise AppServerError("policy receipt is unavailable")
        thread = response.get("thread", response)
        if not isinstance(thread, dict) or thread.get("id") != session_id:
            raise AppServerError("policy receipt thread identity mismatch")
        self._remember(response, resumed=True)
        return self.effective_policy(session_id)

    @staticmethod
    def normalize_policy(policy: dict, cwd: str, data_home: str) -> tuple[dict, list[str]]:
        """Project the supported sandbox defaults and D12 envelope; reject unknown semantics."""
        unknown = []
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            unknown.append("sandbox.cwd")
            cwd = data_home
        sandbox = policy.get("sandbox")
        modes = {"read-only": "readOnly", "readOnly": "readOnly",
                 "workspace-write": "workspaceWrite", "workspaceWrite": "workspaceWrite",
                 "danger-full-access": "dangerFullAccess", "dangerFullAccess": "dangerFullAccess"}
        scalar = isinstance(sandbox, str)
        raw = {"type": sandbox} if scalar else sandbox if isinstance(sandbox, dict) else {}
        mode = modes.get(sandbox) if scalar else raw.get("type")
        if not isinstance(mode, str) or mode not in ("readOnly", "workspaceWrite", "dangerFullAccess"):
            mode = None
        normalized = {"type": mode}
        if mode is None:
            unknown.append("sandbox.type")
        elif mode == "workspaceWrite":
            allowed = {"type", "writableRoots", "networkAccess", "excludeTmpdirEnvVar", "excludeSlashTmp"}
            if set(raw) - allowed:
                unknown.append("sandbox.unsupported_fields")
            roots = [cwd] if scalar else raw.get("writableRoots")
            if not isinstance(roots, list) or any(not isinstance(r, str) or not Path(r).is_absolute() for r in roots):
                unknown.append("sandbox.writable_roots")
                roots = []
            # The same augmentation start_turn applies to every standalone writable turn.
            normalized["writable_roots"] = sorted(set([*roots, cwd, data_home]))
            for wire, key in (("networkAccess", "network_access"),
                              ("excludeTmpdirEnvVar", "exclude_tmpdir_env_var"),
                              ("excludeSlashTmp", "exclude_slash_tmp")):
                # Scalar workspace-write uses the adapter's default sandbox projection.
                # Structured receipts must state these critical details explicitly.
                value = False if scalar else raw.get(wire)
                normalized[key] = value if isinstance(value, bool) else None
                if not isinstance(value, bool):
                    unknown.append("sandbox." + key)
        elif set(raw) != {"type"}:
            unknown.append("sandbox.unsupported_fields")
        approval = policy.get("approval_policy")
        if not isinstance(approval, str) or approval not in ("never", "on-request", "on-failure", "untrusted"):
            approval = None
            unknown.append("approval_policy")
        return {"sandbox": normalized, "approval_policy": approval}, unknown

    @staticmethod
    def wire_policy(normalized: dict) -> dict:
        """Explicit turn/start fields for a normalized, gate-checked policy."""
        sandbox = normalized["sandbox"]
        policy = {"type": sandbox["type"]}
        if sandbox["type"] == "workspaceWrite":
            policy.update({"writableRoots": list(sandbox["writable_roots"]),
                           "networkAccess": sandbox["network_access"],
                           "excludeTmpdirEnvVar": sandbox["exclude_tmpdir_env_var"],
                           "excludeSlashTmp": sandbox["exclude_slash_tmp"]})
        return {"sandboxPolicy": policy, "approvalPolicy": normalized["approval_policy"]}

    @staticmethod
    def _sandbox_policy(sandbox: object, cwd: str, writable_roots: list[str] | None = None) -> dict:
        if isinstance(sandbox, dict):
            return dict(sandbox)
        policies = {
            "read-only": {"type": "readOnly"}, "readOnly": {"type": "readOnly"},
            "workspace-write": {"type": "workspaceWrite", "writableRoots": [cwd]},
            "workspaceWrite": {"type": "workspaceWrite", "writableRoots": [cwd]},
            "danger-full-access": {"type": "dangerFullAccess"},
            "dangerFullAccess": {"type": "dangerFullAccess"},
        }
        try:
            policy = policies[sandbox]
            if policy["type"] == "workspaceWrite" and writable_roots is not None:
                return {"type": "workspaceWrite", "writableRoots": writable_roots,
                        "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}
            return policy
        except (KeyError, TypeError) as exc:
            raise AppServerError(f"unknown thread-start sandbox: {sandbox!r}") from exc

    def set_goal(self, session_id: str, objective: str | None, status: str) -> None:
        self.call("thread/goal/set", {"threadId": session_id, "objective": objective, "status": status})
        # Recovery can retire a durable session before this connection resumes it.
        state = self._threads.get(session_id)
        if status == "paused" and state is not None:
            state["goal_status"] = status
        # An active reset still needs a native observation (R15); the request is not one.

    def start_turn(self, session_id: str, prompt: str, read_only: bool = False,
                   output_schema: dict | None = None) -> str:
        params = {"threadId": session_id, "input": [{"type": "text", "text": prompt}]}
        if self.model is not None:
            params["model"] = self.model
        if self.reasoning_effort is not None:
            params["effort"] = self.reasoning_effort
        if output_schema is not None:
            params["outputSchema"] = output_schema
        state = self._threads.get(session_id)
        if state is None:
            raise NotSentError(f"thread is not resumed: {session_id}")
        if read_only:
            params["sandboxPolicy"] = {"type": "readOnly"}
        elif session_id in self.writer_policies:
            # Never derive a writer turn from state a read-only turn may have changed.
            policy = self.writer_policies[session_id]
            params["sandboxPolicy"] = dict(policy["sandboxPolicy"])
            params["approvalPolicy"] = policy["approvalPolicy"]
        elif self.writable_roots is not None and not state["turns_started"]:
            params["sandboxPolicy"] = self._sandbox_policy("workspace-write", state["thread"]["cwd"], self.writable_roots)
        elif state["turns_started"]:
            cwd = state["thread"]["cwd"]
            params["sandboxPolicy"] = self._sandbox_policy(state.get("sandbox"), cwd, self.writable_roots)
            if state.get("approval_policy") is None:
                raise NotSentError("thread-start approvalPolicy is unavailable")
            params["approvalPolicy"] = state["approval_policy"]
        if self.data_home is not None and not read_only and session_id not in self.writer_policies:
            policy = params.get("sandboxPolicy") or self._sandbox_policy(
                state.get("sandbox"), state["thread"]["cwd"], self.writable_roots)
            if policy.get("type") == "workspaceWrite":
                # Preserve network, temp exclusions and every existing writable root.
                params["sandboxPolicy"] = dict(policy, writableRoots=list(dict.fromkeys(
                    [*policy.get("writableRoots", []), state["thread"]["cwd"], self.data_home])))
        try:
            self.before_turn(session_id, {"read_only": read_only,
                                          "sandbox_policy": params.get("sandboxPolicy"),
                                          "approval_policy": params.get("approvalPolicy")})
        except Exception as exc:
            raise NotSentError("turn policy record failed") from exc
        response = self.call("turn/start", params)
        state["turns_started"] = True
        turn_id = response["turn"]["id"]
        state.setdefault("controller_turns", set()).add(turn_id)
        if turn_id not in self._threads[session_id]["completed_turns"]:
            self._threads[session_id]["active_turn"] = turn_id
            if self._threads[session_id].get("turn_started_at") is None:
                self._threads[session_id]["turn_started_at"] = time.monotonic()
        return turn_id

    def wait_turn(self, session_id: str, turn_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.wait_guard()
            state = self._threads[session_id]
            if turn_id in state["completed_turns"]:
                return True
            if self.process is None or self.process.poll() is not None:
                raise AppServerError("app-server disconnected during turn")
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
        return False

    def quiesce(self, session_id: str, interrupt: bool, timeout: float = 30) -> bool:
        state = self._threads[session_id]
        turn_id = state.get("active_turn")
        if turn_id and interrupt:
            self.call("turn/interrupt", {"threadId": session_id, "turnId": turn_id})
        if turn_id and not self.wait_turn(session_id, turn_id, timeout):
            return False
        self.set_goal(session_id, None, "paused")
        return True

    def interrupt_turn(self, session_id: str, turn_id: str, timeout: float = 30) -> bool:
        """Interrupt by vendor ID even when GCL did not start the turn."""
        try:
            self.call("turn/interrupt", {"threadId": session_id, "turnId": turn_id})
        except RequestRejected:
            if turn_id not in self._threads[session_id]["completed_turns"]:
                raise
            return True  # A short-lived turn can finish before the interrupt reaches the server.
        return self.wait_turn(session_id, turn_id, timeout)

    def fresh_receipt(self, session_id: str) -> dict:
        state = self._threads[session_id]
        thread = state["thread"]
        meta = None
        path = thread.get("path")
        if path and Path(path).is_file():
            with open_regular(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    payload = entry.get("payload") or {}
                    if payload.get("type") == "session_meta":
                        meta = payload
                        break
        if meta is None:
            # The app-server Thread response exposes the same identity fields.
            meta = {"id": thread.get("id"), "session_id": thread.get("sessionId"),
                    "forked_from_id": thread.get("forkedFromId"),
                    "parent_thread_id": thread.get("parentThreadId")}
        usage = state.get("first_usage") or {}
        total = (usage.get("total") or {}).get("totalTokens")
        last = (usage.get("last") or {}).get("totalTokens")
        return {"executor": "codex", "requested": None, "session_meta": meta,
                "first_token_count": {"total": total, "last": last}}

    def signals(self, session_id: str) -> dict:
        state = self._threads[session_id]
        usage = state.get("usage") or {}
        native_goal = state.get("thread", {}).get("goal")
        goal_status = state.get("goal_status") or (native_goal.get("status") if isinstance(native_goal, dict)
                                                   else native_goal if isinstance(native_goal, str) else None)
        return {"context_tokens": (usage.get("last") or {}).get("totalTokens"),
                "context_window": usage.get("modelContextWindow"),
                "goal_status": goal_status,
                "blocker_kind": state.get("blocker_kind"),
                "last_notification_at": state.get("last_notification_at"),
                "active_turn": state.get("active_turn"),
                "active_turn_origin": ("controller" if state.get("active_turn") in state.get("controller_turns", set())
                                       else (state.get("active_turn_observation") or {}).get("origin", "unknown")),
                "active_turn_sandbox_policy": (state.get("active_turn_observation") or {}).get("sandbox_policy"),
                "turn_started_at": state.get("turn_started_at")}

    def retire(self, session_id: str) -> None:
        self.set_goal(session_id, None, "paused")
        self.call("thread/unsubscribe", {"threadId": session_id})

    def last_message(self, session_id: str) -> str:
        return str(self._threads[session_id].get("last_message") or "")

    def turn_error(self, session_id: str, turn_id: str) -> object | None:
        return self._threads[session_id].get("turn_errors", {}).get(turn_id)

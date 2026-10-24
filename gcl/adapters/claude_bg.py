"""Claude Code background-session adapter. Session identities come from agents, not --session-id."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid
import secrets

from gcl.adapters.codex_appserver import AppServerError
from gcl.core.store import LocalStore
from gcl.runtime.files import read_json, write_json


_PERMISSION_LEVELS = {"plan": 0, "default": 1, "inherit": 1,
                      "acceptEdits": 2, "bypassPermissions": 3}
_UNKNOWN_BOUNDARY = "Predecessor permission boundary is UNKNOWN; refusing rollover"


def resolve_rollover_permissions(config, predecessor_permissions=None):
    """Resolve a permission ceiling without confusing absent and empty allowlists."""
    mode = config.get("permission_mode", "inherit")
    if not isinstance(mode, str) or mode not in _PERMISSION_LEVELS:
        raise ValueError("Unsupported permission_mode")

    def valid_tools(tools):
        return tools is None or (isinstance(tools, (list, tuple)) and
                                 all(isinstance(t, str) and t and t != "UNKNOWN" for t in tools))

    tools = config.get("allowed_tools")
    if not valid_tools(tools):
        raise ValueError("Invalid allowed_tools")
    if predecessor_permissions is None:
        predecessor_permissions = config.get("predecessor_permissions")
    if predecessor_permissions is not None:
        before = predecessor_permissions
        if (not isinstance(before, dict) or
                not {"permission_mode", "allowed_tools"} <= before.keys() or
                any(isinstance(v, str) and v == "UNKNOWN" for v in before.values()) or
                not isinstance(before.get("permission_mode"), str) or
                not valid_tools(before.get("allowed_tools"))):
            raise PermissionError(_UNKNOWN_BOUNDARY)
        previous_mode = before["permission_mode"]
        if not isinstance(previous_mode, str) or previous_mode not in _PERMISSION_LEVELS:
            raise ValueError("Unsupported permission_mode")
        if _PERMISSION_LEVELS[mode] > _PERMISSION_LEVELS[previous_mode]:
            if config.get("reject_permission_escalation"):
                raise PermissionError("Permission mode escalation refused")
            mode = previous_mode
        previous_tools = before["allowed_tools"]
        if previous_tools is not None:
            previous_set = set(previous_tools)
            tools = (list(previous_tools) if tools is None else
                     [t for t in tools if t in previous_set])
    if tools is not None and not tools:
        raise PermissionError("Empty allowed_tools intersection cannot be safely enforced by CLI; refusing rollover")
    return {"permission_mode": mode, "allowed_tools": None if tools is None else list(tools)}


class ClaudeBackground:
    def __init__(self, config: dict, directory: Path):
        self.config, self.directory = config, directory
        self.home, self.mission = directory.parent.parent, directory.name
        self.binary = os.environ.get("GCL_CLAUDE_BIN", "claude")
        self.pending: tuple[int, str, bool] | None = None
        self.launches: dict[str, Path] = {}
        self.resolved_permissions: dict | None = None
        self._predecessor_permissions: dict | None = None
        self._quiet_observations: dict[str, tuple[str, int, int, float]] = {}
        self._agent_observations: dict[str, tuple[str | None, str | None] | None] = {}

    def start(self) -> None:
        pass

    def close(self, detach: bool = False) -> None:
        pass  # Background workers survive the controller.

    def prepare_launch(self, epoch: int, prompt: str, read_only: bool = False,
                       predecessor_session_id: str | None = None) -> None:
        self.pending = None
        self._predecessor_permissions = None
        if predecessor_session_id is not None:
            try:
                boundary = read_json(self._path(predecessor_session_id) / "launch.json").get("resolved_permissions")
            except (AppServerError, OSError, ValueError, AttributeError):
                raise PermissionError(_UNKNOWN_BOUNDARY) from None
            self._predecessor_permissions = boundary if boundary is not None else {
                "permission_mode": "UNKNOWN", "allowed_tools": None}
        self.pending = epoch, prompt, read_only

    def _run(self, *args: str, timeout: float = 15, cwd: str | None = None) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run([self.binary, *args], cwd=cwd or self.config["cwd"], text=True,
                                    capture_output=True, timeout=timeout,
                                    env=dict(os.environ, GCL_MANAGED="1") if args and args[0] == "--bg" else None)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AppServerError(f"claude invocation failed: {exc}") from exc
        if result.returncode:
            raise AppServerError(f"claude {' '.join(args[:2])}: {result.stderr.strip() or result.stdout.strip()}")
        return result

    def _agents(self) -> list[dict]:
        body = json.loads(self._run("agents", "--json").stdout)
        if isinstance(body, list):
            return body
        return body.get("agents", [])

    def _agent(self, session_id: str) -> dict | None:
        return next((a for a in self._agents() if a.get("sessionId") == session_id), None)

    def _settings(self, path: Path, epoch: int, capability: str) -> None:
        python = os.environ.get("GCL_HOOK_PYTHON", sys.executable)
        source = str(Path(__file__).resolve().parents[2])
        hooks = {}
        for name in ("SessionStart", "Stop", "PreToolUse", "UserPromptSubmit"):
            argv = [python, "-P", "-B", "-m", "gcl.hooks", "claude", name, "--home", str(self.home),
                    "--mission", self.mission, "--session", path.name, "--epoch", str(epoch)]
            command = "PYTHONPATH=" + shlex.quote(source) + " " + shlex.join(argv)
            hooks[name] = [{"hooks": [{"type": "command", "command": command, "timeout": 2000}]}]
        write_json(path / "settings.json", {"hooks": hooks, "env": {"GCL_SESSION_TOKEN": capability}})

    def create_thread(self, cwd: str) -> str:
        if self.pending is None:
            raise AppServerError("Claude launch prompt is missing")
        epoch, prompt, read_only = self.pending
        self.pending = None
        before = self._predecessor_permissions
        if epoch > 1 and before is None and self.config.get("predecessor_permissions") is None:
            raise PermissionError(_UNKNOWN_BOUNDARY)
        permissions = resolve_rollover_permissions(self.config, before)
        self.resolved_permissions = permissions
        token = uuid.uuid4().hex
        path = self.directory / "sessions" / token
        path.mkdir(parents=True, exist_ok=True)
        capability = secrets.token_hex(32)
        write_json(path / "launch.json", {"epoch": epoch, "brief": prompt, "read_only": read_only,
                                           "cwd": cwd, "resolved_permissions": permissions,
                                           "launched_at": time.time()})
        write_json(path / "turn.json", {"id": uuid.uuid4().hex, "started_at": time.time(),
                                         "started_monotonic": time.monotonic()})
        self._settings(path, epoch, capability)
        argv = ["--bg", "--session-id", token, "--settings", str(path / "settings.json"),
                "-n", f"gcl:{self.mission[:8]}·{epoch}"]
        permission_mode = permissions["permission_mode"]
        if permission_mode != "inherit":
            argv += ["--permission-mode", permission_mode]
        tools = permissions["allowed_tools"]
        if tools:
            argv.append("--allowedTools=" + ",".join(tools))
        output = self._run(*argv, "--", prompt, cwd=cwd).stdout
        match = re.search(r"backgrounded\s*·\s*([^\s·]+)\s*·", output)
        if not match:
            raise AppServerError("Claude --bg did not report a background id")
        short = match.group(1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            agent = next((a for a in self._agents() if a.get("id") == short and
                          a.get("cwd") == cwd and a.get("sessionId")), None)
            if agent:
                sid = agent["sessionId"]
                self.launches[sid] = path
                write_json(path / "resolved.json", {"session_id": sid, "bg_id": short,
                                                   "resolved_permissions": permissions})
                return sid
            time.sleep(.05)
        raise AppServerError("Claude background sessionId could not be resolved")

    def _path(self, session_id: str) -> Path:
        if session_id in self.launches:
            return self.launches[session_id]
        for path in (self.directory / "sessions").glob("*/resolved.json"):
            if read_json(path).get("session_id") == session_id:
                self.launches[session_id] = path.parent
                return path.parent
        raise AppServerError(f"unknown Claude session: {session_id}")

    def resume_thread(self, session_id: str) -> None:
        # Compatibility with the controller interface: resolve an existing session only.
        path = self._path(session_id)
        self.resolved_permissions = read_json(path / "launch.json").get("resolved_permissions")

    def set_goal(self, session_id: str, objective: str | None, status: str) -> None:
        pass  # GCL beacon is the Claude goal authority.

    def start_turn(self, session_id: str, prompt: str, read_only: bool = False) -> str:
        path = self._path(session_id)
        launch = read_json(path / "launch.json")
        if not read_json(path / "initial_turn.json"):
            write_json(path / "initial_turn.json", {"at": time.time()})
            return read_json(path / "turn.json")["id"]
        raise AppServerError("Claude turns are single-launch; a stopped session cannot be restarted")

    def _unmanaged_fork(self, session_id: str, bg_id: str | None, actual: dict) -> None:
        store = LocalStore(self.home / "state.sqlite")
        data = {"expected_session_id": session_id, "bg_id": bg_id,
                "session_id": actual.get("session_id"), "source": actual.get("source"), "at": time.time()}
        store.note_events(self.mission, [("runtime.unmanaged_fork", data),
                                         ("mission.recovery_required", {"reason": "unmanaged_fork", **data}),
                                         ("runtime.blocked", {"reason": "unmanaged_fork"})])
        if bg_id:
            self._run("stop", bg_id)

    def wait_turn(self, session_id: str, turn_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        path = self._path(session_id)
        while time.monotonic() < deadline:
            turn = read_json(path / "turn.json")
            stops = [e for e in LocalStore(self.home / "state.sqlite").events(self.mission)
                     if e["type"] == "runtime.turn_stop" and
                     e["data"].get("session_id") == session_id and
                     e["data"].get("turn_id") == turn.get("id") and
                     e["data"].get("at", 0) >= turn.get("started_at", 0)]
            if turn_id == turn.get("id") and stops:
                return True
            time.sleep(.1)
        return False

    def quiesce(self, session_id: str, interrupt: bool) -> bool:
        """Prove a stopped, tool-free turn from hooks and a quiet final transcript."""
        timeout = float(self.config.get("runtime_policy", {}).get("quiesce_proof_s", 20))
        quiet_s = float(self.config.get("runtime_policy", {}).get("quiesce_quiet_s", 5))
        deadline = time.monotonic() + max(0, timeout)
        while True:
            store = LocalStore(self.home / "state.sqlite")
            events = store.events(self.mission)
            request = next((e for e in reversed(events) if e["type"] == "rollover.requested"), None)
            stops = [e for e in events if e["type"] == "runtime.turn_stop" and
                     e["data"].get("session_id") == session_id]
            stop = stops[-1] if stops else None
            last_tool = next((e for e in reversed(events) if e["type"] == "runtime.pre_tool_use" and
                              e["data"].get("session_id") == session_id), None)
            try:
                agent = self._agent(session_id)
                advisory = (agent.get("state"), agent.get("status")) if agent else None
            except (AppServerError, OSError, ValueError):
                advisory = ("unavailable", None)
            if self._agent_observations.get(session_id, object()) != advisory:
                store.note_event(self.mission, "runtime.claude_agent_advisory",
                                 {"session_id": session_id, "state": advisory[0] if advisory else "absent",
                                  "status": advisory[1] if advisory else None, "at": time.time()})
                self._agent_observations[session_id] = advisory
            # A request during an idle turn may follow its Stop. In either case,
            # the Stop must be newer than every observed tool use.
            stopped = bool(request and stop and
                           (stop["id"] > request["id"] or not last_tool or
                            stop["id"] > last_tool["id"]) and
                           (not last_tool or stop["id"] > last_tool["id"]))
            if stopped and self._quiet_final_transcript(session_id, quiet_s):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(.1, max(0, deadline - time.monotonic())))

    def _quiet_final_transcript(self, session_id: str, quiet_s: float) -> bool:
        path = self._path(session_id)
        latest = read_json(path / "latest_hook.json")
        name = latest.get("transcript_path")
        if not name:
            self._quiet_observations.pop(session_id, None)
            return False
        try:
            transcript = Path(name)
            stat = transcript.stat()
            with transcript.open(encoding="utf-8") as stream:
                lines = stream.readlines()
            after = transcript.stat()
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("transcript changed while reading")
            if not lines or not lines[-1].endswith("\n"):
                raise ValueError("transcript has no complete final record")
            pending = set()
            row = None
            for line in lines:
                record = json.loads(line)
                if record.get("isSidechain") or record.get("isSideChain"):
                    continue
                # Claude appends attachments and turn-end system records after
                # the final assistant message. Neither changes the turn's tail.
                if record.get("type") in ("system", "attachment"):
                    continue
                row = record
                for block in (record.get("message") or {}).get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        pending.add(block.get("id") or "<unknown>")
                    elif block.get("type") == "tool_result":
                        pending.discard(block.get("tool_use_id"))
            if row is None:
                raise ValueError("transcript has no assistant tail")
            message = row.get("message") or {}
            content = message.get("content") or []
            if (row.get("type") != "assistant" or message.get("role") not in (None, "assistant") or
                    pending or
                    any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content) or
                    message.get("stop_reason") == "tool_use"):
                raise ValueError("transcript has no final tool-free assistant message")
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            self._quiet_observations.pop(session_id, None)
            return False
        now = time.monotonic()
        signature = (str(transcript), stat.st_size, stat.st_mtime_ns)
        previous = self._quiet_observations.get(session_id)
        if previous is None or previous[:3] != signature:
            self._quiet_observations[session_id] = (*signature, now)
            return quiet_s <= 0
        return now - previous[3] >= quiet_s

    def fresh_receipt(self, session_id: str) -> dict:
        path = self._path(session_id)
        launch = read_json(path / "launch.json")
        startup = read_json(path / "startup.json")
        transcript = Path(startup.get("transcript_path") or "/nonexistent")
        preexisting = not transcript.is_file() or transcript.stat().st_mtime < launch.get("launched_at", 0)
        return {"executor": "claude", "requested_session_id": session_id,
                "session_id": startup.get("session_id"), "hook_source": startup.get("source"),
                "transcript_preexisting": preexisting, "transcript_path": str(transcript)}

    def signals(self, session_id: str) -> dict:
        path = self._path(session_id)
        agent = self._agent(session_id) or {}
        telemetry = read_json(path / "telemetry.json")
        latest = read_json(path / "latest_hook.json")
        transcript = latest.get("transcript_path") or read_json(path / "startup.json").get("transcript_path")
        if transcript and Path(transcript).is_file():
            from gcl.hooks import transcript_occupancy
            settings = read_json(Path(self.config.get("claude_settings") or "~/.claude/settings.json").expanduser())
            current = transcript_occupancy(transcript, self.config.get("model") or settings.get("model"),
                                           self.config.get("context_window"),
                                           bool(self.config.get("model") or self.config.get("context_window")))
            if current.get("context_tokens") is not None:
                telemetry.update(current)
                telemetry["at"] = time.time()
                write_json(path / "telemetry.json", telemetry)
        active = agent.get("state") == "working" or agent.get("status") == "busy"
        waiting_for = agent.get("waitingFor") or agent.get("waiting_for")
        return {"active_turn": session_id if active else None,
                "turn_started_at": read_json(path / "turn.json").get("started_monotonic") if active else None,
                "context_tokens": telemetry.get("context_tokens"),
                "context_window": telemetry.get("context_window"),
                "occupancy_quality": telemetry.get("quality", "UNKNOWN"),
                "last_notification_at": telemetry.get("at"),
                "waitingFor": waiting_for}

    def retire(self, session_id: str) -> None:
        agent = self._agent(session_id)
        self._run("stop", (agent or {}).get("id", session_id))


def request_worker_stop(home: Path, mission: str, session_id: str, *, safe_point: bool = False) -> dict:
    """Stop the current Claude worker once, or leave a durable request for its Stop hook."""
    store = LocalStore(home / "state.sqlite")
    directory = home / "missions" / mission
    with open(directory / "worker-stop.lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        events = store.events(mission)
        existing = next((e["data"] for e in reversed(events)
                         if e["type"] == "runtime.worker_stop_receipt" and
                         e["data"].get("session_id") == session_id), None)
        if existing:
            return existing
        _, current = store.current_writer(mission)
        if current != session_id:
            return {"session_id": session_id, "state": "stale"}
        config = next(e["data"] for e in events if e["type"] == "runtime.config")
        adapter = ClaudeBackground(config, directory)
        resolved = read_json(adapter._path(session_id) / "resolved.json")
        bg_id = resolved["bg_id"]
        if not safe_point:
            agent = adapter._agent(session_id)
            if agent is None:
                receipt = {"session_id": session_id, "bg_id": bg_id, "state": "absent",
                           "at": time.time()}
                store.note_event(mission, "runtime.worker_stop_receipt", receipt)
                return receipt
            if agent.get("state") == "working" or agent.get("status") == "busy":
                if not any(e["type"] == "runtime.worker_stop_pending" and
                           e["data"].get("session_id") == session_id for e in events):
                    store.note_event(mission, "runtime.worker_stop_pending",
                                     {"session_id": session_id, "bg_id": bg_id, "at": time.time()})
                return {"session_id": session_id, "bg_id": bg_id, "state": "pending"}
        result = adapter._run("stop", bg_id)
        receipt = {"session_id": session_id, "bg_id": bg_id, "state": "stopped",
                   "command": ["claude", "stop", bg_id], "exit_code": result.returncode,
                   "at": time.time()}
        store.note_event(mission, "runtime.worker_stop_receipt", receipt)
        return receipt


def create_adapter(config: dict, directory: Path) -> ClaudeBackground:
    return ClaudeBackground(config, directory)

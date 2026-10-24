"""Session-scoped Claude hooks. Tool authorization fails closed; telemetry is optional."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time

from gcl.core.beacon import render_brief
from gcl.core.regular_file import open_regular
from gcl.core.store import LocalStore
from gcl.runtime.files import read_json, write_json
from gcl.runtime.capability import registered
from gcl.runtime.worker_commands import worker_prefix, worker_prefixes


READ_TOOLS = {"Read", "Grep", "Glob"}


def _worker_command(command: str, home: Path, mission: str, sid: str, epoch: int,
                    successor: bool = False) -> bool:
    """Accept only a single fenced worker invocation, never a shell program."""
    if not isinstance(command, str) or any(char in command for char in ("$", "`", "\n", "\r")):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return False
    if any(set(word) <= set(";&|<>()") for word in words):
        return False
    prefix = next((prefix for prefix in worker_prefixes(home)
                   if tuple(words[:len(prefix)]) == prefix), None)
    if prefix is None:
        return False
    words = words[len(prefix):]
    if words == ["--help"] and not successor:
        return True
    permitted = ("verified", "await-writer", "note") if successor else ("note", "evidence", "help")
    if not words or words[0] not in permitted:
        return False
    if len(words) < 6 or words[1:6] != [mission, "--session", sid, "--epoch", str(epoch)]:
        return False
    tail = words[6:]
    if words[0] == "help":
        return tail in ([], ["--help"])
    if tail == ["--help"] and not successor:
        return True
    if words[0] == "note":
        return len(tail) == 1 and bool(tail[0])
    if words[0] == "await-writer":
        return not tail
    if words[0] == "verified":
        return len(tail) == 2 and tail[0] == "--summary" and bool(tail[1])
    return (len(tail) == 4 and tail[0] == "--claim" and bool(tail[1]) and
            tail[2] == "--path" and bool(tail[3]))


def _note_command(home: Path, mission: str, sid: str, epoch: int) -> str:
    return (f"{worker_prefix(home)} "
            f"note {shlex.quote(mission)} --session {shlex.quote(sid)} --epoch {epoch} "
            "'handoff summary and next step'")


def transcript_occupancy(path: str, model: str | None = None, context_window: int | None = None,
                         explicit: bool = False) -> dict:
    """Take the last unique non-sidechain assistant request, not summed transcript usage."""
    last = None
    requests = {}
    with open_regular(path, "r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("isSidechain") or row.get("isSideChain"):
                continue
            msg = row.get("message") or {}
            if row.get("type") != "assistant" and msg.get("role") != "assistant":
                continue
            usage = msg.get("usage") or row.get("usage") or {}
            if not usage:
                continue
            request = row.get("requestId") or msg.get("requestId")
            transcript_model = msg.get("model") or row.get("model") or ""
            tokens = sum(int(usage.get(k) or 0) for k in
                         ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            effective_model = model or transcript_model
            window = context_window or (1_000_000 if "[1m]" in effective_model.lower() else 200_000)
            candidate = {"context_tokens": tokens, "context_window": window,
                         "quality": "MEASURED" if explicit else "ESTIMATED",
                         "model": transcript_model, "request_id": request}
            if request:
                requests[request] = candidate  # later streaming copies replace earlier copies
                last = requests[request]
            else:
                last = candidate
    return last or {"context_tokens": None, "context_window": None, "quality": "UNKNOWN"}


def _denied(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _authorize_pretool(payload, home, mission, launch, epoch):
    store = LocalStore(home / "state.sqlite")
    path = home / "missions" / mission / "sessions" / launch
    sid = payload.get("session_id") or read_json(path / "resolved.json").get("session_id")
    if not sid or not registered(store, mission, sid, epoch):
        return _denied("GCL unmanaged Claude session")
    tool = payload.get("tool_name") or payload.get("tool") or ""
    launch_info = read_json(path / "launch.json")
    permissions = launch_info.get("resolved_permissions") or {}
    tools = permissions.get("allowed_tools")
    if tools is not None:
        # Scoped CLI rules identify their base tool here; Claude enforces the scope.
        if (not isinstance(tools, list) or not isinstance(tool, str) or not tool or
                not all(isinstance(rule, str) and rule for rule in tools) or
                not any(rule == tool or
                        (rule.endswith(")") and "(" in rule and rule.split("(", 1)[0] == tool)
                        for rule in tools)):
            return _denied("GCL tool outside resolved_permissions.allowed_tools")
    command = (payload.get("tool_input") or {}).get("command", "")
    allowed_worker = tool == "Bash" and _worker_command(command, home, mission, sid, epoch)
    if store.check_writer(mission, sid, epoch):
        with store._read() as db:
            requested = db.execute("SELECT 1 FROM rollovers WHERE mission_id=? AND epoch=? AND state IN "
                                   "('ROLLOVER_REQUESTED','QUIESCING') LIMIT 1", (mission, epoch)).fetchone()
        if requested:
            if tool in READ_TOOLS or allowed_worker:
                return {}
            return _denied("GCL rollover stop now: stop tool use; record progress with " +
                           _note_command(home, mission, sid, epoch) + "; end this turn now.")
        with store._read() as db:
            closed = db.execute("SELECT 1 FROM rollovers WHERE mission_id=? AND epoch=? AND state IN "
                                "('QUIESCING','CHECKPOINT_READY','SUCCESSOR_PREPARED_READ_ONLY') LIMIT 1",
                                (mission, epoch)).fetchone()
        if closed and tool not in READ_TOOLS and not allowed_worker:
            return _denied("GCL mission is quiescing")
        return {}
    current_epoch, current_sid = store.current_writer(mission)
    if launch_info.get("read_only") and current_epoch + 1 == epoch and not current_sid == sid:
        successor_worker = tool == "Bash" and _worker_command(command, home, mission, sid, epoch, successor=True)
        return {} if tool in READ_TOOLS or successor_worker else _denied("GCL successor is read-only until ownership transfer")
    if current_epoch == 0 and epoch == 1:
        return {} if tool in READ_TOOLS else _denied("GCL initial writer is not activated")
    return _denied("GCL stale worker epoch")


def _pretool_telemetry(payload, home, mission, launch, epoch):
    store = LocalStore(home / "state.sqlite")
    path = home / "missions" / mission / "sessions" / launch
    sid = payload.get("session_id") or read_json(path / "resolved.json").get("session_id")
    previous = read_json(path / "latest_hook.json")
    location = {"session_id": sid, "epoch": epoch, "at": time.time(),
                "cwd": payload.get("cwd") or previous.get("cwd"),
                "transcript_path": payload.get("transcript_path") or previous.get("transcript_path")}
    if location['cwd'] or location['transcript_path']:
        write_json(path / "latest_hook.json", location)
        store.note_event(mission, "runtime.hook_path", location)
    store.note_event(mission, "runtime.pre_tool_use", {"session_id": sid, "epoch": epoch,
                                                       "tool": payload.get("tool_name")})
    transcript = location.get("transcript_path")
    if transcript and Path(transcript).is_file():
        config = store.first_event(mission, "runtime.config") or {}
        settings = read_json(Path(config.get("claude_settings") or "~/.claude/settings.json").expanduser())
        telemetry = transcript_occupancy(transcript, config.get("model") or settings.get("model"),
                                         config.get("context_window"),
                                         bool(config.get("model") or config.get("context_window")))
        write_json(path / "telemetry.json", {"at": time.time(), **telemetry})


def handle(event: str, payload: dict, home: Path, mission: str, launch: str, epoch: int) -> dict:
    if event == "PreToolUse":
        try:
            decision = _authorize_pretool(payload, home, mission, launch, epoch)
        except Exception:
            return _denied("GCL cannot confirm tool authorization")
        # Authorization is final before any optional file reads or telemetry writes.
        try:
            _pretool_telemetry(payload, home, mission, launch, epoch)
        except Exception:
            pass
        return decision
    store = LocalStore(home / "state.sqlite")
    path = home / "missions" / mission / "sessions" / launch
    sid = payload.get("session_id") or read_json(path / "resolved.json").get("session_id")
    location = {"session_id": sid, "epoch": epoch, "cwd": payload.get("cwd"),
                "transcript_path": payload.get("transcript_path"), "at": time.time()}
    if location["cwd"] or location["transcript_path"]:
        previous = read_json(path / "latest_hook.json")
        location["cwd"] = location["cwd"] or previous.get("cwd")
        location["transcript_path"] = location["transcript_path"] or previous.get("transcript_path")
        write_json(path / "latest_hook.json", location)
        store.note_event(mission, "runtime.hook_path", location)
    if event == "SessionStart":
        source = payload.get("source")
        if source == "startup":
            write_json(path / "startup.json", {"session_id": sid, "source": source,
                                                  "transcript_path": payload.get("transcript_path"),
                                                  "cwd": payload.get("cwd")})
            token = read_json(path / "settings.json").get("env", {}).get("GCL_SESSION_TOKEN")
            if token and sid:
                store.note_event(mission, "runtime.session_capability",
                                 {"session_id": sid, "epoch": epoch,
                                  "sha256": hashlib.sha256(token.encode()).hexdigest(), "launch": launch})
        store.note_event(mission, "runtime.session_start_hook", {"session_id": sid, "source": source,
                                                                   "epoch": epoch, "launch": launch})
        if source == "compact":
            return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                    "additionalContext": render_brief(store.get_beacon(mission))}}
        if source == "startup":
            prefix = worker_prefix(home)
            command = (f"{prefix} "
                       f"done {mission} --session {shlex.quote(sid)} --epoch {epoch}")
            context = (render_brief(store.get_beacon(mission)) +
                       "\nWhen the goal and done_when are verified, report completion with: " + command)
            if epoch > 1:
                fence = f"{mission} --session {shlex.quote(sid)} --epoch {epoch}"
                context += ("\nFirst verify the checkpoint with read-only tools. Then run exactly: " +
                            f"{prefix} verified {fence} --summary 'checkpoint verified'\n" +
                            "Next run exactly: " + f"{prefix} await-writer {fence}\n" +
                            "Wait for WRITER GRANTED and continue in this turn. "
                            "On NOT GRANTED: stop now.")
            return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                    "additionalContext": context}}
        return {}
    if event == "Stop":
        telemetry = {"at": time.time()}
        transcript = location.get("transcript_path")
        if transcript:
            config = store.first_event(mission, "runtime.config") or {}
            settings = read_json(Path(config.get("claude_settings") or "~/.claude/settings.json").expanduser())
            telemetry.update(transcript_occupancy(transcript, config.get("model") or settings.get("model"),
                                                   config.get("context_window"),
                                                   bool(config.get("model") or config.get("context_window"))))
        write_json(path / "telemetry.json", telemetry)
        store.note_event(mission, "runtime.turn_stop", {"session_id": sid, "epoch": epoch,
                          "turn_id": read_json(path / "turn.json").get("id"), "at": telemetry["at"]})
        events = store.events(mission)
        if any(e["type"] == "runtime.worker_stop_pending" and e["data"].get("session_id") == sid
               for e in events) and not any(e["type"] == "runtime.worker_stop_receipt" and
               e["data"].get("session_id") == sid for e in events):
            from gcl.adapters.claude_bg import request_worker_stop
            request_worker_stop(home, mission, sid, safe_point=True)
            return {}
        if store.check_writer(mission, sid, epoch):
            messages = store.consume_owner_messages(mission, sid, epoch)
            if messages:
                store.note_event(mission, "runtime.owner_delivered", {"epoch": epoch,
                                                                        "seq": messages[-1]["seq"], "via": "stop"})
                return {"decision": "block", "reason": "Owner messages: " +
                        json.dumps(messages, ensure_ascii=False)}
        return {}
    if event == "UserPromptSubmit":
        store.note_event(mission, "runtime.attached_owner_prompt", {"session_id": sid, "epoch": epoch,
                                                                      "prompt": payload.get("prompt", "")})
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=("claude",))
    parser.add_argument("event", choices=("SessionStart", "Stop", "PreToolUse", "UserPromptSubmit"))
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--mission", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        result = handle(args.event, payload, args.home, args.mission, args.session, args.epoch)
    except Exception as exc:
        try:
            LocalStore(args.home / "state.sqlite").note_event(args.mission, "runtime.hook_error",
                {"event": args.event, "error": repr(exc), "at": time.time()})
        except Exception:
            pass
        result = (_denied("GCL cannot confirm tool authorization")
                  if args.event == "PreToolUse" else {})
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

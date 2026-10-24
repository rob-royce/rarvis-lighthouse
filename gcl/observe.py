"""Fail-open, observation-only hooks for native interactive sessions."""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from gcl.core.beacon import render_brief
from gcl.core.monitor import Policy, evaluate
from gcl.core.store import LocalStore
from gcl.runtime.files import read_json, write_json

EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "PostCompact")


def disabled(home: Path) -> bool:
    return os.environ.get("GCL_MODE", "").upper() == "OFF" or (home / "OFF").exists()


def _session(payload: dict) -> str | None:
    return payload.get("session_id") or payload.get("thread_id") or payload.get("threadId")


def _platform(payload: dict) -> bool:
    if os.environ.get("GCL_MANAGED") == "1" or os.environ.get("GCL_PLATFORM_RUN") == "1":
        return True
    if (payload.get("is_interactive") is False or payload.get("non_interactive") is True
            or payload.get("is_print") is True or payload.get("mode") in ("print", "exec")):
        return True
    command = payload.get("command") or payload.get("argv") or []
    if isinstance(command, str):
        command = command.split()
    return ("--print" in command or "-p" in command or "--no-session-persistence" in command
            or ("exec" in command and "codex" in " ".join(command)))


def _path(home: Path, executor: str, session: str) -> Path:
    key = hashlib.sha256((executor + "\0" + session).encode()).hexdigest()
    return home / "native" / executor / (key + ".json")


def _managed(store: LocalStore, session: str) -> bool:
    with store._read() as db:
        return db.execute("SELECT 1 FROM missions WHERE session_id=? LIMIT 1", (session,)).fetchone() is not None


def _telemetry(executor: str, payload: dict) -> dict:
    if executor == "claude" and payload.get("transcript_path"):
        from gcl.hooks import transcript_occupancy
        try:
            return transcript_occupancy(payload["transcript_path"])
        except (OSError, ValueError):
            pass
    usage = payload.get("usage") or payload.get("token_usage") or {}
    tokens = usage.get("input_tokens") or usage.get("total_tokens") or payload.get("context_tokens")
    window = payload.get("context_window") or usage.get("context_window")
    return {"context_tokens": tokens, "context_window": window,
            "quality": "MEASURED" if isinstance(tokens, (int, float)) and isinstance(window, (int, float)) else "UNKNOWN"}


def handle(executor: str, event: str, payload: dict, home: Path) -> dict:
    """Never returns a control decision, even if the monitor suggests one."""
    if disabled(home) or _platform(payload):
        return {}
    session = _session(payload)
    if not isinstance(session, str) or not session:
        return {}
    path = _path(home, executor, session)
    if event == "SessionStart" and payload.get("source") != "startup" and not path.exists():
        return {}
    if event != "SessionStart" and not path.exists():
        return {}
    store = LocalStore(home / "state.sqlite")
    if _managed(store, session) and not read_json(path).get("mission"):
        return {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.with_suffix(".lock")).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(path)
        if event == "SessionStart" and payload.get("source") == "startup" and not state:
            state = {"session_id": session, "source": "startup", "cwd": payload.get("cwd"),
                     "started_at": time.time()}
            write_json(path, state)
            return {}
        if state.get("source") != "startup":
            return {}
        mid = state.get("mission")
        if event == "UserPromptSubmit" and not mid:
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                return {}
            mid = hashlib.sha256(("native\0" + executor + "\0" + session).encode()).hexdigest()
            beacon = {"mission_id": mid, "owner": "human:owner", "owner_instruction": prompt,
                      "objective": prompt, "done_when": [], "non_goals": [], "constraints": [],
                      "route_boundaries": [], "acceptance": [], "decision_refs": [],
                      "assumptions": ["ASSUMPTION: done_when not provided",
                                      "ASSUMPTION: non_goals not provided",
                                      "ASSUMPTION: constraints not provided",
                                      "ASSUMPTION: route_boundaries not provided",
                                      "ASSUMPTION: acceptance not provided"]}
            try:
                store.create_mission(beacon, "human:owner", "OBSERVE_ONLY")
            except sqlite3.IntegrityError:
                pass  # A prior hook committed the mission before its map.
            if store.current_writer(mid)[0] == 0:
                store.activate_initial(mid, session, executor)
            store.note_event(mid, "runtime.config", {"executor": executor, "cwd": state.get("cwd"),
                                                       "native": True, "endpoint": "native"})
            state["mission"] = mid
            write_json(path, state)
        if not mid:
            return {}
        store.note_event(mid, "runtime.native_hook", {"event": event, "session_id": session,
                                                       "at": time.time()})
        if event == "SessionStart" and payload.get("source") == "compact":
            return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                           "additionalContext": render_brief(store.get_beacon(mid))}}
        if event in ("Stop", "PreCompact", "PostCompact"):
            telemetry = {"at": time.time(), "event": event, **_telemetry(executor, payload)}
            store.note_event(mid, "runtime.telemetry", telemetry)
            decision = evaluate({"context_tokens": telemetry.get("context_tokens"),
                                 "context_window": telemetry.get("context_window"),
                                 "now": time.time(), "session_started_at": state.get("started_at", 0)},
                                Policy(), "OBSERVE_ONLY")
            store.note_event(mid, "runtime.monitor_decision", decision)
        return {}


def record_error(home: Path, executor: str, event: str, payload: dict, exc: Exception) -> None:
    """Best effort error evidence; failure here must also remain fail-open."""
    try:
        record = {"at": time.time(), "executor": executor, "event": event,
                  "session_id": _session(payload), "error": repr(exc)}
        path = home / "native" / "hook-errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        session = _session(payload)
        if session and (mapping := read_json(_path(home, executor, session))).get("mission"):
            LocalStore(home / "state.sqlite").note_event(mapping["mission"], "runtime.hook_error", record)
    except Exception:
        pass


def export_beta_summary(store, support_minutes=0, false_positives=0, key_takeaway_present=False):
    """Voluntary offline aggregates only. Never select event content or identifiers."""
    for name, value in (("support_minutes", support_minutes), ("false_positives", false_positives)):
        if type(value) is not int or value < 0:
            raise ValueError(name + " must be a non-negative integer")
    if type(key_takeaway_present) is not bool:
        raise ValueError("key_takeaway_present must be a boolean")
    uri = Path(store.path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    try:
        db.execute("BEGIN")  # One consistent snapshot for all counters.
        missions = db.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
        completed = db.execute("SELECT COUNT(*) FROM missions m WHERE EXISTS "
                               "(SELECT 1 FROM events e WHERE e.mission_id=m.id "
                               "AND e.type IN ('mission.completed','mission.succeeded'))").fetchone()[0]
        rollovers = db.execute("SELECT COUNT(*) FROM rollovers").fetchone()[0]
        timestamps = db.execute("SELECT e.created_at FROM events e "
                                "JOIN missions m ON m.id=e.mission_id")
        utc_dates = set()
        for (timestamp,) in timestamps:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            # SQLite CURRENT_TIMESTAMP is timezone-free UTC, never local time.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            utc_dates.add(parsed.astimezone(timezone.utc).date().isoformat())
        days = len(utc_dates)
    finally:
        db.close()
    return {"schema_version": "gcl.beta_summary.v1", "missions_total": missions,
            "missions_completed": completed, "rollovers_total": rollovers,
            "active_days_count": days, "repeat_use_qualified": days >= 2 or missions >= 2,
            "support_minutes": support_minutes, "false_positives": false_positives,
            "key_takeaway_present": key_takeaway_present,
            "privacy_mode": "STATS_ONLY_NO_CONTENT_NO_PATHS"}

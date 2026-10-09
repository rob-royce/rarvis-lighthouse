"""Standalone GCL command line. JSON is available for every command."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from gcl.adapters.codex_appserver import AppServerError
from gcl.core.errors import GCLError
from gcl.core.monitor import Policy
from gcl.core.regular_file import sha256_regular
from gcl.core.store import LocalStore, completion_awaiting_confirmation, completion_state
from gcl.runtime.files import home_path, live_controller, mission_dir, process_start

# Supervisor/adapter settings carried in runtime_policy (not monitor.Policy fields).
_RUNTIME_KEYS = frozenset({"hard_ratio", "max_turn_s", "quiesce_proof_s", "quiesce_quiet_s", "quiesce_max_s",
                           "await_writer_s", "chunk_wait_s", "idle_nudge_s", "idle_nudges",
                           "delivery_backoff_max_s"})


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rlh")
    p.add_argument("--home", help="state directory: --home > RLH_HOME > GCL_HOME > ~/.rarvis-lighthouse")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--executor", choices=("codex", "claude"), required=True)
    start.add_argument("--cwd", required=True)
    start.add_argument("--mode", choices=("OFF", "OBSERVE_ONLY", "CONTROLLED_AUTO"), default="OBSERVE_ONLY")
    start.add_argument("--codex-endpoint", choices=("own", "daemon"), default="own")
    start.add_argument("--codex-sandbox", choices=("inherit", "read-only", "workspace-write",
                                                    "danger-full-access"), default="inherit")
    start.add_argument("--approval-policy", choices=("inherit", "never", "on-request", "on-failure",
                                                      "untrusted"), default="inherit")
    start.add_argument("--permission-mode", default="inherit")
    start.add_argument("--allowed-tools", action="append", default=[])
    start.add_argument("--runtime-policy", action="append", default=[])
    start.add_argument("--done-check")
    start.add_argument("--allow-self-complete", action="store_true",
                       help="complete when the agent reports done, without a done_check or the owner")
    start.add_argument("--model")
    start.add_argument("--context-window", type=int)
    start.add_argument("--claude-settings", default=os.environ.get("GCL_CLAUDE_SETTINGS", "~/.claude/settings.json"))
    for name in ("done-when", "non-goal", "constraint", "boundary", "acceptance", "policy"):
        start.add_argument("--" + name, action="append", default=[])
    start.add_argument("owner_text", nargs="*")
    for command in ("list", "status", "tail", "send", "attach", "rollover", "mode", "cancel", "resume", "confirm",
                    "note", "evidence", "help", "effect", "job", "done", "verified", "await-writer"):
        q = sub.add_parser(command)
        if command != "list":
            q.add_argument("mission", nargs="?" if command == "status" else None)
        if command in ("note", "evidence", "help", "effect", "job", "done", "verified", "await-writer"):
            q.add_argument("--session")
            q.add_argument("--epoch", type=int)
        if command == "tail":
            q.add_argument("-f", "--follow", action="store_true")
        if command == "attach":
            q.add_argument("--follow", action="store_true")
        if command in ("send", "note"):
            q.add_argument("text")
        if command == "rollover":
            q.add_argument("--reason", required=True, choices=("manual",))
        if command == "mode":
            q.add_argument("value", choices=("OFF", "OBSERVE_ONLY", "CONTROLLED_AUTO"))
            q.add_argument("--stop-worker", action="store_true")
        if command == "evidence":
            q.add_argument("--claim", required=True)
            q.add_argument("--path", required=True)
        if command == "verified":
            q.add_argument("--summary", required=True)
        if command == "effect":
            q.add_argument("action", choices=("begin", "confirm", "fail"))
            q.add_argument("key")
            q.add_argument("--receipt")
        if command == "job":
            q.add_argument("action", choices=("register",))
            q.add_argument("--id", required=True)
            q.add_argument("--cmd", required=True)
            q.add_argument("--log", required=True)
            q.add_argument("--pid", type=int)
            q.add_argument("--uninterruptible", action="store_true")
    hook = sub.add_parser("hook")
    hook.add_argument("executor", choices=("claude", "codex"))
    hook.add_argument("event", choices=("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "PostCompact"))
    hook.add_argument("--auto-observe", action="store_true", required=True)
    adopt = sub.add_parser("adopt")
    adopt.add_argument("mission")
    adopt.add_argument("--mode", choices=("CONTROLLED_AUTO",), required=True)
    sub.add_parser("enable")
    sub.add_parser("disable")
    goal = sub.add_parser("goal")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)
    goal_sub.add_parser("show").add_argument("mission")
    change = goal_sub.add_parser("change")
    change.add_argument("mission")
    change.add_argument("--field", required=True)
    change.add_argument("--value", required=True)
    change.add_argument("--authority-ref", required=True)
    delivery = sub.add_parser("delivery")
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    resolve = delivery_sub.add_parser("resolve")
    resolve.add_argument("mission")
    resolve.add_argument("delivery_id")
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--accepted", action="store_true")
    outcome.add_argument("--rejected", action="store_true")
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--turn-id")
    return p


def _emit(value: object, as_json: bool) -> None:
    if as_json or not isinstance(value, str):
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        print(value)


def _start_supervisor(home: Path, mission: str) -> int:
    directory = mission_dir(home, mission)
    directory.mkdir(parents=True, exist_ok=True)
    log = open(directory / "supervisor.log", "ab", buffering=0)
    try:
        process = subprocess.Popen([sys.executable, "-B", "-m", "gcl.supervisor", "--home", str(home),
                                    "--mission", mission], stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, start_new_session=True, cwd=Path(__file__).resolve().parents[1])
        return process.pid
    finally:
        log.close()


def _policy(values: list[str]) -> dict:
    accepted = set(Policy.__dataclass_fields__) | _RUNTIME_KEYS
    result = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or key not in accepted:
            raise ValueError(f"invalid policy: {item}")
        integer = (key.startswith("max_") and key != "max_turn_s") or key == "idle_nudges"
        result[key] = int(value) if integer else float(value)
        if key in ("hard_ratio", "max_turn_s") and result[key] <= 0:
            raise ValueError(f"policy must be positive: {item}")
        if key in _RUNTIME_KEYS and result[key] < 0:
            raise ValueError(f"policy must be nonnegative: {item}")
    if "hard_ratio" in result and not 0 < result["hard_ratio"] <= 1:
        raise ValueError("hard_ratio must be in (0, 1]")
    return result


def _claude_trusted(cwd: Path) -> bool:
    trust_file = Path(os.environ.get("GCL_CLAUDE_TRUST_FILE", "~/.claude.json")).expanduser()
    try:
        projects = json.loads(trust_file.read_text()).get("projects", {})
        return projects.get(str(cwd), {}).get("hasTrustDialogAccepted") is True
    except (OSError, ValueError, AttributeError):
        return False


def _missions(store: LocalStore) -> list[str]:
    with store._read() as db:
        return [row[0] for row in db.execute("SELECT id FROM missions ORDER BY rowid")]


def _status(store: LocalStore, home: Path, mission: str) -> dict:
    events = store.events(mission)
    epoch, session = store.current_writer(mission)
    with store._read() as db:
        row = db.execute("SELECT cancelled,owner_cursor FROM missions WHERE id=?", (mission,)).fetchone()
        pending_messages = [dict(x) for x in db.execute(
            "SELECT seq,text,actor FROM owner_messages WHERE mission_id=? AND seq>? ORDER BY seq",
            (mission, row["owner_cursor"]))]
        rollover = db.execute("SELECT id,state FROM rollovers WHERE mission_id=? ORDER BY rowid DESC LIMIT 1",
                              (mission,)).fetchone()
        effects = [dict(x) for x in db.execute("SELECT key,kind,state FROM effects WHERE mission_id=?", (mission,))]
        unknown_delivery = db.execute(
            "SELECT * FROM owner_outbox WHERE mission_id=? AND state='UNKNOWN' ORDER BY rowid LIMIT 1",
            (mission,)).fetchone()
    config = next((e["data"] for e in events if e["type"] == "runtime.config"), {})
    thread_policy = next((e["data"] for e in reversed(events) if e["type"] == "runtime.thread_policy"
                          and e["data"].get("session_id") == session), {})
    decision_event = next((e for e in reversed(events) if e["type"] == "runtime.monitor_decision"), None)
    decision = decision_event["data"] if decision_event else None
    recovery = next((e for e in reversed(events) if e["type"] in ("mission.recovery_required", "mission.recovered")), None)
    live, reason = live_controller(mission_dir(home, mission))
    telemetry = next((e["data"] for e in reversed(events) if e["type"] == "runtime.telemetry"), None)
    completion = completion_state(events)
    completed = completion["completed"]
    awaiting_owner = completion_awaiting_confirmation(
        completion, cancelled=bool(row["cancelled"]),
        recovery_required=bool(recovery and recovery["type"] == "mission.recovery_required"),
        owner_delivery_unknown=unknown_delivery is not None)
    worker_stop = next((e["data"] for e in reversed(events) if e["type"] in
                        ("runtime.worker_stop_receipt", "runtime.worker_stop_pending") and
                        e["data"].get("session_id") == session), None)
    delivery = store._delivery(unknown_delivery) if unknown_delivery else store.owner_delivery(mission)
    delivery_recovery = None
    if delivery and delivery['state'] == 'UNKNOWN':
        delivery_recovery = {
            "delivery_id": delivery['id'], "session_id": delivery['session_id'],
            "epoch": delivery['epoch'], "automatic_retry": False,
            "instruction": "Reconcile this delivery ID and session against authoritative executor "
                           "evidence. Do not retry or alter the batch while the outcome is unknown."}
    waiting_for = None
    if config.get("executor") == "claude" and session and not completed:
        try:
            from gcl.adapters.claude_bg import ClaudeBackground
            waiting_for = (ClaudeBackground(config, mission_dir(home, mission))._agent(session) or {}).get("waitingFor")
        except (OSError, ValueError, KeyError, RuntimeError):
            pass
    if delivery_recovery:
        health = "RECOVERY_REQUIRED"
    elif completed:
        health = "COMPLETED"
    elif row["cancelled"]:
        health = "DEGRADED:cancelled"
    elif store.get_mode(mission) == "OFF":
        health = "DEGRADED:off"
    elif waiting_for:
        health = "WAITING_INPUT"
    elif any(e["type"] == "runtime.hook_error" for e in events):
        health = "DEGRADED:hook_error"
    elif config.get("native"):
        health = "OBSERVE_ONLY:hooks"
    elif not live:
        health = "DEGRADED:" + reason
    elif not telemetry or time.time() - telemetry.get("at", 0) > 20:
        health = "DEGRADED:telemetry_stale"
    else:
        health = "PROTECTED"
    latest_block = next((e for e in reversed(events) if e["type"] in
                         ("runtime.blocked", "runtime.rollover_trigger", "mission.recovered")), None)
    if delivery_recovery:
        runtime_status, block_reason = "RECOVERY_REQUIRED", "OWNER_DELIVERY_UNKNOWN"
    elif completed:
        runtime_status, block_reason = "COMPLETED", None
    elif row["cancelled"]:
        runtime_status, block_reason = "CANCELLED", None
    elif recovery and recovery["type"] == "mission.recovery_required":
        runtime_status, block_reason = "RECOVERY_REQUIRED", recovery["data"].get("reason")
    elif awaiting_owner:
        runtime_status, block_reason = "AWAITING_CONFIRMATION", "completion_reported"
    elif waiting_for:
        runtime_status, block_reason = "WAITING_INPUT", None
    elif decision and decision["action"] == "BLOCK":
        runtime_status, block_reason = "BLOCKED", ",".join(decision["reasons"])
    elif (latest_block and latest_block["type"] == "runtime.blocked" and
          (decision_event is None or latest_block["id"] > decision_event["id"])):
        runtime_status, block_reason = "BLOCKED", latest_block["data"].get("reason")
    else:
        runtime_status, block_reason = "ACTIVE", None
    return {"mission": mission, "mode": store.get_mode(mission), "epoch": epoch, "session_id": session,
            "executor": config.get("executor"), "rollover": dict(rollover) if rollover else None,
            "sandbox": (thread_policy.get("sandbox", config.get("codex_sandbox", "inherit"))
                        if config.get("executor") == "codex" else None),
            "approval_policy": (thread_policy.get("approval_policy", config.get("approval_policy", "inherit"))
                                if config.get("executor") == "codex" else None),
            "permission_mode": (config.get("permission_mode", "inherit")
                                if config.get("executor") == "claude" else None),
            "monitor": decision, "controller": health, "pending_owner_messages": len(pending_messages),
            "pending_owner_message_details": pending_messages,
            "owner_delivery": delivery, "owner_delivery_recovery": delivery_recovery,
            "effects": effects, "cancelled": bool(row["cancelled"]),
            "runtime_status": runtime_status, "reason": block_reason,
            "worker_stop": worker_stop, "waitingFor": waiting_for,
            "last_server_request": next((e["data"] for e in reversed(events)
                                         if e["type"] == "runtime.server_request"), None)}


def _worker(store: LocalStore, mission: str, session: str | None, epoch: int | None) -> int:
    session = session or os.environ.get("GCL_SESSION_ID")
    epoch = epoch if epoch is not None else int(os.environ["GCL_EPOCH"])
    config = next((e["data"] for e in store.events(mission) if e["type"] == "runtime.config"), {})
    if config.get("executor") == "claude":
        from gcl.runtime.capability import verify_worker
        if not session or not verify_worker(store, mission, session, epoch):
            raise PermissionError("Claude worker capability rejected")
    elif not session or not store.check_writer(mission, session, epoch):
        raise PermissionError("stale worker epoch")
    return epoch


def run(args: argparse.Namespace) -> object:
    if args.command == "help":
        return _parser().format_help()
    home = home_path(args.home)
    if args.command == "hook":
        from gcl.observe import handle, record_error, disabled
        if disabled(home):
            return {}
        payload = {}
        try:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("hook payload must be an object")
            return handle(args.executor, args.event, payload, home)
        except Exception as exc:
            record_error(home, args.executor, args.event, payload, exc)
            return {}
    if args.command == "disable":
        home.mkdir(parents=True, exist_ok=True)
        (home / "OFF").touch(exist_ok=True)
        return {"enabled": False, "switch": str(home / "OFF")}
    if args.command == "enable":
        (home / "OFF").unlink(missing_ok=True)
        return {"enabled": True, "switch": str(home / "OFF")}
    inbox_value = os.environ.get("RLH_WORKER_INBOX")
    if inbox_value and args.command in {"note", "done", "evidence", "effect", "job"}:
        # A sandboxed worker can write only inside its disposable workspace.
        # The supervising process validates and applies this request to the ledger.
        inbox = Path(inbox_value).resolve()
        if not inbox.is_dir():
            raise ValueError("worker inbox is unavailable")
        session = args.session or os.environ.get("GCL_SESSION_ID")
        epoch = args.epoch if args.epoch is not None else int(os.environ["GCL_EPOCH"])
        if not session or epoch < 1:
            raise ValueError("worker identity is required")
        fields = {key: value for key, value in vars(args).items()
                  if key not in {"home", "json", "command", "mission", "session", "epoch"}}
        payload = {"command": args.command, "mission": args.mission,
                   "session_id": session, "epoch": epoch, "fields": fields}
        name = uuid.uuid4().hex
        temporary = inbox / f"{name}.new"
        ready = inbox / f"{name}.json"
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temporary, ready)
        return {"queued": name}
    if args.command == "start":
        if args.executor == "claude" and importlib.util.find_spec("gcl.adapters.claude_bg") is None:
            raise ValueError("Claude adapter is not installed")
        cwd = Path(args.cwd).resolve()
        if not cwd.is_dir():
            raise ValueError("cwd does not exist")
        subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, check=True)
        text = " ".join(args.owner_text).strip()
        if text.startswith("-- "):
            text = text[3:]
        if not text:
            raise ValueError("owner text is required")
        if args.executor == "claude" and not _claude_trusted(cwd):
            raise ValueError(f"Claude workspace is not trusted at exact cwd {cwd}; accept trust in Claude Code first")
        policy = _policy([part for value in args.policy for part in value.split(",")])
        misplaced = set(policy) - set(Policy.__dataclass_fields__) - {"hard_ratio", "max_turn_s"}
        if misplaced:
            raise ValueError(f"use --runtime-policy for {', '.join(sorted(misplaced))}")
        if args.context_window is not None and args.context_window <= 0:
            raise ValueError("--context-window must be positive")
        runtime_policy = _policy([part for value in args.runtime_policy for part in value.split(",")])
        if set(runtime_policy) - _RUNTIME_KEYS:
            raise ValueError("unsupported --runtime-policy key")
        if "await_writer_s" in runtime_policy and runtime_policy["await_writer_s"] <= 0:
            raise ValueError("await_writer_s must be positive")
        runtime_policy.update({key: policy.pop(key) for key in ("hard_ratio", "max_turn_s") if key in policy})
        store = LocalStore(home / "state.sqlite")
        beacon = {"owner": "human:owner", "owner_instruction": text, "objective": text,
                  "done_when": args.done_when, "non_goals": args.non_goal,
                  "constraints": args.constraint, "route_boundaries": args.boundary,
                  "acceptance": args.acceptance, "decision_refs": [],
                  "assumptions": ["ASSUMPTION: " + x for x in ("done_when", "non_goals", "constraints",
                     "route_boundaries", "acceptance") if not getattr(args, {"done_when": "done_when",
                     "non_goals": "non_goal", "constraints": "constraint", "route_boundaries": "boundary",
                     "acceptance": "acceptance"}[x])]}
        mid = store.create_mission(beacon, "human:owner", args.mode)
        store.note_event(mid, "runtime.config", {"executor": args.executor, "cwd": str(cwd),
                         "endpoint": args.codex_endpoint, "codex_sandbox": args.codex_sandbox,
                         "approval_policy": args.approval_policy,
                         "permission_mode": args.permission_mode, "allowed_tools": args.allowed_tools,
                         "model": args.model, "context_window": args.context_window,
                         "claude_settings": args.claude_settings,
                         "done_check": args.done_check, "self_complete": args.allow_self_complete,
                         "policy": policy, "runtime_policy": runtime_policy})
        pid = _start_supervisor(home, mid) if args.mode != "OFF" else None
        return {"mission": mid, "supervisor_pid": pid}
    store = LocalStore(home / "state.sqlite")
    if args.command == "list":
        return [_status(store, home, mid) for mid in _missions(store)]
    mission = args.mission
    if args.command == "status":
        if mission:
            return _status(store, home, mission)
        return {"enabled": not (home / "OFF").exists() and os.environ.get("GCL_MODE") != "OFF",
                "missions": [_status(store, home, mid) for mid in _missions(store)],
                "hook_errors": (home / "native" / "hook-errors.jsonl").exists()}
    if args.command == "adopt":
        events = store.events(mission)
        config = next((e["data"] for e in events if e["type"] == "runtime.config"), {})
        executor = config.get("executor")
        native = config.get("native", False)
        live, reason = live_controller(mission_dir(home, mission))
        epoch, sid = store.current_writer(mission)
        controlled_session = any(e["type"] == "runtime.session_created" and
                                 e["data"].get("session_id") == sid for e in events)
        if native:
            unavailable = ("native Claude session is not owned by an rlh --bg supervisor" if executor == "claude"
                           else "Codex daemon-control package is not approved and attached")
        elif not sid or epoch < 1 or not controlled_session:
            unavailable = "no supervisor-owned worker session is recorded"
        elif executor == "claude" and not live:
            unavailable = "Claude background supervisor unavailable: " + reason
        elif executor == "codex" and config.get("endpoint") == "own" and not live:
            unavailable = "Codex app-server controller unavailable: " + reason
        elif executor == "codex" and config.get("endpoint") == "daemon":
            unavailable = "Codex daemon-control package is not approved and attached"
        elif executor not in ("claude", "codex"):
            unavailable = "no supported controller owns this worker"
        else:
            unavailable = None
        if unavailable:
            return {"mission": mission, "mode": store.get_mode(mission),
                    "available": False, "reason": "CONTROLLED_AUTO unavailable: " + unavailable}
        store.set_mode(mission, "CONTROLLED_AUTO", "human:owner")
        return {"mission": mission, "mode": "CONTROLLED_AUTO", "available": True}
    if args.command == "send":
        return {"seq": store.post_owner_message(mission, args.text, "human:owner")}
    if args.command == "cancel":
        store.cancel(mission, "human:owner")
        events = store.events(mission)
        config = next((e["data"] for e in events if e["type"] == "runtime.config"), {})
        stop = None
        if config.get("executor") == "claude":
            _, sid = store.current_writer(mission)
            if sid:
                from gcl.adapters.claude_bg import request_worker_stop
                stop = request_worker_stop(home, mission, sid)
        return {"cancelled": mission, "worker_stop": stop}
    if args.command == "mode":
        store.set_mode(mission, args.value, "human:owner")
        stop = None
        if args.value == "OFF" and args.stop_worker:
            events = store.events(mission)
            config = next((e["data"] for e in events if e["type"] == "runtime.config"), {})
            if config.get("executor") == "claude":
                _, sid = store.current_writer(mission)
                if sid:
                    from gcl.adapters.claude_bg import request_worker_stop
                    stop = request_worker_stop(home, mission, sid)
        if args.value != "OFF" and not live_controller(mission_dir(home, mission))[0]:
            _start_supervisor(home, mission)
        return {"mode": args.value, "worker_stop": stop}
    if args.command == "confirm":
        if os.environ.get("GCL_MANAGED") or os.environ.get("GCL_SESSION_TOKEN"):
            raise PermissionError("only the owner confirms completion, not a worker session")
        store.confirm_completion(mission, "human:owner", time.time())
        return {"confirmed": mission}
    if args.command == "resume":
        if store.get_mode(mission) == "OFF":
            raise ValueError("mission is OFF")
        live, _ = live_controller(mission_dir(home, mission))
        return {"supervisor_pid": None if live else _start_supervisor(home, mission), "already_running": live}
    if args.command == "rollover":
        request_id = uuid.uuid4().hex
        store.note_event(mission, "runtime.manual_request", {"request_id": request_id, "reason": args.reason})
        return {"request_id": request_id}
    if args.command == "attach":
        events = store.events(mission)
        config = next(e["data"] for e in events if e["type"] == "runtime.config")
        _, sid = store.current_writer(mission)
        if config["executor"] == "claude":
            binary = os.environ.get("GCL_CLAUDE_BIN", "claude")
            def background_id(current: str) -> str:
                agents = json.loads(subprocess.run([binary, "agents", "--json"], cwd=config["cwd"],
                                   check=True, capture_output=True, text=True).stdout)
                agents = agents if isinstance(agents, list) else agents.get("agents", [])
                return next((a["id"] for a in agents if a.get("sessionId") == current), current)
            if args.follow or (sys.stdin.isatty() and not args.json):
                while sid:
                    subprocess.run([binary, "attach", background_id(sid)], cwd=config["cwd"], check=True)
                    while store.current_writer(mission)[1] == sid:
                        kinds = {e["type"] for e in store.events(mission)}
                        if "mission.completed" in kinds or "mission.cancelled" in kinds:
                            return {"session_id": sid, "finished": True}
                        time.sleep(.2)
                    sid = store.current_writer(mission)[1]
                return {"session_id": None, "finished": True}
            return {"session_id": sid, "instruction": f"{binary} attach {background_id(sid)}"}
        return {"session_id": sid, "instruction": ("codex --remote " + config.get("endpoint", "own") +
                 " resume " + sid) if config.get("endpoint") == "own" else "Thread visible in Codex Desktop"}
    if args.command == "tail":
        seen = 0
        current_epoch, current_session = 0, None
        while True:
            events = store.events(mission)
            for e in events[seen:]:
                data = e["data"]
                if e["type"] in ("writer.activated", "writer.transferred"):
                    current_epoch, current_session = data["epoch"], data["session_id"]
                epoch = data.get("epoch", current_epoch)
                sid = data.get("session_id", current_session)
                if args.json:
                    print(json.dumps({"epoch": epoch, "session_id": sid, "type": e["type"],
                                      "data": data}, ensure_ascii=False), flush=True)
                else:
                    print(f"[epoch {epoch} · session {sid}] {e['type']} {json.dumps(data, ensure_ascii=False)}", flush=True)
            seen = len(events)
            if not args.follow:
                break
            time.sleep(1)
        return None
    if args.command == "delivery":
        store.resolve_owner_delivery(mission, args.delivery_id, accepted=args.accepted,
                                     evidence=args.evidence, turn_id=args.turn_id)
        return {"delivery_id": args.delivery_id, "state": "ACKED" if args.accepted else "PENDING"}
    if args.command == "goal":
        if args.goal_command == "show":
            return store.get_beacon(mission)
        if args.field == "owner":
            raise ValueError("owner cannot be changed from the CLI; later changes would be unauthorized")
        return {"version": store.change_goal(mission, {args.field: json.loads(args.value)},
                  "human:owner", args.authority_ref)}
    if args.command in ("verified", "await-writer"):
        from gcl.runtime.capability import verify_prepared_worker
        session = args.session or os.environ.get("GCL_SESSION_ID")
        epoch = args.epoch if args.epoch is not None else int(os.environ["GCL_EPOCH"])
        if not session or not verify_prepared_worker(store, mission, session, epoch):
            raise PermissionError("Claude successor capability rejected")
        if args.command == "verified":
            with store._read() as db:
                row = db.execute("SELECT id FROM rollovers WHERE mission_id=? AND epoch=? AND state='CHECKPOINT_READY' "
                                 "ORDER BY rowid DESC LIMIT 1", (mission, epoch - 1)).fetchone()
            if not row or not any(e["type"] == "runtime.successor_created" and
                                  e["data"].get("rollover_id") == row["id"] and
                                  e["data"].get("session_id") == session for e in store.events(mission)):
                raise PermissionError("successor is not prepared")
            store.note_event(mission, "runtime.worker_verified", {"rollover_id": row["id"],
                             "session_id": session, "epoch": epoch, "summary": args.summary, "at": time.time()})
            return "VERIFIED: await writer ownership"
        if not any(e["type"] == "runtime.worker_verified" and
                   e["data"].get("session_id") == session and e["data"].get("epoch") == epoch
                   for e in store.events(mission)):
            raise PermissionError("checkpoint verification has not been reported")
        config = next(e["data"] for e in store.events(mission) if e["type"] == "runtime.config")
        deadline = time.monotonic() + float(config.get("runtime_policy", {}).get("await_writer_s", 300))
        while time.monotonic() < deadline:
            with store._read() as db:
                cancelled = db.execute("SELECT cancelled FROM missions WHERE id=?", (mission,)).fetchone()[0]
                rows = db.execute("SELECT state FROM rollovers WHERE mission_id=? AND epoch=? "
                                  "ORDER BY rowid DESC LIMIT 1", (mission, epoch - 1)).fetchone()
            if cancelled or not rows or rows["state"] == "ABORTED":
                return "NOT GRANTED: stop now"
            if store.check_writer(mission, session, epoch):
                messages = store.consume_owner_messages(mission, session, epoch)
                if messages:
                    store.note_event(mission, "runtime.owner_delivered", {"epoch": epoch,
                                     "seq": messages[-1]["seq"], "via": "await-writer"})
                return "WRITER GRANTED\nOwner messages: " + json.dumps(messages, ensure_ascii=False)
            time.sleep(.05)
        return "NOT GRANTED: stop now"
    if args.command == "note":
        from gcl.runtime.capability import verify_prepared_worker
        session = args.session or os.environ.get("GCL_SESSION_ID")
        epoch = args.epoch if args.epoch is not None else int(os.environ["GCL_EPOCH"])
        if session and not store.check_writer(mission, session, epoch):
            with store._read() as db:
                row = db.execute("SELECT id FROM rollovers WHERE mission_id=? AND epoch=? AND "
                                 "state='CHECKPOINT_READY' ORDER BY rowid DESC LIMIT 1",
                                 (mission, epoch - 1)).fetchone()
            if row and verify_prepared_worker(store, mission, session, epoch) and any(
                e["type"] == "runtime.successor_created" and e["data"].get("rollover_id") == row["id"] and
                e["data"].get("session_id") == session for e in store.events(mission)):
                store.note_event(mission, "runtime.note", {"epoch": epoch, "session_id": session,
                                 "text": args.text, "cwd": os.getcwd()})
                return {"ok": True}
    epoch = _worker(store, mission, args.session, args.epoch)
    if args.command == "note":
        store.note_event(mission, "runtime.note", {"epoch": epoch, "text": args.text, "cwd": os.getcwd()})
    elif args.command == "done":
        store.note_event(mission, "runtime.worker_done", {"epoch": epoch, "session_id": args.session or os.environ.get("GCL_SESSION_ID")})
        config = store.first_event(mission, "runtime.config") or {}
        if not config.get("done_check") and not config.get("self_complete"):
            return {"ok": True, "completion": "reported; the owner confirms it, so stop and wait"}
    elif args.command == "evidence":
        path = Path(args.path).resolve()
        store.note_event(mission, "runtime.evidence", {"epoch": epoch, "claim": args.claim,
             "evidence_path": str(path), "sha256": sha256_regular(path),
             "observed_at": time.time(), "cwd": os.getcwd()})
    elif args.command == "effect":
        store.record_effect(mission, args.key, "worker", epoch)
        state = {"begin": "IN_FLIGHT", "confirm": "CONFIRMED_DONE", "fail": "FAILED"}[args.action]
        store.mark_effect(args.key, state, epoch, json.loads(args.receipt) if args.receipt else None)
    elif args.command == "job":
        if args.uninterruptible and (not args.pid or not process_start(args.pid)):
            raise ValueError("uninterruptible job requires a live --pid")
        store.note_event(mission, "runtime.job", {"epoch": epoch, "id": args.id, "cmd": args.cmd,
                         "log": args.log, "pid": args.pid, "process_start": process_start(args.pid) if args.pid else None,
                         "uninterruptible": args.uninterruptible, "cwd": os.getcwd()})
    return {"ok": True}


def main() -> int:
    p = _parser()
    argv = sys.argv[1:]
    if "--json" in argv:
        argv = ["--json"] + [x for x in argv if x != "--json"]
    args = p.parse_args(argv)
    try:
        result = run(args)
        if result is not None:
            _emit(result, args.json)
        return 0
    except (ValueError, KeyError, PermissionError, OSError, subprocess.CalledProcessError,
            GCLError, AppServerError) as exc:
        _emit({"error": str(exc)}, True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

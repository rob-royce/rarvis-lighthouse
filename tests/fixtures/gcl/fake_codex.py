#!/usr/bin/env python3
"""Offline Codex app-server subset used only by GCL runtime tests."""
import json
import os
from pathlib import Path
import sys
import select
import re
import subprocess
import time
import uuid


state_path = Path(os.environ["GCL_FAKE_STATE"])
state_path.parent.mkdir(parents=True, exist_ok=True)


def load():
    return json.loads(state_path.read_text()) if state_path.exists() else {"threads": {}, "calls": [], "starts": 0}


def save(value):
    temporary = state_path.with_suffix(".new")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, state_path)


def emit(value):
    params = value.get("params") or {}
    thread = data.get("threads", {}).get(params.get("threadId"))
    if thread is not None:
        turn = params.get("turn") or {}
        if value.get("method") == "turn/started":
            thread["running_turn"] = dict(turn, status="inProgress")
        elif value.get("method") == "turn/completed" and (thread.get("running_turn") or {}).get("id") == turn.get("id"):
            thread.pop("running_turn", None)
    print(json.dumps(value, separators=(",", ":")), flush=True)


def policy_receipt(thread, switch):
    """Override wire evidence independently of the last turn's sandbox."""
    value = os.environ.get(switch)
    if value is None:
        return {"thread": thread, "sandbox": thread["sandbox"],
                "approvalPolicy": thread["approvalPolicy"]}
    policy = json.loads(value)
    if isinstance(policy, list):
        index = thread.get("policy_receipts", 0)
        thread["policy_receipts"] = index + 1
        policy = policy[min(index, len(policy) - 1)]
    clean = {k: v for k, v in thread.items() if k not in ("sandbox", "approvalPolicy", "sandboxPolicy")}
    return {"thread": clean, **(policy or {})}


def echoed_receipt(thread):
    """Like Codex, report the sandbox of the latest turn override on thread/resume."""
    sandbox = dict(thread["sandboxPolicy"])
    if sandbox.get("type") == "readOnly":
        sandbox.update(json.loads(os.environ.get("GCL_FAKE_READONLY_ECHO_EXTRA", "{}")))
    return {"thread": thread, "sandbox": sandbox, "approvalPolicy": thread["approvalPolicy"]}


def auto_turn(data, sid, on_resume=False):
    thread = data["threads"][sid]
    if thread.get("goal") != "active":
        return
    tid = str(uuid.uuid4())
    policy = thread["sandboxPolicy"]
    data.setdefault("auto_turns", []).append({"threadId": sid, "sandboxPolicy": policy,
                                                "turnId": tid, "origin": "vendor",
                                                "call_index": len(data["calls"]) - 1})
    emit({"method": "turn/started", "params": {"threadId": sid,
          "turn": {"id": tid, "origin": "vendor", "sandboxPolicy": policy}}})
    if on_resume:
        pending[sid] = (tid, float("inf"))
        return
    delay = float(os.environ.get("GCL_FAKE_AUTO_TURN_DELAY", "0"))
    if delay:
        pending[sid] = (tid, time.monotonic() + delay)
        return
    if policy["type"] == "readOnly":
        data.setdefault("blocked_auto_turns", []).append(tid)
        emit({"method": "thread/goal/updated", "params": {"threadId": sid,
              "goal": {"status": "blocked"}}})
    emit({"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": tid}}})


pending = {}
final_pending = {}
work = {}
server_pending = {}
while True:
    ready, _, _ = select.select([sys.stdin], [], [], .05)
    if not ready:
        data = load()
        for sid, steps in list(work.items()):
            if steps and time.monotonic() >= steps[0][0]:
                _, action = steps.pop(0)
                repo = Path(data["threads"][sid]["cwd"])
                if action in ("commit", "commit_dirty"):
                    step = len(data.get("work_steps_done", [])) + 1
                    (repo / "README.md").write_text(f"step {step}\n")
                    if action == "commit_dirty":
                        # Pending work exists before and after this commit. Writing it only after
                        # the commit left a clean worktree that a supervisor tick could observe.
                        (repo / "pending.txt").write_text(f"unfinished step {step}\n")
                        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
                    else:
                        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
                    subprocess.run(["git", "commit", "-qm", "fake long-turn step"], cwd=repo, check=True)
                    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                          capture_output=True, text=True).stdout.strip()
                    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                                check=True, capture_output=True, text=True).stdout)
                    data.setdefault("work_commits", []).append({"step": step, "head": head,
                                                                  "dirty": dirty,
                                                                  "interrupts": sum(call["method"] == "turn/interrupt"
                                                                                    for call in data["calls"])})
                data.setdefault("work_steps_done", []).append(action)
                if not steps:
                    del work[sid]
                save(data)
        for sid, (tid, package, due) in list(final_pending.items()):
            if time.monotonic() >= due:
                emit({"method": "item/completed", "params": {"threadId": sid,
                      "item": {"type": "agentMessage", "text": json.dumps(package)}}})
                emit({"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": tid}}})
                del final_pending[sid]
        for sid, (tid, due) in list(pending.items()):
            if time.monotonic() >= due or (state_path.parent / "release-turn").exists():
                emit({"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": tid}}})
                del pending[sid]
                if os.environ.get("GCL_FAKE_DONE_KEEPS_RUNNING") != "1":
                    auto_turn(data, sid)
                save(data)
        continue
    line = sys.stdin.buffer.raw.readline().decode()
    if not line:
        break
    request = json.loads(line)
    if "id" not in request:
        continue
    if "method" not in request:
        data = load()
        data.setdefault("server_replies", []).append(request)
        waiting = server_pending.pop(request["id"], None)
        if waiting and waiting not in server_pending.values():
            sid, tid, call_id = waiting
            emit({"id": call_id, "result": {"turn": {"id": tid}}})
            emit({"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": tid}}})
        save(data)
        continue
    method = request["method"]
    params = request.get("params") or {}
    data = load()
    data["server_pid"] = os.getpid()
    data["calls"].append({"method": method, "params": params})
    error = None
    result = {}
    if method == "initialize":
        if os.environ.get("GCL_FAKE_CONFIRM_CHECK"):
            data.setdefault("server_pids", []).append(os.getpid())
        result = {"userAgent": "fake"}
    elif method == "thread/start":
        data["starts"] += 1
        fail = int(os.environ.get("GCL_FAKE_FAIL_SUCCESSOR", "0"))
        if data["starts"] > 1 and data["starts"] <= 1 + fail:
            error = {"message": "simulated thread start failure"}
        else:
            sid = str(uuid.uuid4())
            rollout = state_path.parent / (sid + ".jsonl")
            rollout.write_text(json.dumps({"payload": {"type": "session_meta", "id": sid,
                                            "session_id": sid}}) + "\n")
            sandbox = params.get("sandbox", "danger-full-access")
            policy = {"read-only": {"type": "readOnly"},
                      "workspace-write": {"type": "workspaceWrite", "writableRoots": [params["cwd"]],
                                          "networkAccess": False, "excludeTmpdirEnvVar": False,
                                          "excludeSlashTmp": False},
                      "danger-full-access": {"type": "dangerFullAccess"}}[sandbox]
            approval = params.get("approvalPolicy", "on-request")
            thread = {"id": sid, "sessionId": sid, "path": str(rollout), "cwd": params["cwd"],
                      "sandboxPolicy": policy, "approvalPolicy": approval, "sandbox": sandbox,
                      "goal": None}
            data["threads"][sid] = thread
            result = {"thread": thread, "sandbox": policy if os.environ.get("GCL_FAKE_STRUCTURED_SANDBOX")
                      else sandbox, "approvalPolicy": approval}
            switch = "GCL_FAKE_INITIAL_POLICY" if data["starts"] == 1 else "GCL_FAKE_SUCCESSOR_POLICY"
            if switch in os.environ:
                result = policy_receipt(thread, switch)
                thread["sandbox"] = result.get("sandbox", sandbox)
                thread["approvalPolicy"] = result.get("approvalPolicy", approval)
                if os.environ.get("GCL_FAKE_RESUME_ECHOES_TURN") == "1" and isinstance(thread["sandbox"], dict):
                    thread["sandboxPolicy"] = dict(thread["sandbox"])
    elif method == "thread/resume":
        sid = params["threadId"]
        thread = data["threads"][sid]
        if os.environ.get("GCL_FAKE_RESUME_AUTO_TURN") == "1":
            auto_turn(data, sid, on_resume=True)
        switch = ("GCL_FAKE_RESUMED_SUCCESSOR_POLICY" if sid != next(iter(data["threads"]))
                  else "GCL_FAKE_RESUMED_INITIAL_POLICY")
        # Advance receipt sequences on stored state before copying the wire thread.
        if os.environ.get("GCL_FAKE_RESUME_ECHOES_TURN") == "1" and switch not in os.environ:
            result = echoed_receipt(thread)
        else:
            result = policy_receipt(thread, switch)
        result["thread"] = dict(result["thread"],
                                turns=[thread["running_turn"]] if thread.get("running_turn") else [])
        if os.environ.get("GCL_FAKE_CONFIRM_CHECK"):
            # A fresh connection has no native completion signal: the ledger must hold it.
            result["thread"].pop("goal", None)
    elif method == "turn/start":
        sid = params["threadId"]
        thread = data["threads"][sid]
        if "outputSchema" in params and sid in pending:
            error = {"message": "failed to submit turn input: ActiveTurnOutputSchemaMismatch"}
            save(data)
            emit({"id": request["id"], "error": error})
            continue
        thread["sandboxPolicy"] = params.get("sandboxPolicy", thread["sandboxPolicy"])
        thread["approvalPolicy"] = params.get("approvalPolicy", thread["approvalPolicy"])
        tid = str(uuid.uuid4())
        result = {"turn": {"id": tid}}
        if "outputSchema" in params:
            if os.environ.get("GCL_FAKE_FINAL_TURN_ERROR"):
                error = {"message": os.environ["GCL_FAKE_FINAL_TURN_ERROR"]}
                save(data)
                emit({"id": request["id"], "error": error})
                continue
            if os.environ.get("GCL_FAKE_FINAL_COMPLETED_ERROR"):
                emit({"method": "turn/completed", "params": {"threadId": sid,
                      "turn": {"id": tid, "error": os.environ["GCL_FAKE_FINAL_COMPLETED_ERROR"]}}})
                save(data)
                emit({"id": request["id"], "result": result})
                continue
            packages = json.loads(os.environ.get("GCL_FAKE_RESULT_PACKAGES", "[]"))
            final_turns = sum("outputSchema" in call["params"] for call in data["calls"]
                              if call["method"] == "turn/start")
            package = packages[min(final_turns - 1, len(packages) - 1)] if packages else json.loads(
                os.environ["GCL_FAKE_RESULT_PACKAGE"])
            final_delay = float(os.environ.get("GCL_FAKE_FINAL_TURN_DELAY", "0"))
            if final_delay:
                final_pending[sid] = (tid, package, time.monotonic() + final_delay)
                save(data)
                emit({"id": request["id"], "result": result})
                continue
            emit({"method": "item/completed", "params": {"threadId": sid,
                  "item": {"type": "agentMessage", "text": json.dumps(package)}}})
            emit({"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": tid}}})
            save(data)
            emit({"id": request["id"], "result": result})
            continue
        if thread["sandboxPolicy"]["type"] == "readOnly" and os.environ.get("GCL_FAKE_MUTATE_READONLY") == "1":
            (Path(data["threads"][sid]["cwd"]) / "mutated-by-fake.txt").write_text("bad\n")
        if os.environ.get("GCL_FAKE_MAC_CHECK") and thread["sandboxPolicy"]["type"] != "readOnly":
            prompt = params["input"][0]["text"]
            fence = re.search(r"done ([0-9a-f]+) --session ([0-9a-f-]+) --epoch ([12])", prompt)
            if fence:
                mid, worker, epoch = fence.groups()
                repo = Path(thread["cwd"])
                word = "one" if epoch == "1" else "two"
                proof = repo / f"phase{epoch}.txt"
                proof.write_text(f"phase-{word}\n")
                argv = [sys.executable, "-B", "-m", "gcl.cli"]
                identity = [mid, "--session", worker, "--epoch", epoch]
                subprocess.run([*argv, "evidence", *identity, "--claim", f"phase-{word}",
                                "--path", str(proof)], check=True, capture_output=True)
                if epoch == "2":
                    subprocess.run([*argv, "done", *identity], check=True, capture_output=True)
        if os.environ.get("GCL_FAKE_CONFIRM_CHECK") and thread["sandboxPolicy"]["type"] != "readOnly":
            prompt = params["input"][0]["text"]
            fence = re.search(r"done ([0-9a-f]+) --session ([0-9a-f-]+) --epoch ([0-9]+)", prompt)
            if fence:
                thread["confirm_worker"] = list(fence.groups())
            phase = 2 if "Pending owner messages:" in prompt and "phase2.txt" in prompt else 1
            if thread.get("confirm_worker") and phase not in thread.setdefault("confirm_phases", []):
                proof = Path(thread["cwd"]) / f"phase{phase}.txt"
                proof.write_text("phase-one\n" if phase == 1 else "phase-two\n")
                mid, worker, epoch = thread["confirm_worker"]
                subprocess.run([sys.executable, "-B", "-m", "gcl.cli", "done", mid,
                                "--session", worker, "--epoch", epoch], check=True, capture_output=True)
                thread["confirm_phases"].append(phase)
        emit({"method": "turn/started", "params": {"threadId": sid, "turn": {"id": tid}}})
        requests = json.loads(os.environ.get("GCL_FAKE_SERVER_REQUESTS", "[]"))
        if requests and not data.get("server_requests_sent"):
            data["server_requests_sent"] = True
            for index, name in enumerate(requests):
                # The first request deliberately collides with our client's outstanding call.
                server_id = request["id"] if index == 0 else f"server-{index}"
                server_pending[server_id] = (sid, tid, request["id"])
                emit({"id": server_id, "method": name, "params": {
                    "threadId": sid, "command": "synthetic command", "secret": "opaque-test-value"}})
            save(data)
            continue  # Do not complete the turn until every server request has a reply.
        emit({"method": "thread/tokenUsage/updated", "params": {"threadId": sid,
              "tokenUsage": {"total": {"totalTokens": 90}, "last": {"totalTokens": 90},
                             "modelContextWindow": 100}}})
        turn = {"id": tid}
        if os.environ.get("GCL_FAKE_TURN_ERROR") and thread["sandboxPolicy"]["type"] != "readOnly":
            turn["error"] = os.environ["GCL_FAKE_TURN_ERROR"]
        first_turn = sum(c["method"] == "turn/start" and c["params"].get("threadId") == sid
                         for c in data["calls"]) == 1
        delay = float(os.environ.get("GCL_FAKE_TURN_DELAY", "0")) if data["starts"] == 1 and first_turn else 0
        if os.environ.get("GCL_FAKE_DONE_KEEPS_RUNNING") == "1" and thread["sandboxPolicy"]["type"] != "readOnly":
            pending[sid] = (tid, float("inf"))
        elif (thread["sandboxPolicy"]["type"] == "readOnly" and
              data.get("readonly_stalls", 0) < int(os.environ.get("GCL_FAKE_READONLY_STALLS", "0"))):
            # A stalled vendor stream: the read-only turn ends only when interrupted.
            data["readonly_stalls"] = data.get("readonly_stalls", 0) + 1
            pending[sid] = (tid, float("inf"))
        elif delay and thread["sandboxPolicy"]["type"] != "readOnly":
            pending[sid] = (tid, time.monotonic() + delay)
            if data["starts"] == 1 and os.environ.get("GCL_FAKE_WORK_STEPS"):
                actions = json.loads(os.environ["GCL_FAKE_WORK_STEPS"])
                interval = float(os.environ.get("GCL_FAKE_WORK_INTERVAL", ".5"))
                final_delay = float(os.environ.get("GCL_FAKE_WORK_FINAL_DELAY", "0"))
                work[sid] = [(time.monotonic() + interval * (index + 1) +
                              (final_delay if index == len(actions) - 1 else 0), action)
                             for index, action in enumerate(actions)]
        else:
            emit({"method": "turn/completed", "params": {"threadId": sid, "turn": turn}})
            auto_turn(data, sid)
    elif method == "thread/goal/set":
        sid = params["threadId"]
        data["threads"][sid]["goal"] = params["status"]
        emit({"method": "thread/goal/updated", "params": {"threadId": sid,
              "goal": {"status": params["status"]}}})
        if params["status"] == "active" and sid not in pending:
            auto_turn(data, sid)
    elif method == "turn/interrupt":
        sid = params["threadId"]
        data.setdefault("work_steps_cancelled", {})[sid] = len(work.pop(sid, []))
        tid = params["turnId"]
        if sid in pending or (data["threads"][sid].get("running_turn") or {}).get("id") == tid:
            pending.pop(sid, None)
            if os.environ.get("GCL_FAKE_MUTATE_ON_INTERRUPT") == "1":
                (Path(data["threads"][sid]["cwd"]) / "late.txt").write_text("late change\n")
            emit({"method": "turn/completed", "params": {"threadId": sid, "turn": {"id": tid}}})
            if not (os.environ.get("GCL_FAKE_DONE_KEEPS_RUNNING") or os.environ.get("GCL_FAKE_RESUME_AUTO_TURN")):
                auto_turn(data, sid)
    elif method == "thread/unsubscribe":
        pass
    else:
        error = {"message": "unknown method " + method}
    save(data)
    emit({"id": request["id"], "error": error} if error else {"id": request["id"], "result": result})

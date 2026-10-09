#!/usr/bin/env python3
"""Offline Claude --bg/agents/stop stub with actual session-scoped hook invocations."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


state_path = Path(os.environ["GCL_FAKE_CLAUDE_STATE"])
state_path.parent.mkdir(parents=True, exist_ok=True)
lock = open(str(state_path) + ".lock", "a+")


def load():
    return json.loads(state_path.read_text()) if state_path.exists() else {"agents": [], "calls": []}


def save(value):
    temporary = state_path.with_suffix(".new")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, state_path)


def invoke(settings, event, payload):
    configuration = json.loads(Path(settings).read_text())
    hooks = configuration["hooks"][event]
    command = hooks[0]["hooks"][0]["command"]
    result = subprocess.run(command, shell=True, input=json.dumps(payload), text=True,
                            capture_output=True, env=dict(os.environ, **configuration.get("env", {})), timeout=5)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def finish_transcript(agent):
    with open(agent["transcript_path"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "Turn complete."}], "stop_reason": "end_turn"}}) + "\n")


def launch_prompt(args):
    """Model Commander's variadic --allowedTools consumption of following values."""
    positionals = []
    options_with_value = {"--session-id", "--settings", "-n", "--permission-mode"}
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--":
            positionals.extend(args[index + 1:])
            break
        if value in options_with_value:
            index += 2
            continue
        if value == "--allowedTools":
            index += 1
            while index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if value.startswith("--allowedTools=") or value == "--bg":
            index += 1
            continue
        if value.startswith("-"):
            raise RuntimeError(f"unsupported launch option: {value}")
        positionals.append(value)
        index += 1
    return " ".join(positionals)


argv = sys.argv[1:]
fcntl.flock(lock, fcntl.LOCK_EX)
state = load()
state["calls"].append(argv)
if argv == ["agents", "--json"]:
    print(json.dumps(state["agents"]))
elif argv and argv[0] == "stop":
    target = argv[1]
    for agent in state["agents"]:
        if target in (agent["id"], agent["sessionId"]):
            agent.update(state="stopped", status="waiting")
            break
    else:
        raise RuntimeError("unknown agent")
elif argv and argv[0] == "attach":
    if argv[1] not in [value for a in state["agents"] for value in (a["id"], a["sessionId"])]:
        raise RuntimeError("unknown session")
elif argv and argv[0] == "--bg" and "--resume" in argv:
    sid = argv[argv.index("--resume") + 1]
    agent = next(a for a in state["agents"] if a["sessionId"] == sid)
    number = len(state["agents"]) + 1
    actual = dict(agent, id=f"bg-{number}", sessionId=f"session-{number}",
                  settings=None, state="working", status="busy", source="fork")
    state["agents"].append(actual)
    save(state)
    print(f"backgrounded · {actual['id']} · fork")
elif argv and argv[0] == "--bg":
    number = len(state["agents"]) + 1
    sid, short = f"session-{number}", f"bg-{number}"
    settings = argv[argv.index("--settings") + 1]
    title = argv[argv.index("-n") + 1]
    transcript = state_path.parent / f"transcript-{number}.jsonl"
    transcript.write_text("")
    agent = {"id": short, "sessionId": sid, "name": title, "cwd": os.getcwd(),
             "state": "blocked", "status": "waiting", "pid": os.getpid(), "kind": "background",
             "settings": settings, "transcript_path": str(transcript), "prompt": launch_prompt(argv)}
    state["agents"].append(agent)
    save(state)
    payload = {"session_id": sid, "transcript_path": str(transcript), "cwd": os.getcwd(),
               "source": "startup", "model": "claude-opus", "session_title": title}
    invoke(settings, "SessionStart", payload)
    if json.loads(Path(settings).parent.joinpath("launch.json").read_text())["epoch"] > 1:
        subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_claude_successor.py")),
                          settings, sid, str(transcript)], cwd=os.getcwd(),
                         env=os.environ, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        finish_transcript(agent)
        invoke(settings, "Stop", payload)
    print(f"backgrounded · {short} · {title}")
else:
    raise RuntimeError(f"unsupported fake Claude call: {argv}")
save(state)

"""Paths and process identity for one isolated GCL home."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def home_path(value: str | None = None) -> Path:
    """Resolve --home > RLH_HOME > GCL_HOME > default without migrating data.

    Empty values are treated as unset, as with the legacy --home option.
    The compatibility notice is emitted once per resolution, only on fallback.
    """
    explicit = value or os.environ.get("RLH_HOME") or os.environ.get("GCL_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = Path("~/.rarvis-lighthouse").expanduser()
    if Path("~/.gcl").expanduser().exists() and not home.exists():
        print("Lighthouse now defaults to ~/.rarvis-lighthouse. Existing ~/.gcl was not "
              "moved; set RLH_HOME=~/.gcl to keep using it.", file=sys.stderr)
    return home.resolve()


def mission_dir(home: Path, mission: str) -> Path:
    if not mission or any(c not in "0123456789abcdef" for c in mission):
        raise ValueError("invalid mission id")
    return home / "missions" / mission


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.new")
    # Owner-only: session settings carry the worker capability token.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")
    os.replace(temporary, path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def process_start(pid: int) -> str | None:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def identity() -> dict:
    pid = os.getpid()
    argv = "\0".join(os.sys.argv)
    return {"pid": pid, "process_start": process_start(pid),
            "argv_sha256": hashlib.sha256(argv.encode()).hexdigest(), "started_at": time.time()}


def live_controller(directory: Path, max_age: float = 20) -> tuple[bool, str]:
    info = read_json(directory / "supervisor.json")
    heartbeat = read_json(directory / "heartbeat.json")
    pid = info.get("pid")
    if not isinstance(pid, int) or not info.get("process_start"):
        return False, "supervisor_identity_missing"
    if process_start(pid) != info["process_start"]:
        return False, "supervisor_dead"
    if heartbeat.get("pid") != pid or time.time() - heartbeat.get("at", 0) > max_age:
        return False, "heartbeat_stale"
    return True, "fresh"

#!/usr/bin/env python3
"""Drive the stub's one launched successor turn through the worker handshake."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

settings, sid, transcript = sys.argv[1:]
path = Path(settings).parent
launch = json.loads((path / "launch.json").read_text())
epoch = launch["epoch"]
mission = path.parent.parent.name
configuration = json.loads(Path(settings).read_text())
env = dict(os.environ, **configuration["env"])
base = [sys.executable, "-B", "-m", "gcl.cli"]
fence = [mission, "--session", sid, "--epoch", str(epoch)]
for _ in range(200):
    verified = subprocess.run(base + ["verified"] + fence + ["--summary", "stub checked checkpoint"],
                              env=env, capture_output=True, text=True)
    if verified.returncode == 0:
        break
    time.sleep(.05)
else:
    raise RuntimeError("successor never became prepared")
granted = subprocess.run(base + ["await-writer"] + fence, env=env, capture_output=True, text=True,
                         timeout=320)
(path / "successor_result.json").write_text(json.dumps({"verified": verified.stdout,
    "await": granted.stdout, "exit_code": granted.returncode}))
if "WRITER GRANTED" in granted.stdout:
    with open(transcript, "a") as stream:
        stream.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": "Continued in same turn after grant."}],
            "stop_reason": "end_turn"}}) + "\n")
    hook = configuration["hooks"]["Stop"][0]["hooks"][0]["command"]
    subprocess.run(hook, shell=True, input=json.dumps({"session_id": sid,
                   "transcript_path": transcript, "cwd": os.getcwd()}), text=True,
                   capture_output=True, env=env, check=True)

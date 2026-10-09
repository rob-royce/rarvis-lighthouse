"""Exercise emitted commands from a non-editable wheel, outside the checkout.

The subprocess entry also accepts an already installed wheel for a frozen
baseline/candidate comparison. It never repairs or reconstructs worker commands.
"""
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]


def clean_env(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("home", "tmp", "bin"):
        (root / name).mkdir(exist_ok=True)
    # The fake executors' env-python3 shebang must enter this wheel's interpreter.
    launcher = root / "bin/python3"
    launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -I -B "$@"\n')
    launcher.chmod(0o700)
    return {"PATH": f"{root / 'bin'}:/usr/bin:/bin", "HOME": str(root / "home"),
            "TMPDIR": str(root / "tmp"), "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CACHE_DIR": str(root / "cache"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull, "GIT_TERMINAL_PROMPT": "0"}


def run(argv, *, cwd, env, **kwargs):
    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True,
                          timeout=30, **kwargs)


def checked(argv, *, cwd, env):
    result = run(argv, cwd=cwd, env=env)
    assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return result


def until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("installed worker did not reach the expected state")


def emitted(text, verb, mission):
    # Select a complete production line; never replace its executable or prefix.
    return next(line for line in text.splitlines()
                if line.startswith("RLH_HOME=") and f" {verb} {mission} " in line)


def exercise(case, root, fixtures, user_site=None):
    # Production imports occur only in a foreign-cwd installed subprocess:
    # -I for the venv, or normal -P user-site discovery for the user installation.
    import gcl
    from gcl import hooks
    from gcl.adapters.claude_bg import ClaudeBackground
    from gcl.core.store import LocalStore
    from gcl.runtime.files import live_controller, read_json, write_json
    from gcl.supervisor import Supervisor

    report = {"case": case, "commands": [], "events": [], "result": "FAIL"}
    env = clean_env(root)
    if user_site:
        import site
        assert site.ENABLE_USER_SITE
        assert Path(site.getusersitepackages()).resolve() == Path(user_site).resolve()
        env["PYTHONUSERBASE"] = os.environ["PYTHONUSERBASE"]
    repo = root / "worker's repo"
    repo.mkdir()
    home = root / "worker's state"
    env.update(RLH_HOME=str(home), CODEX_HOME=str(root / "home/.codex"),
               GCL_CODEX_BIN=str(fixtures / "fake_codex.py"),
               GCL_CLAUDE_BIN=str(fixtures / "fake_claude.py"),
               GCL_FAKE_STATE=str(root / "fake.json"), GCL_POLL_INTERVAL="0.05",
               GCL_HOOK_PYTHON=sys.executable)
    installed = Path(user_site or sys.prefix).resolve()
    dist = importlib.metadata.distribution("rarvis-lighthouse")
    modules = [Path(module.__file__).resolve() for module in (gcl, hooks)]
    modules.append(Path(sys.modules[Supervisor.__module__].__file__).resolve())
    assert all(path.is_relative_to(installed) for path in modules), modules
    assert Path(dist.locate_file("gcl/supervisor.py")).resolve() == modules[-1]
    assert not json.loads(dist.read_text("direct_url.json") or "{}").get("dir_info", {}).get("editable")
    for path in modules:
        entry = next(entry for entry in dist.files if Path(dist.locate_file(entry)).resolve() == path)
        assert entry.hash.mode == "sha256"
        assert entry.hash.value == base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).decode().rstrip("=")
    report["installation"] = {"prefix": sys.prefix, "executable": sys.executable,
        "kind": "user-site" if user_site else "venv", "installed_root": str(installed),
        "modules": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in modules},
        "distribution": dist.metadata["Name"], "version": dist.version}
    for args in (("init", "-q"), ("config", "user.name", "Synthetic"),
                 ("config", "user.email", "synthetic@example.invalid")):
        checked(["/usr/bin/git", *args], cwd=repo, env=env)
    (repo / "README.md").write_text("synthetic installed-package test\n")
    checked(["/usr/bin/git", "add", "README.md"], cwd=repo, env=env)
    checked(["/usr/bin/git", "commit", "-qm", "synthetic baseline"], cwd=repo, env=env)
    # Worker execution receives hostile import/command lookup inputs. Production
    # generators and the fake executor retain their own clean installed environment.
    decoy = repo / "gcl"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("raise RuntimeError('cwd package selected')\n")
    for name in ("cli", "hooks"):
        (decoy / (name + ".py")).write_text("raise RuntimeError('decoy module selected')\n")
    traps = root / "path traps"
    traps.mkdir()
    marker = root / "decoy-ran"
    for name in ("rlh", "gcl", "python3"):
        path = traps / name
        path.write_text("#!/bin/sh\nprintf bad > " + shlex.quote(str(marker)) + "\nexit 89\n")
        path.chmod(0o700)
    worker_env = dict(env, PATH=f"{traps}:/usr/bin:/bin", PYTHONPATH=str(repo))
    # The emitted binding must work even when user-site discovery is unavailable.
    worker_env["PYTHONUSERBASE"] = str(root / "empty user base")
    cli = ([sys.executable, "-P", "-B", "-m", "gcl.cli"] if user_site
           else [str(Path(sys.prefix) / "bin/rlh")])
    mission = None
    store = None

    def cli_call(*args):
        return json.loads(checked([*cli, *args], cwd=repo, env=env).stdout)

    def execute(command, *, token=None, expected=0):
        command_env = dict(worker_env)
        if token is not None:
            command_env["GCL_SESSION_TOKEN"] = token
        result = run(["/bin/sh", "-c", command], cwd=repo, env=command_env)
        report["commands"].append({"command": command, "returncode": result.returncode,
                                    "stdout": result.stdout, "stderr": result.stderr})
        assert result.returncode == expected, report["commands"][-1]
        assert not marker.exists(), "a PATH decoy ran"
        return result.stdout

    proof = repo / "proof.txt"
    proof.write_text("done\n")
    (repo / "job.log").write_text("synthetic job record\n")
    done_check = shlex.join([sys.executable, "-I", "-B", "-c",
        "from pathlib import Path; import sys; sys.exit(Path('proof.txt').read_bytes() != b'done\\n')"])
    try:
        if case in ("codex", "confirmation"):
            extra = ["--done-check", done_check] if case == "codex" else []
            started = cli_call("start", "--executor", "codex", "--cwd", str(repo),
                "--mode", "CONTROLLED_AUTO", "--policy", "red_ratio=0.99", *extra,
                "--", "verify the exact proof bytes")
            mission = started["mission"]
            store = LocalStore(home / "state.sqlite")
            def prompt():
                data = read_json(root / "fake.json")
                return next((c["params"]["input"][0]["text"] for c in data.get("calls", [])
                             if c["method"] == "turn/start"), None)
            guidance = until(prompt)
            report["guidance"] = guidance
            epoch, sid = store.current_writer(mission)
            assert epoch == 1 and sid
            # First command is deliberately evidence: baseline must fail here with 127.
            execute(emitted(guidance, "evidence", mission))
            evidence = next(e["data"] for e in store.events(mission) if e["type"] == "runtime.evidence")
            assert evidence["sha256"] == hashlib.sha256(proof.read_bytes()).hexdigest()
            assert evidence["epoch"] == epoch and evidence["evidence_path"] == str(proof.resolve())
            execute(emitted(guidance, "note", mission))
            for line in guidance.splitlines():
                if line.startswith("RLH_HOME=") and any(f" {v} {mission} " in line for v in ("effect", "job")):
                    execute(line)
            execute(emitted(guidance, "done", mission))
            done = next(e["data"] for e in store.events(mission) if e["type"] == "runtime.worker_done")
            assert done == {"epoch": epoch, "session_id": sid}
            expected = "mission.completed" if case == "codex" else "runtime.completion_reported"
            until(lambda: any(e["type"] == expected for e in store.events(mission)))
            if case == "confirmation":
                assert cli_call("status", mission)["runtime_status"] == "AWAITING_CONFIRMATION"
                assert not any(e["type"] == "mission.completed" for e in store.events(mission))
            report["writer"] = {"mission": mission, "session": sid, "epoch": epoch}
        else:
            assert case == "claude"
            store = LocalStore(home / "state.sqlite")
            mission = store.create_mission({"owner": "human:test", "objective": "synthetic",
                "done_when": ["proof bytes"]}, "human:test", "CONTROLLED_AUTO")
            config = {"executor": "claude", "cwd": str(repo), "done_check": done_check,
                      "runtime_policy": {"await_writer_s": .15}}
            store.note_event(mission, "runtime.config", config)
            store.activate_initial(mission, "old", "claude")
            directory = home / "missions" / mission
            adapter = ClaudeBackground(config, directory)

            def settings(sid, epoch, read_only):
                path = directory / "sessions" / ("launch-" + sid)
                write_json(path / "launch.json", {"epoch": epoch, "read_only": read_only})
                write_json(path / "resolved.json", {"session_id": sid})
                token = "synthetic-capability-" + sid
                adapter._settings(path, epoch, token)
                return path, token

            def hook(path, event, sid, **payload):
                settings_json = read_json(path / "settings.json")
                command = settings_json["hooks"][event][0]["hooks"][0]["command"]
                result = run(["/bin/sh", "-c", command], cwd=repo,
                    env=dict(worker_env, **settings_json["env"]),
                    input=json.dumps({"session_id": sid, "cwd": str(repo), **payload}))
                assert result.returncode == 0, result.stderr
                assert not marker.exists()
                report.setdefault("hooks", []).append({"command": command, "event": event,
                    "session": sid, "payload": payload, "returncode": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr})
                return json.loads(result.stdout)

            def allowed(path, sid, command):
                result = hook(path, "PreToolUse", sid, tool_name="Bash", tool_input={"command": command})
                return result.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

            old, old_token = settings("old", 1, False)
            context = hook(old, "SessionStart", "old", source="startup")["hookSpecificOutput"]["additionalContext"]
            # Startup embeds the completion command after prose, unlike successor lines.
            old_done = context.split("report completion with: ", 1)[1].splitlines()[0]
            execute(old_done, token=old_token)
            rid = store.request_rollover(mission, "manual", 1)
            store.advance(rid, "ROLLOVER_REQUESTED", "QUIESCING")
            denial = hook(old, "PreToolUse", "old", tool_name="Write")
            reason = denial["hookSpecificOutput"]["permissionDecisionReason"]
            note = reason.split("record progress with ", 1)[1].rsplit("; end this turn now.", 1)[0]
            assert allowed(old, "old", note)
            execute(note, token=old_token)
            for tool in ("Read", "Grep", "Glob"):
                assert hook(old, "PreToolUse", "old", tool_name=tool) == {}
            for tool in ("Write", "Edit"):
                assert hook(old, "PreToolUse", "old", tool_name=tool)["hookSpecificOutput"]["permissionDecision"] == "deny"
            old_prefix = note.split(" note ", 1)[0]
            for tail in ("--help", f"help {mission} --session old --epoch 1",
                         f"note {mission} --session old --epoch 1 --help"):
                assert allowed(old, "old", old_prefix + " " + tail)
            before = len([e for e in store.events(mission) if e["type"] == "runtime.note"])
            execute(note, token="wrong", expected=2)
            execute(note, expected=2)
            assert len([e for e in store.events(mission) if e["type"] == "runtime.note"]) == before
            controller = Supervisor(home, mission, adapter=object())
            guidance = controller._worker_guidance(1, "old")
            assert allowed(old, "old", emitted(guidance, "evidence", mission))
            execute(emitted(guidance, "evidence", mission), token=old_token)
            for verb in ("done", "effect", "job"):
                assert not allowed(old, "old", emitted(guidance, verb, mission))
            for tail in ("verified --summary checked", "await-writer"):
                verb, _, suffix = tail.partition(" ")
                assert not allowed(old, "old", f"{old_prefix} {verb} {mission} --session old --epoch 1 {suffix}")
            # Literal shell punctuation inside quoted note data remains data.
            punctuated = note.replace("handoff summary and next step", "handoff; next step")
            assert allowed(old, "old", punctuated)
            execute(punctuated, token=old_token)
            mutations = [note + tail for tail in ("; true", " && true", " || true", " | cat", " &", " > bad", " < bad", " extra", "\ntrue", "\rtrue")]
            mutations += [note.replace(mission, "other"), note.replace("--session old", "--session wrong"),
                note.replace("--epoch 1", "--epoch 2"),
                note.replace(shlex.quote(str(home)), shlex.quote(str(home / "other"))),
                note.replace("handoff summary and next step", "$(true)"),
                note.replace("handoff summary and next step", "`true`"),
                "EXTRA=yes " + note, note.replace(shlex.quote(sys.executable), "python3"),
                note.replace(shlex.quote(sys.executable), "./python"),
                note.replace(shlex.quote(sys.executable), shlex.quote(str(root / "other python"))),
                note.replace(" -I ", " "), note.replace(" -B ", " "),
                note.replace("from gcl.cli import main", "from other.cli import main"),
                note.replace("sys.path.insert(0,", "sys.path.append("),
                note.replace(" -c ", " -m "),
                note.replace("raise SystemExit(main())", "raise SystemExit(main()); print('extra')"),
                note + " # comment", "(" + note + ")"]
            resolved_python = shlex.quote(str(Path(sys.executable).resolve()))
            if resolved_python != shlex.quote(sys.executable):
                mutations.append(note.replace(shlex.quote(sys.executable), resolved_python))
            bootstrap = shlex.split(note)[5]
            wrong_root = bootstrap.replace(repr(str(Path(hooks.__file__).resolve().parents[1])),
                                           repr(str(root / "other installation")))
            assert wrong_root != bootstrap
            mutations.append(note.replace(shlex.quote(bootstrap), shlex.quote(wrong_root)))
            for command in mutations:
                assert command != note and not allowed(old, "old", command), command
            assert not allowed(old, "unregistered", note)
            store.save_checkpoint(rid, controller._checkpoint(rid, "old"))
            new, new_token = settings("new", 2, True)
            context = hook(new, "SessionStart", "new", source="startup")["hookSpecificOutput"]["additionalContext"]
            store.note_event(mission, "runtime.successor_created", {"rollover_id": rid, "session_id": "new"})
            verified = context.split("Then run exactly: ", 1)[1].splitlines()[0]
            wait = context.split("Next run exactly: ", 1)[1].splitlines()[0]
            new_done = context.split("report completion with: ", 1)[1].splitlines()[0]
            new_note = hooks._note_command(home, mission, "new", 2)
            for command in (verified, wait, new_note):
                assert allowed(new, "new", command)
            new_prefix = new_note.split(" note ", 1)[0]
            assert not allowed(new, "new", new_prefix + " --help")
            assert not allowed(new, "new", f"{new_prefix} help {mission} --session new --epoch 2")
            for tool in ("Read", "Grep", "Glob"):
                assert hook(new, "PreToolUse", "new", tool_name=tool) == {}
            assert hook(new, "PreToolUse", "new", tool_name="Write")["hookSpecificOutput"]["permissionDecision"] == "deny"
            new_guidance = controller._worker_guidance(2, "new")
            for verb in ("evidence", "done", "effect", "job"):
                assert not allowed(new, "new", emitted(new_guidance, verb, mission))
            assert not allowed(new, "new", verified + "; true")
            execute(verified, token="wrong", expected=2)
            execute(wait, token=new_token, expected=2)  # verification is not yet reported
            execute(new_note, token=new_token)
            execute(verified, token=new_token)
            punctuated_summary = verified.replace("checkpoint verified", "checkpoint; verified")
            assert allowed(new, "new", punctuated_summary)
            execute(punctuated_summary, token=new_token)
            assert store.current_writer(mission) == (1, "old")
            assert "NOT GRANTED" in execute(wait, token=new_token)
            assert store.current_writer(mission) == (1, "old")
            store.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY", successor_session_id="new")
            store.transfer_ownership(rid, "new", 1)
            assert "WRITER GRANTED" in execute(wait, token=new_token)
            assert not allowed(old, "old", note)
            execute(note, token=old_token, expected=2)
            assert allowed(new, "new", new_done)
            execute(new_done, token=new_token)
            controller.refresh()
            assert controller._complete_if_reported(2, "new", {}) == "completed"
            assert any(e["type"] == "runtime.worker_verified" for e in store.events(mission))
            assert any(e["type"] == "mission.completed" for e in store.events(mission))
            report["writer"] = {"mission": mission, "session": "new", "epoch": 2}
            report["fence_mutations_denied"] = len(mutations)
        report["result"] = "PASS"
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if store and mission:
            report["events"] = store.events(mission)
            if case in ("codex", "confirmation"):
                cli_call("cancel", mission)
                until(lambda: not live_controller(home / "missions" / mission)[0])
        report["decoy_ran"] = marker.exists()
        (root / "installed-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def install_wheel(wheel, root):
    env = clean_env(root)
    venv = root / "installed venv's"
    checked([sys.executable, "-I", "-B", "-m", "venv", str(venv)], cwd=root, env=env)
    checked([str(venv / "bin/python"), "-I", "-B", "-m", "pip", "install",
             "--no-index", "--no-deps", str(wheel)], cwd=root, env=env)
    return venv


def run_installed_case(venv, case, root):
    env = clean_env(root)
    return run([str(venv / "bin/python"), "-I", "-B", str(Path(__file__).resolve()),
                "--exercise", case, str(root), str(ROOT / "tests/fixtures/gcl")], cwd=root, env=env)


def install_user_site(wheel, root):
    env = clean_env(root)
    env["PYTHONUSERBASE"] = str(root / "user base's")
    # Deliberately use the base interpreter for normal user-site discovery, not
    # a venv with user-site disabled. pip writes only to the synthetic user site.
    interpreter = sys._base_executable
    info = json.loads(checked([interpreter, "-P", "-B", "-c",
        "import json,site; print(json.dumps({'path':site.getusersitepackages(), 'enabled':site.ENABLE_USER_SITE}))"],
        cwd=root, env=env).stdout)
    target = Path(info["path"])
    assert info["enabled"] and target.is_relative_to(root)
    checked([sys.executable, "-I", "-B", "-m", "pip", "install", "--no-index", "--no-deps",
             "--target", str(target), str(wheel)], cwd=root, env=env)
    return {"interpreter": interpreter, "site": str(target), "user_base": env["PYTHONUSERBASE"]}


def run_user_site_case(installation, case, root):
    env = clean_env(root)
    env["PYTHONUSERBASE"] = installation["user_base"]
    return run([installation["interpreter"], "-P", "-B", str(Path(__file__).resolve()),
        "--exercise", case, str(root), str(ROOT / "tests/fixtures/gcl"), installation["site"]], cwd=root, env=env)


if __name__ == "__main__":
    assert sys.argv[1] == "--exercise"
    result = exercise(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]),
                      sys.argv[5] if len(sys.argv) > 5 else None)
    print(json.dumps({key: value for key, value in result.items() if key != "events"}))
    raise SystemExit(0 if result["result"] == "PASS" else 1)


import pytest


@pytest.fixture(scope="module")
def installed_wheel(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed wheel's")
    env = clean_env(root)
    stage = root / "stage"
    stage.mkdir()
    shutil.copytree(ROOT / "gcl", stage / "gcl", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "pyproject.toml", stage / "pyproject.toml")
    wheels = root / "wheels"
    wheels.mkdir()
    # Missing build prerequisites are a hard failure, never a skipped regression.
    checked([sys.executable, "-I", "-B", "-m", "pip", "wheel", "--no-index", "--no-deps",
             "--no-build-isolation", "--wheel-dir", str(wheels), str(stage)], cwd=root, env=env)
    wheel, = wheels.glob("*.whl")
    (root / "wheel.json").write_text(json.dumps({"name": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}))
    return install_wheel(wheel, root / "installation")


@pytest.mark.parametrize("case", ["codex", "confirmation", "claude"])
def test_installed_generated_commands(installed_wheel, tmp_path, case):
    result = run_installed_case(installed_wheel, case, tmp_path / "case's files")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def user_site_wheel(installed_wheel, tmp_path_factory):
    wheel, = (installed_wheel.parent.parent / "wheels").glob("*.whl")
    return install_user_site(wheel, tmp_path_factory.mktemp("user-site wheel's"))


@pytest.mark.parametrize("case", ["codex", "confirmation", "claude"])
def test_user_site_generated_commands(user_site_wheel, tmp_path, case):
    result = run_user_site_case(user_site_wheel, case, tmp_path / "case's files")
    assert result.returncode == 0, result.stdout + result.stderr

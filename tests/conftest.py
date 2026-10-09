"""Isolate all synthetic tests from the user's real homes and executors."""
from pathlib import Path
import sys
import os
import pytest
from tools.run_synthetic_tests import test_environment as synthetic_environment

@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    # No inherited host, Git, provider, session or Python entry points.
    for key in list(os.environ):
        monkeypatch.delenv(key)
    home = tmp_path / "synthetic-user-home"
    home.mkdir()
    environment = synthetic_environment(tmp_path / "synthetic-runtime")
    environment.update({
        "TMPDIR": str(tmp_path),
        "HOME": str(home), "GCL_HOME": str(tmp_path / "synthetic-gcl-home"),
        "CODEX_HOME": str(home / ".codex"),
        "GCL_CODEX_BIN": str(root / "tests/fixtures/gcl/fake_codex.py"),
        "GCL_CLAUDE_BIN": str(root / "tests/fixtures/gcl/fake_claude.py"),
        "GCL_CLAUDE_SETTINGS": str(home / ".claude/settings.json"),
        "GCL_CLAUDE_TRUST_FILE": str(home / ".claude.json"),
        "GCL_CLAUDE_PROJECTS_DIR": str(home / ".claude/projects"),
        "GCL_PYTHON": sys.executable, "GCL_HOOK_PYTHON": sys.executable,
    })
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

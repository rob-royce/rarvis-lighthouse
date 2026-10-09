"""Run pytest with no inherited host/provider/Git/Python configuration."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def test_environment(root):
    root = Path(root).resolve()
    home, temporary = root / 'home', root / 'tmp'
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    # A versioned interpreter directory may have no python3, or a different
    # python3. Pin env-python3 fake executors to this invocation's interpreter.
    binary = root / 'bin'
    binary.mkdir(mode=0o700)
    with (binary / 'python3').open('x') as launcher:
        launcher.write('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        os.fchmod(launcher.fileno(), 0o700)
    return {'PATH': f'{binary}:/usr/bin:/bin',
            'HOME': str(home), 'TMPDIR': str(temporary),
            'GCL_HOME': str(root / 'gcl'), 'CODEX_HOME': str(home / '.codex'),
            'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
            'GIT_CONFIG_VALUE_0': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}


if __name__ == '__main__':
    # Caller chooses a synthetic parent; never use a host-provided TMPDIR.
    parent = Path(sys.argv[1]).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='suite-', dir=parent))
    raise SystemExit(subprocess.call([sys.executable, '-B', '-m', 'pytest',
        '--basetemp', str(root / 'cases'), *sys.argv[2:]],
        cwd=Path(__file__).resolve().parents[1], env=test_environment(root)))

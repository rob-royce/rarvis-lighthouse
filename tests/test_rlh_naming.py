"""D3 names and home compatibility, exclusively in synthetic homes."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

import pytest

from gcl.hooks import _note_command, _worker_command
from gcl.runtime.files import home_path
from gcl.runtime.worker_commands import worker_prefix

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('level', ['flag', 'rlh', 'gcl', 'default'])
def test_home_precedence(level, tmp_path, monkeypatch, capsys):
    user = Path(os.environ['HOME'])
    monkeypatch.delenv('GCL_HOME', raising=False)
    monkeypatch.delenv('RLH_HOME', raising=False)
    expected = user / '.rarvis-lighthouse'
    value = None
    if level != 'default':
        monkeypatch.setenv('GCL_HOME', str(tmp_path / 'legacy'))
        expected = tmp_path / 'legacy'
    if level in ('flag', 'rlh'):
        monkeypatch.setenv('RLH_HOME', str(tmp_path / 'preferred'))
        expected = tmp_path / 'preferred'
    if level == 'flag':
        value = str(tmp_path / 'explicit')
        expected = tmp_path / 'explicit'
    assert home_path(value) == expected
    assert not expected.exists()
    assert capsys.readouterr().err == ''


@pytest.mark.parametrize('selection', [None, 'flag', 'rlh', 'gcl', 'new_exists'])
def test_old_home_notice_never_migrates(selection, monkeypatch, capsys):
    user = Path(os.environ['HOME'])
    old, new = user / '.gcl', user / '.rarvis-lighthouse'
    old.mkdir()
    (old / 'keep.txt').write_text('old state')
    monkeypatch.delenv('GCL_HOME', raising=False)
    monkeypatch.delenv('RLH_HOME', raising=False)
    value = None
    if selection == 'flag':
        value = str(old)
    elif selection in ('rlh', 'gcl'):
        monkeypatch.setenv(selection.upper() + '_HOME', '~/.gcl')
    elif selection == 'new_exists':
        new.mkdir()
    result = home_path(value)
    assert result == (old if selection in ('flag', 'rlh', 'gcl') else new)
    notice = capsys.readouterr().err
    if selection is None:
        assert notice.count('RLH_HOME=~/.gcl') == 1
        assert 'not moved' in notice
    else:
        assert notice == ''
    assert new.exists() == (selection == 'new_exists')
    assert list(old.iterdir()) == [old / 'keep.txt']
    assert (old / 'keep.txt').read_text() == 'old state'


@pytest.mark.parametrize('name,variable', [('rlh', 'RLH_HOME'), ('gcl', 'GCL_HOME')])
def test_fenced_command_forms(name, variable, tmp_path):
    home = tmp_path / 'home with spaces'
    prefix = f'{variable}={shlex.quote(str(home))} {shlex.quote(str(ROOT / "bin" / name))}'
    command = f"{prefix} note abcd --session worker --epoch 2 'checkpoint checked'"
    assert _worker_command(command, home, 'abcd', 'worker', 2)
    assert _worker_command(command, home, 'abcd', 'worker', 2, successor=True)
    for bad in (command + '; touch bad', command + ' && true', command.replace('abcd', 'ffff'),
                command.replace('--epoch 2', '--epoch 1'), command.replace('worker', 'other'),
                command.replace(str(home), str(home / 'other')),
                command.replace('checkpoint checked', '$(touch bad)')):
        assert not _worker_command(bad, home, 'abcd', 'worker', 2)
        assert not _worker_command(bad, home, 'abcd', 'worker', 2, successor=True)


def test_new_hook_guidance_uses_rlh(tmp_path):
    command = _note_command(tmp_path, 'abcd', 'worker', 2)
    assert shlex.split(command)[:2] == [f'RLH_HOME={tmp_path}', str(ROOT / 'bin/rlh')]
    assert _worker_command(command, tmp_path, 'abcd', 'worker', 2)


def test_entry_points_and_launchers_match():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    assert project['name'] == 'rarvis-lighthouse'
    assert project['scripts']['rlh'] == project['scripts']['gcl'] == 'gcl.cli:main'
    outputs = []
    for name in ('rlh', 'gcl'):
        result = subprocess.run([str(ROOT / 'bin' / name), '--help'], env=os.environ.copy(),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ''
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert 'usage: rlh' in outputs[0]
    assert 'RLH_HOME' in outputs[0] and '~/.rarvis-lighthouse' in outputs[0]


@pytest.mark.parametrize('legacy', [False, True])
def test_source_worker_binding_ignores_cwd_and_pythonpath(tmp_path, legacy):
    cwd = tmp_path / "worker's directory"
    decoy = cwd / 'gcl'
    decoy.mkdir(parents=True)
    (decoy / '__init__.py').write_text("raise RuntimeError('cwd decoy selected')\n")
    (decoy / 'cli.py').write_text("raise RuntimeError('PYTHONPATH decoy selected')\n")
    home = tmp_path / "worker's state"
    prefix = worker_prefix(home)
    if legacy:
        prefix = prefix.replace('RLH_HOME=', 'GCL_HOME=').replace('bin/rlh', 'bin/gcl')
    command = prefix + ' --help'
    assert _worker_command(command, home, 'mission', 'session', 1)
    result = subprocess.run(['/bin/sh', '-c', command], cwd=cwd,
        env=dict(os.environ, GCL_PYTHON=sys.executable, PYTHONPATH=str(cwd)),
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'usage: rlh' in result.stdout
    assert not home.exists()

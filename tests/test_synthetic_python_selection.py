"""Synthetic executors must use the selected Python and preserve literal argv."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from conftest import isolated_host
from tools.run_synthetic_tests import test_environment as isolated_environment


@pytest.mark.parametrize('entry', ['runner', 'fixture'])
def test_fake_executor_uses_selected_python_not_other_python3(tmp_path, monkeypatch, entry):
    # Model a versioned interpreter directory with an incompatible python3.
    # The decoy is deliberately local and exits without running any host code.
    interpreter_dir = tmp_path / "selected Python's bin"
    interpreter_dir.mkdir()
    selected = interpreter_dir / 'python3.11'
    selected.symlink_to(sys.executable)
    decoy = interpreter_dir / 'python3'
    decoy.write_text('#!/bin/sh\nexit 49\n')
    decoy.chmod(0o700)
    monkeypatch.setattr(sys, 'executable', str(selected))
    probe = tmp_path / 'fake-executor'
    probe.write_text('#!/usr/bin/env python3\nimport json, sys\n'
                     'print(json.dumps({"version": list(sys.version_info[:3]), "executable": sys.executable}))\n')
    probe.chmod(0o700)
    with pytest.MonkeyPatch.context() as nested:
        if entry == 'runner':
            environment = isolated_environment(tmp_path / 'runner-child')
        else:
            child = tmp_path / 'fixture-child'
            child.mkdir()
            isolated_host.__wrapped__(child, nested)
            environment = dict(os.environ)
        result = subprocess.run([str(probe)], env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    identity = json.loads(result.stdout)
    assert identity['version'] == list(sys.version_info[:3])
    assert os.path.samefile(identity['executable'], selected)


def test_selected_python_preserves_literal_arguments(tmp_path):
    environment = isolated_environment(tmp_path / 'argv-child')
    python = shutil.which('python3', path=environment['PATH'])
    arguments = ['space value', "apostrophe's", '中文边界', '$(synthetic-never-run)',
                 '`synthetic-never-run`', '--looks-like-an-option', '']
    result = subprocess.run([python, '-I', '-B', '-c',
                             'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))',
                             *arguments], env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments


def test_selected_python_accepts_no_arguments(tmp_path):
    environment = isolated_environment(tmp_path / 'no-args-child')
    python = shutil.which('python3', path=environment['PATH'])
    result = subprocess.run([python], env=environment, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('kind', ['regular', 'symlink'])
def test_existing_python_entry_is_never_overwritten(tmp_path, kind):
    root = tmp_path / 'reserved-child'
    bin_dir = root / 'bin'
    bin_dir.mkdir(parents=True)
    protected = tmp_path / 'protected-synthetic-file'
    protected.write_text('preserve these synthetic bytes\n')
    launcher = bin_dir / 'python3'
    if kind == 'symlink':
        launcher.symlink_to(protected)
    else:
        launcher.write_text('preserve this synthetic entry\n')
    previous = launcher.read_bytes()
    with pytest.raises(FileExistsError):
        isolated_environment(root)
    assert launcher.read_bytes() == previous
    assert protected.read_text() == 'preserve these synthetic bytes\n'
    assert launcher.is_symlink() == (kind == 'symlink')

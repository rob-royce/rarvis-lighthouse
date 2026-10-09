"""Regressions for the code-review findings; all paths and executors are synthetic."""
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from gcl import cli
from gcl.adapters.codex_appserver import CodexAppServer, NotSentError, RequestRejected
from gcl.core import snapshot
from gcl.core.store import SCHEMA_VERSION, LocalStore
from gcl.runtime import capability
from gcl.runtime.files import write_json
from gcl.supervisor import Supervisor


def mission(tmp_path):
    home = tmp_path / 'gcl'
    store = LocalStore(home / 'state.sqlite')
    mid = store.create_mission({'owner': 'human:owner', 'objective': 'synthetic'}, 'human:owner')
    store.activate_initial(mid, 'writer', 'codex')
    store.note_event(mid, 'runtime.config', {'executor': 'codex', 'cwd': str(tmp_path),
                                          'runtime_policy': {'delivery_backoff_max_s': 60}})
    return home, store, mid


class Executor:
    def __init__(self, *failures):
        self.failures = list(failures)
        self.accepted = []

    def start_turn(self, sid, prompt):
        if self.failures:
            raise self.failures.pop(0)
        delivery_id = prompt.split('Delivery ID: ')[1].splitlines()[0]
        self.accepted.append(delivery_id)
        return 'turn-' + delivery_id


def run_cli(*argv, capsys):
    old = sys.argv
    sys.argv = ['gcl', '--json', *argv]
    try:
        code = cli.main()
    finally:
        sys.argv = old
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize('error', [NotSentError('app-server is not running'),
                                   RequestRejected('turn/start: {"code": -32600}')])
def test_definite_delivery_failure_retries_with_backoff_instead_of_parking(tmp_path, error):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'synthetic owner message', 'human:owner')
    executor = Executor(error)
    controller = Supervisor(home, mid, adapter=executor)
    controller.poll_interval = 0.05
    assert controller._deliver_owner('writer', 1) is False
    delivery = store.owner_delivery(mid)
    assert delivery['state'] == 'PENDING'
    controller.refresh()
    failure = next(e['data'] for e in controller.events if e['type'] == 'runtime.owner_delivery_failed')
    assert failure['delivery_id'] == delivery['id'] and failure['attempt'] == 1
    # Within the backoff window nothing is sent.
    assert controller._deliver_owner('writer', 1) is False
    assert executor.accepted == []
    time.sleep(max(0, failure['retry_at'] - time.time()) + 0.01)
    assert controller._deliver_owner('writer', 1) is True
    assert executor.accepted == [delivery['id']]
    assert store.owner_delivery(mid) is None


def test_backoff_is_exponential_and_capped(tmp_path):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'synthetic owner message', 'human:owner')
    controller = Supervisor(home, mid, adapter=Executor(*[NotSentError('down')] * 4))
    controller.poll_interval, controller.delivery_backoff_max_s = 10, 25
    delays = []
    for _ in range(3):
        before = time.time()
        assert controller._deliver_owner('writer', 1) is False
        controller.refresh()
        retry_at = [e['data']['retry_at'] for e in controller.events
                    if e['type'] == 'runtime.owner_delivery_failed'][-1]
        delays.append(round(retry_at - before))
        controller.note('runtime.owner_delivery_failed', {
            'delivery_id': store.owner_delivery(mid)['id'], 'error': 'fast-forward',
            'attempt': 0, 'retry_at': 0})
    assert delays[0] == 10 and delays[-1] == 25


def test_lost_local_receipt_is_settled_from_saved_turn(tmp_path, monkeypatch):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'synthetic owner message', 'human:owner')
    executor = Executor()
    controller = Supervisor(home, mid, adapter=executor)
    original = controller.store.ack_owner_delivery
    monkeypatch.setattr(controller.store, 'ack_owner_delivery',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('ledger write lost')))
    assert controller._deliver_owner('writer', 1) is False
    delivery = store.owner_delivery(mid)
    assert delivery['state'] == 'UNKNOWN'
    monkeypatch.setattr(controller.store, 'ack_owner_delivery', original)
    assert controller._settle_receipt(delivery) is True
    assert store.owner_delivery(mid) is None
    assert not controller._receipt_path(delivery['id']).exists()
    assert executor.accepted == [delivery['id']]


def test_unknown_delivery_can_be_resolved_from_the_cli(tmp_path, capsys):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'synthetic owner message', 'human:owner')
    controller = Supervisor(home, mid, adapter=Executor(TimeoutError('no answer')))
    assert controller._deliver_owner('writer', 1) is False
    delivery = store.owner_delivery(mid)
    assert delivery['state'] == 'UNKNOWN'
    code, result = run_cli('--home', str(home), 'delivery', 'resolve', mid, delivery['id'],
                           '--rejected', '--evidence', 'executor thread has no such turn', capsys=capsys)
    assert code == 0 and result['state'] == 'PENDING'
    code, result = run_cli('--home', str(home), 'delivery', 'resolve', mid, delivery['id'],
                           '--rejected', '--evidence', 'again', capsys=capsys)
    assert code == 2 and 'not UNKNOWN' in result['error']


def test_policy_key_for_runtime_is_rejected_before_mission_creation(tmp_path, capsys):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    home = tmp_path / 'gcl'
    code, result = run_cli('--home', str(home), 'start', '--executor', 'codex', '--cwd', str(repo),
                           '--mode', 'OFF', '--policy', 'await_writer_s=10', 'synthetic goal', capsys=capsys)
    assert code == 2 and '--runtime-policy' in result['error']
    code, result = run_cli('--home', str(home), 'start', '--executor', 'codex', '--cwd', str(repo),
                           '--mode', 'OFF', '--runtime-policy', 'chunk_wait_s=30,idle_nudges=1',
                           'synthetic goal', capsys=capsys)
    assert code == 0
    store = LocalStore(home / 'state.sqlite')
    config = store.first_event(result['mission'], 'runtime.config')
    assert config['runtime_policy'] == {'chunk_wait_s': 30.0, 'idle_nudges': 1}


def test_core_errors_keep_the_json_error_contract(tmp_path, capsys):
    home, store, mid = mission(tmp_path)
    code, result = run_cli('--home', str(home), 'goal', 'change', mid, '--field', 'mission_id',
                           '--value', '"other"', '--authority-ref', 'ref', capsys=capsys)
    assert code == 2 and 'immutable' in result['error']
    code, result = run_cli('--home', str(home), 'goal', 'change', mid, '--field', 'owner',
                           '--value', '"someone"', '--authority-ref', 'ref', capsys=capsys)
    assert code == 2 and 'owner' in result['error']
    store.record_effect(mid, 'k', 'worker', 1)
    store.mark_effect('k', 'IN_FLIGHT', 1)
    store.mark_effect('k', 'CONFIRMED_DONE', 1)
    code, result = run_cli('--home', str(home), 'effect', mid, '--session', 'writer', '--epoch', '1',
                           'begin', 'k', capsys=capsys)
    assert code == 2 and 'CONFIRMED_DONE' in result['error']
    assert store.get_beacon(mid)['owner'] == 'human:owner'


def test_ledger_from_a_newer_release_keeps_the_json_error_contract(tmp_path, capsys):
    home, store, mid = mission(tmp_path)
    with store._tx() as db:
        db.execute(f'PRAGMA user_version={SCHEMA_VERSION + 1}')
    code, result = run_cli('--home', str(home), 'status', mid, capsys=capsys)
    assert code == 2 and 'newer than this release' in result['error']


def test_unmanaged_writer_slug_matches_claude_project_names(tmp_path):
    home = tmp_path / 'gcl'
    store = LocalStore(home / 'state.sqlite')
    cwd = tmp_path / 'my_repo with space'
    mid = store.create_mission({'owner': 'human:owner', 'objective': 'synthetic'}, 'human:owner')
    projects = tmp_path / 'projects'
    store.note_event(mid, 'runtime.config', {'executor': 'claude', 'cwd': str(cwd),
                                          'claude_projects_root': str(projects)})
    directory = projects / re.sub(r'[^A-Za-z0-9]', '-', str(cwd))
    directory.mkdir(parents=True)
    (directory / 'foreign-session.jsonl').write_text('{}\n')
    controller = Supervisor(home, mid, adapter=object())
    assert controller._unmanaged_writer() is True


def test_codex_early_notifications_are_bounded():
    server = CodexAppServer()
    assert not hasattr(server, '_notifications')
    for thread in range(100):
        for index in range(300):
            server._handle_notification('item/updated', {'threadId': f'foreign-{thread}', 'n': index})
    assert len(server._early) == 32
    assert all(len(items) == 256 for items in server._early.values())
    assert 'foreign-99' in server._early and 'foreign-0' not in server._early


def test_codex_start_turn_before_send_is_not_sent():
    server = CodexAppServer()
    with pytest.raises(NotSentError):
        server.start_turn('missing-thread', 'prompt')
    server._threads['t'] = {'thread': {'cwd': '/synthetic'}, 'completed_turns': set(),
                            'turns_started': True, 'sandbox': 'read-only'}
    with pytest.raises(NotSentError):
        server.start_turn('t', 'prompt')


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def make_repo(path):
    path.mkdir()
    git(path, 'init', '-q')
    git(path, '-c', 'user.name=t', '-c', 'user.email=t@invalid', 'commit', '-q', '--allow-empty', '-m', 'base')
    (path / 'tracked.txt').write_text('base\n')
    git(path, 'add', 'tracked.txt')
    git(path, '-c', 'user.name=t', '-c', 'user.email=t@invalid', 'commit', '-q', '-m', 'tracked')
    return path


def test_restore_validates_untracked_files_before_applying_diff(tmp_path):
    source = make_repo(tmp_path / 'source')
    (source / 'tracked.txt').write_text('changed\n')
    (source / 'new.txt').write_text('untracked\n')
    manifest = snapshot.capture(source, tmp_path / 'snapshot')
    target = tmp_path / 'target'
    git(tmp_path, 'clone', '-q', str(source), str(target))
    (target / 'new.txt').write_text('someone else\n')
    with pytest.raises(ValueError, match='already exists'):
        snapshot.restore(manifest, target)
    assert (target / 'tracked.txt').read_text() == 'base\n'
    (target / 'new.txt').unlink()
    bad = dict(manifest)
    with tarfile.open(manifest['tar_path'], 'w') as archive:
        archive.add(source / 'tracked.txt', arcname='new.txt')
    bad['tar_sha256'] = snapshot._hash(Path(manifest['tar_path']).read_bytes())
    with pytest.raises(ValueError, match='mismatched'):
        snapshot.restore(bad, target)
    assert (target / 'tracked.txt').read_text() == 'base\n'
    assert not (target / 'new.txt').exists()


def test_capability_files_and_ledger_are_owner_only(tmp_path):
    previous = os.umask(0o022)
    try:
        write_json(tmp_path / 'session' / 'settings.json', {'env': {'GCL_SESSION_TOKEN': 'secret'}})
        LocalStore(tmp_path / 'home' / 'state.sqlite')
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / 'session' / 'settings.json').stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / 'session').stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / 'home' / 'state.sqlite').stat().st_mode) == 0o600


def test_capability_lookup_uses_newest_registration(tmp_path, monkeypatch):
    home, store, mid = mission(tmp_path)
    store.note_event(mid, 'runtime.session_capability', {'session_id': 'writer', 'epoch': 1,
                                                       'sha256': capability.digest('old')})
    store.note_event(mid, 'runtime.session_capability', {'session_id': 'writer', 'epoch': 1,
                                                       'sha256': capability.digest('new')})
    monkeypatch.setattr(store, 'events', lambda *a: pytest.fail('capability check scanned the ledger'))
    assert capability.registered(store, mid, 'writer', 1)
    assert not capability.registered(store, mid, 'writer', 2)
    monkeypatch.setenv('GCL_SESSION_TOKEN', 'new')
    assert capability.verify_prepared_worker(store, mid, 'writer', 1)
    monkeypatch.setenv('GCL_SESSION_TOKEN', 'old')
    monkeypatch.setattr(store, 'note_event', lambda *a: None)
    assert not capability.verify_prepared_worker(store, mid, 'writer', 1)


def test_repeated_cancel_records_one_event(tmp_path):
    home, store, mid = mission(tmp_path)
    store.cancel(mid, 'human:owner')
    store.cancel(mid, 'human:owner')
    assert [e['type'] for e in store.events(mid)].count('mission.cancelled') == 1

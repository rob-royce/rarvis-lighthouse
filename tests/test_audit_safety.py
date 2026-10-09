"""Bounded audit regressions; all paths and executors are synthetic."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

from gcl import hooks
from gcl.core import beacon
from gcl.core.store import LocalStore
from gcl.runtime.files import write_json
from gcl.supervisor import Supervisor


def mission(tmp_path):
    home = tmp_path / 'gcl'
    store = LocalStore(home / 'state.sqlite')
    mid = store.create_mission({'owner': 'human:test', 'objective': 'synthetic',
        'owner_instruction': 'keep this instruction', 'done_when': ['tested'],
        'non_goals': ['no expansion'], 'constraints': ['offline'],
        'route_boundaries': {'forbidden': ['outside/**']}, 'acceptance': ['safe'],
        'decision_refs': ['decision:synthetic']}, 'human:test')
    store.activate_initial(mid, 'writer', 'claude')
    store.note_event(mid, 'runtime.config', {'executor': 'codex', 'cwd': str(tmp_path),
                                          'claude_settings': str(tmp_path / 'settings.json')})
    return home, store, mid


@pytest.mark.parametrize('state', ['stale', 'read_only', 'quiescing'])
@pytest.mark.parametrize('tool', ['Write', 'Edit', 'Bash'])
@pytest.mark.parametrize('fault', ['null', 'type', 'io', 'write', 'telemetry_write', 'settings_null'])
def test_pretool_fence_survives_telemetry_failure(tmp_path, monkeypatch, capsys, state, tool, fault):
    home, store, mid = mission(tmp_path)
    sid, epoch = ('writer', 1) if state == 'quiescing' else ('other', 2 if state == 'read_only' else 1)
    store.note_event(mid, 'runtime.session_capability', {'session_id': sid, 'epoch': epoch})
    path = home / 'missions' / mid / 'sessions' / 'launch'
    write_json(path / 'launch.json', {'read_only': state == 'read_only'})
    if state == 'quiescing':
        rid = store.request_rollover(mid, 'test', 1)
        store.advance(rid, 'ROLLOVER_REQUESTED', 'QUIESCING')
    transcript = tmp_path / 'transcript.jsonl'
    transcript.write_text('null\n' if fault == 'null' else '[]\n' if fault == 'type' else '{}\n')
    (tmp_path / 'settings.json').write_text('null' if fault == 'settings_null' else '{}')
    if fault == 'io':
        monkeypatch.setattr(hooks, 'transcript_occupancy', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic')))
    if fault == 'write':
        monkeypatch.setattr(hooks, 'write_json', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic')))
    if fault == 'telemetry_write':
        original_write = hooks.write_json
        def fail_telemetry(target, value):
            if target.name == 'telemetry.json':
                raise OSError('synthetic telemetry write failure')
            return original_write(target, value)
        monkeypatch.setattr(hooks, 'write_json', fail_telemetry)
    payload = {'session_id': sid, 'tool_name': tool, 'tool_input': {'command': 'touch forbidden'},
               'transcript_path': str(transcript), 'cwd': str(tmp_path)}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, 'argv', ['hook', 'claude', 'PreToolUse', '--home', str(home),
        '--mission', mid, '--session', 'launch', '--epoch', str(epoch)])
    assert hooks.main() == 0
    assert json.loads(capsys.readouterr().out)['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('source', ['initial', 'successor', 'compact'])
def test_complete_versioned_contract_on_every_handoff(tmp_path, source):
    home, store, mid = mission(tmp_path)
    controller = Supervisor(home, mid, adapter=object())
    previous_hash = None
    for version in (1, 2):
        if version == 2:
            store.change_goal(mid, {'constraints': ['offline', 'new boundary'],
                                  'owner_instruction': 'x' * 12000 + ' END'}, 'human:test', 'owner:changed')
        current = store.get_beacon(mid)
        assert current["version"] == version
        assert current["hash"] != previous_hash
        previous_hash = current["hash"]
        if source == 'successor':
            text = controller._brief({'repo': {'path': str(tmp_path), 'branch': 'synthetic'}, **{k: [] for k in ('next_step', 'verified_results', 'blockers', 'unverified_claims', 'abandoned_hypotheses', 'pending_effects', 'active_jobs')}})
        else:
            text = hooks.handle('SessionStart', {'session_id': 'writer',
                'source': 'compact' if source == 'compact' else 'startup'}, home, mid, 'launch', 1)['hookSpecificOutput']['additionalContext']
        for key in (*beacon.CONTROLLED_FIELDS, 'version', 'hash'):
            assert f'{key}: ' + json.dumps(current.get(key), ensure_ascii=False) in text


class FakeExecutor:
    def __init__(self):
        self.accepted = {}
        self.fail = False
    def observe_policy(self, sid):
        return {'sandbox': 'danger-full-access', 'approval_policy': 'on-request',
                'policy_evidence': {'source': 'synthetic-receipt',
                                    'fields': {'sandbox': 'observed', 'approval_policy': 'observed'}}}
    def start_turn(self, sid, prompt):
        delivery_id = prompt.split('Delivery ID: ')[1].splitlines()[0]
        assert delivery_id not in self.accepted, 'duplicate RPC side effect'
        self.accepted[delivery_id] = {'turn_id': 'turn-' + delivery_id, 'session_id': sid}
        if self.fail:
            raise OSError('accepted but receipt lost')
        return self.accepted[delivery_id]['turn_id']


def test_outbox_unknown_restart_and_reconciliation(tmp_path):
    home, store, mid = mission(tmp_path)
    seq = store.post_owner_message(mid, 'synthetic owner message', 'human:test')
    executor = FakeExecutor()
    controller = Supervisor(home, mid, adapter=executor)
    executor.fail = True
    assert controller._deliver_owner('writer', 1) is False
    delivery = store.owner_delivery(mid)
    assert delivery['state'] == 'UNKNOWN'
    assert delivery['messages'][0]['seq'] == seq
    restarted = Supervisor(home, mid, adapter=executor)
    assert restarted._deliver_owner('writer', 1) is False
    assert len(executor.accepted) == 1
    store.resolve_owner_delivery(mid, delivery['id'], accepted=True,
        evidence='synthetic executor ledger', turn_id=executor.accepted[delivery['id']]['turn_id'])
    assert restarted._deliver_owner('writer', 1) is None
    assert store.consume_owner_messages(mid, 'writer', 1) == []


def test_outbox_pre_rpc_restart_and_local_receipt_crash(tmp_path, monkeypatch):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'same text', 'human:test')
    store.post_owner_message(mid, 'same text', 'human:test')
    staged = store.prepare_owner_delivery(mid, 'writer', 1)
    assert store.owner_delivery(mid)['state'] == 'PENDING'
    executor = FakeExecutor()
    controller = Supervisor(home, mid, adapter=executor)
    original = controller.store.ack_owner_delivery
    monkeypatch.setattr(controller.store, 'ack_owner_delivery', lambda *a, **k: (_ for _ in ()).throw(OSError('local receipt unavailable')))
    assert controller._deliver_owner('writer', 1) is False
    unknown = LocalStore(store.path).owner_delivery(mid)
    assert unknown['id'] == staged['id']
    assert len(unknown['messages']) == 2
    assert len(executor.accepted) == 1
    monkeypatch.setattr(controller.store, 'ack_owner_delivery', original)
    assert Supervisor(home, mid, adapter=executor)._deliver_owner('writer', 1) is False


def test_explicit_brief_limit_never_silently_truncates():
    with pytest.raises(ValueError, match='contract'):
        beacon.render_brief({'objective': 'x' * 10000, 'constraints': ['boundary']}, max_chars=4000)


def test_polluted_host_environment_is_not_inherited(tmp_path):
    import subprocess
    from tools.run_synthetic_tests import test_environment
    fake_host = tmp_path / 'fake-host'
    fake_host.mkdir()
    marker = fake_host / 'sentinel'
    marker.write_text('unchanged')
    polluted = {'RLH_WORKER_INBOX': str(fake_host / 'inbox'),
                'GCL_SESSION_TOKEN': 'synthetic', 'GIT_DIR': str(fake_host / 'git'),
                'GIT_WORK_TREE': str(fake_host), 'PYTHONPATH': str(fake_host),
                'CLAUDE_CONFIG_DIR': str(fake_host), 'GCL_CODEX_BIN': str(fake_host / 'codex')}
    script = '''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.run_synthetic_tests import test_environment
safe = test_environment(sys.argv[2])
assert not set(sys.argv[3:]) & set(safe)
os.environ.clear()
os.environ.update(safe)
from gcl.cli import main
sys.argv = ['gcl', '--help']
main()
'''
    env = dict(os.environ, **polluted)
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script,
        str(Path(__file__).resolve().parents[1]), str(tmp_path / 'child'), *polluted],
        env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(fake_host.iterdir()) == [marker]
    assert marker.read_text() == 'unchanged'
    for key in polluted:
        assert os.environ.get(key) != polluted[key]


def test_idle_rpc_failure_does_not_consume_cursor(tmp_path, monkeypatch):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'must survive RPC failure', 'human:test')
    class IdleFailure:
        def resume_thread(self, sid): pass
        def signals(self, sid): return {'active_turn': None}
        def start_turn(self, sid, prompt): raise OSError('synthetic RPC failure')
    controller = Supervisor(home, mid, adapter=IdleFailure())
    monkeypatch.setattr(controller, '_observe_worktree', lambda sid: True)
    try:
        controller.tick()
    except OSError:
        pass
    with store._read() as db:
        assert db.execute('SELECT owner_cursor FROM missions WHERE id=?', (mid,)).fetchone()[0] == 0


@pytest.mark.parametrize('state', ['PENDING', 'UNKNOWN'])
def test_delivery_successor_switch_preserves_identity_and_uncertainty(tmp_path, state):
    from test_gcl_core_durability import _prepared
    path, mid, rid = _prepared(tmp_path)
    store = LocalStore(path)
    # Prepare before quiescence using a separate synthetic mission; transfer its
    # already-validated rollover state after staging to simulate the boundary.
    with store._tx() as db:
        db.execute("UPDATE rollovers SET state='ROLLOVER_REQUESTED' WHERE id=?", (rid,))
    store.post_owner_message(mid, 'carry across', 'human:test')
    delivery = store.prepare_owner_delivery(mid, 'old', 1)
    if state == 'UNKNOWN':
        assert store.claim_owner_delivery(mid, delivery['id'], 'old', 1)
    with store._tx() as db:
        db.execute("UPDATE rollovers SET state='SUCCESSOR_PREPARED_READ_ONLY' WHERE id=?", (rid,))
    store.transfer_ownership(rid, 'new', 1)
    rebound = LocalStore(path).prepare_owner_delivery(mid, 'new', 2, 'new contract', 'successor')
    assert rebound['id'] == delivery['id']
    assert rebound['messages'] == delivery['messages']
    if state == 'UNKNOWN':
        assert rebound['session_id'] == 'old'
        assert not store.claim_owner_delivery(mid, delivery['id'], 'new', 2)
        store.resolve_owner_delivery(mid, delivery['id'], accepted=False, evidence='fake executor negative receipt')
        rebound = store.prepare_owner_delivery(mid, 'new', 2, 'new contract', 'successor')
    assert rebound['session_id'] == 'new'
    assert rebound['prefix'] == 'new contract'
    assert store.claim_owner_delivery(mid, delivery['id'], 'new', 2)
    store.ack_owner_delivery(mid, delivery['id'], 'synthetic-turn')
    assert store.owner_delivery(mid) is None
    assert store.prepare_owner_delivery(mid, 'new', 2, 'new contract', 'successor')['state'] == 'ACKED'
    assert store.consume_owner_messages(mid, 'new', 2) == []


def test_claim_before_rpc_crash_does_not_blind_retry(tmp_path):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'not sent yet', 'human:test')
    delivery = store.prepare_owner_delivery(mid, 'writer', 1)
    assert store.claim_owner_delivery(mid, delivery['id'], 'writer', 1)
    executor = FakeExecutor()
    restarted = Supervisor(home, mid, executor)
    assert restarted._deliver_owner('writer', 1) is False
    assert executor.accepted == {}
    store.resolve_owner_delivery(mid, delivery['id'], accepted=False, evidence='fake executor: no matching request')
    assert restarted._deliver_owner('writer', 1) is True
    assert list(executor.accepted) == [delivery['id']]
    assert restarted._deliver_owner('writer', 1) is None


def test_successor_receipt_and_turn_event_commit_together(tmp_path, monkeypatch):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'pending', 'human:test')
    executor = FakeExecutor()
    controller = Supervisor(home, mid, executor)
    original = controller.store._event
    def fail_event(db, mission_id, kind, data=None):
        if kind == 'runtime.turn_started':
            raise OSError('crash writing local turn event')
        return original(db, mission_id, kind, data)
    monkeypatch.setattr(controller.store, '_event', fail_event)
    assert controller._deliver_owner('writer', 1, 'full contract', 'successor') is False
    assert store.owner_delivery(mid)['state'] == 'UNKNOWN'
    with store._read() as db:
        assert db.execute('SELECT owner_cursor FROM missions WHERE id=?', (mid,)).fetchone()[0] == 0
    restarted = Supervisor(home, mid, executor)
    assert restarted._deliver_owner('writer', 1, 'full contract', 'successor') is False
    delivery_id = store.owner_delivery(mid)['id']
    store.resolve_owner_delivery(mid, delivery_id, accepted=True, evidence='fake executor ledger',
                                 turn_id=executor.accepted[delivery_id]['turn_id'])
    assert restarted._deliver_owner('writer', 1, 'full contract', 'successor') is True
    assert len(executor.accepted) == 1
    assert any(e['type'] == 'runtime.turn_started' for e in store.events(mid))


@pytest.mark.parametrize('tool', ['Read', 'Write', 'Edit', 'Bash'])
def test_authorized_writer_remains_allowed_when_telemetry_fails(tmp_path, monkeypatch, tool):
    home, store, mid = mission(tmp_path)
    store.note_event(mid, 'runtime.session_capability', {'session_id': 'writer', 'epoch': 1})
    monkeypatch.setattr(hooks, 'write_json', lambda *a: (_ for _ in ()).throw(OSError('optional')))
    assert hooks.handle('PreToolUse', {'session_id': 'writer', 'tool_name': tool,
        'tool_input': {'command': 'synthetic command'}, 'cwd': str(tmp_path)}, home, mid, 'launch', 1) == {}


@pytest.mark.parametrize('payload', [None, [], {'tool_input': 'bad'}])
def test_unconfirmed_authorization_is_denied(tmp_path, payload):
    home, store, mid = mission(tmp_path)
    result = hooks.handle('PreToolUse', payload, home, mid, 'launch', 1)
    assert result['hookSpecificOutput']['permissionDecision'] == 'deny'


@pytest.mark.parametrize('version', [1, 2])
def test_initial_codex_prompt_contains_complete_contract(tmp_path, monkeypatch, version):
    home, store, original = mission(tmp_path)
    mid = store.create_mission(store.get_beacon(original) | {'mission_id': 'a' * 32}, 'human:test')
    store.note_event(mid, 'runtime.config', {'executor': 'codex', 'cwd': str(tmp_path)})
    if version == 2:
        store.change_goal(mid, {'acceptance': ['changed'], 'decision_refs': ['updated-decision']}, 'human:test', 'owner:changed')
    class Initial:
        def create_thread(self, cwd): return 'initial'
        def start_turn(self, sid, prompt): self.prompt = prompt; return 'turn'
        def set_goal(self, *a): pass
    executor = Initial()
    controller = Supervisor(home, mid, executor)
    monkeypatch.setattr(controller, '_record_thread_policy', lambda sid: None)
    monkeypatch.setattr(controller, '_repo_state', lambda cwd: {'path': str(tmp_path), 'branch': 'synthetic', 'head': 'synthetic'})
    controller._ensure_session()
    current = store.get_beacon(mid)
    for key in (*beacon.CONTROLLED_FIELDS, 'version', 'hash'):
        assert f'{key}: ' + json.dumps(current.get(key), ensure_ascii=False) in executor.prompt


def test_real_successor_drive_stops_on_unknown_then_resumes_after_receipt(tmp_path, monkeypatch):
    from test_gcl_core_durability import _prepared
    path, mid, rid = _prepared(tmp_path)
    Path(path).rename(tmp_path / 'state.sqlite')
    store = LocalStore(tmp_path / 'state.sqlite')
    store.post_owner_message(mid, 'owner instruction across rollover', 'human:test')
    store.note_event(mid, 'runtime.thread_policy', {'session_id': 'old', **FakeExecutor().observe_policy('old')})
    store.note_event(mid, 'runtime.thread_policy', {'session_id': 'new', **FakeExecutor().observe_policy('new')})
    store.transfer_ownership(rid, 'new', 1)
    store.note_event(mid, 'runtime.config', {'executor': 'codex', 'cwd': str(tmp_path / 'repo')})
    class Successor(FakeExecutor):
        def resume_thread(self, sid): pass
        def set_goal(self, *a): pass
        def retire(self, sid): pass
    executor = Successor()
    executor.fail = True
    controller = Supervisor(tmp_path, mid, executor)
    monkeypatch.setattr(controller, '_brief', lambda cp, current=None: beacon.render_brief(current or store.get_beacon(mid)))
    controller._drive(controller._rollovers()[0])
    assert store.rollover_state(rid) == 'OWNERSHIP_TRANSFERRED'
    unknown = store.owner_delivery(mid)
    assert unknown['state'] == 'UNKNOWN'
    restarted = Supervisor(tmp_path, mid, executor)
    monkeypatch.setattr(restarted, '_brief', lambda cp, current=None: beacon.render_brief(current or store.get_beacon(mid)))
    restarted._drive(restarted._rollovers()[0])
    assert len(executor.accepted) == 1
    store.resolve_owner_delivery(mid, unknown['id'], accepted=True, evidence='fake executor receipt',
                                 turn_id=executor.accepted[unknown['id']]['turn_id'])
    restarted.refresh()
    restarted._drive(restarted._rollovers()[0])
    assert store.rollover_state(rid) == 'PREDECESSOR_RETIRED'
    assert len(executor.accepted) == 1


def test_fixture_blocks_polluted_inbox_write(tmp_path):
    import subprocess
    fake_host = tmp_path / 'fake-host'
    fake_host.mkdir()
    child = tmp_path / 'child'
    child.mkdir()
    root = Path(__file__).resolve().parents[1]
    script = '''import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]) / 'tests')]
import pytest
from conftest import isolated_host
isolated_host.__wrapped__(Path(sys.argv[2]), pytest.MonkeyPatch())
from gcl.cli import main
sys.argv = ['gcl', 'note', 'a' * 32, '--session', 'synthetic', '--epoch', '1', 'test note']
raise SystemExit(main())
'''
    env = dict(os.environ, RLH_WORKER_INBOX=str(fake_host), GCL_SESSION_ID='synthetic',
               GCL_EPOCH='1', GIT_DIR=str(fake_host), GIT_WORK_TREE=str(fake_host))
    result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(root), str(child)],
                            env=env, capture_output=True, text=True)
    # Without isolation this command succeeds through the host inbox branch.
    assert result.returncode != 0
    assert list(fake_host.iterdir()) == []

"""Synthetic regressions for pending goal refresh and visible UNKNOWN recovery."""
import json
from pathlib import Path

import pytest

from gcl.cli import _status
from gcl.core.store import LocalStore
from gcl.supervisor import Supervisor
from test_audit_safety import FakeExecutor, mission
from test_gcl_core_durability import _prepared


class CaptureSuccessor(FakeExecutor):
    def __init__(self):
        super().__init__()
        self.prompts = []
    def resume_thread(self, sid): pass
    def set_goal(self, *args): pass
    def retire(self, sid): pass
    def start_turn(self, sid, prompt):
        self.prompts.append(prompt)
        return super().start_turn(sid, prompt)


def successor(tmp_path):
    path, mid, rid = _prepared(tmp_path)
    Path(path).rename(tmp_path / 'state.sqlite')
    store = LocalStore(tmp_path / 'state.sqlite')
    store.post_owner_message(mid, 'preserve this message', 'human:test')
    store.note_event(mid, 'runtime.thread_policy', {'session_id': 'old', **FakeExecutor().observe_policy('old')})
    store.note_event(mid, 'runtime.thread_policy', {'session_id': 'new', **FakeExecutor().observe_policy('new')})
    store.transfer_ownership(rid, 'new', 1)
    store.note_event(mid, 'runtime.config', {'executor': 'codex', 'cwd': str(tmp_path / 'repo')})
    # The golden checkpoint fixture omits branch; add it to the read-only view
    # for this real supervisor prompt path, without weakening checkpoint checks.
    return store, mid, rid


def controller_for(home, store, mid, executor, monkeypatch):
    controller = Supervisor(home, mid, executor)
    get_checkpoint = controller.store.get_checkpoint
    def checkpoint_with_branch(rid):
        cp = get_checkpoint(rid)
        cp['repo']['branch'] = 'synthetic'
        return cp
    monkeypatch.setattr(controller.store, 'get_checkpoint', checkpoint_with_branch)
    return controller


def test_pending_crash_goal_change_resume_sends_current_prompt(tmp_path, monkeypatch):
    store, mid, rid = successor(tmp_path)
    executor = CaptureSuccessor()
    controller = controller_for(tmp_path, store, mid, executor, monkeypatch)
    def crash_before_claim(*args, **kwargs):
        raise OSError('synthetic crash after prepare, before claim/RPC')
    monkeypatch.setattr(controller.store, 'claim_owner_delivery', crash_before_claim)
    with pytest.raises(OSError, match='synthetic crash'):
        controller._drive(controller._rollovers()[0])
    pending = store.owner_delivery(mid)
    assert pending['state'] == 'PENDING'
    assert executor.prompts == []
    old = store.get_beacon(mid)
    store.change_goal(mid, {'objective': 'revised synthetic objective',
        'constraints': ['REVISED boundary'], 'owner_instruction': 'revised owner instruction'},
        'human:test', 'owner:revision')
    current = store.get_beacon(mid)
    resumed = controller_for(tmp_path, store, mid, executor, monkeypatch)
    resumed._drive(resumed._rollovers()[0])
    assert len(executor.prompts) == 1
    prompt = executor.prompts[0]
    assert 'constraints: ["REVISED boundary"]' in prompt
    assert f'hash: "{current["hash"]}"' in prompt
    assert f'hash: "{old["hash"]}"' not in prompt
    assert f'Delivery ID: {pending["id"]}' in prompt
    assert 'preserve this message' in prompt
    with store._read() as db:
        receipt = dict(db.execute('SELECT * FROM owner_outbox WHERE id=?', (pending['id'],)).fetchone())
    assert receipt['beacon_version'] == current['version']
    assert receipt['beacon_hash'] == current['hash']
    assert receipt['state'] == 'ACKED'
    assert store.rollover_state(rid) == 'PREDECESSOR_RETIRED'


def test_unknown_status_requires_reconciliation_without_resend(tmp_path):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'keep pending', 'human:test')
    executor = FakeExecutor()
    executor.fail = True
    controller = Supervisor(home, mid, executor)
    assert controller._deliver_owner('writer', 1) is False
    unknown = store.owner_delivery(mid)
    assert Supervisor(home, mid, executor).tick() is True
    status = _status(store, home, mid)
    assert status['runtime_status'] == 'RECOVERY_REQUIRED'
    assert status['reason'] == 'OWNER_DELIVERY_UNKNOWN'
    assert status['controller'] == 'RECOVERY_REQUIRED'
    recovery = status['owner_delivery_recovery']
    assert recovery['delivery_id'] == unknown['id']
    assert recovery['session_id'] == 'writer'
    assert recovery['epoch'] == 1
    assert 'authoritative executor' in recovery['instruction']
    assert recovery['automatic_retry'] is False
    assert len(executor.accepted) == 1
    store.resolve_owner_delivery(mid, unknown['id'], accepted=True, evidence='fake executor ledger',
        turn_id=executor.accepted[unknown['id']]['turn_id'])
    resolved = _status(LocalStore(store.path), home, mid)
    assert resolved['runtime_status'] != 'RECOVERY_REQUIRED'
    assert resolved['reason'] != 'OWNER_DELIVERY_UNKNOWN'
    assert resolved['owner_delivery_recovery'] is None


def test_goal_change_between_prepare_and_claim_defers_rpc(tmp_path, monkeypatch):
    store, mid, rid = successor(tmp_path)
    executor = CaptureSuccessor()
    controller = controller_for(tmp_path, store, mid, executor, monkeypatch)
    claim = controller.store.claim_owner_delivery
    def change_then_claim(*args, **kwargs):
        store.change_goal(mid, {'constraints': ['CHANGED before claim']}, 'human:test', 'owner:race')
        return claim(*args, **kwargs)
    monkeypatch.setattr(controller.store, 'claim_owner_delivery', change_then_claim)
    controller._drive(controller._rollovers()[0])
    pending = store.owner_delivery(mid)
    assert pending['state'] == 'PENDING'
    assert executor.prompts == []
    resumed = controller_for(tmp_path, store, mid, executor, monkeypatch)
    resumed._drive(resumed._rollovers()[0])
    assert len(executor.prompts) == 1
    assert 'constraints: ["CHANGED before claim"]' in executor.prompts[0]
    assert f'Delivery ID: {pending["id"]}' in executor.prompts[0]


def test_unknown_contract_is_immutable_after_owner_goal_change(tmp_path):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'pending', 'human:test')
    executor = CaptureSuccessor()
    executor.fail = True
    from gcl.core.beacon import render_brief
    controller = Supervisor(home, mid, executor)
    assert controller._deliver_owner('writer', 1, render_brief, 'successor') is False
    unknown = store.owner_delivery(mid)
    assert unknown['state'] == 'UNKNOWN'
    assert unknown['beacon_hash'] == store.get_beacon(mid)['hash']
    store.change_goal(mid, {'objective': 'new objective'}, 'human:test', 'owner:new')
    def must_not_render(current):
        raise AssertionError('UNKNOWN must not be rewritten')
    assert Supervisor(home, mid, executor)._deliver_owner('writer', 1, must_not_render, 'successor') is False
    assert store.owner_delivery(mid) == unknown
    assert len(executor.prompts) == 1
    assert _status(store, home, mid)['reason'] == 'OWNER_DELIVERY_UNKNOWN'


@pytest.mark.parametrize('state', ['PENDING', 'UNKNOWN'])
def test_legacy_outbox_binding_migration_is_safe(tmp_path, state):
    home, store, mid = mission(tmp_path)
    store.post_owner_message(mid, 'legacy message', 'human:test')
    # Reproduce the prior schema in a disposable ledger only. Ledgers from then are unversioned.
    with store._tx() as db:
        db.execute('PRAGMA user_version=0')
        db.execute('DROP TABLE owner_outbox')
        db.execute('CREATE TABLE owner_outbox (id TEXT PRIMARY KEY, mission_id TEXT, session_id TEXT, '
                   'epoch INTEGER, state TEXT, messages TEXT, prefix TEXT, kind TEXT, through_seq INTEGER, receipt TEXT)')
        db.execute('INSERT INTO owner_outbox VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('legacy', mid, 'writer', 1, state, json.dumps([{'seq': 1, 'text': 'legacy message', 'actor': 'human:test'}]),
                    'old unbound prefix', 'successor', 1, None))
    reopened = LocalStore(store.path)
    assert reopened.owner_delivery(mid)['beacon_version'] is None
    assert reopened.owner_delivery(mid)['beacon_hash'] is None
    assert not reopened.claim_owner_delivery(mid, 'legacy', 'writer', 1)
    from gcl.core.beacon import render_brief
    refreshed = reopened.prepare_owner_delivery(mid, 'writer', 1, render_brief, 'successor')
    if state == 'UNKNOWN':
        assert refreshed['beacon_hash'] is None
        assert refreshed['prefix'] == 'old unbound prefix'
        assert refreshed['state'] == 'UNKNOWN'
    else:
        assert refreshed['beacon_hash'] == reopened.get_beacon(mid)['hash']
        assert 'old unbound prefix' not in refreshed['prefix']
        assert reopened.claim_owner_delivery(mid, 'legacy', 'writer', 1)
    # Reopening again must not remigrate or modify the batch.
    assert LocalStore(store.path).owner_delivery(mid) == reopened.owner_delivery(mid)

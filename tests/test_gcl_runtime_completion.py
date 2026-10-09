"""Completion remains mission state even after the supervisor exits."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from gcl.adapters.codex_appserver import CodexAppServer, RequestRejected
from gcl.core.store import LocalStore
from gcl.core.monitor import Policy, evaluate
from gcl.supervisor import Supervisor
from gcl.runtime.files import process_start
from gcl.cli import _policy


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hold_fake(tmp_path, monkeypatch):
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("synthetic\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
                    "commit", "-qm", "synthetic"], cwd=repo, check=True)
    monkeypatch.setenv("GCL_CODEX_BIN", str(ROOT / "tests/fixtures/gcl/fake_codex.py"))
    monkeypatch.setenv("GCL_FAKE_STATE", str(tmp_path / "fake.json"))
    monkeypatch.setenv("GCL_FAKE_DONE_KEEPS_RUNNING", "1")
    monkeypatch.setenv("GCL_FAKE_RESUME_AUTO_TURN", "1")
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "finish",
                                "done_when": ["proof exists"]}, "human:owner", "OBSERVE_ONLY")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo), "policy": {},
                     "runtime_policy": {"idle_nudge_s": 1000}})
    controllers = []

    def start():
        supervisor = Supervisor(home, mid)
        supervisor.directory.mkdir(parents=True, exist_ok=True)
        supervisor.adapter.start()
        controllers.append(supervisor)
        return supervisor

    controller = start()
    controller.tick()
    epoch, sid = store.current_writer(mid)
    store.note_event(mid, "runtime.worker_done", {"epoch": epoch, "session_id": sid})
    yield controller, start, store, mid, sid, tmp_path / "fake.json"
    for supervisor in controllers:
        supervisor.adapter.close()


def test_hold_pauses_goal_and_quiesces_reporting_turn(hold_fake):
    controller, _, store, mid, sid, path = hold_fake
    assert controller.tick() is True
    assert controller.adapter.signals(sid)["active_turn"] is None
    assert json.loads(path.read_text())["threads"][sid]["goal"] == "paused"
    quiesced = [e for e in store.events(mid) if e["type"] == "runtime.completion_hold_quiesced"]
    report = next(e for e in store.events(mid) if e["type"] == "runtime.completion_reported")
    assert len(quiesced) == 1
    assert quiesced[0]["data"]["report_id"] == report["id"]
    assert quiesced[0]["data"]["interrupted"] is True
    assert quiesced[0]["data"]["goal_status"] == "paused"
    before = len(json.loads(path.read_text())["calls"])
    assert controller.tick() is True
    assert len(json.loads(path.read_text())["calls"]) == before
    assert _types(store, mid).count("runtime.completion_hold_quiesced") == 1


def test_restart_while_held_does_not_start_or_keep_a_turn(hold_fake):
    controller, start, store, mid, sid, path = hold_fake
    controller.tick()
    # Simulate native goal drift before reconnect; the resume switch creates a vendor turn.
    controller.adapter.set_goal(sid, None, "active")
    controller.adapter.close()
    before = len(json.loads(path.read_text())["calls"])
    restarted = start()
    assert restarted.tick() is True
    assert restarted.adapter.signals(sid)["active_turn"] is None
    fake = json.loads(path.read_text())
    assert fake["threads"][sid]["goal"] == "paused"
    assert not any(c["method"] == "turn/start" for c in fake["calls"][before:])
    unexpected = [e["data"] for e in store.events(mid) if e["type"] == "runtime.unexpected_turn_while_held"]
    assert unexpected and all(e["origin"] == "vendor" for e in unexpected)
    assert unexpected[-1]["turn_id"] == fake["auto_turns"][-1]["turnId"]
    assert unexpected[-1]["sandbox_policy"] == {"type": "dangerFullAccess"}
    assert _types(store, mid).count("runtime.completion_reported") == 1
    assert _types(store, mid).count("runtime.completion_hold_quiesced") == 1
    assert "mission.completed" not in _types(store, mid)


def test_crash_between_report_and_quiesce_completes_quiesce(hold_fake, monkeypatch):
    controller, start, store, mid, sid, path = hold_fake
    resolve = controller.store.resolve_reported_completion

    def crash(*args, **kwargs):
        assert resolve(*args, **kwargs) == "awaiting"
        raise RuntimeError("synthetic crash after report")

    monkeypatch.setattr(controller.store, "resolve_reported_completion", crash)
    with pytest.raises(RuntimeError, match="synthetic crash after report"):
        controller.tick()
    assert _types(store, mid).count("runtime.completion_reported") == 1
    assert "runtime.completion_hold_quiesced" not in _types(store, mid)
    controller.adapter.close()
    restarted = start()
    assert restarted.tick() is True
    assert restarted.adapter.signals(sid)["active_turn"] is None
    assert json.loads(path.read_text())["threads"][sid]["goal"] == "paused"
    assert _types(store, mid).count("runtime.completion_hold_quiesced") == 1


def test_owner_message_after_quiesce_reactivates_and_delivers(hold_fake):
    controller, _, store, mid, sid, path = hold_fake
    controller.tick()
    store.post_owner_message(mid, "synthetic owner correction", "human:owner")
    for _ in range(3):
        controller.tick()
    fake = json.loads(path.read_text())
    assert fake["threads"][sid]["goal"] == "active"
    delivered = [c for c in fake["calls"] if c["method"] == "turn/start" and
                 "synthetic owner correction" in c["params"]["input"][0]["text"]]
    assert len(delivered) == 1
    assert _types(store, mid).count("runtime.completion_declined") == 1
    assert _types(store, mid).count("runtime.owner_delivered") == 1


def test_owner_confirm_after_quiesce_completes(hold_fake):
    controller, _, store, mid, sid, path = hold_fake
    controller.tick()
    store.confirm_completion(mid, "human:owner", time.time())
    assert controller.tick() is False
    assert json.loads(path.read_text())["threads"][sid]["goal"] == "complete"
    assert _types(store, mid).count("mission.completed") == 1


def test_reporting_turn_finishes_during_hold_grace(hold_fake):
    controller, _, store, mid, sid, path = hold_fake
    release = threading.Timer(.15, lambda: (path.parent / "release-turn").touch())
    release.start()
    try:
        controller.tick()
    finally:
        release.join()
    assert controller.adapter.signals(sid)["active_turn"] is None
    quiesced = next(e["data"] for e in store.events(mid) if e["type"] == "runtime.completion_hold_quiesced")
    assert quiesced["interrupted"] is False and quiesced["goal_status"] == "paused"
    assert not any(c["method"] == "turn/interrupt" for c in json.loads(path.read_text())["calls"])


@pytest.mark.parametrize("origin", ["controller", "unknown"])
def test_unexpected_held_turn_is_audited_even_if_transient(hold_fake, origin):
    controller, _, store, mid, sid, path = hold_fake
    controller.tick()
    if origin == "controller":
        tid = controller.adapter.start_turn(sid, "synthetic unexpected controller turn")
    else:
        tid = "synthetic-transient"
        controller.adapter._handle_notification("turn/started", {"threadId": sid, "turn": {"id": tid}})
        controller.adapter._handle_notification("turn/completed", {"threadId": sid, "turn": {"id": tid}})
    for kind in ("runtime.note", "runtime.evidence", "runtime.worker_done"):
        with pytest.raises(PermissionError, match="completion report is held"):
            store.note_event(mid, kind, {"epoch": 1, "session_id": sid})
    controller.tick()
    controller.tick()
    unexpected = [e["data"] for e in store.events(mid) if e["type"] == "runtime.unexpected_turn_while_held"]
    assert len(unexpected) == 1 and unexpected[0]["origin"] == origin
    assert unexpected[0]["turn_id"] == tid
    assert "sandbox_policy" not in unexpected[0]
    assert any(c["method"] == "turn/interrupt" and c["params"]["turnId"] == tid
               for c in json.loads(path.read_text())["calls"])
    assert _types(store, mid).count("runtime.completion_reported") == 1
    assert controller.adapter.signals(sid)["active_turn"] is None


def test_crash_after_hold_interrupt_retries_without_duplicate_quiesce(hold_fake, monkeypatch):
    controller, start, store, mid, sid, path = hold_fake
    note = controller.note

    def crash(kind, data):
        if kind == "runtime.completion_hold_quiesced":
            raise RuntimeError("synthetic crash after interrupt")
        note(kind, data)

    monkeypatch.setattr(controller, "note", crash)
    with pytest.raises(RuntimeError, match="synthetic crash after interrupt"):
        controller.tick()
    assert controller.adapter.signals(sid)["active_turn"] is None
    controller.adapter.close()
    restarted = start()
    restarted.tick()
    restarted.tick()
    quiesced = [e["data"] for e in store.events(mid) if e["type"] == "runtime.completion_hold_quiesced"]
    assert len(quiesced) == 1 and quiesced[0]["interrupted"] is True


def test_crash_after_unexpected_turn_audit_retries_interrupt(hold_fake, monkeypatch):
    controller, _, store, mid, sid, path = hold_fake
    controller.tick()
    tid = controller.adapter.start_turn(sid, "synthetic unexpected turn")

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic crash before interrupt")

    with monkeypatch.context() as scoped:
        scoped.setattr(controller.adapter, "interrupt_turn", crash)
        with pytest.raises(RuntimeError, match="synthetic crash before interrupt"):
            controller.tick()
    controller.tick()
    unexpected = [e["data"] for e in store.events(mid) if e["type"] == "runtime.unexpected_turn_while_held"]
    assert len(unexpected) == 1 and unexpected[0]["turn_id"] == tid
    assert unexpected[0]["origin"] == "controller"
    assert controller.adapter.signals(sid)["active_turn"] is None


def test_rejected_interrupt_requires_a_completed_turn_receipt(monkeypatch):
    adapter = CodexAppServer()
    adapter._remember({"id": "synthetic-thread"})

    def reject(*args, **kwargs):
        raise RequestRejected("synthetic no active turn")

    monkeypatch.setattr(adapter, "call", reject)
    with pytest.raises(RequestRejected):
        adapter.interrupt_turn("synthetic-thread", "synthetic-turn")
    adapter._handle_notification("turn/completed", {"threadId": "synthetic-thread", "turn": {"id": "synthetic-turn"}})
    assert adapter.interrupt_turn("synthetic-thread", "synthetic-turn") is True


def test_goal_reset_request_does_not_fabricate_native_active_observation(monkeypatch):
    adapter = CodexAppServer()
    adapter._remember({"id": "synthetic-thread", "goal": "complete"})
    monkeypatch.setattr(adapter, "call", lambda *args, **kwargs: {})
    adapter.set_goal("synthetic-thread", "synthetic objective", "active")
    assert adapter.signals("synthetic-thread")["goal_status"] == "complete"
    adapter._handle_notification("thread/goal/updated", {"threadId": "synthetic-thread", "goal": {"status": "active"}})
    assert adapter.signals("synthetic-thread")["goal_status"] == "active"


@pytest.mark.parametrize("signal,reason", [
    ("repeated_error_count", "error_loop"),
    ("turns_without_evidence", "no_progress"),
])
def test_relay_pending_still_detects_stalled_work(signal, reason):
    policy = Policy(min_work_s=0)
    data = {"context_tokens": 210_000, "context_window": 1_000_000,
            "at_chunk_boundary": False, "now": 1000, "session_started_at": 0,
            signal: 10}
    result = evaluate(data, policy, "CONTROLLED_AUTO")
    assert result["action"] == "ROLLOVER"
    assert result["reasons"] == ["relay_pending_chunk_boundary", reason]


class CompleteAdapter:
    def __init__(self):
        self.goal_calls = []

    def resume_thread(self, session_id):
        pass

    def set_goal(self, session_id, objective, status):
        self.goal_calls.append((session_id, objective, status))

    def signals(self, session_id):
        return {"goal_status": "complete", "last_notification_at": None, "active_turn": "in-flight"}

    def quiesce(self, session_id, interrupt):
        raise AssertionError("completion must not interrupt the in-flight turn")


class QuiescenceAdapter(CompleteAdapter):
    def __init__(self, signals):
        self.observation = signals

    def signals(self, session_id):
        return self.observation

    def quiesce(self, session_id, interrupt):
        return False

    def set_goal(self, session_id, objective, status):
        pass


def test_codex_goal_completion_requires_done_check_and_stops_monitoring(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "finish",
                                "done_when": ["proof exists"]}, "human:owner", "OBSERVE_ONLY")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo),
                                              "policy": {}, "done_check": "/bin/test -f proof"})
    store.activate_initial(mid, "codex-thread", "codex")
    adapter = CompleteAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    assert not any(e["type"] == "mission.completed" for e in store.events(mid))
    (repo / "proof").write_text("done\n")
    assert supervisor.tick() is False
    assert supervisor.tick() is False
    assert len([e for e in store.events(mid) if e["type"] == "mission.completed"]) == 1
    assert adapter.goal_calls == [("codex-thread", "finish", "complete")]
    env = dict(os.environ, GCL_HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", "status", mid],
                            cwd=ROOT, env=env, text=True, capture_output=True, check=True)
    assert '"runtime_status": "COMPLETED"' in result.stdout


class GoalAdapter(CompleteAdapter):
    def __init__(self):
        super().__init__()
        self.status = "complete"
        self.active_turn = "in-flight"

    def signals(self, session_id):
        return {"goal_status": self.status, "last_notification_at": None, "active_turn": self.active_turn}

    def wait_turn(self, session_id, turn_id, timeout):
        self.active_turn = None
        return True

    def set_goal(self, session_id, objective, status):
        super().set_goal(session_id, objective, status)
        if status == "paused":
            self.status = status
        elif status == "active":
            self.active_turn = "in-flight"  # Reset is asynchronous; retain the old status until observed.


def _codex_mission(tmp_path, **config):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "finish",
                                "done_when": ["proof exists"]}, "human:owner", "OBSERVE_ONLY")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo), "policy": {}, **config})
    store.activate_initial(mid, "codex-thread", "codex")
    return home, store, mid


def _cli(home, *args, **env):
    environment = {k: v for k, v in os.environ.items() if k not in ("GCL_MANAGED", "GCL_SESSION_TOKEN")}
    result = subprocess.run([sys.executable, "-B", "-m", "gcl.cli", *args], cwd=ROOT, text=True,
                            capture_output=True, env=dict(environment, GCL_HOME=str(home),
                                                          PYTHONDONTWRITEBYTECODE="1", **env))
    return result.returncode, result.stdout


def _types(store, mid):
    return [e["type"] for e in store.events(mid)]


def test_agent_reported_completion_waits_for_the_owner(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    code, out = _cli(home, "confirm", mid)
    assert code == 2 and "no reported completion" in out
    assert supervisor.tick() is True
    assert supervisor.tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 1
    assert "mission.completed" not in _types(store, mid)
    assert adapter.goal_calls == [("codex-thread", None, "paused")]
    assert '"AWAITING_CONFIRMATION"' in _cli(home, "status", mid)[1]
    code, out = _cli(home, "confirm", mid, GCL_MANAGED="1")
    assert code == 2 and "only the owner" in out
    code, out = _cli(home, "confirm", mid)
    assert code == 0 and json.loads(out) == {"confirmed": mid}
    assert supervisor.tick() is False
    completed = [e["data"] for e in store.events(mid) if e["type"] == "mission.completed"]
    assert len(completed) == 1 and completed[0]["confirmed_by"] == "owner"
    assert adapter.goal_calls == [("codex-thread", None, "paused"), ("codex-thread", "finish", "complete")]
    assert '"COMPLETED"' in _cli(home, "status", mid)[1]


def test_confirmation_refuses_unknown_owner_delivery_until_acknowledged(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    store.post_owner_message(mid, "verify the proof", "human:owner")
    delivery = store.prepare_owner_delivery(mid, "codex-thread", 1)
    supervisor = Supervisor(home, mid, GoalAdapter())
    assert supervisor.tick() is True
    assert json.loads(_cli(home, "status", mid)[1])["runtime_status"] == "AWAITING_CONFIRMATION"
    assert store.claim_owner_delivery(mid, delivery["id"], "codex-thread", 1)
    assert store.owner_delivery(mid)["state"] == "UNKNOWN"
    status = json.loads(_cli(home, "status", mid)[1])
    assert status["runtime_status"] == "RECOVERY_REQUIRED"
    assert status["reason"] == "OWNER_DELIVERY_UNKNOWN"
    code, out = _cli(home, "confirm", mid)
    assert code == 2 and "no reported completion is awaiting the owner's confirmation" in out
    assert "mission.completion_confirmed" not in _types(store, mid)
    assert "mission.completed" not in _types(store, mid)
    store.resolve_owner_delivery(mid, delivery["id"], accepted=True,
                                 evidence="synthetic executor receipt", turn_id="synthetic-turn")
    assert json.loads(_cli(home, "status", mid)[1])["runtime_status"] == "AWAITING_CONFIRMATION"
    assert _cli(home, "confirm", mid)[0] == 0
    assert supervisor.tick() is False
    assert _types(store, mid).count("mission.completion_confirmed") == 1
    assert _types(store, mid).count("mission.completed") == 1


@pytest.mark.parametrize("blocked_status", ["RECOVERY_REQUIRED", "CANCELLED", "COMPLETED"])
def test_confirmation_respects_recovery_and_terminal_status(tmp_path, blocked_status):
    home, store, mid = _codex_mission(tmp_path)
    supervisor = Supervisor(home, mid, GoalAdapter())
    assert supervisor.tick() is True
    if blocked_status == "RECOVERY_REQUIRED":
        store.note_event(mid, "mission.recovery_required", {"reason": "synthetic recovery"})
    elif blocked_status == "CANCELLED":
        store.cancel(mid, "human:owner")
    else:
        assert _cli(home, "confirm", mid)[0] == 0
        assert supervisor.tick() is False
    confirmations = _types(store, mid).count("mission.completion_confirmed")
    assert json.loads(_cli(home, "status", mid)[1])["runtime_status"] == blocked_status
    code, out = _cli(home, "confirm", mid)
    assert code == 2 and "no reported completion is awaiting the owner's confirmation" in out
    assert _types(store, mid).count("mission.completion_confirmed") == confirmations
    if blocked_status == "RECOVERY_REQUIRED":
        store.note_event(mid, "mission.recovered", {})
        assert json.loads(_cli(home, "status", mid)[1])["runtime_status"] == "AWAITING_CONFIRMATION"
        assert _cli(home, "confirm", mid)[0] == 0


def test_owner_message_declines_a_reported_completion(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    store.post_owner_message(mid, "not yet: the proof is missing", "human:owner")
    assert supervisor.tick() is True
    assert "runtime.completion_declined" in _types(store, mid)
    assert adapter.goal_calls == [("codex-thread", None, "paused"), ("codex-thread", "finish", "active")]
    assert '"AWAITING_CONFIRMATION"' not in _cli(home, "status", mid)[1]
    # Codex may still report the old status until it applies the reset; that is not a new report.
    assert supervisor.tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 1
    adapter.status = "active"
    assert supervisor.tick() is True
    adapter.status = "complete"
    assert supervisor.tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 2
    assert "mission.completed" not in _types(store, mid)


@pytest.mark.parametrize("confirm_first", [False, True])
def test_owner_message_invalidates_report_before_tick(tmp_path, confirm_first):
    home, store, mid = _codex_mission(tmp_path)
    supervisor = Supervisor(home, mid, GoalAdapter())
    assert supervisor.tick() is True
    if confirm_first:
        assert _cli(home, "confirm", mid)[0] == 0
    store.post_owner_message(mid, "not done: add missing proof", "human:owner")
    if not confirm_first:
        assert _cli(home, "confirm", mid)[0] == 2
    assert supervisor.tick() is True
    assert "mission.completed" not in _types(store, mid)


def test_saved_report_survives_vendor_status_change(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    adapter.status = "active"
    assert _cli(home, "confirm", mid)[0] == 0
    assert supervisor.tick() is False
    assert "mission.completed" in _types(store, mid)


def test_rejected_completion_survives_restart(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    store.post_owner_message(mid, "not done: add missing proof", "human:owner")
    assert supervisor.tick() is True
    assert "runtime.completion_declined" in _types(store, mid)
    assert Supervisor(home, mid, adapter).tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 1


def test_confirmation_races_owner_message(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    supervisor = Supervisor(home, mid, GoalAdapter())
    assert supervisor.tick() is True
    barrier = threading.Barrier(2)

    def confirm():
        barrier.wait(timeout=5)
        return _cli(home, "confirm", mid)[0]

    def message():
        # Each writer opens its own SQLite connection.
        other = LocalStore(home / "state.sqlite")
        barrier.wait(timeout=5)
        return other.post_owner_message(mid, "not done: add missing proof", "human:owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmation = pool.submit(confirm)
        owner_message = pool.submit(message)
        assert confirmation.result(timeout=10) in (0, 2)
        assert owner_message.result(timeout=10) == 1
    events = store.events(mid)
    report = next(e for e in events if e["type"] == "runtime.completion_reported")
    assert next(e for e in events if e["type"] == "owner.message")["id"] > report["id"]
    assert supervisor.tick() is True
    assert "mission.completed" not in _types(store, mid)


@pytest.mark.parametrize("event_type", ["mission.completion_confirmed", "mission.completed"])
def test_completion_decisions_hold_sqlite_writer_lock(tmp_path, monkeypatch, event_type):
    home, store, mid = _codex_mission(tmp_path)
    supervisor = Supervisor(home, mid, GoalAdapter())
    assert supervisor.tick() is True
    if event_type == "mission.completed":
        assert _cli(home, "confirm", mid)[0] == 0
    other = LocalStore(home / "state.sqlite")
    before_write, release, message_started = threading.Event(), threading.Event(), threading.Event()
    append = supervisor.store._event

    def paused_append(db, mission_id, kind, data=None):
        if kind == event_type:
            before_write.set()
            assert release.wait(timeout=5)
        return append(db, mission_id, kind, data)

    monkeypatch.setattr(supervisor.store, "_event", paused_append)

    def decide():
        if event_type == "mission.completed":
            return supervisor.tick()
        return supervisor.store.confirm_completion(mid, "human:owner", time.time())

    def message():
        message_started.set()
        return other.post_owner_message(mid, "not done: add missing proof", "human:owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        decision = pool.submit(decide)
        try:
            assert before_write.wait(timeout=5)
            owner_message = pool.submit(message)
            assert message_started.wait(timeout=5)
            with pytest.raises(TimeoutError):
                owner_message.result(timeout=.1)
        finally:
            release.set()
        decision.result(timeout=10)
        assert owner_message.result(timeout=10) == 1
    events = store.events(mid)
    decided = next(e for e in events if e["type"] == event_type)
    message = next(e for e in events if e["type"] == "owner.message")
    assert decided["id"] < message["id"]
    if event_type == "mission.completion_confirmed":
        assert supervisor.tick() is True
        assert "mission.completed" not in _types(store, mid)
    else:
        # The competing message serialized after completion, never between its check and write.
        assert supervisor.tick() is False


def test_new_completion_after_rejection_and_restart_is_accepted(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    store.post_owner_message(mid, "not done: add missing proof", "human:owner")
    assert supervisor.tick() is True
    assert Supervisor(home, mid, adapter).tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 1
    adapter.status = "active"
    assert Supervisor(home, mid, adapter).tick() is True
    adapter.status = "complete"
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 2
    assert _cli(home, "confirm", mid)[0] == 0
    assert supervisor.tick() is False
    assert _types(store, mid).count("mission.completed") == 1


def test_saved_report_holds_after_restart_with_missing_vendor_status(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    assert Supervisor(home, mid, adapter).tick() is True
    adapter.status = None
    assert Supervisor(home, mid, adapter).tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 1
    assert "runtime.monitor_decision" not in _types(store, mid)
    assert '"AWAITING_CONFIRMATION"' in _cli(home, "status", mid)[1]
    assert _cli(home, "confirm", mid)[0] == 0
    assert Supervisor(home, mid, adapter).tick() is False


def test_missing_vendor_status_does_not_erase_rejection(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    adapter.status = None
    store.post_owner_message(mid, "not done: add missing proof", "human:owner")
    assert '"AWAITING_CONFIRMATION"' not in _cli(home, "status", mid)[1]
    assert supervisor.tick() is True
    assert "runtime.completion_declined" in _types(store, mid)
    assert Supervisor(home, mid, adapter).tick() is True
    adapter.status = "complete"
    assert Supervisor(home, mid, adapter).tick() is True
    assert _types(store, mid).count("runtime.completion_reported") == 1


def test_completion_rechecks_owner_message_after_tick_snapshot(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    assert _cli(home, "confirm", mid)[0] == 0
    signals = adapter.signals

    def message_during_tick(session_id):
        store.post_owner_message(mid, "not done: add missing proof", "human:owner")
        return signals(session_id)

    adapter.signals = message_during_tick
    assert supervisor.tick() is True
    assert "mission.completed" not in _types(store, mid)
    assert "runtime.completion_declined" in _types(store, mid)


def test_goal_reset_is_retried_after_crash(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    store.post_owner_message(mid, "not done: add missing proof", "human:owner")
    set_goal = adapter.set_goal

    def crash(session_id, objective, status):
        assert status == "active"
        raise RuntimeError("synthetic reset crash")

    adapter.set_goal = crash
    with pytest.raises(RuntimeError, match="synthetic reset crash"):
        supervisor.tick()
    assert "runtime.completion_declined" in _types(store, mid)
    adapter.set_goal = set_goal
    assert Supervisor(home, mid, adapter).tick() is True
    assert adapter.goal_calls == [("codex-thread", None, "paused"), ("codex-thread", "finish", "active")]
    assert _types(store, mid).count("runtime.completion_reported") == 1


def test_confirmation_is_bound_to_one_report(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    adapter = GoalAdapter()
    supervisor = Supervisor(home, mid, adapter)
    assert supervisor.tick() is True
    report = next(e for e in store.events(mid) if e["type"] == "runtime.completion_reported")
    assert _cli(home, "confirm", mid)[0] == 0
    confirmation = next(e for e in store.events(mid) if e["type"] == "mission.completion_confirmed")
    assert confirmation["data"]["report_id"] == report["id"]
    store.post_owner_message(mid, "not done: add missing proof", "human:owner")
    assert supervisor.tick() is True
    adapter.status = "active"
    assert supervisor.tick() is True
    adapter.status = "complete"
    assert supervisor.tick() is True
    # The old confirmation cannot approve a new completion report.
    assert supervisor.tick() is True
    assert "mission.completed" not in _types(store, mid)
    assert _cli(home, "confirm", mid)[0] == 0
    assert supervisor.tick() is False
    reports = [e for e in store.events(mid) if e["type"] == "runtime.completion_reported"]
    completed = next(e for e in store.events(mid) if e["type"] == "mission.completed")
    assert completed["data"]["report_id"] == reports[-1]["id"] != report["id"]


def test_unbound_confirmation_requires_owner_to_confirm_again(tmp_path):
    home, store, mid = _codex_mission(tmp_path)
    supervisor = Supervisor(home, mid, GoalAdapter())
    assert supervisor.tick() is True
    store.note_event(mid, "mission.completion_confirmed", {"actor": "human:owner"})
    assert supervisor.tick() is True
    assert "mission.completed" not in _types(store, mid)
    assert _cli(home, "confirm", mid)[0] == 0
    assert supervisor.tick() is False


def test_allow_self_complete_restores_completion_on_the_agents_word(tmp_path):
    home, store, mid = _codex_mission(tmp_path, self_complete=True)
    adapter = GoalAdapter()
    assert Supervisor(home, mid, adapter).tick() is False
    assert "runtime.completion_reported" not in _types(store, mid)
    completed = [e["data"] for e in store.events(mid) if e["type"] == "mission.completed"]
    assert len(completed) == 1 and "confirmed_by" not in completed[0]


def test_codex_goal_notification_reaches_completion_signal():
    adapter = CodexAppServer()
    adapter._remember({"id": "thread", "cwd": "/nonexistent"})
    adapter._handle_notification("thread/goal/updated", {"threadId": "thread", "goal": {"status": "complete"}})
    assert adapter.signals("thread")["goal_status"] == "complete"
    resumed = CodexAppServer()
    resumed._remember({"id": "resumed", "cwd": "/nonexistent", "goal": "complete"})
    assert resumed.signals("resumed")["goal_status"] == "complete"


@pytest.mark.parametrize("signal,expected", [
    ({"context_tokens": 90, "context_window": 100, "turn_started_at": None}, "hard_ratio"),
    ({"context_tokens": 10, "context_window": 100, "turn_started_at": time.monotonic() - 30}, "max_turn_s"),
])
def test_runtime_policy_drives_quiescence_cause(tmp_path, signal, expected):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    store = LocalStore(home / "state.sqlite")
    mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                               "human:owner", "CONTROLLED_AUTO")
    store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo),
                                              "policy": {}, "runtime_policy": {"hard_ratio": .8,
                                                                                "max_turn_s": 10}})
    store.activate_initial(mid, "thread", "codex")
    store.note_event(mid, "runtime.session_created", {"epoch": 1, "session_id": "thread"})
    rid = store.request_rollover(mid, "context_pressure", 1)
    store.note_event(mid, "runtime.rollover_trigger", {"rollover_id": rid, "trigger": "MONITOR"})
    supervisor = Supervisor(home, mid, QuiescenceAdapter(dict(signal, active_turn="turn")))
    supervisor.tick()
    causes = [e["data"]["cause"] for e in store.events(mid) if e["type"] == "runtime.quiescence"]
    assert causes == [expected]


class RelayAdapter:
    def __init__(self, tokens, active_turn="turn"):
        self.tokens, self.active_turn = tokens, active_turn
        self.interrupts = []

    def resume_thread(self, session_id):
        pass

    def set_goal(self, session_id, objective, status):
        pass

    def signals(self, session_id):
        return {"context_tokens": self.tokens, "context_window": 1_000_000,
                "active_turn": self.active_turn, "turn_started_at": time.monotonic()}

    def quiesce(self, session_id, interrupt):
        self.interrupts.append(interrupt)
        return False


def test_relay_runtime_hard_interrupt_soft_boundary_and_uninterruptible_defer(tmp_path):
    assert _policy(["relay_soft_tokens=200000", "relay_hard_tokens=300000"]) == {
        "relay_soft_tokens": 200000.0, "relay_hard_tokens": 300000.0}
    for label, tokens, note, job, action in (
        ("soft", 210_000, False, False, "CONTINUE"),
        ("boundary", 210_000, True, False, "ROLLOVER"),
        ("hard", 310_000, False, False, "ROLLOVER"),
        ("defer", 310_000, False, True, "DEFER"),
    ):
        repo = tmp_path / label
        repo.mkdir()
        home = tmp_path / (label + "-home")
        store = LocalStore(home / "state.sqlite")
        mid = store.create_mission({"owner": "human:owner", "objective": "work"},
                                   "human:owner", "CONTROLLED_AUTO")
        store.note_event(mid, "runtime.config", {"executor": "codex", "cwd": str(repo),
                                                  "policy": {"min_work_s": 0}})
        store.activate_initial(mid, "thread", "codex")
        store.note_event(mid, "runtime.session_created", {"epoch": 1, "session_id": "thread",
                                                        "at": time.time() - 1000})
        supervisor = Supervisor(home, mid, RelayAdapter(tokens))
        if note:
            store.note_event(mid, "runtime.note", {"epoch": 1, "text": "milestone"})
        if job:
            store.note_event(mid, "runtime.job", {"epoch": 1, "pid": os.getpid(),
                                                   "process_start": process_start(os.getpid()),
                                                   "uninterruptible": True})
        supervisor.tick()
        decision = next(e["data"] for e in reversed(store.events(mid))
                        if e["type"] == "runtime.monitor_decision")
        assert decision["action"] == action
        if action == "ROLLOVER":
            supervisor.tick()
            assert supervisor.adapter.interrupts == [label == "hard"]
        else:
            assert not any(e["type"] == "runtime.rollover_trigger" for e in store.events(mid))
        if job:
            assert any(e["type"] == "runtime.relay_deferred" for e in store.events(mid))

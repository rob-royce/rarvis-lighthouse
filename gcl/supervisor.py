"""One external controller per standalone mission; the store is its authority."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from gcl.adapters import create_adapter
from gcl.adapters.codex_appserver import (AppServerError, CodexAppServer, NotSentError,
                                          RequestRejected, classify_error)
from gcl.core import beacon as beacon_api, monitor, snapshot, verify
from gcl.core.errors import MissionCancelled, StateMoved
from gcl.core.store import LocalStore, completion_state
from gcl.runtime.files import home_path, identity, mission_dir, read_json, write_json, process_start
from gcl.runtime.worker_commands import worker_prefix


# A reporting turn may finish naturally for two seconds after its durable report.
# Restarts use the report timestamp, so they cannot renew this grace period.
COMPLETION_HOLD_GRACE_S = 2.0


def _latest(events: list[dict], event_type: str, **matches) -> dict | None:
    for event in reversed(events):
        if event["type"] == event_type and all(event["data"].get(k) == v for k, v in matches.items()):
            return event["data"]
    return None


class Supervisor:
    def __init__(self, home: Path, mission: str, adapter=None):
        self.home, self.mission = home, mission
        self.directory = mission_dir(home, mission)
        self.store = LocalStore(home / "state.sqlite")
        self.events = self.store.events(mission)
        config = _latest(self.events, "runtime.config")
        if config is None:
            raise ValueError("mission runtime configuration missing")
        self.config = config
        self.adapter = adapter or create_adapter(config["executor"], config, self.directory)
        if adapter is None and config["executor"] == "codex":
            self.adapter.data_home = str(home.resolve())
            self.adapter.codex_sandbox = config.get("codex_sandbox", "inherit")
            self.adapter.approval_policy = config.get("approval_policy", "inherit")
            self.adapter.recorded_policies = {
                e["data"]["session_id"]: e["data"] for e in self.events
                if e["type"] == "runtime.thread_policy"}
        if isinstance(self.adapter, CodexAppServer):
            # Restore gate-checked writer policies after a restart.
            for event in self.events:
                if event["type"] == "runtime.policy_continuity_checked":
                    self.adapter.writer_policies[event["data"]["successor_session_id"]] = (
                        CodexAppServer.wire_policy(event["data"]["successor_policy"]))
            self.adapter.before_turn = self._record_turn_policy
        self.poll_interval = float(os.environ.get("GCL_POLL_INTERVAL", "5"))
        self.policy = monitor.Policy(**config.get("policy", {}))
        runtime_policy = config.get("runtime_policy", {})
        self.hard_ratio = float(runtime_policy.get("hard_ratio", .95))
        self.max_turn_s = float(runtime_policy.get("max_turn_s", 3600))
        self.chunk_wait_s = float(runtime_policy.get("chunk_wait_s", 900))
        self.idle_nudge_s = float(runtime_policy.get("idle_nudge_s", 60))
        self.idle_nudges = int(runtime_policy.get("idle_nudges", 2))
        self.delivery_backoff_max_s = float(runtime_policy.get("delivery_backoff_max_s", 300))
        self.read_only_turn_s = min(3600.0, max(30.0, float(runtime_policy.get("read_only_turn_s", 300))))
        self.writer_guard = lambda: None
        self._boundary_event_id = self.events[-1]["id"] if self.events else 0

    def note(self, kind: str, data: dict) -> None:
        self.note_all((kind, data))

    def note_all(self, *events: tuple[str, dict]) -> None:
        """Record a group of related events atomically, so no reader or crash sees part of it."""
        self.store.note_events(self.mission, list(events))
        self.events = self.store.events(self.mission)

    def refresh(self) -> None:
        self.events = self.store.events(self.mission)

    def _record_thread_policy(self, session_id: str) -> None:
        policy = self.adapter.effective_policy(session_id)
        self.adapter.recorded_policies[session_id] = policy
        self.note("runtime.thread_policy", {"session_id": session_id, **policy})

    def _record_turn_policy(self, session_id: str, sent: dict) -> None:
        self.note("runtime.turn_policy_sent", {"session_id": session_id, **sent, "at": time.time()})

    def _policy_continuity(self, rid: str, epoch: int, predecessor: str, successor: str) -> bool:
        if self.config["executor"] != "codex":
            return True
        blocked = _latest(self.events, "runtime.policy_continuity_blocked", rollover_id=rid)
        if blocked:
            if not _latest(self.events, "mission.recovery_required", rollover_id=rid, reason=blocked["reason"]):
                self._recover(rid, blocked["reason"], predecessor)
            return False  # A later vendor default cannot automatically clear a policy hold.
        baseline = _latest(self.events, "runtime.thread_policy", session_id=predecessor) or {}
        # The successor's own thread/start receipt is its policy baseline.
        start = _latest(self.events, "runtime.thread_policy", session_id=successor) or {}
        observed = {}
        try:
            observe_policy = getattr(self.adapter, "observe_policy", None)
            if observe_policy is not None:
                observed = observe_policy(successor)
        except AppServerError:
            pass  # No fresh receipt: persist only the policy hold, never raw RPC content.
        cwd = self.config["cwd"]
        home = str(self.home.resolve())
        before, before_unknown = CodexAppServer.normalize_policy(baseline, baseline.get("cwd") or cwd, home)
        started, start_unknown = CodexAppServer.normalize_policy(start, start.get("cwd") or cwd, home)
        after, after_unknown = CodexAppServer.normalize_policy(observed, observed.get("cwd") or cwd, home)
        requested, requested_unknown = CodexAppServer.normalize_policy({
            "sandbox": self.config.get("codex_sandbox", "inherit"),
            "approval_policy": self.config.get("approval_policy", "inherit")}, cwd, home)
        if self.config.get("codex_sandbox", "inherit") == "inherit":
            requested.pop("sandbox")
            requested_unknown = [f for f in requested_unknown if not f.startswith("sandbox.")]
        if self.config.get("approval_policy", "inherit") == "inherit":
            requested.pop("approval_policy")
            requested_unknown = [f for f in requested_unknown if f != "approval_policy"]
        evidence = observed.get("policy_evidence", {})
        start_evidence = start.get("policy_evidence", {})
        # Codex keeps a turn's sandboxPolicy on the thread, so thread/resume reports the
        # controller's own read-only verification turn. Only that exact override, recorded
        # as this controller's latest turn on the successor, is replaced by the start receipt.
        sent = _latest(self.events, "runtime.turn_policy_sent", session_id=successor) or {}
        override = bool(sent.get("read_only") and sent.get("sandbox_policy") == {"type": "readOnly"}
                        and after["sandbox"]["type"] == "readOnly"
                        and evidence.get("fields", {}).get("sandbox") == "observed")
        effective = dict(after, sandbox=started["sandbox"]) if override else after
        if override:
            after_unknown = [f for f in after_unknown if not f.startswith("sandbox.")]
        unknown = [*before_unknown, *start_unknown, *after_unknown, *requested_unknown]
        for field in ("sandbox", "approval_policy"):
            if evidence.get("fields", {}).get(field) != "observed":
                unknown.append(field)
            if start_evidence.get("fields", {}).get(field) != "observed":
                unknown.append(field)

        def differences(left, right, prefix=""):
            fields = []
            for key in left.keys() | right.keys():
                name = prefix + key
                a, b = left.get(key), right.get(key)
                if isinstance(a, dict) and isinstance(b, dict):
                    fields.extend(differences(a, b, name + "."))
                elif a != b:
                    fields.append(name)
            return fields

        differing = differences(before, started) + differences(before, effective)
        for policy in (started, effective):
            differing.extend(differences(requested, {k: policy[k] for k in requested}))
        binding = {"rollover_id": rid, "predecessor_session_id": predecessor,
                   "successor_session_id": successor, "epoch": epoch,
                   "predecessor_policy": before, "successor_policy": started,
                   "successor_observed_policy": after, "controller_read_only_override": override,
                   "requested_policy": requested,
                   "differing_fields": sorted(set(differing + unknown)), "evidence": evidence,
                   "start_evidence": start_evidence}
        if unknown or differing:
            reason = "successor_policy_unknown" if unknown else "successor_policy_mismatch"
            self.note("runtime.policy_continuity_blocked", dict(binding, reason=reason))
            self._recover(rid, reason, predecessor)
            return False
        self.note("runtime.policy_continuity_checked", binding)
        if isinstance(self.adapter, CodexAppServer):
            # Every successor writer turn sends this gate-checked policy explicitly.
            self.adapter.writer_policies[successor] = CodexAppServer.wire_policy(started)
        return True

    def _rollovers(self) -> list[dict]:
        with self.store._read() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM rollovers WHERE mission_id=? ORDER BY rowid", (self.mission,))]

    def _cancelled(self) -> bool:
        with self.store._read() as db:
            return bool(db.execute("SELECT cancelled FROM missions WHERE id=?", (self.mission,)).fetchone()[0])

    def _session(self, epoch: int) -> str | None:
        data = _latest(self.events, "runtime.session_created", epoch=epoch)
        if data:
            return data["session_id"]
        current_epoch, current_sid = self.store.current_writer(self.mission)
        return current_sid if current_epoch == epoch else None

    def _repo_state(self, cwd: str) -> dict:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                              check=True, capture_output=True, text=True).stdout.strip()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              check=True, capture_output=True, text=True).stdout.strip()
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=root,
                                check=True, capture_output=True, text=True).stdout.strip()
        return {"path": root, "branch": branch, "head": head}

    def _clean_commit(self, sid: str, branch: str, head: str | None = None) -> dict | None:
        """A safe point is a commit on the same branch with no pending files."""
        state = self._repo_state(self._effective_workdir(sid))
        if not branch or state["branch"] != branch or (head and state["head"] != head):
            return None
        status = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain=v1",
                                 "--untracked-files=normal"], cwd=state["path"],
                                check=True, capture_output=True).stdout
        return state if not status else None

    def _effective_workdir(self, sid: str) -> str:
        hook = _latest(self.events, "runtime.hook_path", session_id=sid) or {}
        return self._repo_state(hook.get("cwd") or self.config["cwd"])["path"]

    def _observe_worktree(self, sid: str) -> bool:
        if not _latest(self.events, "runtime.base_head"):
            return True
        state = self._repo_state(self._effective_workdir(sid))
        seen = _latest(self.events, "runtime.worktree_seen", path=state["path"])
        base = _latest(self.events, "runtime.base_head")
        if base and seen is None and state["head"] != base["head"]:
            self.note_all(("runtime.goal_boundary_red", {"reason": "foreign_commit_worktree", **state}),
                          ("runtime.blocked", {"reason": "foreign_commit_worktree"}))
            return False
        if seen is None or seen["head"] != state["head"] or seen["branch"] != state["branch"]:
            self.note("runtime.worktree_seen", {"session_id": sid, **state})
        return True

    def _unmanaged_writer(self) -> bool:
        """Fail closed on new Claude transcripts outside the managed session ledger."""
        if self.config.get("executor") != "claude":
            return False
        if _latest(self.events, "runtime.unmanaged_writer_detected"):
            return True  # An owner must resolve the boundary; disappearance is not clearance.
        config_event = next(e for e in self.events if e["type"] == "runtime.config")
        started_at = datetime.fromisoformat(config_event["created_at"]).replace(
            tzinfo=timezone.utc).timestamp()
        roots = {self.config["cwd"]}
        roots.update(e["data"]["path"] for e in self.events
                     if e["type"] == "runtime.worktree_seen" and e["data"].get("path"))
        roots.update(e["data"]["cwd"] for e in self.events
                     if e["type"] == "runtime.hook_path" and e["data"].get("cwd"))
        managed = {e["data"]["session_id"] for e in self.events
                   if e["type"] == "runtime.session_capability"}
        projects = Path(self.config.get("claude_projects_root") or
                        os.environ.get("GCL_CLAUDE_PROJECTS_DIR", "~/.claude/projects")).expanduser()
        for cwd in roots:
            # Claude Code maps every non-alphanumeric path character to "-".
            slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
            directory = projects / slug
            if not directory.is_dir():
                continue
            for transcript in directory.glob("*.jsonl"):
                try:
                    recent = transcript.stat().st_mtime >= started_at
                except OSError:
                    continue
                if recent and transcript.stem not in managed:
                    data = {"reason": "unmanaged_writer_detected", "transcript_path": str(transcript),
                            "session_id": transcript.stem, "cwd": cwd, "at": time.time()}
                    self.note_all(("runtime.unmanaged_writer_detected", data),
                                  ("runtime.goal_boundary_red", data), ("runtime.blocked", data))
                    return True
        return False

    def _ensure_session(self) -> tuple[int, str]:
        epoch, sid = self.store.current_writer(self.mission)
        if epoch:
            if self.config["executor"] != "claude":
                self.adapter.resume_thread(sid)
            return epoch, sid
        brief = beacon_api.render_brief(self.store.get_beacon(self.mission))
        if hasattr(self.adapter, "prepare_launch"):
            self.adapter.prepare_launch(1, brief)
        self.writer_guard()
        sid = self.adapter.create_thread(self.config["cwd"])
        if self.config["executor"] == "codex":
            self._record_thread_policy(sid)
        self.note("runtime.session_created", {"epoch": 1, "session_id": sid, "at": time.time()})
        initial = self._repo_state(self.config["cwd"])
        self.note("runtime.base_head", initial)
        self.note("runtime.worktree_seen", {"session_id": sid, **initial})
        self._boundary_event_id = self.events[-1]["id"]
        self.store.activate_initial(self.mission, sid, self.config["executor"])
        turn_id = self.adapter.start_turn(sid, brief + self._worker_guidance(1, sid))
        self.adapter.set_goal(sid, self.store.get_beacon(self.mission)["objective"], "active")
        self.note("runtime.turn_started", {"epoch": 1, "session_id": sid, "kind": "initial", "turn_id": turn_id})
        return 1, sid

    def _checkpoint(self, rid: str, sid: str) -> dict:
        current = self.store.get_beacon(self.mission)
        workdir = self._effective_workdir(sid)
        if not self._observe_worktree(sid):
            raise ValueError("foreign_commit_worktree")
        manifest = snapshot.capture(workdir, self.directory / "snapshots" / rid)
        repo = self._repo_state(workdir)
        with self.store._read() as db:
            cursor = db.execute("SELECT owner_cursor FROM missions WHERE id=?", (self.mission,)).fetchone()[0]
            effects = [dict(row) for row in db.execute(
                "SELECT key,kind,state FROM effects WHERE mission_id=? AND state IN ('IN_FLIGHT','OUTCOME_UNKNOWN')",
                (self.mission,))]
        notes = [e["data"] for e in self.events if e["type"] == "runtime.note"]
        evidence = [e["data"] for e in self.events if e["type"] == "runtime.evidence"]
        return {"mission_id": self.mission, "predecessor_session_id": sid,
                "beacon_version": current["version"], "beacon_hash": current["hash"],
                "phase": "working", "next_step": {"action": "continue toward acceptance"},
                "verified_results": evidence, "blockers": [], "unverified_claims": notes,
                "abandoned_hypotheses": [], "repo": repo,
                "snapshot": manifest, "active_jobs": [e["data"] for e in self.events if e["type"] == "runtime.job"],
                "pending_effects": effects, "owner_cursor": cursor, "resume_method": "fresh"}

    def _brief(self, checkpoint: dict, beacon: dict | None = None) -> str:
        beacon = beacon if beacon is not None else self.store.get_beacon(self.mission)
        return (beacon_api.render_brief(beacon) + "\nCheckpoint (verify against workspace first):\n" +
                f"Continue in the exact worktree {checkpoint['repo']['path']} on branch {checkpoint['repo']['branch']}.\n" +
                json.dumps({k: checkpoint[k] for k in ("next_step", "verified_results", "blockers",
                   "unverified_claims", "abandoned_hypotheses", "pending_effects", "active_jobs")},
                   ensure_ascii=False) + "\nDo not re-issue OUTCOME_UNKNOWN effects. Continue only after verification.")

    def _successor_brief(self, checkpoint: dict, epoch: int) -> str:
        return ("Read-only verification: verify the workspace against this checkpoint using read-only tools.\n" +
                self._brief(checkpoint) +
                "\nYour SessionStart context supplies the exact rlh verified and rlh await-writer commands "
                f"for epoch {epoch}. Run them in that order. await-writer blocks until ownership is granted. "
                "After WRITER GRANTED, act on its owner messages and continue the task in this same turn. "
                "On NOT GRANTED: stop now; never write while waiting.")

    def _worker_guidance(self, epoch: int, session_id: str) -> str:
        prefix = worker_prefix(self.home)
        fence = f"{self.mission} --session {shlex.quote(session_id)} --epoch {epoch}"
        workdir = self._effective_workdir(session_id)
        return ("\nRecord progress with these worker commands (replace quoted examples):\n"
                f"{prefix} note {fence} 'observation'\n"
                f"{prefix} evidence {fence} --claim 'verified result' --path {shlex.quote(workdir + '/proof.txt')}\n"
                f"{prefix} effect {fence} begin EFFECT_KEY\n"
                f"{prefix} effect {fence} confirm EFFECT_KEY --receipt '{{}}'\n"
                f"{prefix} job {fence} register --id JOB_ID --cmd 'command' --log {shlex.quote(workdir + '/job.log')}\n"
                f"{prefix} done {fence}  # only after the goal is complete\n"
                f"If you create a worktree, name it gcl-{self.mission[:8]}.\n"
                "For a long job that must finish before relay, append --pid with its live PID and --uninterruptible.\n"
                "The evidence file and job log paths must exist when used. Do not replay completed effects.")

    def _complete_if_reported(self, epoch: int, sid: str, signals: dict) -> str | None:
        """Return "completed", "awaiting" while the owner must confirm, or None to keep working."""
        command = self.config.get("done_check")
        if not command and not self.config.get("self_complete"):
            completion = self.store.resolve_reported_completion(
                self.mission, epoch, sid, goal_status=signals.get("goal_status"),
                reset_goal=self.config["executor"] == "codex", at=time.time(),
                reporting_turn_id=signals.get("active_turn"))
            self.refresh()
            if completion == "reset":
                self.adapter.set_goal(sid, self.store.get_beacon(self.mission)["objective"], "active")
                return None
            if completion == "completed" and self.config["executor"] == "codex":
                self.adapter.set_goal(sid, self.store.get_beacon(self.mission)["objective"], "complete")
            if completion == "awaiting":
                self._quiesce_completion_hold(sid, completion_state(self.events)["report"])
            return completion
        declined = max((e["id"] for e in self.events if e["type"] == "runtime.completion_declined"), default=0)
        goal_complete = signals.get("goal_status") == "complete"
        done = goal_complete or any(
            e["type"] == "runtime.worker_done" and e["id"] > declined and e["data"].get("epoch") == epoch and
            e["data"].get("session_id") == sid for e in self.events)
        if not done:
            return None
        if command:
            try:
                result = subprocess.run(shlex.split(command), cwd=self.config["cwd"],
                                        capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.note("runtime.done_check_failed", {"error": str(exc)})
                return None
            if result.returncode:
                self.note("runtime.done_check_failed", {"exit_code": result.returncode})
                return None
        if self.config["executor"] == "codex":
            self.adapter.set_goal(sid, self.store.get_beacon(self.mission)["objective"], "complete")
        completed = {"epoch": epoch, "session_id": sid, "done_check": command, "at": time.time()}
        self.note("mission.completed", completed)
        return "completed"

    def _quiesce_completion_hold(self, sid: str, report: dict) -> bool:
        """Restore a Codex hold from the ledger before any delivery or monitor work."""
        if self.config["executor"] != "codex":
            return True
        report_id = report["id"]
        quiesced = _latest(self.events, "runtime.completion_hold_quiesced", report_id=report_id)
        signals = self.adapter.signals(sid)
        reporting_turn = report["data"].get("turn_id")
        active = signals.get("active_turn")
        if not quiesced and active and active == reporting_turn:
            remaining = min(COMPLETION_HOLD_GRACE_S,
                            max(0, COMPLETION_HOLD_GRACE_S - (time.time() - report["data"].get("at", 0))))
            if not self.adapter.wait_turn(sid, active, remaining):
                self.note("runtime.completion_hold_interrupt_requested",
                          {"report_id": report_id, "session_id": sid, "turn_id": active})
                if not self.adapter.quiesce(sid, interrupt=True):
                    self.adapter.set_goal(sid, None, "paused")
                    return False
            signals = self.adapter.signals(sid)
        # Pause before stopping unexpected turns, preventing another native continuation.
        if signals.get("goal_status") != "paused":
            self.adapter.set_goal(sid, None, "paused")
        starts = self.adapter.drain_turn_starts(sid) if hasattr(self.adapter, "drain_turn_starts") else []
        signals = self.adapter.signals(sid)
        active = signals.get("active_turn")
        if active and not any(t["turn_id"] == active for t in starts):
            observation = {"turn_id": active, "origin": signals.get("active_turn_origin", "unknown")}
            if signals.get("active_turn_sandbox_policy") is not None:
                observation["sandbox_policy"] = signals["active_turn_sandbox_policy"]
            starts.append(observation)
        for turn in starts:
            tid = turn["turn_id"]
            if not tid or (not quiesced and tid == reporting_turn and tid != active):
                continue  # The reporting turn finished during grace or its bounded interrupt.
            if any(e["type"] in ("runtime.turn_started", "runtime.owner_delivered", "runtime.idle_nudge")
                   and e["data"].get("turn_id") == tid and e["data"].get("session_id") == sid
                   for e in self.events):
                turn["origin"] = "controller"
            if not _latest(self.events, "runtime.unexpected_turn_while_held", report_id=report_id, turn_id=tid):
                self.note("runtime.unexpected_turn_while_held",
                          {"report_id": report_id, "session_id": sid, **turn})
            if not self.adapter.interrupt_turn(sid, tid):
                self.adapter.set_goal(sid, None, "paused")
                return False
            self.adapter.set_goal(sid, None, "paused")
        signals = self.adapter.signals(sid)
        if signals.get("active_turn") or signals.get("goal_status") != "paused":
            return False
        if not quiesced:
            interrupted = any(e["type"] in ("runtime.completion_hold_interrupt_requested",
                                            "runtime.unexpected_turn_while_held") and
                              e["data"].get("report_id") == report_id for e in self.events)
            self.note("runtime.completion_hold_quiesced", {"report_id": report_id,
                      "session_id": sid, "interrupted": interrupted, "goal_status": "paused"})
        return True

    def _progress_since_checkpoint(self, previous: dict | None, epoch: int) -> bool:
        if previous is None:
            return True
        checkpoint = self.store.get_checkpoint(previous["id"])
        if checkpoint is None:
            return False
        boundary = next((e["id"] for e in reversed(self.events) if e["type"] == "checkpoint.saved"
                         and e["data"]["id"] == previous["id"]), 0)
        if any(e["id"] > boundary and e["type"] in ("runtime.note", "runtime.evidence")
               and e["data"].get("epoch") == epoch for e in self.events):
            return True
        return not snapshot.verify(checkpoint["snapshot"], checkpoint["repo"]["path"])

    def _recover(self, rid: str, reason: str, predecessor: str | None) -> None:
        state = self.store.rollover_state(rid)
        if state in ("ROLLOVER_REQUESTED", "QUIESCING", "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY"):
            created = _latest(self.events, "runtime.successor_created", rollover_id=rid)
            if created:
                try:
                    self.adapter.retire(created["session_id"])
                except AppServerError:
                    pass
            self.store.abort_rollover(rid, reason)
            if predecessor and not self._cancelled():
                try:
                    self.adapter.set_goal(predecessor, self.store.get_beacon(self.mission)["objective"], "active")
                except AppServerError:
                    pass
        self.note_all(("mission.recovery_required", {"reason": reason, "rollover_id": rid}),
                      ("runtime.blocked", {"reason": reason}))

    def _exhaust_chunk_wait(self, rid: str, predecessor: str, started: float, active: bool) -> None:
        self.store.abort_rollover(rid, "chunk_wait_exhausted")
        self.note("runtime.chunk_wait_exhausted", {"rollover_id": rid,
                  "elapsed_s": time.time() - started, "at": time.time()})
        if not active:
            self.adapter.start_turn(predecessor, "Continue the current task after the deferred chunk relay.")
        self.adapter.set_goal(predecessor, self.store.get_beacon(self.mission)["objective"], "active")

    def _drive(self, row: dict) -> None:
        rid, state, epoch = row["id"], row["state"], row["epoch"]
        predecessor = self._session(epoch) or self.store.get_checkpoint(rid) and self.store.get_checkpoint(rid)["predecessor_session_id"]
        if self._cancelled():
            if state not in ("OWNERSHIP_TRANSFERRED", "SUCCESSOR_ACTIVE", "PREDECESSOR_RETIRED"):
                self.store.abort_rollover(rid, "cancelled")
            return
        if predecessor and state in ("ROLLOVER_REQUESTED", "QUIESCING", "CHECKPOINT_READY",
                                     "SUCCESSOR_PREPARED_READ_ONLY") and not self._observe_worktree(predecessor):
            self.store.abort_rollover(rid, "foreign_commit_worktree")
            self.note("runtime.blocked", {"reason": "foreign_commit_worktree"})
            return
        if state == "ROLLOVER_REQUESTED":
            trigger = _latest(self.events, "runtime.rollover_trigger", rollover_id=rid) or {}
            if trigger.get("trigger") == "MONITOR":
                if self.config["executor"] != "claude":
                    self.adapter.resume_thread(predecessor)
                signals = self.adapter.signals(predecessor)
                active = signals.get("active_turn")
                if active:
                    tokens, window = signals.get("context_tokens"), signals.get("context_window")
                    occupancy = tokens / window if isinstance(tokens, (int, float)) and isinstance(window, (int, float)) and window > 0 else None
                    started = signals.get("turn_started_at")
                    elapsed = time.monotonic() - started if isinstance(started, (int, float)) else 0
                    cause = (row.get("reason") if row.get("reason") in ("hard_ratio", "max_turn_s") else
                             "relay_hard" if trigger.get("urgency") == "now" else
                             "chunk_boundary" if trigger.get("urgency") == "chunk_boundary" else
                             "hard_ratio" if occupancy is not None and occupancy >= self.hard_ratio else
                             "max_turn_s" if elapsed >= self.max_turn_s else None)
                    if cause is None:
                        if not _latest(self.events, "runtime.goal_paused_for_safe_point", rollover_id=rid):
                            self.adapter.set_goal(predecessor, None, "paused")
                            self.note("runtime.goal_paused_for_safe_point", {"rollover_id": rid})
                        return
                    self.note("runtime.quiescence", {"rollover_id": rid, "cause": cause,
                                                       "occupancy": occupancy, "elapsed_s": elapsed,
                                                       "at": time.time()})
                else:
                    self.note("runtime.quiescence", {"rollover_id": rid, "cause": "turn_completed",
                                                       "at": time.time()})
            else:
                self.note("runtime.quiescence", {"rollover_id": rid, "cause": "manual",
                                                   "at": time.time()})
            self.store.advance(rid, state, "QUIESCING")
            state = "QUIESCING"
        if state == "QUIESCING":
            if not predecessor:
                self._recover(rid, "predecessor_missing", None)
                return
            if self.config["executor"] != "claude":
                self.adapter.resume_thread(predecessor)
            quiescence = next((e for e in reversed(self.events) if e["type"] == "runtime.quiescence"
                               and e["data"].get("rollover_id") == rid), None)
            cause = quiescence["data"].get("cause") if quiescence else None
            trigger = _latest(self.events, "runtime.rollover_trigger", rollover_id=rid) or {}
            chunk = bool(self.config["executor"] == "codex" and
                         trigger.get("urgency") == "chunk_boundary" and trigger.get("head") and
                         trigger.get("branch") and _latest(self.events, "runtime.base_head"))
            chunk_relay = chunk
            if cause == "relay_hard":
                chunk = False
            if chunk:
                signals = self.adapter.signals(predecessor)
                started = trigger.get("at", time.time())
                if not signals.get("active_turn") and not _latest(
                        self.events, "runtime.chunk_interrupt_requested", rollover_id=rid):
                    chunk = False  # Natural turn completion uses the existing checkpoint path.
                tokens, window = signals.get("context_tokens"), signals.get("context_window")
                hard = min(self.policy.relay_hard_tokens, window * .95) if isinstance(window, (int, float)) and window > 0 else self.policy.relay_hard_tokens
                if chunk and isinstance(tokens, (int, float)) and tokens >= hard:
                    chunk = False
                    cause = "relay_hard"
                    self.note("runtime.quiescence", {"rollover_id": rid, "cause": cause, "at": time.time()})
                elif time.time() - started >= self.chunk_wait_s:
                    self._exhaust_chunk_wait(rid, predecessor, started,
                                             bool(signals.get("active_turn")))
                    return
                elif chunk:
                    current = self._repo_state(self._effective_workdir(predecessor))
                    moved = next((e["id"] for e in reversed(self.events)
                                  if e["type"] == "runtime.chunk_boundary_moved" and
                                  e["data"].get("rollover_id") == rid), 0)
                    interrupted = next((e["data"] for e in reversed(self.events)
                                        if e["id"] > moved and e["type"] == "runtime.chunk_interrupt_requested"
                                        and e["data"].get("rollover_id") == rid), None)
                    candidate = (current["head"] != trigger.get("head") or
                                 current["head"] == trigger.get("boundary_head"))
                    if not interrupted and (not candidate or not self._clean_commit(
                            predecessor, trigger.get("branch", ""), current["head"])):
                        return
                    if not interrupted:
                        self.note("runtime.chunk_interrupt_requested", {"rollover_id": rid,
                                  "head": current["head"], "at": time.time()})
                    remaining = max(0, self.chunk_wait_s - (time.time() - started))
                    if isinstance(self.adapter, CodexAppServer):
                        quiet = self.adapter.quiesce(predecessor, interrupt=not interrupted,
                                                     timeout=min(5, remaining))
                    else:
                        quiet = self.adapter.quiesce(predecessor, interrupt=not interrupted)
                    if not quiet:
                        return
                    if self._cancelled():
                        self.store.abort_rollover(rid, "cancelled")
                        return
                    if not self._clean_commit(predecessor, trigger.get("branch", ""),
                                              interrupted["head"] if interrupted else current["head"]):
                        self.note("runtime.chunk_boundary_moved", {"rollover_id": rid,
                                  "head": current["head"], "at": time.time()})
                        self.adapter.start_turn(predecessor, "Continue the current task to the next clean commit boundary.")
                        self.adapter.set_goal(predecessor, self.store.get_beacon(self.mission)["objective"], "active")
                        return
                    checkpoint = self._checkpoint(rid, predecessor)
                    if (checkpoint["repo"]["head"] != (interrupted["head"] if interrupted else current["head"])
                            or checkpoint["snapshot"]["untracked"] or
                            Path(checkpoint["snapshot"]["diff_path"]).read_bytes() or
                            not snapshot.verify(checkpoint["snapshot"], checkpoint["repo"]["path"])):
                        self.note("runtime.chunk_boundary_moved", {"rollover_id": rid,
                                  "head": current["head"], "at": time.time()})
                        self.adapter.start_turn(predecessor, "Continue the current task to the next clean commit boundary.")
                        self.adapter.set_goal(predecessor, self.store.get_beacon(self.mission)["objective"], "active")
                        return
                    self.store.save_checkpoint(rid, checkpoint)
                    state = "CHECKPOINT_READY"
            if state == "QUIESCING":
                kwargs = {}
                if chunk_relay and cause != "relay_hard" and isinstance(self.adapter, CodexAppServer):
                    remaining = max(0, self.chunk_wait_s - (time.time() - trigger.get("at", time.time())))
                    kwargs["timeout"] = min(5, remaining)
                quiet = self.adapter.quiesce(
                    predecessor, interrupt=cause in ("relay_hard", "hard_ratio", "max_turn_s", "manual"),
                    **kwargs)
            else:
                quiet = True
            if state == "QUIESCING" and not quiet:
                if self.config["executor"] == "claude":
                    origin = quiescence or next((e for e in self.events if e["type"] == "rollover.requested"
                                                 and e["data"].get("id") == rid), None)
                    started = (origin["data"].get("at") if origin else None) or (
                        datetime.fromisoformat(origin["created_at"]).replace(tzinfo=timezone.utc).timestamp()
                        if origin else time.time())
                    elapsed = time.time() - started
                    self.note("runtime.quiescence_proof_failed", {"rollover_id": rid,
                              "elapsed_s": elapsed, "at": time.time()})
                    if elapsed < float(self.config.get("runtime_policy", {}).get("quiesce_max_s", 300)):
                        return
                if chunk_relay and cause != "relay_hard":
                    started = trigger.get("at", time.time())
                    if time.time() - started >= self.chunk_wait_s:
                        self._exhaust_chunk_wait(rid, predecessor, started,
                                                 bool(self.adapter.signals(predecessor).get("active_turn")))
                    return
                self._recover(rid, "quiescence_unproven", predecessor)
                return
            if state == "QUIESCING" and self._cancelled():
                self.store.abort_rollover(rid, "cancelled")
                return
            if state == "QUIESCING":
                self.store.save_checkpoint(rid, self._checkpoint(rid, predecessor))
                state = "CHECKPOINT_READY"
        checkpoint = self.store.get_checkpoint(rid)
        if state == "CHECKPOINT_READY":
            created = _latest(self.events, "runtime.successor_created", rollover_id=rid)
            successor = created["session_id"] if created else None
            timeouts = [e["data"] for e in self.events if e["type"] == "runtime.read_only_turn_timeout"
                        and e["data"].get("rollover_id") == rid]
            if successor is not None and any(t["session_id"] == successor for t in timeouts):
                successor = None  # Its read-only turn timed out: the one retry uses a new successor.
            if successor is None:
                for attempt in range(2):
                    try:
                        if hasattr(self.adapter, "prepare_launch"):
                            self.adapter.prepare_launch(epoch + 1,
                                self._successor_brief(checkpoint, epoch + 1) if self.config["executor"] == "claude" else
                                "Read-only verification: verify the workspace against this checkpoint.\n" +
                                self._brief(checkpoint), read_only=True,
                                **({"predecessor_session_id": predecessor}
                                   if self.config["executor"] == "claude" else {}))
                        self.writer_guard()
                        successor = self.adapter.create_thread(checkpoint["repo"]["path"])
                        if self.config["executor"] == "codex":
                            self._record_thread_policy(successor)
                        self.note("runtime.successor_created", {"rollover_id": rid, "session_id": successor,
                                                                "attempt": attempt + 1,
                                                                "read_only_attempt": len(timeouts) + 1,
                                                                "at": time.time()})
                        created = _latest(self.events, "runtime.successor_created", rollover_id=rid)
                        break
                    except PermissionError as exc:
                        self.note("runtime.permission_continuity_blocked", {"rollover_id": rid,
                                  "session_id": predecessor, "error": str(exc)})
                        self._recover(rid, "permission_boundary_unknown_or_widened", predecessor)
                        return
                    except AppServerError as exc:
                        kind = classify_error(exc)
                        if kind:
                            self._recover(rid, "environment_blocker:" + kind, predecessor)
                            return
                if successor is None:
                    self._recover(rid, "successor_start_failed_twice", predecessor)
                    return
            if self.config["executor"] != "claude":
                self.adapter.resume_thread(successor)
            verified = _latest(self.events, "runtime.successor_verified", rollover_id=rid)
            if verified is None:
                if self.config["executor"] == "claude":
                    signal = _latest(self.events, "runtime.worker_verified", rollover_id=rid,
                                     session_id=successor, epoch=epoch + 1)
                    if signal is None:
                        if time.time() - created.get("at", time.time()) > float(
                                self.config.get("runtime_policy", {}).get("await_writer_s", 300)):
                            self._recover(rid, "read_only_turn_timeout", predecessor)
                        return
                else:
                    prompt = "Read-only verification: verify the workspace against this checkpoint.\n" + self._brief(checkpoint)
                    turn = self.adapter.start_turn(successor, prompt, read_only=True)
                    if not self.adapter.wait_turn(successor, turn, self.read_only_turn_s):
                        try:
                            interrupted = self.adapter.interrupt_turn(successor, turn)
                        except AppServerError:
                            interrupted = False
                        self.note("runtime.read_only_turn_timeout", {
                            "rollover_id": rid, "session_id": successor, "turn_id": turn,
                            "attempt": len(timeouts) + 1, "limit_s": self.read_only_turn_s,
                            "interrupted": interrupted, "at": time.time()})
                        if len(timeouts) + 1 >= 2:
                            self._recover(rid, "read_only_turn_timeout", predecessor)
                            return
                        try:
                            self.adapter.retire(successor)
                        except AppServerError:
                            pass
                        # The one retry: a new successor for the same checkpoint.
                        return self._drive(dict(row, state="CHECKPOINT_READY"))
                receipt = self.adapter.fresh_receipt(successor)
                fresh_ok, fresh_reason = verify.verify_fresh(receipt)
                verdict = verify.verify_checkpoint(checkpoint, checkpoint["repo"]["path"],
                    self.store.get_beacon(self.mission), time.time(), 86400)
                self.note("runtime.verification", {"rollover_id": rid, "fresh_receipt": receipt,
                     "fresh_ok": fresh_ok, "fresh_reason": fresh_reason, "checkpoint": verdict})
                if not fresh_ok or verdict["verdict"] == "REJECTED":
                    self._recover(rid, "checkpoint_or_fresh_rejected", predecessor)
                    return
                self.note("runtime.successor_verified", {"rollover_id": rid, "session_id": successor})
            self.store.advance(rid, "CHECKPOINT_READY", "SUCCESSOR_PREPARED_READ_ONLY",
                               successor_session_id=successor)
            state = "SUCCESSOR_PREPARED_READ_ONLY"
        if state == "SUCCESSOR_PREPARED_READ_ONLY":
            self.writer_guard()
            successor = row.get("successor_session_id") or _latest(
                self.events, "runtime.successor_verified", rollover_id=rid)["session_id"]
            if not self._policy_continuity(rid, epoch, predecessor, successor):
                return
            self.store.transfer_ownership(rid, successor, epoch)
            self.note("runtime.session_created", {"epoch": epoch + 1, "session_id": successor, "at": time.time()})
            state = "OWNERSHIP_TRANSFERRED"
        if state == "OWNERSHIP_TRANSFERRED":
            self.writer_guard()
            if self._cancelled():
                return
            successor = self.store.current_writer(self.mission)[1]
            if not self._policy_continuity(rid, epoch, predecessor, successor):
                return
            if self.config["executor"] != "claude":
                self.adapter.resume_thread(successor)
            if not self._cancelled() and not _latest(self.events, "runtime.turn_started", epoch=epoch + 1, kind="successor"):
                if self.config["executor"] != "claude":
                    guidance = self._worker_guidance(epoch + 1, successor)
                    prompt = lambda current: self._brief(checkpoint, current) + guidance
                    if not self._deliver_owner(successor, epoch + 1, prompt, kind="successor"):
                        return
                if self.config["executor"] == "claude":
                    self.note("runtime.turn_started", {"epoch": epoch + 1, "session_id": successor, "kind": "successor"})
            self.adapter.set_goal(successor, self.store.get_beacon(self.mission)["objective"], "active")
            self.store.advance(rid, "OWNERSHIP_TRANSFERRED", "SUCCESSOR_ACTIVE")
            state = "SUCCESSOR_ACTIVE"
        if state == "SUCCESSOR_ACTIVE":
            if predecessor:
                if self.config["executor"] != "claude":
                    self.adapter.resume_thread(predecessor)
                self.adapter.retire(predecessor)
            self.store.advance(rid, "SUCCESSOR_ACTIVE", "PREDECESSOR_RETIRED")
            self.note("mission.recovered", {"rollover_id": rid})

    def _deliver_owner(self, sid, epoch, prefix='', kind='owner'):
        delivery = self.store.prepare_owner_delivery(self.mission, sid, epoch, prefix, kind)
        if delivery is None:
            return None
        if delivery['state'] == 'ACKED':
            return True
        if delivery['state'] == 'UNKNOWN':
            return False
        failures = [e["data"] for e in self.events if e["type"] == "runtime.owner_delivery_failed"
                    and e["data"].get("delivery_id") == delivery['id']]
        if failures and time.time() < failures[-1]["retry_at"]:
            return False
        if not self.store.claim_owner_delivery(
                self.mission, delivery['id'], sid, epoch,
                beacon_version=delivery['beacon_version'], beacon_hash=delivery['beacon_hash']):
            return False
        prompt = (delivery['prefix'] + "\nDelivery ID: " + delivery['id'] +
                  "\nPending owner messages: " + json.dumps(delivery['messages'], ensure_ascii=False))
        try:
            turn = self.adapter.start_turn(sid, prompt)
        except (NotSentError, RequestRejected) as exc:
            # Authoritative negative outcome: the request never reached the
            # executor, or the executor answered with an error. Return the batch
            # to PENDING under the same delivery ID and retry with capped
            # exponential backoff instead of parking the mission forever.
            self.store.resolve_owner_delivery(self.mission, delivery['id'], accepted=False,
                                              evidence=f"{type(exc).__name__}: {exc}")
            delay = min(self.delivery_backoff_max_s, self.poll_interval * 2 ** len(failures))
            self.note("runtime.owner_delivery_failed", {"delivery_id": delivery['id'], "error": str(exc),
                      "attempt": len(failures) + 1, "retry_at": time.time() + delay})
            return False
        except Exception:
            # Timeouts and broken pipes may follow acceptance. Keep UNKNOWN
            # durable and expose it through status for reconciliation.
            return False
        try:
            self.store.ack_owner_delivery(self.mission, delivery['id'], turn)
        except Exception:
            # The executor accepted this turn; only the ledger write failed.
            # Keep the receipt outside the ledger so the next tick can settle it.
            try:
                write_json(self._receipt_path(delivery['id']), {"turn_id": turn, "at": time.time()})
            except OSError:
                pass  # Stays UNKNOWN; rlh delivery resolve settles it from executor evidence.
            return False
        self.refresh()
        return True

    def _receipt_path(self, delivery_id: str) -> Path:
        return self.directory / "deliveries" / f"{delivery_id}.json"

    def _settle_receipt(self, delivery: dict) -> bool:
        """Acknowledge an UNKNOWN delivery whose executor turn receipt was saved locally."""
        turn = read_json(self._receipt_path(delivery['id'])).get("turn_id")
        if not turn:
            return False
        self.store.resolve_owner_delivery(self.mission, delivery['id'], accepted=True,
                                          evidence="executor returned turn " + str(turn), turn_id=turn)
        self._receipt_path(delivery['id']).unlink(missing_ok=True)
        self.refresh()
        return True

    def tick(self) -> bool:
        self.refresh()
        if isinstance(self.adapter, CodexAppServer):
            requests = self.adapter.drain_server_requests()
            if requests:
                self.note_all(*(("runtime.server_request", request) for request in requests))
        if _latest(self.events, "mission.completed"):
            return False
        # Even a pending owner decision must reconnect through the physical hold first.
        # A crash after reporting, OFF mode, recovery or an UNKNOWN outbox cannot skip it.
        held = completion_state(self.events)["report"]
        if held and self.config["executor"] == "codex":
            _, held_sid = self.store.current_writer(self.mission)
            self.adapter.resume_thread(held_sid)
            if not self._quiesce_completion_hold(held_sid, held):
                return True
        mode = self.store.get_mode(self.mission)
        if mode == "OFF":
            return False
        if self._cancelled():
            epoch, sid = self.store.current_writer(self.mission)
            if sid:
                if self.config["executor"] != "claude":
                    self.adapter.resume_thread(sid)
                self.adapter.quiesce(sid, interrupt=True)
            return False
        blocked = _latest(self.events, "runtime.policy_continuity_blocked")
        if blocked and not _latest(self.events, "mission.recovery_required",
                                   rollover_id=blocked["rollover_id"], reason=blocked["reason"]):
            # Finish recovery if a crash separated its policy event from abort/recovery events.
            try:
                self._policy_continuity(blocked["rollover_id"], blocked["epoch"],
                                        blocked["predecessor_session_id"], blocked["successor_session_id"])
            except StateMoved as exc:
                self.note("runtime.transition_lost", {"rollover_id": blocked["rollover_id"], "error": str(exc)})
            return True
        delivery = self.store.owner_delivery(self.mission)
        if delivery and delivery['state'] == 'UNKNOWN' and self.config["executor"] == "codex":
            rows = [r for r in self._rollovers() if r["state"] in (
                "SUCCESSOR_PREPARED_READ_ONLY", "OWNERSHIP_TRANSFERRED")]
            if rows:
                row = rows[-1]
                checkpoint = self.store.get_checkpoint(row["id"])
                predecessor = self._session(row["epoch"]) or checkpoint["predecessor_session_id"]
                successor = row["successor_session_id"]
                try:
                    allowed = self._policy_continuity(row["id"], row["epoch"], predecessor, successor)
                except StateMoved as exc:
                    self.note("runtime.transition_lost", {"rollover_id": row["id"], "error": str(exc)})
                    return True
                if not allowed:
                    return True
        if delivery and delivery['state'] == 'UNKNOWN' and not self._settle_receipt(delivery):
            return True  # Reconcile (rlh delivery resolve) before further automatic worker turns.
        if self._unmanaged_writer():
            return True
        if _latest(self.events, "runtime.unmanaged_fork"):
            return True
        open_rows = [r for r in self._rollovers() if r["state"] not in ("ABORTED", "PREDECESSOR_RETIRED")]
        if open_rows:
            try:
                self._drive(open_rows[-1])
            except StateMoved as exc:
                # Another actor won this compare-and-set. Losing it is not fatal:
                # the next tick re-reads the rollover and drives from its actual state.
                self.note("runtime.transition_lost", {"rollover_id": open_rows[-1]["id"], "error": str(exc)})
            return True
        recovery = next((e["type"] for e in reversed(self.events)
                         if e["type"] in ("mission.recovery_required", "mission.recovered")), None)
        if recovery == "mission.recovery_required":
            return True
        epoch, sid = self._ensure_session()
        signals = self.adapter.signals(sid)
        if not completion_state(self.events)["report"] and hasattr(self.adapter, "drain_turn_starts"):
            self.adapter.drain_turn_starts(sid)
        if not self._observe_worktree(sid):
            return True
        completion = self._complete_if_reported(epoch, sid, signals)
        if completion == "completed":
            return False
        if completion == "awaiting":
            return True  # Hold for the owner: no nudges, deliveries or rollovers until they answer.
        signals = self.adapter.signals(sid)  # A decline may have just reset the vendor goal/turn.
        now = time.time()
        rollovers = [r for r in self._rollovers() if r["state"] == "PREDECESSOR_RETIRED"]
        created = _latest(self.events, "runtime.session_created", epoch=epoch) or {}
        previous = rollovers[-1] if rollovers else None
        triggers = [e for e in self.events if e["type"] == "runtime.rollover_trigger"]
        last_rollover_at = triggers[-1]["data"].get("at") if triggers else None
        evidence_after = self._progress_since_checkpoint(previous, epoch)
        boundary_events = [e for e in self.events if e["type"] in ("runtime.note", "runtime.evidence")
                           and e["data"].get("epoch") == epoch]
        boundary_events += [e for e in self.events if e["type"] == "runtime.worktree_seen"
                            and e["data"].get("session_id") == sid]
        fresh_commits = [e for e in boundary_events if e["type"] == "runtime.worktree_seen"
                         and e["id"] > self._boundary_event_id]
        previous_seen = next((e for e in reversed(self.events) if e["type"] == "runtime.worktree_seen"
                              and e["data"].get("session_id") == sid and
                              (not fresh_commits or e["id"] < fresh_commits[-1]["id"])), None)
        new_commit = bool(fresh_commits and previous_seen and
                          fresh_commits[-1]["data"]["branch"] == previous_seen["data"]["branch"] and
                          fresh_commits[-1]["data"]["head"] != previous_seen["data"]["head"])
        at_boundary = not signals.get("active_turn") or any(e["id"] > self._boundary_event_id for e in boundary_events)
        self._boundary_event_id = max((e["id"] for e in boundary_events), default=self._boundary_event_id)
        jobs = [e["data"] for e in self.events if e["type"] == "runtime.job" and
                e["data"].get("epoch") == epoch and e["data"].get("uninterruptible")]
        uninterruptible = any(isinstance(j.get("pid"), int) and
                              process_start(j["pid"]) == j.get("process_start") for j in jobs)
        recent = [e for e in triggers if isinstance(e["data"].get("at"), (int, float))
                  and 0 <= now - e["data"]["at"] < 3600]
        signals.update({"now": now, "session_started_at": created.get("at", now),
                        "last_rollover_at": last_rollover_at, "rollovers_last_hour": len(recent),
                        "previous_rollover_reason": previous["reason"] if previous else None,
                        "evidence_since_last_rollover": evidence_after,
                        "at_chunk_boundary": at_boundary,
                        "uninterruptible_job_running": uninterruptible})
        if signals.get("last_notification_at") and now - signals["last_notification_at"] < 20:
            self.note("runtime.telemetry", {"at": signals["last_notification_at"], "epoch": epoch,
                                           "session_id": sid})
        decision = monitor.evaluate(signals, self.policy, mode)
        if signals.get("occupancy_quality") == "ESTIMATED" and decision["metrics"]["occupancy"] is not None:
            decision["metrics"]["occupancy_quality"] = "ESTIMATED"
        summary = {"epoch": epoch, "level": decision["level"], "action": decision["action"],
                   "reasons": decision["reasons"], "metrics": decision["metrics"],
                   "urgency": decision["urgency"], "trigger": "MONITOR"}
        previous_decision = _latest(self.events, "runtime.monitor_decision")
        if previous_decision != summary:
            self.note("runtime.monitor_decision", summary)
        manual = _latest(self.events, "runtime.manual_request")
        handled = _latest(self.events, "runtime.manual_handled")
        if manual and (handled is None or handled.get("request_id") != manual["request_id"]):
            self.note("runtime.manual_handled", {"request_id": manual["request_id"]})
            if self.store.get_mode(self.mission) != "CONTROLLED_AUTO":
                self.note("runtime.blocked", {"reason": "manual_rollover_requires_CONTROLLED_AUTO"})
                return True
            rid = self.store.request_rollover(self.mission, manual["reason"], epoch)
            self.note("runtime.rollover_trigger", {"rollover_id": rid, "trigger": "MANUAL", "at": now,
                                                   "urgency": "now"})
            return True
        tokens, window = signals.get("context_tokens"), signals.get("context_window")
        occupancy = tokens / window if isinstance(tokens, (int, float)) and isinstance(window, (int, float)) and window > 0 else None
        started = signals.get("turn_started_at")
        emergency = ("hard_ratio" if occupancy is not None and occupancy >= self.hard_ratio else
                     "max_turn_s" if signals.get("active_turn") and isinstance(started, (int, float))
                     and time.monotonic() - started >= self.max_turn_s else None)
        if emergency and mode == "CONTROLLED_AUTO" and not uninterruptible:
            rid = self.store.request_rollover(self.mission, emergency, epoch)
            self.note("runtime.rollover_trigger", {"rollover_id": rid, "trigger": "MONITOR", "at": now,
                                                   "urgency": "now"})
        elif decision["action"] == "ROLLOVER":
            rid = self.store.request_rollover(self.mission, decision["reasons"][0], epoch)
            trigger = {"rollover_id": rid, "trigger": "MONITOR", "at": now,
                       "urgency": decision["urgency"]}
            if (self.config["executor"] == "codex" and decision["urgency"] == "chunk_boundary"
                    and _latest(self.events, "runtime.base_head")):
                try:
                    repo = self._repo_state(self._effective_workdir(sid))
                except subprocess.CalledProcessError:
                    pass  # In-memory test adapters may have no Git worktree.
                else:
                    trigger.update({"head": repo["head"], "branch": repo["branch"],
                                    "boundary_head": repo["head"] if new_commit else None})
            self.note("runtime.rollover_trigger", trigger)
        elif decision["action"] == "DEFER":
            self.note("runtime.relay_deferred", {"epoch": epoch, "reasons": decision["reasons"]})
        elif decision["action"] == "BLOCK":
            self.note("runtime.blocked", {"reason": ",".join(decision["reasons"])})
        elif not signals.get("active_turn"):
            if self.config["executor"] == "claude":
                # The current turn has ended. Leave messages visible in status/attach;
                # never restart this session to deliver them.
                return True
            try:
                delivered = self._deliver_owner(sid, epoch)
            except MissionCancelled:
                return False
            if delivered is False:
                return True
            if delivered is None:
                nudges = [e["data"] for e in self.events if e["type"] == "runtime.idle_nudge"
                          and e["data"].get("epoch") == epoch and e["data"].get("session_id") == sid]
                last_activity = signals.get("last_notification_at") or created.get("at", now)
                if nudges:
                    last_activity = max(last_activity, nudges[-1]["at"])
                idle = now - last_activity
                if idle >= self.idle_nudge_s:
                    if len(nudges) < self.idle_nudges:
                        prompt = ("GCL notice: If every done_when item is verified, run the done command "
                                  "for this session now. Otherwise continue the task and run done only "
                                  "when the goal is complete.\n" + self._worker_guidance(epoch, sid))
                        turn_id = self.adapter.start_turn(sid, prompt)
                        self.note("runtime.idle_nudge", {"epoch": epoch, "session_id": sid,
                                                         "seq": len(nudges) + 1, "idle_s": idle, "at": now,
                                                         "turn_id": turn_id})
                    elif not _latest(self.events, "runtime.blocked",
                                     reason="worker_idle_without_done"):
                        self.note("runtime.blocked", {"reason": "worker_idle_without_done"})
        return True

    def run(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / "supervisor.lock", "a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            info = identity()
            write_json(self.directory / "supervisor.json", info)
            self.adapter.start()
            detach = False
            try:
                while True:
                    write_json(self.directory / "heartbeat.json", {"pid": os.getpid(), "at": time.time()})
                    if not self.tick():
                        detach = (self.store.get_mode(self.mission) == "OFF" or
                                  (self.config["executor"] == "codex" and
                                   bool(_latest(self.store.events(self.mission), "mission.completed"))))
                        return
                    time.sleep(self.poll_interval)
            finally:
                try:
                    if isinstance(self.adapter, CodexAppServer):
                        requests = self.adapter.drain_server_requests()
                        if requests:
                            self.note_all(*(("runtime.server_request", request) for request in requests))
                finally:
                    self.adapter.close(detach=detach)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--mission", required=True)
    args = parser.parse_args()
    Supervisor(home_path(args.home), args.mission).run()


if __name__ == "__main__":
    main()

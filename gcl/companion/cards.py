"""Clean-room B2 companion view models, StepSummaryCard, v0.2 Result Card, and Redacted Exporter.

Implements:
  1. StepSummaryCard: Collapsible deterministic signal & step cards (out-of-scope edits,
     evidence gaps, repeated errors, rollover transitions, verified evidence).
  2. DoneCheckResultCard (v0.2): Item-by-item result card separating 4 orthogonal states:
     - execution_state: COMPLETED | IN_PROGRESS | BLOCKED | NOT_STARTED | UNKNOWN
     - verification_state: VERIFIED | FAILED | PENDING | UNKNOWN (missing evidence -> UNKNOWN)
     - independent_review_state: APPROVED | REJECTED | PENDING_REVIEW | UNKNOWN
     - owner_confirmation_state: CONFIRMED | AWAITING_CONFIRMATION | NOT_REQUIRED | DECLINED | UNKNOWN
  3. PendingActionCommandCard: Read-only CLI command preview for pending confirmation actions
     (AWAITING_CONFIRMATION -> `rlh confirm`, OWNER_DELIVERY_UNKNOWN -> `rlh delivery resolve`,
     OBSERVE_ONLY adopt -> `rlh adopt`), strictly prohibiting direct DB write buttons.
  4. export_redacted_handoff: Sanitized handoff bundle stripping host paths, private IPs,
     ports, tokens, and credentials (OD-14 / C7).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
from typing import Any

EXECUTION_STATES = ("COMPLETED", "IN_PROGRESS", "BLOCKED", "NOT_STARTED", "UNKNOWN")
VERIFICATION_STATES = ("VERIFIED", "FAILED", "PENDING", "UNKNOWN")
INDEPENDENT_REVIEW_STATES = ("APPROVED", "REJECTED", "PENDING_REVIEW", "UNKNOWN")
OWNER_CONFIRMATION_STATES = (
    "CONFIRMED",
    "AWAITING_CONFIRMATION",
    "NOT_REQUIRED",
    "DECLINED",
    "UNKNOWN",
)

_SIGNAL_CATEGORIES = frozenset({
    "OUT_OF_SCOPE_EDIT",
    "EVIDENCE_GAP",
    "REPEATED_ERROR",
    "POLICY_HOLD",
    "ROLLOVER_STEP",
    "EVIDENCE_RECORDED",
    "EXECUTION_NOTE",
})

# OD-14 / C7 Public Redaction patterns
_IPV4_PORT_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}|"
    r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3})(?::\d{2,5})?\b"
)
_TAILNET_HOST_RE = re.compile(r"\b[a-zA-Z0-9._-]+\.ts\.net(?::\d{2,5})?\b", re.IGNORECASE)
_LOCALHOST_PORT_RE = re.compile(r"\blocalhost:\d{2,5}\b", re.IGNORECASE)
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{10,}|Bearer\s+[A-Za-z0-9._-]+|"
    r"GCL_SESSION_TOKEN=[A-Za-z0-9._-]+)\b"
)
_UNIX_USER_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s\"']+(?:/[^\s\"']*)?")
_PRIVATE_TMP_PATH_RE = re.compile(r"/(?:private/tmp|private/var/folders|var/folders|tmp)/[^\s\"']+")


def redact_public_text(text: str | None) -> str:
    """Redact host filesystem paths, internal/tailnet IPs, ports, and secret tokens (OD-14 / C7)."""
    if not text:
        return ""
    value = str(text)
    value = _SECRET_TOKEN_RE.sub("<REDACTED_SECRET>", value)
    value = _IPV4_PORT_RE.sub("<REDACTED_INTERNAL_ENDPOINT>", value)
    value = _TAILNET_HOST_RE.sub("<REDACTED_TAILNET_ENDPOINT>", value)
    value = _LOCALHOST_PORT_RE.sub("<REDACTED_LOCAL_ENDPOINT>", value)

    def _replace_path(match: re.Match) -> str:
        raw = match.group(0)
        name = Path(raw).name
        return f"<REDACTED_PATH>/{name}" if name else "<REDACTED_PATH>"

    value = _PRIVATE_TMP_PATH_RE.sub(_replace_path, value)
    value = _UNIX_USER_PATH_RE.sub(_replace_path, value)
    return value


@dataclass(frozen=True)
class StepSummaryCard:
    card_id: str
    mission_id: str
    epoch: int
    category: str
    severity: str
    title: str
    summary: str
    collapsed_by_default: bool
    event_ids: tuple[int, ...]
    evidence_sha256: str | None
    remediation_cli: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_ids"] = list(self.event_ids)
        return payload


@dataclass(frozen=True)
class DoneCheckItemView:
    item_id: str
    criterion: str
    execution_state: str
    verification_state: str
    independent_review_state: str
    owner_confirmation_state: str
    overall_verdict: str
    evidence_claim: str | None
    evidence_path: str | None
    evidence_sha256: str | None
    independent_review_ref: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PendingActionCommandCard:
    action_id: str
    mission_id: str
    action_kind: str
    runtime_status: str
    title: str
    explanation: str
    cli_command: str
    direct_write_enabled: bool
    receipt_binding: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoneCheckResultCard:
    schema_version: str
    mission_id: str
    beacon_version: int | None
    beacon_hash: str
    objective: str
    done_check_command: str | None
    freshness: str
    execution_state: str
    verification_state: str
    independent_review_state: str
    owner_confirmation_state: str
    overall_status: str
    items: tuple[DoneCheckItemView, ...]
    pending_actions: tuple[PendingActionCommandCard, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["items"] = [item.to_dict() for item in self.items]
        payload["pending_actions"] = [act.to_dict() for act in self.pending_actions]
        return payload


def build_step_summary_cards(mission: dict[str, Any]) -> list[StepSummaryCard]:
    """Project mission events into clean-room collapsible StepSummaryCards."""
    mid = str(mission.get("mission", ""))
    events = list(mission.get("events", []))
    cards: list[StepSummaryCard] = []

    for event in events:
        eid = int(event.get("id", 0))
        etype = str(event.get("type", ""))
        data = event.get("data") or {}
        epoch = int(data.get("epoch") or mission.get("epoch") or 0)

        if etype in ("runtime.goal_boundary_red", "runtime.unmanaged_writer_detected", "runtime.unmanaged_fork", "goal.change_rejected"):
            reason = data.get("reason") or ("unauthorized_goal_change" if etype == "goal.change_rejected" else etype)
            cards.append(
                StepSummaryCard(
                    card_id=f"step-{mid[:8]}-{eid}",
                    mission_id=mid,
                    epoch=epoch,
                    category="OUT_OF_SCOPE_EDIT",
                    severity="RED",
                    title=f"Boundary Alert: {reason}",
                    summary=f"Detected out-of-scope or unauthorized boundary violation ({reason}).",
                    collapsed_by_default=False,
                    event_ids=(eid,),
                    evidence_sha256=None,
                    remediation_cli=f"rlh status {shlex.quote(mid)}",
                )
            )
        elif etype in ("runtime.permission_continuity_blocked", "runtime.policy_continuity_blocked"):
            reason = data.get("reason", "policy_continuity_blocked")
            cards.append(
                StepSummaryCard(
                    card_id=f"step-{mid[:8]}-{eid}",
                    mission_id=mid,
                    epoch=epoch,
                    category="POLICY_HOLD",
                    severity="RED",
                    title=f"Rollover Permission Hold: {reason}",
                    summary="Successor session permission or sandbox boundary did not match predecessor ceiling.",
                    collapsed_by_default=False,
                    event_ids=(eid,),
                    evidence_sha256=None,
                    remediation_cli=f"rlh status {shlex.quote(mid)}",
                )
            )
        elif etype == "runtime.monitor_decision":
            reasons = data.get("reasons") or []
            level = str(data.get("level", "UNKNOWN"))
            if "no_progress" in reasons or "no_evidence_since_last_rollover" in reasons:
                cards.append(
                    StepSummaryCard(
                        card_id=f"step-{mid[:8]}-{eid}",
                        mission_id=mid,
                        epoch=epoch,
                        category="EVIDENCE_GAP",
                        severity=level if level in ("RED", "YELLOW") else "YELLOW",
                        title="Evidence Gap Detected",
                        summary=f"Long stretch of turns without verified evidence ({', '.join(reasons)}).",
                        collapsed_by_default=False,
                        event_ids=(eid,),
                        evidence_sha256=None,
                        remediation_cli=None,
                    )
                )
            elif "error_loop" in reasons or any(str(r).startswith("environment_blocker:") for r in reasons):
                cards.append(
                    StepSummaryCard(
                        card_id=f"step-{mid[:8]}-{eid}",
                        mission_id=mid,
                        epoch=epoch,
                        category="REPEATED_ERROR",
                        severity="RED",
                        title="Repeated Execution Error Signal",
                        summary=f"Monitor triggered repeated error or blocker threshold ({', '.join(reasons)}).",
                        collapsed_by_default=False,
                        event_ids=(eid,),
                        evidence_sha256=None,
                        remediation_cli=None,
                    )
                )
            elif "goal_violation" in reasons:
                cards.append(
                    StepSummaryCard(
                        card_id=f"step-{mid[:8]}-{eid}",
                        mission_id=mid,
                        epoch=epoch,
                        category="OUT_OF_SCOPE_EDIT",
                        severity="RED",
                        title="Goal Contract Violation",
                        summary="Monitor detected an unauthorized goal change or hash mismatch.",
                        collapsed_by_default=False,
                        event_ids=(eid,),
                        evidence_sha256=None,
                        remediation_cli=f"rlh goal show {shlex.quote(mid)}",
                    )
                )
        elif etype == "runtime.done_check_failed":
            detail = data.get("error") or f"exit_code={data.get('exit_code')}"
            cards.append(
                StepSummaryCard(
                    card_id=f"step-{mid[:8]}-{eid}",
                    mission_id=mid,
                    epoch=epoch,
                    category="REPEATED_ERROR",
                    severity="YELLOW",
                    title="Done-Check Command Failed",
                    summary=f"Verification command did not pass ({detail}).",
                    collapsed_by_default=False,
                    event_ids=(eid,),
                    evidence_sha256=None,
                    remediation_cli=None,
                )
            )
        elif etype == "runtime.evidence":
            claim = str(data.get("claim", "recorded evidence"))
            digest = data.get("sha256")
            cards.append(
                StepSummaryCard(
                    card_id=f"step-{mid[:8]}-{eid}",
                    mission_id=mid,
                    epoch=epoch,
                    category="EVIDENCE_RECORDED",
                    severity="GREEN",
                    title=f"Evidence: {claim}",
                    summary=f"Recorded sha256={str(digest)[:12]}...",
                    collapsed_by_default=True,
                    event_ids=(eid,),
                    evidence_sha256=digest,
                    remediation_cli=None,
                )
            )
        elif etype == "runtime.note":
            text = str(data.get("text", "")).strip()
            cards.append(
                StepSummaryCard(
                    card_id=f"step-{mid[:8]}-{eid}",
                    mission_id=mid,
                    epoch=epoch,
                    category="EXECUTION_NOTE",
                    severity="INFO",
                    title=f"Worker Note (epoch {epoch})",
                    summary=text[:140] + ("..." if len(text) > 140 else ""),
                    collapsed_by_default=True,
                    event_ids=(eid,),
                    evidence_sha256=None,
                    remediation_cli=None,
                )
            )
        elif etype in ("rollover.requested", "checkpoint.saved", "writer.transferred"):
            cards.append(
                StepSummaryCard(
                    card_id=f"step-{mid[:8]}-{eid}",
                    mission_id=mid,
                    epoch=epoch,
                    category="ROLLOVER_STEP",
                    severity="INFO",
                    title=f"Relay Step: {etype}",
                    summary=f"Session continuity transition ({etype}) at epoch {epoch}.",
                    collapsed_by_default=True,
                    event_ids=(eid,),
                    evidence_sha256=data.get("sha256"),
                    remediation_cli=None,
                )
            )

    return cards


def build_pending_action_cards(
    mission: dict[str, Any],
    *,
    home_override: str | None = None,
) -> list[PendingActionCommandCard]:
    """Build read-only CLI command preview cards for actions awaiting owner confirmation.

    Strictly sets direct_write_enabled=False so the companion UI never mutates the SQLite
    ledger directly.
    """
    mid = str(mission.get("mission", ""))
    runtime_status = str(mission.get("runtime_status", "UNKNOWN"))
    beacon = mission.get("beacon") or {}
    completion = mission.get("completion_state") or {}
    report = completion.get("report")
    home_prefix = f"--home {shlex.quote(home_override)} " if home_override else ""
    cards: list[PendingActionCommandCard] = []

    if runtime_status == "AWAITING_CONFIRMATION":
        report_id = report.get("id") if isinstance(report, dict) else None
        cards.append(
            PendingActionCommandCard(
                action_id=f"act-confirm-{mid[:8]}-{report_id or 'pending'}",
                mission_id=mid,
                action_kind="CONFIRM_COMPLETION",
                runtime_status=runtime_status,
                title="Confirm Mission Completion",
                explanation=(
                    "Worker reported completion without an automated done_check. "
                    "Review recorded evidence and run this CLI command in your terminal to confirm."
                ),
                cli_command=f"rlh {home_prefix}confirm {shlex.quote(mid)}".strip(),
                direct_write_enabled=False,
                receipt_binding={
                    "mission_id": mid,
                    "epoch": mission.get("epoch"),
                    "session_id": mission.get("session_id"),
                    "report_id": report_id,
                    "beacon_version": beacon.get("version"),
                    "beacon_hash": beacon.get("hash"),
                },
            )
        )

    recovery = mission.get("owner_delivery_recovery")
    if isinstance(recovery, dict) and recovery.get("delivery_id"):
        did = str(recovery["delivery_id"])
        cards.append(
            PendingActionCommandCard(
                action_id=f"act-delivery-{mid[:8]}-{did[:8]}",
                mission_id=mid,
                action_kind="RESOLVE_UNKNOWN_DELIVERY",
                runtime_status=runtime_status,
                title="Reconcile Unknown Owner Delivery",
                explanation=str(recovery.get("instruction", "Reconcile against authoritative executor evidence.")),
                cli_command=(
                    f"rlh {home_prefix}delivery resolve {shlex.quote(mid)} {shlex.quote(did)} "
                    "--accepted --turn-id <TURN_ID> --evidence 'verified in executor transcript'"
                ).strip(),
                direct_write_enabled=False,
                receipt_binding={
                    "mission_id": mid,
                    "delivery_id": did,
                    "epoch": recovery.get("epoch"),
                    "session_id": recovery.get("session_id"),
                    "beacon_version": beacon.get("version"),
                    "beacon_hash": beacon.get("hash"),
                },
            )
        )

    if mission.get("mode") == "OBSERVE_ONLY" and runtime_status == "ACTIVE" and not (mission.get("events") and any(
        e.get("type") == "runtime.config" and e.get("data", {}).get("native") for e in mission.get("events", [])
    )):
        cards.append(
            PendingActionCommandCard(
                action_id=f"act-adopt-{mid[:8]}",
                mission_id=mid,
                action_kind="ADOPT_CONTROLLED_AUTO",
                runtime_status=runtime_status,
                title="Adopt Mission into CONTROLLED_AUTO",
                explanation="Switch from passive observation to supervisor-managed rollover continuity.",
                cli_command=(
                    f"rlh {home_prefix}adopt {shlex.quote(mid)} --mode CONTROLLED_AUTO"
                ).strip(),
                direct_write_enabled=False,
                receipt_binding={
                    "mission_id": mid,
                    "epoch": mission.get("epoch"),
                    "session_id": mission.get("session_id"),
                    "beacon_version": beacon.get("version"),
                    "beacon_hash": beacon.get("hash"),
                },
            )
        )

    return cards


def _derive_four_states(
    mission: dict[str, Any],
) -> tuple[str, str, str, str]:
    """Derive the 4 orthogonal v0.2 states for a mission."""
    events = list(mission.get("events", []))
    runtime_status = str(mission.get("runtime_status", "UNKNOWN"))
    completion = mission.get("completion_state") or {}
    evidence_list = list(mission.get("evidence", []))
    done_check_cmd = mission.get("done_check")

    # 1. Execution state
    has_completed_event = any(e.get("type") in ("mission.completed", "mission.succeeded") for e in events)
    has_worker_done = any(
        e.get("type") in ("runtime.worker_done", "runtime.completion_reported") for e in events
    )
    if runtime_status in ("BLOCKED", "RECOVERY_REQUIRED"):
        execution_state = "BLOCKED"
    elif has_completed_event or has_worker_done or runtime_status in ("COMPLETED", "AWAITING_CONFIRMATION"):
        execution_state = "COMPLETED"
    elif int(mission.get("epoch") or 0) > 0 or any(e.get("type") == "runtime.turn_started" for e in events):
        execution_state = "IN_PROGRESS"
    elif events:
        execution_state = "NOT_STARTED"
    else:
        execution_state = "UNKNOWN"

    # 2. Verification state (missing or unverified evidence MUST be marked UNKNOWN)
    completed_event = next(
        (e for e in reversed(events) if e.get("type") in ("mission.completed", "mission.succeeded")),
        None,
    )
    done_check_failed = any(e.get("type") == "runtime.done_check_failed" for e in events)
    verified_evidence = [ev for ev in evidence_list if ev.get("file_verified") is True]
    unverified_evidence = [ev for ev in evidence_list if ev.get("file_verified") is False]

    if completed_event and completed_event.get("data", {}).get("done_check"):
        verification_state = "VERIFIED"
    elif done_check_failed and not completed_event:
        verification_state = "FAILED"
    elif verified_evidence and not unverified_evidence:
        verification_state = "VERIFIED"
    elif unverified_evidence:
        # File missing or hash mismatch -> fail-closed UNKNOWN
        verification_state = "UNKNOWN"
    elif done_check_cmd and execution_state == "IN_PROGRESS":
        verification_state = "PENDING"
    else:
        # Missing evidence or no done_check proof -> strictly UNKNOWN
        verification_state = "UNKNOWN"

    # 3. Independent review state
    review_events = [
        e
        for e in events
        if e.get("type")
        in (
            "runtime.verification",
            "runtime.independent_review",
            "mission.independent_review",
        )
    ]
    if review_events:
        latest_rev = review_events[-1].get("data") or {}
        verdict = latest_rev.get("verdict") or (latest_rev.get("checkpoint") or {}).get("verdict")
        fresh_ok = latest_rev.get("fresh_ok", True)
        if verdict == "REJECTED" or fresh_ok is False:
            independent_review_state = "REJECTED"
        elif verdict == "TRUSTED" and fresh_ok is True:
            independent_review_state = "APPROVED"
        elif verdict in ("PARTIAL", "PENDING", "PASS_PENDING_INDEPENDENT_REVIEW"):
            independent_review_state = "PENDING_REVIEW"
        else:
            independent_review_state = "UNKNOWN"
    else:
        # No independent review receipt exists -> UNKNOWN
        independent_review_state = "UNKNOWN"

    # 4. Owner confirmation state
    confirmed_event = completion.get("confirmation") or (
        completed_event
        if completed_event and completed_event.get("data", {}).get("confirmed_by") == "owner"
        else None
    )
    declined_event = completion.get("declined")
    if confirmed_event:
        owner_confirmation_state = "CONFIRMED"
    elif runtime_status == "AWAITING_CONFIRMATION":
        owner_confirmation_state = "AWAITING_CONFIRMATION"
    elif declined_event and not completion.get("report"):
        owner_confirmation_state = "DECLINED"
    elif done_check_cmd or mission.get("self_complete"):
        owner_confirmation_state = "NOT_REQUIRED"
    else:
        owner_confirmation_state = "UNKNOWN"

    return (
        execution_state,
        verification_state,
        independent_review_state,
        owner_confirmation_state,
    )


def _match_evidence_for_criterion(
    criterion: str,
    index: int,
    evidence_list: list[dict[str, Any]],
) -> dict[str, Any] | None:
    criterion_lower = criterion.lower()
    for ev in evidence_list:
        claim = str(ev.get("claim", "")).lower()
        if claim and (claim == criterion_lower or criterion_lower in claim or claim in criterion_lower):
            return ev
    if len(evidence_list) == 1 and index == 0:
        return evidence_list[0]
    return None


def build_done_check_result_card(
    mission: dict[str, Any],
    *,
    home_override: str | None = None,
) -> DoneCheckResultCard:
    """Build the v0.2 item-by-item Done-Check Result Card with 4 orthogonal states."""
    mid = str(mission.get("mission", ""))
    beacon = mission.get("beacon") or {}
    done_when = list(beacon.get("done_when") or [])
    acceptance = list(beacon.get("acceptance") or [])
    criteria = done_when + [item for item in acceptance if item not in done_when]
    if not criteria:
        criteria = [str(beacon.get("objective") or "Primary mission objective")]

    (
        mission_exec,
        mission_verif,
        mission_review,
        mission_owner,
    ) = _derive_four_states(mission)

    evidence_list = list(mission.get("evidence", []))
    events = list(mission.get("events", []))
    completed_event = next(
        (e for e in reversed(events) if e.get("type") in ("mission.completed", "mission.succeeded")),
        None,
    )
    done_check_passed = bool(completed_event and completed_event.get("data", {}).get("done_check"))

    review_event = next(
        (
            e
            for e in reversed(events)
            if e.get("type")
            in ("runtime.verification", "runtime.independent_review", "mission.independent_review")
        ),
        None,
    )
    review_ref = f"event:{review_event['id']}" if review_event else None

    items: list[DoneCheckItemView] = []
    for idx, criterion in enumerate(criteria):
        matched_ev = _match_evidence_for_criterion(str(criterion), idx, evidence_list)
        if matched_ev is not None:
            if matched_ev.get("file_verified") is True:
                item_verif = "VERIFIED"
            else:
                item_verif = "UNKNOWN"
            ev_claim = matched_ev.get("claim")
            ev_path = matched_ev.get("evidence_path")
            ev_sha = matched_ev.get("sha256")
        elif done_check_passed:
            item_verif = "VERIFIED"
            ev_claim = f"done_check: {mission.get('done_check')}"
            ev_path = None
            ev_sha = None
        else:
            # Missing evidence MUST be labeled UNKNOWN (fail-closed)
            item_verif = "UNKNOWN"
            ev_claim = None
            ev_path = None
            ev_sha = None

        if item_verif == "VERIFIED" and mission_review == "APPROVED":
            verdict = "VERIFIED_AND_REVIEWED"
        elif item_verif == "VERIFIED" and mission_review in ("UNKNOWN", "PENDING_REVIEW"):
            verdict = "PASS_PENDING_INDEPENDENT_REVIEW"
        elif item_verif == "VERIFIED" and mission_owner == "CONFIRMED":
            verdict = "OWNER_CONFIRMED"
        elif mission_exec == "BLOCKED" or mission_review == "REJECTED":
            verdict = "BLOCKED"
        else:
            verdict = "UNKNOWN"

        items.append(
            DoneCheckItemView(
                item_id=f"item-{idx + 1}",
                criterion=str(criterion),
                execution_state=mission_exec,
                verification_state=item_verif,
                independent_review_state=mission_review,
                owner_confirmation_state=mission_owner,
                overall_verdict=verdict,
                evidence_claim=ev_claim,
                evidence_path=ev_path,
                evidence_sha256=ev_sha,
                independent_review_ref=review_ref,
            )
        )

    pending_actions = tuple(build_pending_action_cards(mission, home_override=home_override))
    if any(i.verification_state == "UNKNOWN" for i in items) and mission_verif == "VERIFIED" and not done_check_passed:
        overall_verif = "UNKNOWN"
    else:
        overall_verif = mission_verif

    if overall_verif == "VERIFIED" and mission_review == "APPROVED":
        overall_status = "VERIFIED_COMPLETE"
    elif overall_verif == "VERIFIED" and mission_owner == "CONFIRMED":
        overall_status = "OWNER_CONFIRMED_COMPLETE"
    elif overall_verif == "VERIFIED":
        overall_status = "PASS_PENDING_INDEPENDENT_REVIEW"
    elif mission.get("runtime_status") == "AWAITING_CONFIRMATION":
        overall_status = "AWAITING_CONFIRMATION"
    elif mission_exec == "BLOCKED":
        overall_status = "BLOCKED"
    else:
        overall_status = "UNKNOWN"

    return DoneCheckResultCard(
        schema_version="rlh.companion.result_card.v0.2",
        mission_id=mid,
        beacon_version=beacon.get("version"),
        beacon_hash=str(beacon.get("hash", "UNKNOWN")),
        objective=str(beacon.get("objective", "")),
        done_check_command=mission.get("done_check"),
        freshness=str(mission.get("freshness", "UNKNOWN")),
        execution_state=mission_exec,
        verification_state=overall_verif,
        independent_review_state=mission_review,
        owner_confirmation_state=mission_owner,
        overall_status=overall_status,
        items=tuple(items),
        pending_actions=pending_actions,
    )


def export_redacted_handoff(
    mission: dict[str, Any],
    *,
    snapshot_revision: str | None = None,
) -> dict[str, Any]:
    """Export a sanitized v0.2 handoff package safe for cross-session / cross-vendor sharing."""
    card = build_done_check_result_card(mission)
    steps = build_step_summary_cards(mission)
    beacon = mission.get("beacon") or {}

    redacted_items = [
        {
            "item_id": item.item_id,
            "criterion": redact_public_text(item.criterion),
            "execution_state": item.execution_state,
            "verification_state": item.verification_state,
            "independent_review_state": item.independent_review_state,
            "owner_confirmation_state": item.owner_confirmation_state,
            "overall_verdict": item.overall_verdict,
            "evidence_claim": redact_public_text(item.evidence_claim) if item.evidence_claim else None,
            "evidence_path": redact_public_text(item.evidence_path) if item.evidence_path else None,
            "evidence_sha256": item.evidence_sha256,
            "independent_review_ref": item.independent_review_ref,
        }
        for item in card.items
    ]

    redacted_steps = [
        {
            "card_id": step.card_id,
            "epoch": step.epoch,
            "category": step.category,
            "severity": step.severity,
            "title": redact_public_text(step.title),
            "summary": redact_public_text(step.summary),
            "collapsed_by_default": step.collapsed_by_default,
            "event_ids": list(step.event_ids),
            "evidence_sha256": step.evidence_sha256,
            "remediation_cli": redact_public_text(step.remediation_cli) if step.remediation_cli else None,
        }
        for step in steps
    ]

    redacted_actions = [
        {
            "action_id": act.action_id,
            "action_kind": act.action_kind,
            "runtime_status": act.runtime_status,
            "title": redact_public_text(act.title),
            "explanation": redact_public_text(act.explanation),
            "cli_command": redact_public_text(act.cli_command),
            "direct_write_enabled": act.direct_write_enabled,
            "receipt_binding": act.receipt_binding,
        }
        for act in card.pending_actions
    ]

    bundle = {
        "schema_version": "rlh.companion.handoff.v0.2",
        "privacy_mode": "REDACTED_PUBLIC_SAFE_OD14",
        "snapshot_revision": snapshot_revision,
        "mission_id": card.mission_id,
        "epoch": mission.get("epoch"),
        "runtime_status": mission.get("runtime_status"),
        "freshness": card.freshness,
        "beacon": {
            "version": card.beacon_version,
            "hash": card.beacon_hash,
            "objective": redact_public_text(card.objective),
            "done_when": [redact_public_text(x) for x in (beacon.get("done_when") or [])],
            "non_goals": [redact_public_text(x) for x in (beacon.get("non_goals") or [])],
            "constraints": [redact_public_text(x) for x in (beacon.get("constraints") or [])],
        },
        "result_card": {
            "schema_version": card.schema_version,
            "execution_state": card.execution_state,
            "verification_state": card.verification_state,
            "independent_review_state": card.independent_review_state,
            "owner_confirmation_state": card.owner_confirmation_state,
            "overall_status": card.overall_status,
            "done_check_command": redact_public_text(card.done_check_command) if card.done_check_command else None,
            "items": redacted_items,
        },
        "step_summary_cards": redacted_steps,
        "pending_actions": redacted_actions,
    }
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    bundle["handoff_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return bundle

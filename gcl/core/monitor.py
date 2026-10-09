"""Deterministic continuity signals and rollover budgets."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    yellow_ratio: float = 0.60
    red_ratio: float = 0.80
    min_work_s: float = 300
    cooldown_s: float = 900
    max_rollovers_per_hour: int = 3
    max_error_repeats: int = 3
    max_turns_without_evidence: int = 6
    relay_soft_tokens: int = 200_000
    relay_hard_tokens: int = 300_000


def evaluate(signals: dict, policy: Policy, mode: str) -> dict:
    tokens, window = signals.get("context_tokens"), signals.get("context_window")
    measured = isinstance(tokens, (int, float)) and isinstance(window, (int, float)) and window > 0 and tokens >= 0
    occupancy = tokens / window if measured else None
    metrics = {"occupancy": occupancy, "occupancy_quality": "MEASURED" if measured else "UNKNOWN"}
    reasons = []
    urgency = None
    if signals.get("unauthorized_goal_change") or signals.get("goal_hash_matches") is False:
        level, action = "RED", "BLOCK"
        reasons.append("goal_violation")
    elif signals.get("blocker_kind"):
        level, action = "RED", "BLOCK"
        reasons.append("environment_blocker:" + str(signals["blocker_kind"]))
    else:
        rollover_reason = None
        relay_tokens = isinstance(tokens, (int, float)) and tokens >= 0
        soft = min(policy.relay_soft_tokens, window * policy.red_ratio) if measured else policy.relay_soft_tokens
        hard = min(policy.relay_hard_tokens, window * .95) if measured else policy.relay_hard_tokens
        if relay_tokens and tokens >= hard:
            rollover_reason, urgency = "relay_hard", "now"
        elif relay_tokens and tokens >= soft:
            if signals.get("at_chunk_boundary", True):
                rollover_reason, urgency = "relay_chunk", "chunk_boundary"
            else:
                reasons.append("relay_pending_chunk_boundary")
        if rollover_reason is None and not reasons and occupancy is not None and occupancy >= policy.red_ratio:
            rollover_reason = "context_pressure"
        elif rollover_reason is None and signals.get("repeated_error_count", 0) >= policy.max_error_repeats:
            rollover_reason = "error_loop"
        elif rollover_reason is None and signals.get("turns_without_evidence", 0) >= policy.max_turns_without_evidence:
            rollover_reason = "no_progress"
        if rollover_reason:
            level, action = ("YELLOW" if rollover_reason == "relay_chunk" and
                             (occupancy is None or occupancy < policy.red_ratio) else "RED"), "ROLLOVER"
            reasons.append(rollover_reason)
            if signals.get("rollovers_last_hour", 0) >= policy.max_rollovers_per_hour:
                action = "BLOCK"
                reasons.append("hourly_rollover_cap")
            elif (signals.get("previous_rollover_reason") == rollover_reason
                  and signals.get("evidence_since_last_rollover") is False):
                action = "BLOCK"
                reasons.append("no_evidence_since_last_rollover")
            elif signals.get("now", 0) - signals.get("session_started_at", 0) < policy.min_work_s:
                action = "CONTINUE"
                reasons.append("minimum_work_interval")
            elif (signals.get("last_rollover_at") is not None
                  and signals.get("now", 0) - signals["last_rollover_at"] < policy.cooldown_s):
                action = "CONTINUE"
                reasons.append("rollover_cooldown")
            if action == "ROLLOVER" and urgency and signals.get("uninterruptible_job_running"):
                action = "DEFER"
                reasons.append("uninterruptible_job")
            if action != "ROLLOVER":
                urgency = None
        elif reasons:
            level, action = ("RED" if occupancy is not None and occupancy >= policy.red_ratio
                             else "YELLOW"), "CONTINUE"
        elif occupancy is not None and occupancy >= policy.yellow_ratio:
            level, action = "YELLOW", "CONTINUE"
            reasons.append("context_warning")
        elif occupancy is None:
            level, action = "UNKNOWN", "CONTINUE"
        else:
            level, action = "GREEN", "CONTINUE"
    if mode == "OFF":
        action = "NONE"
    elif mode == "OBSERVE_ONLY":
        action = "OBSERVE"
    elif mode != "CONTROLLED_AUTO":
        raise ValueError(mode)
    if action != "ROLLOVER":
        urgency = None
    return {"level": level, "action": action, "reasons": reasons, "metrics": metrics,
            "urgency": urgency}

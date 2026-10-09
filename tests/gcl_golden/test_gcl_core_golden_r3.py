"""GCL core golden tests round 3 (normative): Owner decision 2026-09-29 — task-chunk relay.

Owner (human:owner): rollover is driven by task chunks with an average relay unit of
200k-300k tokens, not by the model's maximum window; uninterruptible long jobs excepted.
Contract: docs/gcl/ADR-GCL-001.md section 7.
"""
from __future__ import annotations

from gcl.core import monitor


def _sig(**over):
    now = 10_000.0
    s = {"now": now, "session_started_at": now - 3600, "last_rollover_at": None, "rollovers_last_hour": 0,
         "context_tokens": 50_000, "context_window": 1_000_000, "cumulative_tokens": 5_000_000,
         "repeated_error_count": 0, "turns_without_evidence": 0, "blocker_kind": None,
         "goal_hash_matches": True, "unauthorized_goal_change": False,
         "previous_rollover_reason": None, "evidence_since_last_rollover": True,
         "at_chunk_boundary": False, "uninterruptible_job_running": False}
    s.update(over)
    return s


P = monitor.Policy()


def test_defaults_are_owner_relay_units():
    assert P.relay_soft_tokens == 200_000 and P.relay_hard_tokens == 300_000


def test_large_window_does_not_wait_for_model_limit():
    # 1M window: 210k is only 21% but above the soft relay unit.
    d = monitor.evaluate(_sig(context_tokens=210_000, at_chunk_boundary=True), P, "CONTROLLED_AUTO")
    assert d["action"] == "ROLLOVER" and "relay_chunk" in d["reasons"] and d["urgency"] == "chunk_boundary"


def test_soft_relay_waits_for_chunk_boundary():
    d = monitor.evaluate(_sig(context_tokens=210_000, at_chunk_boundary=False), P, "CONTROLLED_AUTO")
    assert d["action"] == "CONTINUE" and "relay_pending_chunk_boundary" in d["reasons"]
    assert d["level"] == "YELLOW"


def test_hard_relay_stops_now_even_mid_chunk():
    d = monitor.evaluate(_sig(context_tokens=310_000, at_chunk_boundary=False), P, "CONTROLLED_AUTO")
    assert d["action"] == "ROLLOVER" and "relay_hard" in d["reasons"] and d["urgency"] == "now"
    assert d["level"] == "RED"


def test_uninterruptible_job_defers_even_hard_relay():
    d = monitor.evaluate(_sig(context_tokens=310_000, uninterruptible_job_running=True), P, "CONTROLLED_AUTO")
    assert d["action"] == "DEFER" and "uninterruptible_job" in d["reasons"]
    d = monitor.evaluate(_sig(context_tokens=210_000, at_chunk_boundary=True, uninterruptible_job_running=True),
                         P, "CONTROLLED_AUTO")
    assert d["action"] == "DEFER"


def test_small_window_caps_relay_units():
    # Codex window 258_400: hard 300k is capped below the window (<= 0.95 * window).
    d = monitor.evaluate(_sig(context_window=258_400, context_tokens=250_000, at_chunk_boundary=False),
                         P, "CONTROLLED_AUTO")
    assert d["action"] == "ROLLOVER" and d["urgency"] == "now"


def test_below_soft_relay_is_green_on_large_window():
    d = monitor.evaluate(_sig(context_tokens=150_000, at_chunk_boundary=True), P, "CONTROLLED_AUTO")
    assert d["level"] == "GREEN" and d["action"] == "CONTINUE"


def test_relay_units_are_configurable():
    p = monitor.Policy(relay_soft_tokens=40_000, relay_hard_tokens=60_000)
    assert monitor.evaluate(_sig(context_tokens=45_000, at_chunk_boundary=True), p, "CONTROLLED_AUTO")["action"] == "ROLLOVER"
    assert monitor.evaluate(_sig(context_tokens=65_000), p, "CONTROLLED_AUTO")["urgency"] == "now"


def test_unknown_tokens_never_relay_and_guards_still_apply():
    assert monitor.evaluate(_sig(context_tokens=None, context_window=None, at_chunk_boundary=True),
                            P, "CONTROLLED_AUTO")["action"] != "ROLLOVER"
    assert monitor.evaluate(_sig(context_tokens=310_000, blocker_kind="rate_limit"), P, "CONTROLLED_AUTO")["action"] == "BLOCK"
    assert monitor.evaluate(_sig(context_tokens=310_000, rollovers_last_hour=3), P, "CONTROLLED_AUTO")["action"] == "BLOCK"
    assert monitor.evaluate(_sig(context_tokens=310_000, session_started_at=10_000.0 - 30), P,
                            "CONTROLLED_AUTO")["action"] != "ROLLOVER"
    assert monitor.evaluate(_sig(context_tokens=310_000), P, "OBSERVE_ONLY")["action"] == "OBSERVE"

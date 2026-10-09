"""Conservative side effect replay decisions."""
STATES = ("NOT_STARTED", "IN_FLIGHT", "CONFIRMED_DONE", "FAILED", "OUTCOME_UNKNOWN")


def may_execute(state: str, has_idempotent_query: bool) -> bool:
    return state in ("NOT_STARTED", "FAILED")


def may_query(state: str, has_idempotent_query: bool) -> bool:
    return state == "OUTCOME_UNKNOWN" and has_idempotent_query

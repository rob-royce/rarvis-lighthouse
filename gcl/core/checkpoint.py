"""Required durable checkpoint fields."""
from .errors import CheckpointIncomplete

REQUIRED_FIELDS = ("mission_id", "predecessor_session_id", "beacon_version", "beacon_hash", "phase",
                   "next_step", "verified_results", "blockers", "unverified_claims", "abandoned_hypotheses",
                   "repo", "snapshot", "active_jobs", "pending_effects", "owner_cursor", "resume_method")


def validate(value: dict) -> None:
    missing = [field for field in REQUIRED_FIELDS if field not in value or value[field] is None]
    if missing:
        raise CheckpointIncomplete("missing checkpoint fields: " + ", ".join(missing))

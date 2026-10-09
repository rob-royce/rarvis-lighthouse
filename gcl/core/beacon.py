"""Versioned goal identity and complete controlled contract rendering."""
import hashlib
import json

CONTROLLED_FIELDS = ("mission_id", "owner", "owner_instruction", "objective", "done_when",
                     "non_goals", "constraints", "route_boundaries", "acceptance", "decision_refs")
MUTABLE_FIELDS = ("plan", "assumptions")


def canonical_hash(beacon: dict) -> str:
    payload = {key: beacon[key] for key in CONTROLLED_FIELDS if key in beacon}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def render_brief(beacon: dict, max_chars: int | None = None) -> str:
    """Render the complete controlled contract; an explicit cap must fail visibly.

    Runtime handoffs use the full contract. Truncation would change its meaning,
    so callers with a hard transport limit must handle an oversized contract.
    """
    fields = ("mission_id", "version", "hash", *CONTROLLED_FIELDS[1:])
    text = "\n".join(f"{key}: " + json.dumps(beacon.get(key), ensure_ascii=False)
                     for key in fields)
    if max_chars is not None and len(text) > max_chars:
        raise ValueError("complete goal contract exceeds max_chars")
    return text

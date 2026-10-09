"""Ledger-backed capabilities for managed Claude sessions."""
from __future__ import annotations

import hashlib
import hmac
import os
import time


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _capability(store, mission: str, session: str, epoch: int) -> dict | None:
    # Indexed lookup: this runs on every Claude tool call, so never scan the ledger.
    return store.latest_event(mission, "runtime.session_capability", session_id=session, epoch=epoch)


def registered(store, mission: str, session: str, epoch: int) -> bool:
    return _capability(store, mission, session, epoch) is not None


def verify_worker(store, mission: str, session: str, epoch: int) -> bool:
    token = os.environ.get("GCL_SESSION_TOKEN", "")
    capability = _capability(store, mission, session, epoch) or {}
    valid = bool(token and capability.get("sha256") and store.check_writer(mission, session, epoch) and
                 hmac.compare_digest(capability["sha256"], digest(token)))
    if not valid:
        store.note_event(mission, "runtime.impersonation_rejected",
                         {"session_id": session, "epoch": epoch, "at": time.time()})
    return valid


def verify_prepared_worker(store, mission: str, session: str, epoch: int) -> bool:
    """Authenticate a launched successor without granting it writer rights."""
    token = os.environ.get("GCL_SESSION_TOKEN", "")
    capability = _capability(store, mission, session, epoch) or {}
    valid = bool(token and capability.get("sha256") and hmac.compare_digest(capability["sha256"], digest(token)))
    if not valid:
        store.note_event(mission, "runtime.impersonation_rejected",
                         {"session_id": session, "epoch": epoch, "at": time.time()})
    return valid

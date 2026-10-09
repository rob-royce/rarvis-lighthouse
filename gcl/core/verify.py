"""Independent checkpoint and fresh-session evidence checks."""
from __future__ import annotations

import subprocess

from . import beacon as beacon_api, snapshot
from .regular_file import sha256_regular


def verify_checkpoint(checkpoint: dict, repo_dir, beacon: dict, now: float, max_evidence_age_s: float) -> dict:
    items = []

    def check(name, ok, detail):
        items.append({"check": name, "ok": bool(ok), "detail": detail})

    hash_ok = (checkpoint.get("beacon_hash") == beacon_api.canonical_hash(beacon)
               and checkpoint.get("beacon_hash") == beacon.get("hash"))
    check("beacon_hash", hash_ok, "controlled goal hash")
    version_ok = checkpoint.get("beacon_version") == beacon.get("version")
    check("beacon_version", version_ok, "goal version")
    repo = checkpoint.get("repo") or {}
    expected_head = repo.get("head")
    try:
        actual_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True,
                                     capture_output=True, text=True).stdout.strip()
        exists = subprocess.run(["git", "cat-file", "-t", expected_head or ""], cwd=repo_dir,
                                check=True, capture_output=True, text=True).stdout.strip() == "commit"
    except (OSError, subprocess.CalledProcessError):
        actual_head, exists = None, False
    head_ok = exists and actual_head == expected_head
    check("repo.head", head_ok, "base commit exists and is checked out")
    snapshot_ok = snapshot.verify(checkpoint.get("snapshot") or {}, repo_dir)
    check("snapshot", snapshot_ok, "worktree matches captured files")
    rejected = not all(item["ok"] for item in items)
    for index, evidence in enumerate(checkpoint.get("verified_results", [])):
        try:
            file_hash = sha256_regular(evidence["evidence_path"])
            age = now - float(evidence["observed_at"])
            ok = file_hash == evidence["sha256"] and 0 <= age <= max_evidence_age_s
        except (OSError, KeyError, TypeError, ValueError):
            ok = False
        check(f"verified_results[{index}]", ok, "evidence hash and age")
    return {"verdict": "REJECTED" if rejected else "PARTIAL" if not all(item["ok"] for item in items)
            else "TRUSTED", "items": items}


def verify_fresh(receipt: dict) -> tuple[bool, str]:
    executor = receipt.get("executor")
    if executor == "codex":
        meta = receipt.get("session_meta") or {}
        if not meta.get("id") or meta.get("session_id") != meta.get("id"):
            return False, "session identity mismatch"
        if meta.get("forked_from_id") or meta.get("parent_thread_id"):
            return False, "session has an ancestor"
        tokens = receipt.get("first_token_count") or {}
        if tokens.get("total") is None or tokens.get("total") != tokens.get("last"):
            return False, "first token count does not prove a fresh context"
        return True, "fresh Codex thread"
    if executor == "claude":
        if (not receipt.get("requested_session_id") or receipt.get("session_id") != receipt.get("requested_session_id")
                or receipt.get("hook_source") != "startup" or receipt.get("transcript_preexisting") is not False):
            return False, "Claude session is not a fresh startup"
        return True, "fresh Claude session"
    return False, "unknown executor"

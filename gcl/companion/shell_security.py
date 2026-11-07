"""Tauri 2.0 minimal security shell configuration and clean-room license auditor.

Enforces:
  1. CSP `connect-src 'none'` and `default-src 'self'` (zero external network egress).
  2. Strict Rust/IPC capability allowlist: read-only companion commands only
     (`companion_describe`, `companion_refresh`, `companion_snapshot`, `companion_export_handoff`).
     No shell:allow-execute, no http/websocket/fetch plugins, and no SQLite write commands.
  3. Clean-room license policy (Apache-2.0): audits THIRD_PARTY_NOTICES.md and dependency
     manifests to ensure zero AGPL-3.0 or prohibited competitor references (`opcode`,
     `paseo`, `claude-squad`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALLOWED_IPC_COMMANDS = (
    "companion_describe",
    "companion_refresh",
    "companion_snapshot",
    "companion_export_handoff",
)

FORBIDDEN_TAURI_PERMISSIONS = (
    "shell:allow-execute",
    "shell:allow-spawn",
    "shell:allow-open",
    "http:default",
    "http:allow-fetch",
    "fs:allow-write",
    "fs:allow-remove",
    "sql:allow-execute",
)

FORBIDDEN_CRATES = (
    "reqwest",
    "ureq",
    "hyper",
    "tokio-tungstenite",
    "tauri-plugin-http",
    "tauri-plugin-shell",
    "tauri-plugin-updater",
)

FORBIDDEN_COMPETITOR_AND_LICENSE_TOKENS = (
    "agpl",
    "agpl-3.0",
    "opcode",
    "paseo",
    "claude-squad",
)


def default_tauri_security_config() -> dict[str, Any]:
    """Return the canonical Tauri 2.0 minimal read-only companion security configuration."""
    return {
        "$schema": "https://schema.tauri.app/config/2",
        "productName": "RARVIS-Lighthouse Companion",
        "version": "0.2.0",
        "identifier": "com.rarvis.lighthouse.companion",
        "app": {
            "withGlobalTauri": False,
            "security": {
                "csp": (
                    "default-src 'self'; "
                    "connect-src 'none'; "
                    "img-src 'self' data:; "
                    "style-src 'self' 'unsafe-inline'; "
                    "script-src 'self'; "
                    "object-src 'none'; "
                    "frame-ancestors 'none'; "
                    "base-uri 'self'; "
                    "form-action 'none'"
                ),
                "freezePrototype": True,
                "dangerousDisableAssetCspModification": False,
                "capabilities": ["companion-readonly-default"],
            },
            "windows": [
                {
                    "label": "main",
                    "title": "RARVIS-Lighthouse Companion (Read-Only)",
                    "width": 1080,
                    "height": 760,
                    "resizable": True,
                    "fullscreen": False,
                }
            ],
        },
        "companionGuardrails": {
            "readOnlyEnforced": True,
            "directDbWriteButtons": False,
            "outboundNetworkAllowed": False,
            "allowedIpcCommands": list(ALLOWED_IPC_COMMANDS),
            "forbiddenPermissions": list(FORBIDDEN_TAURI_PERMISSIONS),
            "forbiddenCrates": list(FORBIDDEN_CRATES),
            "licensePolicy": "Apache-2.0 Clean-Room (No AGPL-3.0)",
        },
    }


def audit_companion_shell_security(config: dict[str, Any]) -> dict[str, Any]:
    """Audit a Tauri 2.0 configuration dictionary against fail-closed companion security rules."""
    violations: list[str] = []
    app = config.get("app") if isinstance(config, dict) else None
    security = app.get("security") if isinstance(app, dict) else None
    if not isinstance(security, dict):
        return {"ok": False, "violations": ["missing app.security block"]}

    csp = security.get("csp")
    if not isinstance(csp, str) or "connect-src 'none'" not in csp:
        violations.append("CSP must explicitly enforce connect-src 'none'")
    if isinstance(csp, str):
        for directive in csp.split(";"):
            part = directive.strip()
            if part.startswith("connect-src") and part != "connect-src 'none'":
                violations.append(f"connect-src must be strictly 'none', found: {part}")
            if "http:" in part or "https:" in part or "ws:" in part or "wss:" in part:
                violations.append(f"CSP contains external network scheme in directive: {part}")

    if security.get("freezePrototype") is not True:
        violations.append("app.security.freezePrototype must be True")

    guardrails = config.get("companionGuardrails") or {}
    if guardrails.get("readOnlyEnforced") is not True:
        violations.append("companionGuardrails.readOnlyEnforced must be True")
    if guardrails.get("directDbWriteButtons") is not False:
        violations.append("companionGuardrails.directDbWriteButtons must be False")
    if guardrails.get("outboundNetworkAllowed") is not False:
        violations.append("companionGuardrails.outboundNetworkAllowed must be False")

    ipc_commands = guardrails.get("allowedIpcCommands")
    if not isinstance(ipc_commands, list) or not ipc_commands:
        violations.append("companionGuardrails.allowedIpcCommands must be a non-empty list")
    else:
        unexpected = [cmd for cmd in ipc_commands if cmd not in ALLOWED_IPC_COMMANDS]
        if unexpected:
            violations.append(f"unauthorized IPC commands in allowlist: {unexpected}")

    permissions = security.get("permissions") or []
    for perm in permissions:
        if perm in FORBIDDEN_TAURI_PERMISSIONS:
            violations.append(f"forbidden Tauri permission enabled: {perm}")

    crates = guardrails.get("dependencies") or []
    for crate in crates:
        if crate in FORBIDDEN_CRATES:
            violations.append(f"forbidden network/shell crate present: {crate}")

    return {"ok": len(violations) == 0, "violations": violations}


def audit_third_party_notices(notices_path: str | Path) -> dict[str, Any]:
    """Verify THIRD_PARTY_NOTICES.md complies with Apache-2.0 clean-room rules."""
    path = Path(notices_path)
    if not path.is_file():
        return {"ok": False, "violations": ["THIRD_PARTY_NOTICES.md is missing"]}
    text = path.read_text(encoding="utf-8")
    lower = text.lower()
    violations: list[str] = []
    if "apache license 2.0" not in lower and "apache-2.0" not in lower:
        violations.append("Apache-2.0 license declaration missing")
    for token in FORBIDDEN_COMPETITOR_AND_LICENSE_TOKENS:
        if token in lower:
            violations.append(f"prohibited token found in THIRD_PARTY_NOTICES.md: {token}")
    return {"ok": len(violations) == 0, "violations": violations}


def load_repo_tauri_security_config(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Load companion/tauri.conf.security.json from the repository or return default."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]
    config_path = Path(repo_root) / "companion" / "tauri.conf.security.json"
    if config_path.is_file():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return default_tauri_security_config()

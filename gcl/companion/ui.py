"""Clean-room deterministic HTML/JSON UI renderer for the RARVIS-Lighthouse v0.2 Companion."""
from __future__ import annotations

import html
import json
from typing import Any

from .cards import (
    DoneCheckResultCard,
    PendingActionCommandCard,
    StepSummaryCard,
    build_done_check_result_card,
    build_step_summary_cards,
    export_redacted_handoff,
)
from .datasource import DataSourceSnapshot


def build_companion_view_model(
    snapshot: DataSourceSnapshot,
    *,
    mission_id: str | None = None,
    home_override: str | None = None,
) -> dict[str, Any]:
    """Assemble the complete clean-room companion UI state for a snapshot."""
    selected = None
    if snapshot.missions:
        if mission_id:
            selected = next((m for m in snapshot.missions if m.get("mission") == mission_id), None)
        else:
            selected = snapshot.missions[-1]

    if selected is None:
        return {
            "contract_version": snapshot.contract_version,
            "descriptor": snapshot.descriptor.to_dict(),
            "selected_mission": None,
            "beacon_header": None,
            "step_summary_cards": [],
            "result_card": None,
            "pending_actions": [],
            "redacted_handoff": None,
        }

    beacon = selected.get("beacon") or {}
    step_cards = build_step_summary_cards(selected)
    result_card = build_done_check_result_card(selected, home_override=home_override)
    handoff = export_redacted_handoff(selected, snapshot_revision=snapshot.revision)

    return {
        "contract_version": snapshot.contract_version,
        "descriptor": snapshot.descriptor.to_dict(),
        "selected_mission": selected["mission"],
        "beacon_header": {
            "mission_id": selected["mission"],
            "version": beacon.get("version"),
            "hash": beacon.get("hash"),
            "objective": beacon.get("objective"),
            "mode": selected.get("mode"),
            "epoch": selected.get("epoch"),
            "session_id": selected.get("session_id"),
            "runtime_status": selected.get("runtime_status"),
            "freshness": selected.get("freshness"),
            "done_when": list(beacon.get("done_when") or []),
            "non_goals": list(beacon.get("non_goals") or []),
            "constraints": list(beacon.get("constraints") or []),
        },
        "step_summary_cards": [c.to_dict() for c in step_cards],
        "result_card": result_card.to_dict(),
        "pending_actions": [a.to_dict() for a in result_card.pending_actions],
        "redacted_handoff": handoff,
    }


def render_companion_html(view_model: dict[str, Any]) -> str:
    """Render a self-contained, CSP-compliant (connect-src 'none') HTML view of the Companion."""
    descriptor = view_model.get("descriptor") or {}
    header = view_model.get("beacon_header")
    result_card = view_model.get("result_card")
    step_cards = view_model.get("step_summary_cards") or []
    pending_actions = view_model.get("pending_actions") or []
    handoff = view_model.get("redacted_handoff")

    esc = lambda v: html.escape(str(v if v is not None else ""))

    if not header:
        body_html = (
            f"<section class='empty-state'>"
            f"<h2>Ledger State: {esc(descriptor.get('ledger_state'))}</h2>"
            f"<p>Freshness: <span class='badge'>{esc(descriptor.get('freshness'))}</span> "
            f"({esc(descriptor.get('freshness_reason'))})</p>"
            f"<p>Read-Only Enforced: <code>{esc(descriptor.get('read_only_enforced'))}</code> | "
            f"Active WAL: <code>{esc(descriptor.get('active_wal'))}</code></p>"
            f"</section>"
        )
    else:
        items_rows = "".join(
            f"<tr>"
            f"<td><code>{esc(item['item_id'])}</code></td>"
            f"<td>{esc(item['criterion'])}</td>"
            f"<td><span class='state state-{esc(item['execution_state'])}'>{esc(item['execution_state'])}</span></td>"
            f"<td><span class='state state-{esc(item['verification_state'])}'>{esc(item['verification_state'])}</span></td>"
            f"<td><span class='state state-{esc(item['independent_review_state'])}'>{esc(item['independent_review_state'])}</span></td>"
            f"<td><span class='state state-{esc(item['owner_confirmation_state'])}'>{esc(item['owner_confirmation_state'])}</span></td>"
            f"<td><code>{esc(item['overall_verdict'])}</code></td>"
            f"</tr>"
            for item in (result_card.get("items") if result_card else [])
        )
        steps_html = "".join(
            f"<details class='step-card severity-{esc(step['severity'])}' {'open' if not step['collapsed_by_default'] else ''}>"
            f"<summary><strong>[{esc(step['category'])}]</strong> {esc(step['title'])} "
            f"<span class='meta'>epoch {esc(step['epoch'])}</span></summary>"
            f"<p>{esc(step['summary'])}</p>"
            + (
                f"<pre class='cli-preview'>{esc(step['remediation_cli'])}</pre>"
                if step.get("remediation_cli")
                else ""
            )
            + "</details>"
            for step in step_cards
        )
        actions_html = "".join(
            f"<div class='pending-action-card' data-direct-write='false'>"
            f"<h4>{esc(act['title'])} <span class='badge'>CLI-ONLY (No Direct DB Write)</span></h4>"
            f"<p>{esc(act['explanation'])}</p>"
            f"<pre class='cli-command'>{esc(act['cli_command'])}</pre>"
            f"</div>"
            for act in pending_actions
        )
        handoff_json = esc(json.dumps(handoff, indent=2, ensure_ascii=False)) if handoff else ""

        body_html = f"""
        <header class="beacon-header">
          <div class="beacon-meta">
            <span class="badge">Freshness: {esc(header.get('freshness'))}</span>
            <span class="badge">Revision: {esc(descriptor.get('revision'))}</span>
            <span class="badge">Active WAL: {esc(descriptor.get('active_wal'))}</span>
            <span class="badge">Status: {esc(header.get('runtime_status'))}</span>
          </div>
          <h1>Mission Objective: {esc(header.get('objective'))}</h1>
          <p class="contract-anchor">
            Mission <code>{esc(header.get('mission_id'))}</code> ·
            Beacon v<code>{esc(header.get('version'))}</code> ·
            Hash <code>{esc(header.get('hash'))}</code> ·
            Epoch <code>{esc(header.get('epoch'))}</code>
          </p>
        </header>

        <section class="result-card-section">
          <h2>v0.2 Done-Check Result Card ({esc(result_card.get('overall_status') if result_card else 'UNKNOWN')})</h2>
          <table class="result-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Criterion</th>
                <th>1. Execution</th>
                <th>2. Verification</th>
                <th>3. Independent Review</th>
                <th>4. Owner Confirmation</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {items_rows}
            </tbody>
          </table>
        </section>

        <section class="pending-actions-section">
          <h2>Pending Confirmation Actions (Read-Only CLI Preview)</h2>
          {actions_html or "<p>No pending owner actions.</p>"}
        </section>

        <section class="step-cards-section">
          <h2>Collapsible Signal &amp; Step Summary Cards (StepSummaryCard)</h2>
          {steps_html or "<p>No step signals recorded.</p>"}
        </section>

        <section class="handoff-exporter-section">
          <h2>Redacted Handoff Package (OD-14 / C7)</h2>
          <details>
            <summary>View Sanitized JSON Bundle</summary>
            <pre class="handoff-json">{handoff_json}</pre>
          </details>
        </section>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'self'; connect-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none';" />
  <title>RARVIS-Lighthouse Companion (Read-Only)</title>
</head>
<body>
  <main id="companion-root" data-read-only="true">
    {body_html}
  </main>
</body>
</html>
"""

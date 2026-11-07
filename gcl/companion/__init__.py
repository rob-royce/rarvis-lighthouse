"""Clean-room read-only companion layer and desktop shell contracts for RARVIS-Lighthouse."""
from .cards import (
    EXECUTION_STATES,
    INDEPENDENT_REVIEW_STATES,
    OWNER_CONFIRMATION_STATES,
    VERIFICATION_STATES,
    DoneCheckItemView,
    DoneCheckResultCard,
    PendingActionCommandCard,
    StepSummaryCard,
    build_done_check_result_card,
    build_pending_action_cards,
    build_step_summary_cards,
    export_redacted_handoff,
    redact_public_text,
)
from .datasource import (
    FRESHNESS_STATES,
    LEDGER_STATES,
    CompanionDataSourceError,
    DataSourceDescriptor,
    DataSourceSnapshot,
    LighthouseReadOnlyDataSource,
    RevisionMismatchError,
)
from .shell_security import (
    audit_companion_shell_security,
    audit_third_party_notices,
    default_tauri_security_config,
)
from .ui import build_companion_view_model, render_companion_html

__all__ = [
    "CompanionDataSourceError",
    "DataSourceDescriptor",
    "DataSourceSnapshot",
    "DoneCheckItemView",
    "DoneCheckResultCard",
    "EXECUTION_STATES",
    "FRESHNESS_STATES",
    "INDEPENDENT_REVIEW_STATES",
    "LEDGER_STATES",
    "LighthouseReadOnlyDataSource",
    "OWNER_CONFIRMATION_STATES",
    "PendingActionCommandCard",
    "RevisionMismatchError",
    "StepSummaryCard",
    "VERIFICATION_STATES",
    "audit_companion_shell_security",
    "audit_third_party_notices",
    "build_companion_view_model",
    "build_done_check_result_card",
    "build_pending_action_cards",
    "build_step_summary_cards",
    "default_tauri_security_config",
    "export_redacted_handoff",
    "redact_public_text",
    "render_companion_html",
]

"""Freeze the public surface of the ``cairn.server.services`` facade.

The facade re-exports every domain helper so routers keep a single import site.
That surface is a contract with its importers: extending it is fine, but renaming
or dropping a name must fail here first instead of leaking out silently.
"""

from __future__ import annotations

import types

from cairn.server import services

# The frozen contract. Edit deliberately when the facade's surface changes.
EXPECTED_EXPORTS = {
    "PROJECT_COLUMNS",
    "QUESTION_PENDING_TTL_SECONDS",
    "QUESTION_RUNNING_TTL_SECONDS",
    "RECALL_MAX_LIMIT",
    "SESSION_COLUMNS",
    "SESSION_STALE_SECONDS",
    "build_documents",
    "build_findings",
    "build_findings_summary",
    "build_goals",
    "build_project_usage",
    "build_questions",
    "build_session_messages",
    "build_sessions",
    "build_steps",
    "check_project_active",
    "check_project_completed",
    "check_project_hint_writable",
    "clear_project_decide",
    "doc_ids_by_owner",
    "document_to_meta",
    "document_to_model",
    "expire_decide_leases",
    "expire_questions",
    "expire_stale_sessions",
    "expire_steps",
    "get_claimable_step_or_404",
    "get_completion_step_or_409",
    "get_context_summary",
    "get_decide_timeout",
    "get_document_or_404",
    "get_goal_or_404",
    "get_project_or_404",
    "get_question_or_404",
    "get_recall_plan",
    "get_releasable_step_or_404",
    "get_root_goal_or_404",
    "get_session_or_404",
    "get_step_or_404",
    "get_step_timeout",
    "goal_to_model",
    "insert_document",
    "list_pending_questions",
    "next_document_id",
    "next_fact_id",
    "next_finding_id",
    "next_goal_id",
    "next_hint_id",
    "next_project_id",
    "next_question_id",
    "next_session_id",
    "next_session_message_seq",
    "next_step_id",
    "normalize_finding_severity",
    "project_decide_from_row",
    "project_meta_from_row",
    "question_to_model",
    "recall",
    "recall_tokens",
    "save_context_summary",
    "save_recall_plan",
    "session_to_model",
    "step_to_model",
    "utcnow",
    "utcnow_before",
    "validate_creator_worker",
    "validate_facts_exist",
    "validate_goal_exists",
}


def test_exports_match_frozen_contract() -> None:
    assert set(services.__all__) == EXPECTED_EXPORTS
    assert len(services.__all__) == len(EXPECTED_EXPORTS)


def test_every_export_is_importable() -> None:
    missing = sorted(name for name in services.__all__ if not hasattr(services, name))
    assert missing == []


def test_no_unlisted_public_exports() -> None:
    def is_export_candidate(name: str, value: object) -> bool:
        if name.startswith("_") or name in EXPECTED_EXPORTS:
            return False
        if isinstance(value, types.ModuleType):
            return False
        # `from __future__ import annotations` binds a real name; not API surface.
        return type(value).__module__ != "__future__"

    unexpected = sorted(
        name for name, value in vars(services).items() if is_export_candidate(name, value)
    )
    assert unexpected == []

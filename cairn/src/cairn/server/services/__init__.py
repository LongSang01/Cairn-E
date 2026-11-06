"""Server domain helpers, grouped by concern.

Every public name is re-exported here so routers keep a single import site
(``from cairn.server.services import ...``).
"""

from __future__ import annotations

from .constants import (
    PROJECT_COLUMNS,
    QUESTION_PENDING_TTL_SECONDS,
    QUESTION_RUNNING_TTL_SECONDS,
    RECALL_MAX_LIMIT,
    SESSION_COLUMNS,
    SESSION_STALE_SECONDS,
)
from .context import (
    get_context_summary,
    save_context_summary,
)
from .documents import (
    build_documents,
    doc_ids_by_owner,
    document_to_meta,
    document_to_model,
    insert_document,
)
from .evidence import (
    build_finding_evidence,
    exchange_evidence_id,
    find_exchange_by_url,
    get_exchange,
    parse_http_exchanges,
    resolve_evidence,
    store_exchanges,
    store_finding_evidence,
)
from .graph import (
    build_findings,
    build_findings_summary,
    build_goals,
    build_steps,
    goal_to_model,
    normalize_finding_severity,
    step_to_model,
)
from .guards import (
    check_project_active,
    check_project_completed,
    check_project_hint_writable,
    get_claimable_step_or_404,
    get_completion_step_or_409,
    get_document_or_404,
    get_goal_or_404,
    get_project_or_404,
    get_question_or_404,
    get_releasable_step_or_404,
    get_root_goal_or_404,
    get_session_or_404,
    get_step_or_404,
    validate_creator_worker,
    validate_facts_exist,
    validate_goal_exists,
)
from .ids import (
    next_document_id,
    next_fact_id,
    next_finding_id,
    next_goal_id,
    next_hint_id,
    next_project_id,
    next_question_id,
    next_session_id,
    next_session_message_seq,
    next_step_id,
)
from .leases import (
    clear_project_decide,
    expire_decide_leases,
    expire_questions,
    expire_stale_sessions,
    expire_steps,
    get_decide_timeout,
    get_step_timeout,
    project_decide_from_row,
    project_meta_from_row,
)
from .questions import (
    build_questions,
    list_pending_questions,
    question_to_model,
)
from .recall import (
    get_recall_plan,
    recall,
    recall_tokens,
    save_recall_plan,
)
from .sessions import (
    build_session_messages,
    build_sessions,
    session_to_model,
)
from .timeutil import (
    utcnow,
    utcnow_before,
)
from .usage import (
    build_project_usage,
)

__all__ = [
    "PROJECT_COLUMNS",
    "QUESTION_PENDING_TTL_SECONDS",
    "QUESTION_RUNNING_TTL_SECONDS",
    "RECALL_MAX_LIMIT",
    "SESSION_COLUMNS",
    "SESSION_STALE_SECONDS",
    "build_documents",
    "build_finding_evidence",
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
    "exchange_evidence_id",
    "expire_decide_leases",
    "expire_questions",
    "expire_stale_sessions",
    "expire_steps",
    "find_exchange_by_url",
    "get_claimable_step_or_404",
    "get_completion_step_or_409",
    "get_context_summary",
    "get_decide_timeout",
    "get_document_or_404",
    "get_exchange",
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
    "parse_http_exchanges",
    "project_decide_from_row",
    "project_meta_from_row",
    "question_to_model",
    "recall",
    "recall_tokens",
    "resolve_evidence",
    "save_context_summary",
    "save_recall_plan",
    "session_to_model",
    "step_to_model",
    "store_exchanges",
    "store_finding_evidence",
    "utcnow",
    "utcnow_before",
    "validate_creator_worker",
    "validate_facts_exist",
    "validate_goal_exists",
]

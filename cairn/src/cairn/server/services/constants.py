"""Column lists and TTLs shared across the server services."""

from __future__ import annotations

PROJECT_COLUMNS = (
    "id, title, status, created_at, decide_worker, decide_trigger, "
    "decide_started_at, decide_last_heartbeat_at"
)

SESSION_COLUMNS = (
    "s.*, (SELECT COUNT(*) FROM session_messages m "
    "WHERE m.project_id = s.project_id AND m.session_id = s.id) AS message_count"
)

SESSION_STALE_SECONDS = 60

QUESTION_PENDING_TTL_SECONDS = 300

QUESTION_RUNNING_TTL_SECONDS = 900

RECALL_MAX_LIMIT = 20

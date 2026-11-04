"""Rolling context summary used by the dispatcher's graph budget.

The summary is produced by the dispatcher when older facts leave the recent window; the
server only stores it so the next Decide run can reuse it instead of summarizing again.
"""

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import ContextSummary
from cairn.server.services import (
    get_context_summary,
    get_project_or_404,
    save_context_summary,
)

router = APIRouter(tags=["context"])


@router.get("/projects/{project_id}/context", response_model=ContextSummary)
def read_context_summary(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return get_context_summary(conn, project_id)


@router.put("/projects/{project_id}/context", response_model=ContextSummary)
def write_context_summary(project_id: str, body: ContextSummary):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return save_context_summary(
            conn,
            project_id,
            body.summary,
            body.facts_covered,
            step_summary=body.step_summary,
            steps_covered=body.steps_covered,
            finding_summary=body.finding_summary,
            findings_covered=body.findings_covered,
        )

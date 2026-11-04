from fastapi import APIRouter, Query

from cairn.server.db import get_conn
from cairn.server.models import RecallPlan, RecallResponse
from cairn.server.services import (
    check_project_active,
    get_project_or_404,
    get_recall_plan,
    recall,
    save_recall_plan,
)

router = APIRouter(tags=["recall"])


@router.get("/projects/{project_id}/recall", response_model=RecallResponse)
def recall_project(
    project_id: str,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=5, ge=1, le=20),
):
    """Keyword recall over titles/descriptions: the way to reach history outside the window."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return recall(conn, project_id, q, limit=limit)


@router.get("/projects/{project_id}/recall-plan", response_model=RecallPlan)
def read_recall_plan(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return get_recall_plan(conn, project_id)


@router.put("/projects/{project_id}/recall-plan", response_model=RecallPlan)
def write_recall_plan(project_id: str, body: RecallPlan):
    """Decide stores what it wants looked up; the next run injects the results."""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        return save_recall_plan(conn, project_id, body)

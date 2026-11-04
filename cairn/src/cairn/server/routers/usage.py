"""Measured token usage of the worker runs in one project."""

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import ProjectUsage
from cairn.server.services import build_project_usage, get_project_or_404

router = APIRouter(tags=["usage"])


@router.get("/projects/{project_id}/usage", response_model=ProjectUsage)
def get_project_usage(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_project_usage(conn, project_id)

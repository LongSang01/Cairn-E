"""Findings as a report: the graded products of the search, plus their aggregates."""

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import Finding, FindingsSummary
from cairn.server.services import (
    build_findings,
    build_findings_summary,
    get_project_or_404,
)

router = APIRouter(tags=["findings"])


@router.get("/projects/{project_id}/findings", response_model=list[Finding])
def list_findings(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_findings(conn, project_id)


@router.get("/projects/{project_id}/findings/summary", response_model=FindingsSummary)
def summarize_findings(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_findings_summary(conn, project_id)

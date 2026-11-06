"""Guard endpoints used by the pi extension and by the web UI."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import ToolApprovalDecision, ToolGuardCheckRequest
from cairn.server.services import (
    decide_tool_approval,
    expire_tool_approvals,
    get_approval,
    get_project_or_404,
    guard_check,
    list_tool_audit,
    pending_approvals,
)

router = APIRouter()


def _approval_payload(row) -> dict[str, object]:
    return {
        "id": row["id"],
        "status": row["status"],
        "tool": row["tool"],
        "command": row["command"],
        "worker": row["worker"],
        "session": row["session_id"],
        "reason": row["reason"],
        "decision_source": row["decision_source"],
        "decided_by": row["decided_by"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


@router.post("/projects/{project_id}/tool-guard/check")
def check_command(project_id: str, body: ToolGuardCheckRequest):
    """Called by the worker extension before every shell command."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_tool_approvals(conn)
        return guard_check(
            conn,
            project_id=project_id,
            tool=body.tool,
            command=body.command,
            worker=body.worker,
            session_id=body.session,
        )


@router.get("/projects/{project_id}/tool-approvals")
def list_pending(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_tool_approvals(conn)
        return {"approvals": [_approval_payload(row) for row in pending_approvals(conn, project_id)]}


@router.get("/projects/{project_id}/tool-approvals/{approval_id}")
def read_approval(project_id: str, approval_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_tool_approvals(conn)
        row = get_approval(conn, project_id, approval_id)
        if row is None:
            raise HTTPException(404, f"Approval {approval_id} not found")
        return _approval_payload(row)


@router.post("/projects/{project_id}/tool-approvals/{approval_id}/decide")
def decide(project_id: str, approval_id: str, body: ToolApprovalDecision):
    """Human verdict. The extension is polling this row and unblocks on the change."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        try:
            row = decide_tool_approval(
                conn,
                project_id,
                approval_id,
                approve=body.approve,
                actor=body.actor,
                note=body.note,
            )
        except LookupError as exc:
            raise HTTPException(404, f"Approval {approval_id} not found") from exc
        except ValueError as exc:
            raise HTTPException(409, f"Approval is already {exc}") from exc
        return _approval_payload(row)


@router.get("/projects/{project_id}/tool-audit")
def read_audit(project_id: str, limit: int = 100):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = list_tool_audit(conn, project_id, limit=limit)
        return {
            "entries": [
                {
                    "id": row["id"],
                    "tool": row["tool"],
                    "command": row["command"],
                    "digest": row["digest"],
                    "decision": row["decision"],
                    "decision_source": row["decision_source"],
                    "rule_id": row["rule_id"],
                    "actor": row["actor"],
                    "worker": row["worker"],
                    "session": row["session_id"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

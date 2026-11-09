"""Task-level authorization scope: which hosts this test may touch."""

from __future__ import annotations

import ipaddress
import sqlite3

from fastapi import APIRouter, HTTPException

from cairn.contracts.models import ScopeEntry
from cairn.server.db import get_conn
from cairn.server.models import CreateScopeEntryRequest, ScopeCheckRequest
from cairn.server.services import (
    check_project_hint_writable,
    get_project_or_404,
    utcnow,
)
from cairn.server.services.hosts import host_candidates
from cairn.server.services.scope import (
    first_out_of_scope_host,
    host_in_scope,
    load_scope,
)

router = APIRouter(tags=["scope"])


def _entry_from_row(row) -> ScopeEntry:
    return ScopeEntry(
        id=row["id"],
        kind=row["kind"],
        value=row["value"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _validate(kind: str, value: str) -> None:
    if kind in ("ip", "cidr"):
        try:
            if kind == "cidr":
                ipaddress.ip_network(value, strict=False)
            else:
                ipaddress.ip_address(value)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid {kind}: {value}") from exc
    elif not value or " " in value or "/" in value:
        raise HTTPException(422, f"Invalid {kind}: {value}")


@router.get("/projects/{project_id}/scope")
def list_scope(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return {"entries": [_entry_from_row(row) for row in load_scope(conn, project_id)]}


@router.post("/projects/{project_id}/scope", response_model=ScopeEntry, status_code=201)
def add_scope_entry(project_id: str, body: CreateScopeEntryRequest):
    """Adding the first entry closes the gate: unlisted hosts become out of scope."""
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        _validate(body.kind, body.value)
        try:
            cursor = conn.execute(
                """
                INSERT INTO project_scope (project_id, kind, value, source, created_at)
                VALUES (?, ?, ?, 'human', ?)
                """,
                (project_id, body.kind, body.value, utcnow()),
            )
        except sqlite3.IntegrityError as exc:
            # Only a UNIQUE(project_id, kind, value) collision is a 409; a
            # locking or schema error must not be disguised as "already exists".
            raise HTTPException(409, "Scope entry already exists") from exc
        row = conn.execute(
            "SELECT * FROM project_scope WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _entry_from_row(row)


@router.delete("/projects/{project_id}/scope/{entry_id}", status_code=204)
def delete_scope_entry(project_id: str, entry_id: int):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        cursor = conn.execute(
            "DELETE FROM project_scope WHERE id = ? AND project_id = ?",
            (entry_id, project_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, f"Scope entry {entry_id} not found")
        return None


@router.post("/projects/{project_id}/scope/check")
def check_scope(project_id: str, body: ScopeCheckRequest):
    """Dry-run the *scope* gate only. No audit row, no approval queue, no `ask`.

    Tool-rule verdicts belong to `/tool-guard/check`; returning them here would
    produce an `ask` without an `approval_id` that the UI cannot act on.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        entries = load_scope(conn, project_id)
        hosts = host_candidates(body.command)
        out_of_scope = (
            [host for host in hosts if not host_in_scope(host, entries)] if entries else []
        )
        denied_host = first_out_of_scope_host(body.command, entries)
        return {
            "decision": "deny" if denied_host else "allow",
            "reason": (
                f"【Cairn 平台管控·非目标防御】目标 {denied_host} 不在本次测试范围"
                if denied_host
                else None
            ),
            "hosts": hosts,
            "out_of_scope": out_of_scope,
            "gate": bool(entries),
        }

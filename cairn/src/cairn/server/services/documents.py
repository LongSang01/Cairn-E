"""Evidence documents attached to facts and steps."""

from __future__ import annotations

import sqlite3

from cairn.contracts.models import Document, DocumentMeta

from .ids import next_document_id
from .timeutil import utcnow


def document_to_meta(row: sqlite3.Row) -> DocumentMeta:
    content = row["content"] or ""
    return DocumentMeta(
        id=row["id"],
        title=row["title"],
        fact_id=row["fact_id"],
        step_id=row["step_id"],
        size=len(content),
        created_at=row["created_at"],
    )

def document_to_model(row: sqlite3.Row) -> Document:
    return Document(
        id=row["id"],
        title=row["title"],
        fact_id=row["fact_id"],
        step_id=row["step_id"],
        size=len(row["content"] or ""),
        created_at=row["created_at"],
        content=row["content"] or "",
    )

def insert_document(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    title: str,
    content: str,
    fact_id: str | None = None,
    step_id: str | None = None,
    created_at: str | None = None,
) -> Document:
    did = next_document_id(conn, project_id)
    now = created_at or utcnow()
    conn.execute(
        """
        INSERT INTO documents (id, project_id, step_id, fact_id, title, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (did, project_id, step_id, fact_id, title, content, now),
    )
    return Document(
        id=did,
        title=title,
        fact_id=fact_id,
        step_id=step_id,
        size=len(content),
        created_at=now,
        content=content,
    )

def build_documents(conn: sqlite3.Connection, project_id: str) -> list[DocumentMeta]:
    rows = conn.execute(
        "SELECT * FROM documents WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [document_to_meta(row) for row in rows]

def doc_ids_by_owner(
    conn: sqlite3.Connection, project_id: str
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Map owner ids to their document ids without loading document bodies."""
    by_fact: dict[str, list[str]] = {}
    by_step: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT id, fact_id, step_id FROM documents WHERE project_id = ? ORDER BY rowid",
        (project_id,),
    ).fetchall()
    for row in rows:
        if row["fact_id"]:
            by_fact.setdefault(row["fact_id"], []).append(row["id"])
        if row["step_id"]:
            by_step.setdefault(row["step_id"], []).append(row["id"])
    return by_fact, by_step

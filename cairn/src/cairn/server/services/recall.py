"""Keyword recall over graph titles/descriptions plus the Decide recall plan."""

from __future__ import annotations

import json
import re
import sqlite3

from fastapi import HTTPException

from cairn.contracts.models import RecallHit, RecallPlan, RecallResponse

from .constants import RECALL_MAX_LIMIT
from .documents import doc_ids_by_owner

_LATIN_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.\-/]{1,}")

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

def recall_tokens(query: str) -> list[str]:
    """Keyword tokens: latin words plus CJK bigrams, de-duplicated in order."""
    tokens = [token.lower() for token in _LATIN_TOKEN.findall((query or "").lower())]
    for run in _CJK_RUN.findall(query or ""):
        chars = list(run)
        if len(chars) == 1:
            tokens.append(run)
            continue
        tokens.extend("".join(chars[index : index + 2]) for index in range(len(chars) - 1))
    unique: dict[str, None] = {}
    for token in tokens:
        unique.setdefault(token, None)
    return list(unique)

def _recall_score(tokens: list[str], title: str | None, description: str | None) -> int:
    heading = (title or "").lower()
    body = (description or "").lower()
    score = 0
    for token in tokens:
        if token in heading:
            score += 3
        elif token in body:
            score += 1
    return score

def recall(
    conn: sqlite3.Connection, project_id: str, query: str, *, limit: int = 5
) -> RecallResponse:
    """Cheap keyword recall over titles/descriptions; documents are fetched by id."""
    tokens = recall_tokens(query)
    if not tokens:
        raise HTTPException(422, "query must contain at least one keyword")
    limit = max(1, min(limit, RECALL_MAX_LIMIT))
    docs_by_fact, docs_by_step = doc_ids_by_owner(conn, project_id)

    facts: list[RecallHit] = []
    for row in conn.execute(
        "SELECT id, title, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall():
        score = _recall_score(tokens, row["title"], row["description"])
        if score <= 0:
            continue
        facts.append(
            RecallHit(
                kind="fact",
                id=row["id"],
                title=row["title"],
                description=row["description"],
                score=score,
                doc_ids=docs_by_fact.get(row["id"], []),
            )
        )
    facts.sort(key=lambda hit: (-hit.score, hit.id))

    steps: list[RecallHit] = []
    for row in conn.execute(
        """
        SELECT id, title, description, status FROM steps
        WHERE project_id = ? AND status NOT IN ('pending', 'running')
        """,
        (project_id,),
    ).fetchall():
        score = _recall_score(tokens, row["title"], row["description"])
        if score <= 0:
            continue
        steps.append(
            RecallHit(
                kind="step",
                id=row["id"],
                title=row["title"],
                description=row["description"],
                status=row["status"],
                score=score,
                doc_ids=docs_by_step.get(row["id"], []),
            )
        )
    steps.sort(key=lambda hit: (-hit.score, hit.id))

    return RecallResponse(query=query.strip(), facts=facts[:limit], steps=steps[:limit])

def get_recall_plan(conn: sqlite3.Connection, project_id: str) -> RecallPlan:
    row = conn.execute("SELECT recall_plan FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return _parse_recall_plan(row["recall_plan"])

def save_recall_plan(conn: sqlite3.Connection, project_id: str, plan: RecallPlan) -> RecallPlan:
    cursor = conn.execute(
        "UPDATE projects SET recall_plan = ? WHERE id = ?",
        (json.dumps(plan.model_dump(by_alias=True), ensure_ascii=False), project_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Project not found")
    return plan

def _parse_recall_plan(raw: str | None) -> RecallPlan:
    if not raw:
        return RecallPlan()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return RecallPlan()
    if not isinstance(data, dict):
        return RecallPlan()
    # A row written by another version may not match the schema; drop what cannot be
    # read and keep the rest rather than failing the whole Decide run.
    doc_ids = [
        item
        for item in data.get("doc_ids") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ][:5]
    try:
        return RecallPlan(
            queries=[str(item) for item in data.get("queries") or []][:5],
            doc_ids=doc_ids,
        )
    except ValueError:
        return RecallPlan(queries=[str(item) for item in data.get("queries") or []][:5])

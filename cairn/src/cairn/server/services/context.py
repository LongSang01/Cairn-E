"""Rolling context summaries: each channel may only move forward."""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from cairn.contracts.models import ContextSummary

_CONTEXT_COLUMNS = (
    "context_summary, context_summary_facts,"
    " context_step_summary, context_step_summary_facts,"
    " context_finding_summary, context_finding_summary_facts"
)

def get_context_summary(conn: sqlite3.Connection, project_id: str) -> ContextSummary:
    row = conn.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return _context_summary_from_row(row)

def save_context_summary(
    conn: sqlite3.Connection,
    project_id: str,
    summary: str,
    facts_covered: int,
    *,
    step_summary: str = "",
    steps_covered: int = 0,
    finding_summary: str = "",
    findings_covered: int = 0,
) -> ContextSummary:
    """Merge summaries in, letting each channel only move forward.

    Summaries are written by background tasks, so a slow one must never clobber a newer
    one: a channel is replaced only when its coverage count does not go backwards.
    """
    row = conn.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")

    facts_text, facts_count = _merge_channel(
        row["context_summary"], row["context_summary_facts"], summary, facts_covered
    )
    steps_text, steps_count = _merge_channel(
        row["context_step_summary"], row["context_step_summary_facts"], step_summary, steps_covered
    )
    findings_text, findings_count = _merge_channel(
        row["context_finding_summary"],
        row["context_finding_summary_facts"],
        finding_summary,
        findings_covered,
    )
    merged = ContextSummary(
        summary=facts_text,
        facts_covered=facts_count,
        step_summary=steps_text,
        steps_covered=steps_count,
        finding_summary=findings_text,
        findings_covered=findings_count,
    )
    conn.execute(
        "UPDATE projects SET context_summary = ?, context_summary_facts = ?,"
        " context_step_summary = ?, context_step_summary_facts = ?,"
        " context_finding_summary = ?, context_finding_summary_facts = ? WHERE id = ?",
        (
            merged.summary,
            merged.facts_covered,
            merged.step_summary,
            merged.steps_covered,
            merged.finding_summary,
            merged.findings_covered,
            project_id,
        ),
    )
    return merged

def _merge_channel(
    current: str | None, current_covered: int | None, new: str, covered: int
) -> tuple[str, int]:
    """Keep whichever side covers more history; a stale summary must not clobber a newer one."""
    previous_text = current or ""
    previous_covered = int(current_covered or 0)
    if not new or covered < previous_covered:
        return previous_text, previous_covered
    return new, covered

def _context_summary_from_row(row: sqlite3.Row) -> ContextSummary:
    return ContextSummary(
        summary=row["context_summary"] or "",
        facts_covered=int(row["context_summary_facts"] or 0),
        step_summary=row["context_step_summary"] or "",
        steps_covered=int(row["context_step_summary_facts"] or 0),
        finding_summary=row["context_finding_summary"] or "",
        findings_covered=int(row["context_finding_summary_facts"] or 0),
    )

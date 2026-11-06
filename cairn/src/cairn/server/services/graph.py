"""Build FGS graph models (facts, goals, steps, findings) from rows."""

from __future__ import annotations

import sqlite3

from cairn.contracts.models import (
    FINDING_SEVERITIES,
    FINDING_STATUSES,
    Finding,
    FindingsSummary,
    Goal,
    Step,
)

from .evidence import build_finding_evidence


def goal_to_model(row: sqlite3.Row) -> Goal:
    return Goal(
        id=row["id"],
        parent=row["parent_id"],
        description=row["description"],
        status=row["status"],
        creator=row["creator"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


def build_goals(conn: sqlite3.Connection, project_id: str) -> list[Goal]:
    rows = conn.execute(
        "SELECT * FROM goals WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [goal_to_model(row) for row in rows]


def _step_from_row(row: sqlite3.Row, source_fact_ids: list[str]) -> Step:
    return Step(
        id=row["id"],
        from_=source_fact_ids,
        to=row["result_fact_id"],
        goal=row["goal_id"],
        kind=row["kind"],
        description=row["description"],
        title=row["title"],
        rationale=row["rationale"],
        priority=row["priority"],
        status=row["status"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        note=row["note"],
    )


def step_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Step:
    sources = conn.execute(
        "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return _step_from_row(row, [source["fact_id"] for source in sources])


def build_steps(conn: sqlite3.Connection, project_id: str) -> list[Step]:
    rows = conn.execute(
        "SELECT * FROM steps WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    if not rows:
        return []
    # One query for every step's sources instead of one query per step. The project
    # detail endpoint is polled every few seconds, so this is the hot path.
    sources = conn.execute(
        "SELECT step_id, fact_id FROM step_sources WHERE project_id = ? ORDER BY rowid",
        (project_id,),
    ).fetchall()
    by_step: dict[str, list[str]] = {}
    for source in sources:
        by_step.setdefault(source["step_id"], []).append(source["fact_id"])
    return [_step_from_row(row, by_step.get(row["id"], [])) for row in rows]


def build_findings(conn: sqlite3.Connection, project_id: str) -> list[Finding]:
    rows = conn.execute(
        "SELECT * FROM findings WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    evidence = build_finding_evidence(conn, project_id, [row["id"] for row in rows])
    return [
        Finding(
            id=row["id"],
            kind=row["kind"],
            description=row["description"],
            severity=normalize_finding_severity(row["severity"]),
            status=normalize_finding_status(row["status"]),
            evidence=evidence.get(row["id"], []),
            step=row["step_id"],
            creator=row["creator"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def normalize_finding_status(value: object) -> str:
    """Coerce a stored status; anything unrecognised means `tentative`."""
    status = str(value or "tentative").strip().lower()
    return status if status in FINDING_STATUSES else "tentative"


def normalize_finding_severity(value: object) -> str:
    """Coerce a stored grade to a known severity; unknown or missing means `info`."""
    severity = str(value or "info").strip().lower()
    return severity if severity in FINDING_SEVERITIES else "info"


def build_findings_summary(conn: sqlite3.Connection, project_id: str) -> FindingsSummary:
    """Aggregate findings by severity and kind, newest last."""
    findings = build_findings(conn, project_id)
    by_severity: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        by_kind[finding.kind] = by_kind.get(finding.kind, 0) + 1
    ordered_severity = {
        severity: by_severity[severity]
        for severity in FINDING_SEVERITIES
        if severity in by_severity
    }
    return FindingsSummary(
        project_id=project_id,
        total=len(findings),
        by_severity=ordered_severity,
        by_kind=dict(sorted(by_kind.items())),
    )

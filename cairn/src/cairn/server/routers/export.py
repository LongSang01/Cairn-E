from datetime import datetime

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from cairn.server.db import get_conn
from cairn.server.models import FINDING_SEVERITIES
from cairn.server.services import (
    build_documents,
    build_findings,
    doc_ids_by_owner,
    expire_decide_leases,
    expire_steps,
    get_project_or_404,
    normalize_finding_severity,
)

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_steps(conn, project_id)
    expire_decide_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    def rows(query: str):
        return conn.execute(query, (project_id,)).fetchall()

    facts = rows(
        "SELECT id, title, description, step_id, created_at FROM facts"
        " WHERE project_id = ? ORDER BY rowid"
    )
    goals = rows("SELECT * FROM goals WHERE project_id = ? ORDER BY created_at, rowid")
    steps = rows("SELECT * FROM steps WHERE project_id = ? ORDER BY created_at, rowid")
    findings = rows("SELECT * FROM findings WHERE project_id = ? ORDER BY created_at, rowid")
    hints = rows(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at"
    )

    sources_by_step: dict[str, list[str]] = {}
    for step in steps:
        source_rows = conn.execute(
            "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
            (step["id"], project_id),
        ).fetchall()
        sources_by_step[step["id"]] = [r["fact_id"] for r in source_rows]

    return proj, facts, goals, steps, findings, hints, sources_by_step


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, goals, steps, findings, hints, sources_by_step = _load_project_data(
        conn, project_id
    )
    docs_by_fact, docs_by_step = doc_ids_by_owner(conn, project_id)

    data: dict = {
        "project": {
            "title": proj["title"],
            "status": proj["status"],
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [
        _node_entry(f, docs_by_fact.get(f["id"], []), from_step=f["step_id"]) for f in facts
    ]
    data["goals"] = [
        {
            "id": g["id"],
            "parent": g["parent_id"],
            "description": g["description"],
            "status": g["status"],
        }
        for g in goals
    ]
    if steps:
        data["steps"] = [
            {
                **_node_entry(s, docs_by_step.get(s["id"], [])),
                "from": sources_by_step.get(s["id"], []),
                "goal": s["goal_id"],
                "kind": s["kind"],
                "rationale": s["rationale"],
                "priority": s["priority"],
                "status": s["status"],
                "worker": s["worker"],
                "result": s["result_fact_id"],
                "note": s["note"],
            }
            for s in steps
        ]
    documents = _export_yaml_documents(conn, project_id)
    if documents:
        data["documents"] = documents
    if findings:
        data["findings"] = [
            {
                "id": d["id"],
                "kind": d["kind"],
                "severity": normalize_finding_severity(d["severity"]),
                "description": d["description"],
                "step": d["step_id"],
                "created_at": format_export_timestamp(d["created_at"]),
            }
            for d in findings
        ]

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _node_entry(row, doc_ids: list[str], **extra) -> dict:
    """Titles and document ids only: bulk evidence is fetched on demand."""
    entry = {"id": row["id"]}
    # sqlite3.Row has no `__contains__`; `.keys()` is the only membership check.
    if "title" in row.keys() and row["title"]:  # noqa: SIM118
        entry["title"] = row["title"]
    entry["description"] = row["description"]
    entry.update(extra)
    if doc_ids:
        entry["doc_ids"] = doc_ids
    return entry


def _export_yaml_documents(conn, project_id: str) -> list[dict]:
    return [
        {
            "id": doc.id,
            "title": doc.title,
            "fact": doc.fact_id,
            "step": doc.step_id,
            "size": doc.size,
            "created_at": format_export_timestamp(doc.created_at),
        }
        for doc in build_documents(conn, project_id)
    ]


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, goals, steps, findings, hints, sources_by_step = _load_project_data(
        conn, project_id
    )

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    roots = [g for g in goals if g["parent_id"] is None]
    goal_desc = roots[0]["description"] if roots else ""
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for g in goals:
        ts = format_export_timestamp(g["created_at"]) or ""
        parent = g["parent_id"] or "-"
        block = (
            f"[{ts}] GOAL DECLARED {g['id']} by {g['creator']}\n"
            f"  parent: {parent}\n  {g['description']}"
        )
        events.append((g["created_at"] or "", order, block))
        order += 1
        if g["status"] == "achieved" and g["parent_id"] is not None:
            ts = format_export_timestamp(g["closed_at"]) or ""
            events.append((g["closed_at"] or "", order, f"[{ts}] GOAL ACHIEVED {g['id']}"))
            order += 1
        elif g["status"] == "abandoned":
            ts = format_export_timestamp(g["closed_at"]) or ""
            events.append((g["closed_at"] or "", order, f"[{ts}] GOAL DROPPED {g['id']}"))
            order += 1

    for s in steps:
        src = sources_by_step.get(s["id"], [])
        from_str = ", ".join(src)
        ts = format_export_timestamp(s["created_at"]) or ""
        meta = f"  from: {from_str}\n  priority: {s['priority']}"
        if s["worker"] and s["status"] == "running":
            meta += f"\n  worker: {s['worker']} (in progress)"
        block = f"[{ts}] STEP DECLARED {s['id']} by {s['creator']}\n{meta}\n  {s['description']}"
        events.append((s["created_at"] or "", order, block))
        order += 1

        if s["status"] not in ("done", "abandoned"):
            continue

        ts = format_export_timestamp(s["concluded_at"]) or ""
        actor = s["worker"] or s["creator"]
        if s["kind"] == "completion":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {s['id']} from {from_str}\n  {s['description']}"
        elif s["status"] == "abandoned":
            block = f"[{ts}] STEP ABANDONED {s['id']} by {actor}\n  {s['note'] or ''}"
        else:
            fact_desc = facts_by_id.get(s["result_fact_id"] or "", "")
            produced = s["result_fact_id"] or "no fact"
            block = (
                f"[{ts}] STEP CONCLUDED {s['id']} by {actor}\n"
                f"  from: {from_str}\n  produced: {produced}\n  {fact_desc}"
            )
        events.append((s["concluded_at"] or "", order, block))
        order += 1

    for d in findings:
        ts = format_export_timestamp(d["created_at"]) or ""
        block = (
            f"[{ts}] FINDING {d['id']} ({d['kind']}, {normalize_finding_severity(d['severity'])})"
            f" by {d['creator']}\n"
            f"  step: {d['step_id'] or '-'}\n  {d['description']}"
        )
        events.append((d["created_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


_SEVERITY_LABELS = {
    "critical": "Critical",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "info": "Info",
}


def _export_findings(conn, project_id: str) -> str:
    """Findings as a graded report, grouped by severity."""
    proj = get_project_or_404(conn, project_id)
    findings = build_findings(conn, project_id)
    counts: dict[str, int] = {}
    for item in findings:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    lines = [f"# Findings — {proj['title']}", "", f"Total: {len(findings)}"]
    if counts:
        by_severity = ", ".join(
            f"{_SEVERITY_LABELS[severity]} {counts[severity]}"
            for severity in FINDING_SEVERITIES
            if severity in counts
        )
        lines.append(f"By severity: {by_severity}")
    if not findings:
        lines.extend(["", "No findings recorded."])
        return "\n".join(lines) + "\n"
    for severity in FINDING_SEVERITIES:
        items = [item for item in findings if item.severity == severity]
        if not items:
            continue
        lines.extend(["", f"## {_SEVERITY_LABELS[severity]} ({len(items)})"])
        for item in items:
            step = item.step or "-"
            lines.append(f"- **{item.id}** [{item.kind}] (step {step}): {item.description}")
    return "\n".join(lines) + "\n"


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline", "findings"):
        raise HTTPException(400, "Supported formats: yaml, timeline, findings")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "findings":
            text = _export_findings(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")

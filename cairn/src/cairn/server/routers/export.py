from datetime import datetime

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from cairn.server.db import get_conn
from cairn.server.services import expire_decide_leases, expire_steps, get_project_or_404

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

    facts = rows("SELECT id, description, step_id, created_at FROM facts WHERE project_id = ? ORDER BY rowid")
    goals = rows("SELECT * FROM goals WHERE project_id = ? ORDER BY created_at, rowid")
    steps = rows("SELECT * FROM steps WHERE project_id = ? ORDER BY created_at, rowid")
    findings = rows("SELECT * FROM findings WHERE project_id = ? ORDER BY created_at, rowid")
    hints = rows("SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at")

    sources_by_step: dict[str, list[str]] = {}
    for step in steps:
        source_rows = conn.execute(
            "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
            (step["id"], project_id),
        ).fetchall()
        sources_by_step[step["id"]] = [r["fact_id"] for r in source_rows]

    return proj, facts, goals, steps, findings, hints, sources_by_step


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, goals, steps, findings, hints, sources_by_step = _load_project_data(conn, project_id)

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
        {"id": f["id"], "description": f["description"], "from_step": f["step_id"]} for f in facts
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
                "id": s["id"],
                "from": sources_by_step.get(s["id"], []),
                "goal": s["goal_id"],
                "kind": s["kind"],
                "description": s["description"],
                "rationale": s["rationale"],
                "priority": s["priority"],
                "status": s["status"],
                "worker": s["worker"],
                "result": s["result_fact_id"],
                "note": s["note"],
            }
            for s in steps
        ]
    if findings:
        data["findings"] = [
            {
                "id": d["id"],
                "kind": d["kind"],
                "description": d["description"],
                "step": d["step_id"],
                "created_at": format_export_timestamp(d["created_at"]),
            }
            for d in findings
        ]

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, goals, steps, findings, hints, sources_by_step = _load_project_data(conn, project_id)

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
            f"[{ts}] FINDING {d['id']} ({d['kind']}) by {d['creator']}\n"
            f"  step: {d['step_id'] or '-'}\n  {d['description']}"
        )
        events.append((d["created_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "Supported formats: yaml, timeline")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")

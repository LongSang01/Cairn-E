from datetime import datetime

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from cairn.server.db import get_conn
from cairn.server.models import FINDING_SEVERITIES, TERMINAL_STEP_STATUSES
from cairn.server.services import (
    build_documents,
    build_findings,
    doc_ids_by_owner,
    expire_decide_leases,
    expire_steps,
    get_exchange,
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
    # An export is the audit trail: the authorization boundary, the human's
    # in-flight instructions and the planner's replies must survive a replay.
    constraints = rows(
        "SELECT kind, origin, text, created_at FROM constraints"
        " WHERE project_id = ? ORDER BY rowid"
    )
    steers = rows(
        "SELECT id, text, creator, created_at, answer, answered_at FROM steers"
        " WHERE project_id = ? ORDER BY rowid"
    )
    scope = rows(
        "SELECT kind, value, source, created_at FROM project_scope"
        " WHERE project_id = ? ORDER BY id"
    )

    sources_by_step: dict[str, list[str]] = {}
    for step in steps:
        source_rows = conn.execute(
            "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
            (step["id"], project_id),
        ).fetchall()
        sources_by_step[step["id"]] = [r["fact_id"] for r in source_rows]

    return (
        proj,
        facts,
        goals,
        steps,
        findings,
        hints,
        sources_by_step,
        constraints,
        steers,
        scope,
    )


def _export_yaml(conn, project_id: str) -> str:
    (
        proj,
        facts,
        goals,
        steps,
        findings,
        hints,
        sources_by_step,
        constraints,
        steers,
        scope,
    ) = _load_project_data(conn, project_id)
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
    if constraints:
        data["constraints"] = [
            {
                "kind": c["kind"],
                "origin": c["origin"],
                "text": c["text"],
                "created_at": format_export_timestamp(c["created_at"]),
            }
            for c in constraints
        ]
    if steers:
        data["steers"] = [
            {
                "id": s["id"],
                "text": s["text"],
                "creator": s["creator"],
                "created_at": format_export_timestamp(s["created_at"]),
                "answer": s["answer"],
                "answered_at": format_export_timestamp(s["answered_at"]),
            }
            for s in steers
        ]
    if scope:
        data["scope"] = [
            {
                "kind": e["kind"],
                "value": e["value"],
                "source": e["source"],
                "created_at": format_export_timestamp(e["created_at"]),
            }
            for e in scope
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
    (
        proj,
        facts,
        goals,
        steps,
        findings,
        hints,
        sources_by_step,
        constraints,
        steers,
        scope,
    ) = _load_project_data(conn, project_id)

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

    for c in constraints:
        ts = format_export_timestamp(c["created_at"]) or ""
        label = "REQUIRED" if c["kind"] == "allow" else "FORBIDDEN"
        block = f"[{ts}] CONSTRAINT {label} ({c['origin']})\n  {c['text']}"
        events.append((c["created_at"] or "", order, block))
        order += 1

    for s in steers:
        ts = format_export_timestamp(s["created_at"]) or ""
        block = f"[{ts}] STEER by {s['creator']}\n  {s['text']}"
        if s["answer"]:
            answer_ts = format_export_timestamp(s["answered_at"]) or ""
            block += f"\n[{answer_ts}] STEER ANSWER {s['id']}\n  {s['answer']}"
        events.append((s["created_at"] or "", order, block))
        order += 1

    for e in scope:
        ts = format_export_timestamp(e["created_at"]) or ""
        block = f"[{ts}] SCOPE ENTRY {e['kind']}={e['value']} ({e['source']})"
        events.append((e["created_at"] or "", order, block))
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

        if s["status"] not in TERMINAL_STEP_STATUSES:
            continue

        ts = format_export_timestamp(s["concluded_at"]) or ""
        actor = s["worker"] or s["creator"]
        if s["kind"] == "completion":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {s['id']} from {from_str}\n  {s['description']}"
        elif s["status"] == "cancelled":
            block = f"[{ts}] STEP CANCELLED {s['id']} by {actor}\n  {s['note'] or ''}"
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


def _code_block(text: str, language: str = "") -> list[str]:
    return [f"```{language}", text.rstrip() or "(empty)", "```"]


def _document_block(conn, project_id: str, document_id: str) -> list[str]:
    row = conn.execute(
        "SELECT title, content FROM documents WHERE project_id = ? AND id = ?",
        (project_id, document_id),
    ).fetchone()
    if row is None:
        return [f"_文档 {document_id} 已不存在_"]
    return [f"**文档 {document_id} · {row['title']}**", "", *_code_block(row["content"], "text")]


def _exchange_block(conn, project_id: str, ref: str) -> list[str]:
    parts = ref.split(":")
    if len(parts) != 3:
        return [f"_证据 {ref} 格式无效_"]
    session_id, seq_text, idx_text = parts
    try:
        row = get_exchange(conn, project_id, session_id, int(seq_text), int(idx_text))
    except ValueError:
        row = None
    if row is None:
        return [f"_证据 {ref} 已不存在_"]
    lines = [f"**报文 {ref}**（session {session_id} · seq {seq_text}）", ""]
    lines.extend(["请求：", *_code_block(row["request"], "http"), "", "响应："])
    lines.extend(_code_block(row["response"], "http"))
    return lines


def _export_report(conn, project_id: str) -> str:
    """A paste-ready SRC-style report: one section per finding, proof inlined."""
    proj = get_project_or_404(conn, project_id)
    findings = build_findings(conn, project_id)
    step_titles = {
        row["id"]: row["description"]
        for row in conn.execute(
            "SELECT id, description FROM steps WHERE project_id = ?", (project_id,)
        ).fetchall()
    }

    confirmed = [item for item in findings if item.status == "confirmed"]
    tentative = [item for item in findings if item.status != "confirmed"]

    lines = [f"# {proj['title']} — 漏洞报告", ""]
    lines.append(f"- 项目 ID：`{project_id}`")
    lines.append(f"- 生成时间：{format_export_timestamp(datetime.now().isoformat())}")
    lines.append(f"- 结论：{len(findings)} 条（confirmed {len(confirmed)} / tentative {len(tentative)}）")

    lines.extend(["", "## 严重度分布", ""])
    if findings:
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        for severity in FINDING_SEVERITIES:
            if severity in counts:
                lines.append(f"- {_SEVERITY_LABELS[severity]}：{counts[severity]}")
    else:
        lines.append("- 无")

    if tentative:
        lines.extend(
            [
                "",
                "> 标记为 tentative 的条目缺少可复现证据，报告末尾单独列出，未计入已确认结论。",
            ]
        )

    for item in confirmed:
        lines.extend(["", "---", "", f"## {_SEVERITY_LABELS[item.severity]} · {item.id}（{item.kind}）", ""])
        lines.extend([item.description, ""])
        if item.step:
            step_text = step_titles.get(item.step) or "（步骤已不存在）"
            lines.append(f"- 关联步骤：`{item.step}` — {step_text}")
        lines.append(f"- 状态：confirmed（证据 {len(item.evidence)} 条）")
        lines.extend(["", "### 复现", ""])
        if item.step and step_titles.get(item.step):
            lines.append(f"1. 执行步骤 `{item.step}`：{step_titles[item.step]}")
        lines.append("2. 按下列请求重放并比对响应")
        for ref in item.evidence:
            lines.append("")
            if ref.kind == "exchange":
                lines.extend(_exchange_block(conn, project_id, ref.ref))
            elif ref.kind == "document":
                lines.extend(_document_block(conn, project_id, ref.ref))
            else:
                lines.append(f"- 原始会话消息 seq {ref.ref}")

    if tentative:
        lines.extend(["", "---", "", "## 待验证条目（tentative）", ""])
        for item in tentative:
            lines.append(f"- {_SEVERITY_LABELS[item.severity]} `{item.id}`（{item.kind}）：{item.description}")

    lines.extend(["", "---", "", "## 证据索引", ""])
    for item in findings:
        for ref in item.evidence:
            lines.append(f"- `{item.id}` → {ref.kind}:{ref.ref}（digest {ref.digest or '-'}）")
    if not any(item.evidence for item in findings):
        lines.append("- 无")

    return "\n".join(lines).rstrip() + "\n"


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline", "findings", "report"):
        raise HTTPException(400, "Supported formats: yaml, timeline, findings, report")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "findings":
            text = _export_findings(conn, project_id)
        elif format == "report":
            return Response(content=_export_report(conn, project_id), media_type="text/markdown")
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")

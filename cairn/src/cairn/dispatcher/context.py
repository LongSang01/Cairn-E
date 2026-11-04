"""Explicit context/token budgeting for the graph snapshot handed to a worker.

Decide and Execute both read the whole FGS graph. On a long engagement that graph grows
past the model window, so the snapshot is rebuilt as a layered view instead of being
pasted verbatim:

* goals are always complete (they are small and they hold the "why"),
* the most recent ``recent_items`` facts and the open steps keep their original text,
* older facts are folded into a rolling summary that is persisted across runs, and all
  of their ids stay referenceable through ``older_fact_ids``,
* closed steps and findings degrade last, and a hard cap trims the tail if even the
  compact form does not fit.

The budget itself is explicit::

    input_budget = context_window - max_output_tokens - safety_margin
    safety_margin = clamp(context_window * safety_ratio, safety_min, safety_max)

No silent truncation happens without leaving a note in the YAML, so the model can tell
that history was summarized and how to reference it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from cairn.dispatcher.config import ContextBudgetConfig, WorkerConfig

# CJK characters cost roughly one token each; latin text is far cheaper per character.
_CJK = re.compile(r"[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af\uff00-\uffef]")
_CHARS_PER_TOKEN = 4
_MAX_TRUNCATE_STEPS = 24

HINT_LIMIT = 20
OLDER_FACT_ID_LIMIT = 400
DEFAULT_SUMMARIZE_PROMPT = """You compress the history of a penetration-testing graph.

Facts are confirmed, append-only observations. You will receive the facts that are about to
leave the recent window together with the previous summary. Merge them into one updated
summary that keeps every still-useful conclusion: hosts, credentials, services, versions,
decided dead ends, and anything a later step would want to know.

Rules:
- Output plain text, at most {max_bullets} bullet lines, each starting with "- ".
- Keep fact ids in parentheses when a conclusion came from one, e.g. "- db reachable (f012)".
- Drop chatter, commands and raw tool output; keep conclusions and constraints.
- Never invent facts that are not in the input.

Previous summary:
{previous_summary}

New facts to fold in:
{facts}
"""


@dataclass(frozen=True)
class SnapshotBudget:
    """Resolved budget for one phase, in tokens."""

    context_window: int
    max_output_tokens: int
    safety_tokens: int
    overhead_tokens: int
    snapshot_tokens: int

    @property
    def input_budget(self) -> int:
        return max(1, self.context_window - self.max_output_tokens - self.safety_tokens)


@dataclass(slots=True)
class GraphView:
    """The (possibly compacted) snapshot plus what happened to it."""

    yaml: str
    tokens: int
    budget_tokens: int
    needs_summary: bool = False
    old_facts: list[dict[str, Any]] = field(default_factory=list)
    summary_facts: int = 0
    truncated: bool = False
    compacted: bool = False


def estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate: 1 per CJK char, 1 per 4 latin chars."""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = max(0, len(text) - cjk)
    return cjk + math.ceil(other / _CHARS_PER_TOKEN)


def resolve_context_window(config: ContextBudgetConfig, worker: WorkerConfig) -> int:
    """Per-worker override first, then the configured default."""
    for key in ("CONTEXT_WINDOW", "MODEL_CONTEXT_WINDOW", "PI_MODEL_CONTEXT_WINDOW"):
        raw = worker.env.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            continue
        if value > 0:
            return value
    return config.context_window


def resolve_snapshot_budget(
    config: ContextBudgetConfig,
    worker: WorkerConfig,
    *,
    overhead_tokens: int = 0,
) -> SnapshotBudget:
    window = resolve_context_window(config, worker)
    safety = round(window * config.safety_ratio)
    safety = max(config.safety_min_tokens, min(config.safety_max_tokens, safety))
    max_output = min(config.max_output_tokens, max(1, window // 2))
    input_budget = max(1, window - max_output - safety)
    snapshot_tokens = max(config.min_snapshot_tokens, input_budget - max(0, overhead_tokens))
    return SnapshotBudget(
        context_window=window,
        max_output_tokens=max_output,
        safety_tokens=safety,
        overhead_tokens=max(0, overhead_tokens),
        snapshot_tokens=snapshot_tokens,
    )


def build_graph_view(
    graph_yaml: str,
    *,
    budget: SnapshotBudget,
    recent_items: int,
    summary: str = "",
    summary_facts: int = 0,
    phase: str = "decide",
) -> GraphView:
    """Return a snapshot that fits ``budget.snapshot_tokens`` if at all possible."""
    budget_tokens = budget.snapshot_tokens
    data = _load_yaml(graph_yaml)
    if data is None:
        return GraphView(
            yaml=graph_yaml,
            tokens=estimate_tokens(graph_yaml),
            budget_tokens=budget_tokens,
        )

    full_tokens = estimate_tokens(graph_yaml)
    if full_tokens <= budget_tokens:
        return GraphView(yaml=graph_yaml, tokens=full_tokens, budget_tokens=budget_tokens)

    facts = _dicts(data.get("facts"))
    old_facts = facts[:-recent_items] if recent_items < len(facts) else []
    needs_summary = bool(old_facts) and summary_facts < len(old_facts)

    for level in (1, 2, 3):
        view = _compose(
            data,
            budget=budget,
            recent_items=recent_items,
            summary=summary,
            summary_facts=summary_facts,
            level=level,
            phase=phase,
        )
        text = _dump(view)
        tokens = estimate_tokens(text)
        if tokens <= budget_tokens:
            return GraphView(
                yaml=text,
                tokens=tokens,
                budget_tokens=budget_tokens,
                needs_summary=needs_summary,
                old_facts=old_facts,
                summary_facts=summary_facts,
                compacted=True,
            )

    view = _compose(
        data,
        budget=budget,
        recent_items=recent_items,
        summary=summary,
        summary_facts=summary_facts,
        level=3,
        phase=phase,
    )
    text = _hard_cut(_dump(view), budget_tokens)
    return GraphView(
        yaml=text,
        tokens=estimate_tokens(text),
        budget_tokens=budget_tokens,
        needs_summary=needs_summary,
        old_facts=old_facts,
        summary_facts=summary_facts,
        compacted=True,
        truncated=True,
    )


def prepare_snapshot(
    graph_yaml: str,
    *,
    config: ContextBudgetConfig,
    worker: WorkerConfig,
    overhead_tokens: int,
    phase: str,
    summary: str = "",
    summary_facts: int = 0,
) -> GraphView:
    """Resolve the budget for this worker and return the snapshot view to write."""
    if not config.enabled:
        return GraphView(
            yaml=graph_yaml,
            tokens=estimate_tokens(graph_yaml),
            budget_tokens=estimate_tokens(graph_yaml),
        )
    budget = resolve_snapshot_budget(config, worker, overhead_tokens=overhead_tokens)
    return build_graph_view(
        graph_yaml,
        budget=budget,
        recent_items=config.recent_items,
        summary=summary,
        summary_facts=summary_facts,
        phase=phase,
    )


def summarize_prompt(previous_summary: str, facts: list[dict[str, Any]], *, max_bullets: int = 40) -> str:
    lines = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        description = str(fact.get("description") or "").strip()
        if not description:
            continue
        lines.append(f"- {fact.get('id')}: {description}")
    return DEFAULT_SUMMARIZE_PROMPT.format(
        max_bullets=max_bullets,
        previous_summary=previous_summary.strip() or "(none)",
        facts="\n".join(lines) or "(none)",
    )


# --------------------------------------------------------------------------- internals


def _load_yaml(graph_yaml: str) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(graph_yaml)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dump(data: dict[str, Any]) -> str:
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _cut(text: Any, limit: int) -> str:
    value = "" if text is None else str(text).strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _compose(
    data: dict[str, Any],
    *,
    budget: SnapshotBudget,
    recent_items: int,
    summary: str,
    summary_facts: int,
    level: int,
    phase: str,
) -> dict[str, Any]:
    facts = _dicts(data.get("facts"))
    steps = _dicts(data.get("steps"))
    findings = _dicts(data.get("findings"))
    hints = _dicts(data.get("hints"))

    old_facts = facts[:-recent_items] if recent_items < len(facts) else []
    recent_facts = facts[-recent_items:]
    open_steps = [step for step in steps if step.get("status") in ("pending", "running")]
    closed_steps = [step for step in steps if step.get("status") not in ("pending", "running")]

    view: dict[str, Any] = {"project": data.get("project", {})}
    note = (
        f"History is compacted to fit {budget.context_window} tokens of context. "
        f"Goals and the last {recent_items} facts/steps are verbatim."
    )
    if old_facts:
        note += (
            f" {len(old_facts)} older facts are summarized in confirmed_facts_summary; "
            "their ids stay valid and are listed in older_fact_ids."
        )
    if level >= 3 or budget.snapshot_tokens <= 0:
        note += " Findings and old step details were dropped; ask for a step to inspect more."
    view["context"] = {
        "phase": phase,
        "note": note,
        "input_budget_tokens": budget.input_budget,
        "snapshot_budget_tokens": budget.snapshot_tokens,
        "recent_items": recent_items,
        "older_fact_count": len(old_facts),
        "summarized_fact_count": summary_facts,
    }

    if hints:
        hint_limit = 300 if level >= 2 else 800
        view["hints"] = [
            {
                "content": _cut(hint.get("content"), hint_limit),
                "creator": hint.get("creator"),
                "created_at": hint.get("created_at"),
            }
            for hint in hints[-HINT_LIMIT:]
        ]

    view["goals"] = data.get("goals") or []

    fact_limit = 300 if level >= 2 else 2000
    view["facts"] = [
        {
            "id": fact.get("id"),
            "description": _cut(fact.get("description"), fact_limit),
            "from_step": fact.get("from_step"),
        }
        for fact in recent_facts
    ]

    if old_facts:
        view["older_fact_ids"] = [fact.get("id") for fact in old_facts[-OLDER_FACT_ID_LIMIT:]]
        if summary:
            view["confirmed_facts_summary"] = _cut(summary, 2400 if level < 3 else 1200)
        else:
            # Deterministic fallback: keep one short line per old fact, newest first.
            lines = [
                f"- {fact.get('id')}: {_cut(fact.get('description'), 160)}"
                for fact in old_facts[-60:]
            ]
            view["confirmed_facts_summary"] = "\n".join(lines)

    step_entries = []
    for step in open_steps:
        step_entries.append(
            {
                "id": step.get("id"),
                "from": step.get("from") or [],
                "goal": step.get("goal"),
                "kind": step.get("kind"),
                "description": _cut(step.get("description"), 400 if level < 3 else 240),
                "rationale": step.get("rationale") if level < 2 else None,
                "priority": step.get("priority"),
                "status": step.get("status"),
                "worker": step.get("worker"),
            }
        )
    for step in closed_steps[-recent_items:]:
        entry = {
            "id": step.get("id"),
            "goal": step.get("goal"),
            "description": _cut(step.get("description"), 240 if level < 2 else 120),
            "status": step.get("status"),
            "priority": step.get("priority"),
            "result": step.get("result"),
        }
        if level < 2:
            entry["from"] = step.get("from") or []
            entry["rationale"] = step.get("rationale")
        if step.get("note"):
            entry["note"] = _cut(step.get("note"), 160)
        step_entries.append(entry)
    if step_entries:
        view["steps"] = step_entries

    if findings and level < 3:
        view["findings"] = [
            {
                "id": finding.get("id"),
                "kind": finding.get("kind"),
                "description": _cut(finding.get("description"), 400 if level < 2 else 200),
                "step": finding.get("step"),
            }
            for finding in findings[-recent_items:]
        ]

    return view


def _hard_cut(text: str, budget_tokens: int) -> str:
    marker = "\n# ... [snapshot truncated to fit the context budget]\n"
    # The marker itself costs tokens, so cut the body to what is left after it.
    limit = max(1, budget_tokens - estimate_tokens(marker))
    for _ in range(_MAX_TRUNCATE_STEPS):
        if estimate_tokens(text) <= limit:
            break
        text = text[: max(1, int(len(text) * 0.8))]
    return text.rstrip() + marker

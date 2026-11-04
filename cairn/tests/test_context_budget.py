from __future__ import annotations

import yaml

from cairn.dispatcher.config import ContextBudgetConfig, WorkerConfig
from cairn.dispatcher.context import (
    SnapshotBudget,
    build_graph_view,
    estimate_tokens,
    prepare_snapshot,
    resolve_context_window,
    resolve_snapshot_budget,
    split_summary_sections,
    summarize_prompt,
)


def _worker(**env: str) -> WorkerConfig:
    return WorkerConfig.model_validate(
        {
            "name": "pi-main",
            "type": "pi",
            "task_types": ["decide"],
            "max_running": 1,
            "priority": 0,
            "env": env,
        }
    )


def _graph(*, facts: int = 60, steps: int = 10) -> str:
    data = {
        "project": {"title": "engagement", "status": "active"},
        "facts": [
            {"id": f"f{index:03d}", "description": f"fact {index} " + "x" * 300, "from_step": None}
            for index in range(1, facts + 1)
        ],
        "goals": [
            {"id": "g001", "parent": None, "description": "root goal", "status": "active"},
            {"id": "g002", "parent": "g001", "description": "sub goal", "status": "active"},
        ],
        "steps": [
            {
                "id": f"s{index:03d}",
                "from": ["f001"],
                "goal": "g001",
                "kind": "action",
                "description": f"step {index} " + "y" * 200,
                "rationale": "why " + "z" * 120,
                "priority": 1,
                "status": "done" if index < steps else "pending",
                "worker": "pi-main",
                "result": f"f{index:03d}",
                "note": "note " + "n" * 80,
            }
            for index in range(1, steps + 1)
        ],
        "findings": [
            {"id": f"d{index:03d}", "kind": "finding", "description": f"finding {index}", "step": "s001"}
            for index in range(1, 6)
        ],
    }
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


SMALL_WINDOW = ContextBudgetConfig(
    context_window=8_000,
    max_output_tokens=1_000,
    safety_ratio=0.05,
    safety_min_tokens=100,
    safety_max_tokens=400,
    recent_items=5,
    min_snapshot_tokens=300,
)


def test_token_estimate_counts_cjk_heavier_than_latin() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("端口开放") == 4
    assert estimate_tokens("端口 a") == 3


def test_budget_math_reserves_output_and_safety_margin() -> None:
    config = ContextBudgetConfig(
        context_window=10_000,
        max_output_tokens=1_000,
        safety_ratio=0.05,
        safety_min_tokens=100,
        safety_max_tokens=800,
    )
    budget = resolve_snapshot_budget(config, _worker(), overhead_tokens=2_000)

    assert budget.safety_tokens == 500  # 5% of the window, inside [100, 800]
    assert budget.input_budget == 8_500
    assert budget.snapshot_tokens == 6_500


def test_safety_margin_is_clamped_at_both_ends() -> None:
    floor = resolve_snapshot_budget(
        ContextBudgetConfig(context_window=10_000, safety_ratio=0.05, safety_min_tokens=2_000),
        _worker(),
    )
    assert floor.safety_tokens == 2_000

    ceiling = resolve_snapshot_budget(
        ContextBudgetConfig(
            context_window=1_000_000, safety_ratio=0.05, safety_min_tokens=100, safety_max_tokens=3_000
        ),
        _worker(),
    )
    assert ceiling.safety_tokens == 3_000


def test_worker_env_override_wins_over_default_window() -> None:
    config = ContextBudgetConfig(context_window=200_000)
    assert resolve_context_window(config, _worker(CONTEXT_WINDOW="32000")) == 32_000
    assert resolve_context_window(config, _worker(PI_MODEL_CONTEXT_WINDOW="64000")) == 64_000
    assert resolve_context_window(config, _worker()) == 200_000


def test_snapshot_that_fits_is_left_untouched() -> None:
    graph = _graph(facts=3, steps=1)
    budget = resolve_snapshot_budget(ContextBudgetConfig(context_window=200_000), _worker())

    view = build_graph_view(graph, budget=budget, recent_items=20)

    assert view.yaml == graph
    assert view.compacted is False
    assert view.needs_summary is False
    assert view.old_facts == []


def test_compacted_view_carries_titles_and_document_ids() -> None:
    data = {
        "project": {"title": "engagement", "status": "active"},
        "facts": [
            {"id": "f001", "title": "old fact", "description": "x" * 40_000, "from_step": None},
            {"id": "f002", "title": "recent title", "description": "recent body", "doc_ids": ["doc001"]},
            {"id": "f003", "description": "no title here", "from_step": None},
        ],
        "goals": [{"id": "g001", "parent": None, "description": "root", "status": "active"}],
        "steps": [
            {
                "id": "s001",
                "from": ["f001"],
                "goal": "g001",
                "kind": "action",
                "title": "probe redis",
                "description": "step one",
                "priority": 1,
                "status": "pending",
                "worker": "pi-main",
            },
            {
                "id": "s002",
                "from": ["f001"],
                "goal": "g001",
                "kind": "action",
                "description": "step two",
                "priority": 1,
                "status": "done",
                "result": "f002",
                "doc_ids": ["doc002"],
            },
        ],
    }
    graph = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    budget = resolve_snapshot_budget(SMALL_WINDOW, _worker())

    view = build_graph_view(graph, budget=budget, recent_items=2)
    snapshot = yaml.safe_load(view.yaml)

    assert view.compacted is True
    facts = {fact["id"]: fact for fact in snapshot["facts"]}
    assert facts["f002"]["title"] == "recent title"
    assert facts["f002"]["doc_ids"] == ["doc001"]
    # A fact without a title falls back to its description.
    assert facts["f003"]["title"] == "no title here"
    assert "doc_ids" not in facts["f003"]
    steps = {step["id"]: step for step in snapshot["steps"]}
    assert steps["s001"]["title"] == "probe redis"
    assert steps["s002"]["title"] == "step two"
    assert steps["s002"]["doc_ids"] == ["doc002"]


def test_oversized_snapshot_is_layered_not_truncated() -> None:
    graph = _graph(facts=80, steps=10)
    budget = resolve_snapshot_budget(SMALL_WINDOW, _worker())

    view = build_graph_view(graph, budget=budget, recent_items=5)

    data = yaml.safe_load(view.yaml)
    assert view.compacted is True
    assert view.truncated is False
    assert view.tokens <= budget.snapshot_tokens
    # Goals are always complete.
    assert [goal["id"] for goal in data["goals"]] == ["g001", "g002"]
    # Only the recent window keeps its original text.
    assert [fact["id"] for fact in data["facts"]] == ["f076", "f077", "f078", "f079", "f080"]
    # Older facts stay referenceable and are folded into a summary.
    assert data["older_fact_ids"][:3] == ["f001", "f002", "f003"]
    assert data["confirmed_facts_summary"].startswith("- f")
    assert data["context"]["older_fact_count"] == 75
    assert data["context"]["recent_items"] == 5
    # Open steps survive; closed ones are compacted.
    assert data["steps"][0]["id"] == "s010"
    assert view.needs_summary is True
    assert len(view.old_facts) == 75


def test_persisted_summary_is_reused() -> None:
    graph = _graph(facts=80, steps=10)
    budget = resolve_snapshot_budget(SMALL_WINDOW, _worker())

    view = build_graph_view(
        graph,
        budget=budget,
        recent_items=5,
        summary="- db reachable on 10.0.0.5 (f012)",
        summary_facts=75,
        step_summary="- sqlmap against /login found nothing (s003)",
        summary_steps=4,
    )

    data = yaml.safe_load(view.yaml)
    assert data["confirmed_facts_summary"] == "- db reachable on 10.0.0.5 (f012)"
    assert data["closed_steps_summary"] == "- sqlmap against /login found nothing (s003)"
    # Everything older is already summarized, so no extra model call is needed.
    assert view.needs_summary is False


def test_hard_cut_keeps_the_snapshot_inside_the_budget() -> None:
    budget = SnapshotBudget(
        context_window=1_000,
        max_output_tokens=100,
        safety_tokens=100,
        overhead_tokens=0,
        snapshot_tokens=150,
    )

    view = build_graph_view(_graph(facts=80, steps=10), budget=budget, recent_items=5)

    assert view.truncated is True
    # Per-chunk ceil() rounding can add a token or two on top of the exact sum.
    assert view.tokens <= budget.snapshot_tokens + 2
    assert "truncated to fit the context budget" in view.yaml


def test_disabled_budget_keeps_the_graph_verbatim() -> None:
    graph = _graph(facts=80)
    config = ContextBudgetConfig(enabled=False, context_window=1_000)

    view = prepare_snapshot(
        graph,
        config=config,
        worker=_worker(),
        overhead_tokens=0,
        phase="decide",
    )

    assert view.yaml == graph
    assert view.compacted is False


def test_summarize_prompt_carries_previous_summary_and_ids() -> None:
    prompt = summarize_prompt(
        "- old: ssh open (f001)",
        [{"id": "f002", "description": "db reachable"}, {"id": "f003", "description": "creds found"}],
    )

    assert "- old: ssh open (f001)" in prompt
    assert "- f002: db reachable" in prompt
    assert "- f003: creds found" in prompt
    assert "never invent" in prompt.lower() or "Never invent" in prompt


def test_old_steps_and_findings_carry_summaries_instead_of_being_dropped() -> None:
    graph = _graph(facts=5, steps=12)
    budget = SnapshotBudget(
        context_window=1_000,
        max_output_tokens=100,
        safety_tokens=100,
        overhead_tokens=0,
        snapshot_tokens=2_000,
    )

    view = build_graph_view(graph, budget=budget, recent_items=2)

    data = yaml.safe_load(view.yaml)
    # 11 closed steps, the last 2 stay verbatim, the rest become ids + a summary.
    assert len(view.old_steps) == 9
    assert data["older_step_ids"] == [f"s{index:03d}" for index in range(1, 10)]
    assert data["closed_steps_summary"].startswith("- s001:")
    # 5 findings, the last 2 stay verbatim, the rest become ids + a summary.
    assert len(view.old_findings) == 3
    assert data["older_finding_ids"] == ["d001", "d002", "d003"]
    assert data["findings_summary"].startswith("- d001:")


def test_step_and_finding_summaries_replace_the_deterministic_fallback() -> None:
    graph = _graph(facts=5, steps=12)
    budget = SnapshotBudget(
        context_window=1_000,
        max_output_tokens=100,
        safety_tokens=100,
        overhead_tokens=0,
        snapshot_tokens=2_000,
    )

    view = build_graph_view(
        graph,
        budget=budget,
        recent_items=2,
        summary="- db reachable (f002)",
        summary_facts=3,
        step_summary="- sqlmap against /login found nothing (s001)",
        summary_steps=9,
        finding_summary="- openssh 8.2 on 22/tcp (d001)",
        summary_findings=3,
    )

    data = yaml.safe_load(view.yaml)
    assert data["closed_steps_summary"] == "- sqlmap against /login found nothing (s001)"
    assert data["findings_summary"] == "- openssh 8.2 on 22/tcp (d001)"
    assert view.needs_summary is False


def test_split_summary_sections_accepts_headers_or_plain_text() -> None:
    sections = split_summary_sections(
        "FACTS:\n- db reachable (f001)\n\nSTEPS:\n- tried ssh (s001)\n\nFINDINGS:\n- openssh 8.2 (d001)"
    )

    assert sections == {
        "facts": "- db reachable (f001)",
        "steps": "- tried ssh (s001)",
        "findings": "- openssh 8.2 (d001)",
    }
    # A summary written before sections existed still counts as facts.
    assert split_summary_sections("- plain (f001)") == {
        "facts": "- plain (f001)",
        "steps": "",
        "findings": "",
    }
    assert split_summary_sections("FACTS:\n- only facts (f001)")["findings"] == ""


def test_summarize_prompt_covers_steps_and_findings() -> None:
    prompt = summarize_prompt(
        "- old: ssh open (f001)",
        [{"id": "f002", "description": "db reachable"}],
        steps=[
            {"id": "s003", "description": "sqlmap /login", "result": "no injectable params", "status": "done"}
        ],
        findings=[{"id": "d004", "description": "openssh 8.2"}],
    )

    assert "- s003: sqlmap /login -> no injectable params [done]" in prompt
    assert "- d004: openssh 8.2" in prompt
    assert "STEPS:" in prompt and "FINDINGS:" in prompt

"""Prompt rendering and placeholder integrity.

Pins three guarantees:

* the operating-discipline block is code-owned and injected into Execute, and its
  ``{opsec}`` placeholder is enforced by config validation (a custom prompt group
  cannot silently drop it);
* every default prompt renders without a leftover ``{key}`` placeholder, so a newly
  added placeholder cannot silently reach the model;
* the Execute template only references placeholders the task actually injects.
"""

from __future__ import annotations

import re

from cairn.dispatcher.config import DEFAULT_PROMPT_REQUIRED_TOKENS
from cairn.dispatcher.constraints import OPSEC_BLOCK, opsec_block, render_scope_block
from cairn.dispatcher.prompting import load_prompt, render_prompt

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

_DEFAULT_PROMPTS = ("decide.md", "execute.md", "execute_conclude.md", "question.md")

# The keys run_execute_task puts into the execute prompt (see tasks/execute.py).
_EXECUTE_BLOCKS_KEYS = frozenset(
    {"step_id", "step_description", "opsec", "constraints", "steers", "scope", "graph_yaml"}
)


def _placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def test_opsec_block_is_code_owned_and_enforced() -> None:
    assert opsec_block() == OPSEC_BLOCK
    for phrase in (
        "Reuse what the graph",
        "rate limit",
        "verify=False",
        "Verify against yourself",
        "Record only increments",
    ):
        assert phrase in OPSEC_BLOCK

    # The placeholder is a required token, so validate_prompt_resources rejects a
    # custom group whose execute.md dropped it.
    assert "{opsec}" in DEFAULT_PROMPT_REQUIRED_TOKENS["execute.md"]
    # Decide never touches the world and conclude only reports, so neither carries it.
    assert "{opsec}" not in DEFAULT_PROMPT_REQUIRED_TOKENS["decide.md"]
    assert "{opsec}" not in DEFAULT_PROMPT_REQUIRED_TOKENS["execute_conclude.md"]


def test_default_prompts_render_without_leftover_placeholders() -> None:
    for name in _DEFAULT_PROMPTS:
        template = load_prompt("default", name)
        rendered = render_prompt(template, {key: f"<{key}>" for key in _placeholders(template)})
        assert not _PLACEHOLDER.search(rendered), f"{name} left a placeholder: {rendered!r}"


def test_execute_template_references_only_injected_placeholders() -> None:
    template = load_prompt("default", "execute.md")
    assert _placeholders(template) <= _EXECUTE_BLOCKS_KEYS, (
        f"execute.md references placeholders the task does not inject: "
        f"{_placeholders(template) - _EXECUTE_BLOCKS_KEYS}"
    )
    rendered = render_prompt(template, {key: f"<{key}>" for key in _EXECUTE_BLOCKS_KEYS})
    assert not _PLACEHOLDER.search(rendered)


def test_execute_renders_opsec_into_the_prompt() -> None:
    template = load_prompt("default", "execute.md")
    rendered = render_prompt(
        template,
        {
            "opsec": opsec_block(),
            "step_id": "s001",
            "step_description": "probe",
            "constraints": "c",
            "steers": "s",
            "scope": "sc",
            "graph_yaml": "g",
        },
    )
    assert "## Operating discipline" in rendered
    assert "Verify against yourself" in rendered


def test_render_scope_block_empty_vs_whitelist() -> None:
    assert "未设范围白名单" in render_scope_block([])
    block = render_scope_block([{"kind": "host", "value": "api.example.com"}])
    assert "仅允许以下目标" in block
    assert "api.example.com" in block

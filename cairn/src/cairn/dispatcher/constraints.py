"""Render the constraint block that opens every Decide / Execute prompt.

Modelled on ARTEX's ``constraintBlock``: the rules are read fresh from the
project on each run and placed ahead of the goal, so a rule added while the
project runs takes effect on the next Decide, without interrupting the worker
that is already executing (its prompt is built; killing it to inject policy
would throw away the work it is about to submit).

Constraints are prompt-level authorization only. The tool guard deliberately
does not read them: its rules stay platform-owned, so neither a constraint nor
a prompt injection inside one can widen what a command may do.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Bounds mirror the hint budget in dispatcher/context.py: a constraint is meant
# to be one sentence, and an unbounded block would crowd out the graph.
MAX_CONSTRAINTS = 20
MAX_CONSTRAINT_CHARS = 300
MAX_SCOPE_ENTRIES = 20
# Only the tail of the conversation is useful: older steers are already reflected
# in the graph (constraints, notes, cancelled steps).
MAX_STEERS = 8

HEADER = "【操作约束（最高优先级，先于目标与计划）】"
EMPTY = "- 无附加约束；仍以任务目标为限，不自行扩大范围。"
TAIL = (
    "约束之外的新目标、新主机、新端口、新账号不算授权：遇到时记为 out-of-scope 事实并跳过，"
    "不要为了推进目标而自行扩大范围。被禁止的动作即使服务于目标也不做。"
)


def _one_line(text: str, limit: int = MAX_CONSTRAINT_CHARS) -> str:
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _rule(entry: Any) -> tuple[str, str]:
    """Return ``(kind, text)`` for a contract model, a dict or a raw string.

    A bare string (an older export, or a caller that only has text) is treated as
    a prohibition: guessing "allow" would silently widen the task.
    """
    if isinstance(entry, str):
        return "deny", entry
    if isinstance(entry, dict):
        kind = entry.get("kind")
        text = entry.get("text") or entry.get("content") or ""
    else:
        kind = getattr(entry, "kind", None)
        text = getattr(entry, "text", "") or getattr(entry, "content", "")
    return ("allow" if kind == "allow" else "deny"), str(text)


SCOPE_HEADER = "【测试范围（平台硬闸门）】"


def render_scope_block(entries: Iterable[Any] | None) -> str:
    """Show the planner which hosts a command may legally name.

    Empty scope means the gate is off; a single entry turns it on, so the prompt
    has to say so explicitly or the model would keep probing discovered hosts.
    """
    values: list[str] = []
    for entry in entries or ():
        if isinstance(entry, dict):
            kind, value = entry.get("kind", "host"), entry.get("value", "")
        else:
            kind, value = getattr(entry, "kind", "host"), getattr(entry, "value", "")
        if str(value).strip():
            values.append(f"{kind}={_one_line(str(value))}")
    values = values[:MAX_SCOPE_ENTRIES]
    if not values:
        return "（未设范围白名单：主机范围由目标与约束界定，平台不拦截命令中的主机）"
    return (
        SCOPE_HEADER
        + "\n仅允许以下目标：\n"
        + "\n".join(f"- {value}" for value in values)
        + "\n命令中出现范围外的主机会被平台拦截。不要安排针对范围外主机的步骤，也不要试图绕过拦截。"
    )


STEER_HEADER = "【运行中用户指令（优先于当前计划）】"


def render_steers_block(steers: Iterable[Any] | None) -> str:
    """Messages sent while the project runs, newest last.

    Append-only by design: a worker that starts after the message was delivered
    to Decide must still see it, otherwise it would repeat the very action the
    human just called out.
    """
    messages = []
    for entry in steers or ():
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict):
            text = str(entry.get("text") or "")
        else:
            text = str(getattr(entry, "text", "") or "")
        text = _one_line(text)
        if text:
            messages.append(text)
    messages = messages[-MAX_STEERS:]
    if not messages:
        return "（无运行中指令）"
    lines = [f"{index}. {text}" for index, text in enumerate(messages, 1)]
    lines.append("最后一条是本轮最新指令；先落实它，再安排后续步骤。")
    return STEER_HEADER + "\n" + "\n".join(lines)


def render_constraints_block(constraints: Iterable[Any] | None) -> str:
    """Compose the block. Always returns a block so the prompt shape is stable."""
    rules = [_rule(entry) for entry in (constraints or ())]
    rules = [(kind, text) for kind, text in rules if text.strip()][:MAX_CONSTRAINTS]
    if not rules:
        return f"{HEADER}\n{EMPTY}"

    allow = [f"- 必须：{_one_line(text)}" for kind, text in rules if kind == "allow"]
    deny = [f"- 禁止：{_one_line(text)}" for kind, text in rules if kind == "deny"]
    body = "\n".join([*deny, *allow])
    return f"{HEADER}\n{body}\n{TAIL}"


def policy_blocks(project: Any) -> dict[str, str]:
    """The three authorization blocks shared by every Decide / Execute prompt.

    Keeping them in one place means a new block cannot be added to Decide and
    silently forgotten in Execute (or in its conclude fallback).
    """
    return {
        "constraints": render_constraints_block(getattr(project, "constraints", None)),
        "steers": render_steers_block(getattr(project, "steers", None)),
        "scope": render_scope_block(getattr(project, "scope", None)),
    }


# Operating discipline is static, platform-owned guidance (how to probe within the
# authorization boundary) rather than runtime data read from the project. It lives in
# code — not in the editable prompt file — so a custom prompt group cannot silently
# drop it: ``{opsec}`` is enforced as a required placeholder by
# ``validate_prompt_resources``. Modeled on ARTEX's ``opsecPromptBlock``.
OPSEC_BLOCK = """## Operating discipline

- Reuse what the graph already knows before probing again: if a fact or a recorded
  exchange already answers the question, do not re-run the same request, scan or command.
- Do not rotate user agents, batch requests, port-sweep, directory-brute-force or run
  large-scale scans. Stop this direction the moment you hit a rate limit, a CAPTCHA or
  a WAF.
- Do not disable TLS verification (`-k`, `--insecure`, `verify=False`) or forge an
  identity, device or browser fingerprint just to reach the target.
- A first failure (a filtered payload, a 404, no echo) does not prove the path is dead:
  try different encodings, methods, parameters or paths until the reasonable options for
  this direction are exhausted before you conclude.
- Verify against yourself: before you mark a finding `confirmed`, re-trigger it once
  through a different path or an independent command. A second, independent observation
  confirms it; repeating your original evidence does not.
- Record only increments: submit facts you newly confirmed. A fact that only restates
  what is already on the graph is not a new result.
"""


def opsec_block() -> str:
    """Return the static operating-discipline block for the Execute prompt.

    Execute is the only phase that touches the world, so the discipline is injected
    there (and never into Decide, which only reads and evolves the graph).
    """
    return OPSEC_BLOCK

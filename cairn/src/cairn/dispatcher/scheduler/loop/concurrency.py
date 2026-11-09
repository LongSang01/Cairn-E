"""When two steps may run at the same time.

The default is parallel. Naming the same asset is not a conflict by itself: probing
two ports of one host, or reading two pages of one site, does not interfere. The
planner marks a step ``exclusive`` when it really does compete — brute force against
one login, a write to one record, a chain that has to keep one shell.

The keyword net is the backstop for a planner that forgets to mark one. It follows the
same division as the command guard: the model judges the grey area, a deterministic
rule catches the cases that must never overlap, and the rule only covers what it can
recognise without guessing.
"""

from __future__ import annotations

import re

from cairn.contracts.models import Step

COMPETING_PATTERNS = re.compile(
    r"爆破|密码喷洒|撞库|批量登录"
    r"|brute[\s-]*force|password\s*spray|credential\s*stuffing|\bflood\b",
    re.IGNORECASE,
)


def effective_exclusive(step: Step) -> bool:
    """True when the step holds its asset: stated by the planner, or forced by the net."""
    if step.exclusive:
        return True
    return bool(COMPETING_PATTERNS.search(step.description or ""))


def conflict_reason(step: Step, running: list[Step]) -> str | None:
    """Why ``step`` must wait, or ``None`` when it may start next to ``running``.

    The reason is only used for the skip log; both cases mean the same thing: one of
    the two runs holds the asset.
    """
    if not step.asset:
        return None
    for other in running:
        if other.asset != step.asset:
            continue
        if effective_exclusive(step):
            return "asset_exclusive"
        if effective_exclusive(other):
            return "asset_busy"
    return None

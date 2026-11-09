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
    r"爆破|暴力破解|猜解|密码喷洒|撞库|批量登录"
    r"|brute[\s-]*force|password\s*spray|credential\s*stuffing|\bflood\b",
    re.IGNORECASE,
)


def effective_exclusive(step: Step) -> bool:
    """True when the step holds its asset: stated by the planner, or forced by the net.

    Both fields are searched: a planner that writes "hydra 爆破" as the title and the
    mechanics as the description is still describing a competing run.
    """
    if step.exclusive:
        return True
    text = f"{step.title or ''}\n{step.description or ''}"
    return COMPETING_PATTERNS.search(text) is not None


def conflict_reason(step: Step, running: list[Step]) -> str | None:
    """Why ``step`` must wait, or ``None`` when it may start next to ``running``.

    Two declared assets are compared directly: same asset and either side exclusive
    means the pair queues, different assets never wait. A missing asset cannot be
    compared, so a competing run without one is treated as possibly sharing every
    sibling's target — the planner is expected to give `exclusive` a real asset, and
    being serialized is the safe answer when it did not.

    The reason is only used for the skip log; both cases mean the same thing: one of
    the two runs holds the asset.
    """
    claim = effective_exclusive(step)
    if not step.asset and not claim:
        return None
    for other in running:
        other_claim = effective_exclusive(other)
        if step.asset and other.asset:
            if step.asset != other.asset:
                continue
            if not claim and not other_claim:
                continue
        elif not (claim or other_claim):
            continue
        return "asset_exclusive" if claim else "asset_busy"
    return None

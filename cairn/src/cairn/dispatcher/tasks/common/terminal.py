"""Why a run ended, as a small closed vocabulary instead of free text.

ARTEX keeps a `TerminalReason` table and maps each entry to a different next step:
retry, fail over to another provider, or stop. Cairn is the only component that sees
a timeout, a crashed CLI or a provider refusal, so it has to say *which* of those
happened — "failed" alone makes the scheduler blame the step for the worker's problem.

Reasons are carried as the prefix of ``RunningTask.failure_reason`` (``"quota: ..."``).
That keeps the wire format a plain string, and lets the reaper classify without the
task modules having to return a struct through a thread pool.
"""

from __future__ import annotations

BUDGET_EXHAUSTED = "budget_exhausted"
PARSE_ERROR = "parse"
HEARTBEAT_LOST = "heartbeat_lost"
CRASHED = "crashed"
NOOP = "noop"
QUOTA = "quota"
AUTH = "auth"

# Task outcomes (cancelled / unhealthy / rejected) stay in the task's return value;
# this vocabulary only names *why* an attempt failed, so it holds exactly the
# prefixes Execute can produce.
KNOWN = frozenset({BUDGET_EXHAUSTED, PARSE_ERROR, HEARTBEAT_LOST, CRASHED, NOOP, QUOTA, AUTH})

# A step failed, but not because of the step: the provider refused the request or the
# credentials are dead. Re-running the same step on the same worker only burns another
# run, so the scheduler cools that worker down and treats this as ``rejected``.
WORKER_FAULTS = frozenset({QUOTA, AUTH})

# Text a provider puts in its error body when the account cannot serve the request.
# Deliberately phrase-based: a bare 401/402/429 also shows up in the *target's*
# responses, which say nothing about the worker's own credentials.
QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "exceeded your current quota",
    "credit balance is too low",
    "insufficient balance",
    "余额不足",
    "额度不足",
    "配额不足",
)

AUTH_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "authentication_error",
    "api key not valid",
    "unauthorized: invalid token",
    "invalid bearer token",
)


def classify(reason: str | None) -> str:
    """The reason's closed-vocabulary prefix, or ``""`` when it is free text."""
    if not reason:
        return ""
    head = reason.split(":", 1)[0].strip().lower()
    return head if head in KNOWN else ""


def is_worker_fault(reason: str | None) -> bool:
    """True when the run failed on the platform side, not on the step's."""
    return classify(reason) in WORKER_FAULTS


def provider_fault(text: str) -> str | None:
    """Scan a worker's own output for a provider refusal (quota or credentials)."""
    lowered = (text or "").lower()
    for marker in QUOTA_MARKERS:
        if marker in lowered:
            return QUOTA
    for marker in AUTH_MARKERS:
        if marker in lowered:
            return AUTH
    return None


def report(reason: str, detail: str) -> str:
    """Build the ``failure_reason`` string: a known prefix plus a human detail."""
    head = reason if reason in KNOWN else CRASHED
    text = " ".join(detail.split())
    return f"{head}: {text}" if text else head

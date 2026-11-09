"""Aggregate measured token usage over a project's sessions."""

from __future__ import annotations

import sqlite3

from cairn.contracts.models import ProjectUsage, UsageBucket


def _accumulate_usage(bucket: UsageBucket, row: sqlite3.Row) -> None:
    bucket.sessions += 1
    bucket.input_tokens += row["input_tokens"]
    bucket.output_tokens += row["output_tokens"]
    bucket.cache_read_tokens += row["cache_read_tokens"]
    bucket.cache_write_tokens += row["cache_write_tokens"]
    bucket.total_tokens += row["total_tokens"]


def _settle_usage(bucket: UsageBucket) -> UsageBucket:
    """A client-supplied total can never make a bucket smaller than its parts."""
    measured = (
        bucket.input_tokens
        + bucket.output_tokens
        + bucket.cache_read_tokens
        + bucket.cache_write_tokens
    )
    bucket.total_tokens = max(bucket.total_tokens, measured)
    return bucket


BUDGET_BLOCK_PREFIX = "token budget exceeded"


def enforce_token_budget(conn: sqlite3.Connection, project_id: str) -> bool:
    """Stop a project that passed its token budget, and unblock it if raised.

    Runs after every usage write, so the status flips as soon as the last run
    reports its tokens instead of waiting for the next scheduler tick. The
    scheduler only dispatches `active` projects, so a blocked one simply stops.
    """
    row = conn.execute(
        "SELECT status, token_budget, blocked_reason FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None or not row["token_budget"]:
        return False

    total = build_project_usage(conn, project_id).total_tokens
    budget = int(row["token_budget"])
    blocked_by_budget = str(row["blocked_reason"] or "").startswith(BUDGET_BLOCK_PREFIX)
    if total >= budget and row["status"] == "active":
        conn.execute(
            "UPDATE projects SET status = 'blocked', blocked_reason = ? WHERE id = ?",
            (f"{BUDGET_BLOCK_PREFIX}: {total}/{budget} tokens", project_id),
        )
        return True
    if total < budget and row["status"] == "blocked" and blocked_by_budget:
        conn.execute(
            "UPDATE projects SET status = 'active', blocked_reason = NULL WHERE id = ?",
            (project_id,),
        )
        return True
    return False


def build_project_usage(conn: sqlite3.Connection, project_id: str) -> ProjectUsage:
    """Sum the measured token usage of every session in the project."""
    rows = conn.execute(
        "SELECT phase, worker, input_tokens, output_tokens, cache_read_tokens,"
        " cache_write_tokens, total_tokens FROM sessions WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    total = UsageBucket()
    by_phase: dict[str, UsageBucket] = {}
    by_worker: dict[str, UsageBucket] = {}
    for row in rows:
        buckets = (
            total,
            by_phase.setdefault(row["phase"], UsageBucket()),
            by_worker.setdefault(row["worker"], UsageBucket()),
        )
        for bucket in buckets:
            _accumulate_usage(bucket, row)
    for bucket in (total, *by_phase.values(), *by_worker.values()):
        _settle_usage(bucket)
    return ProjectUsage(
        project_id=project_id,
        by_phase=dict(sorted(by_phase.items())),
        by_worker=dict(sorted(by_worker.items())),
        **total.model_dump(),
    )

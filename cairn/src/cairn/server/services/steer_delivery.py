"""Deliver in-flight human instructions to a running worker once.

Cairn's pi extension asks the server before every bash call. That is the natural
PreToolUse hook (ARTEX drains its steer queue there): the first call after a
message was sent returns the instruction, the extension blocks that one call and
hands the text back to the model. A receipt keyed by steer/worker/session keeps
one message from blocking every later command in the same run.
"""

from __future__ import annotations

import sqlite3

from .timeutil import utcnow

MAX_STEERS_PER_CALL = 3


def claim_steers(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    worker: str | None,
    session_id: str | None = None,
) -> list[dict[str, str]]:
    if not worker:
        return []
    session_key = session_id or ""
    rows = conn.execute(
        """
        SELECT s.id, s.text FROM steers s
        LEFT JOIN steer_deliveries d
          ON d.project_id = s.project_id
         AND d.steer_id = s.id
         AND d.worker = ?
         AND d.session_id = ?
        WHERE s.project_id = ? AND s.answer IS NULL AND d.steer_id IS NULL
        ORDER BY s.rowid
        LIMIT ?
        """,
        (worker, session_key, project_id, MAX_STEERS_PER_CALL),
    ).fetchall()
    if not rows:
        return []
    now = utcnow()
    claimed: list[dict[str, str]] = []
    for row in rows:
        # The insert is the receipt: under concurrent tool calls only the request
        # that actually writes a row is allowed to deliver the steer.
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO steer_deliveries
                (project_id, steer_id, worker, session_id, delivered_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, row["id"], worker, session_key, now),
        )
        if cursor.rowcount:
            claimed.append({"id": row["id"], "text": row["text"]})
    return claimed

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None
_db_generation = 0
_local = threading.local()

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    step_timeout INTEGER NOT NULL DEFAULT 20,
    decide_timeout INTEGER NOT NULL DEFAULT 20
);

INSERT OR IGNORE INTO settings (rowid) VALUES (1);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    decide_worker TEXT,
    decide_trigger TEXT,
    decide_started_at TEXT,
    decide_last_heartbeat_at TEXT,
    token_budget INTEGER,
    blocked_reason TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT,
    description TEXT NOT NULL,
    step_id TEXT,
    created_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS steps (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_id TEXT,
    kind TEXT NOT NULL DEFAULT 'action',
    title TEXT,
    description TEXT NOT NULL,
    rationale TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    result_fact_id TEXT,
    note TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS step_sources (
    step_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (step_id, project_id, fact_id),
    FOREIGN KEY (step_id, project_id) REFERENCES steps(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id TEXT,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    status TEXT NOT NULL DEFAULT 'tentative',
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

-- Parsed request/response pairs lifted out of session_messages, so evidence stays
-- readable after the transcript is truncated or cleaned up.
CREATE TABLE IF NOT EXISTS http_exchanges (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    method TEXT,
    url TEXT,
    status INTEGER,
    request TEXT,
    response TEXT,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, session_id, seq, idx)
);

CREATE INDEX IF NOT EXISTS http_exchanges_by_digest ON http_exchanges (digest);

-- What proves a finding: exchange->http_exchanges.digest, document->documents.id,
-- seq->session_messages.seq. Kept in its own table so one finding can cite many.
CREATE TABLE IF NOT EXISTS finding_evidence (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    finding_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    digest TEXT,
    PRIMARY KEY (project_id, finding_id, kind, ref)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id TEXT,
    fact_id TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS documents_by_project ON documents (project_id, created_at);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

-- Constraints are the authorization boundary, not another hint: a hint is free
-- judgement absorbed into the graph, while a constraint outranks the goal and is
-- rendered first in every Decide/Execute prompt. `origin` records who wrote it
-- (human via the API, planner via Decide) so the two can never be confused.
CREATE TABLE IF NOT EXISTS constraints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'deny' CHECK (kind IN ('allow', 'deny')),
    text TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'human',
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS constraints_by_project ON constraints (project_id, kind);

-- In-flight instructions from the human to the planner (ARTEX's /api/chat +
-- steer_work equivalent). They are append-only: a steer stays in the transcript
-- so a worker that starts later can still see "stop touching that host".
CREATE TABLE IF NOT EXISTS steers (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answer TEXT,
    answered_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS steers_by_project ON steers (project_id, created_at);

-- Each steer is delivered once per worker session at its next tool call, so a
-- running executor sees it immediately instead of waiting for its next run.
CREATE TABLE IF NOT EXISTS steer_deliveries (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    steer_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    session_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (project_id, steer_id, worker, session_id)
);

-- Task-level authorization scope: IPs, CIDRs and domains the test may touch.
-- Empty means "no host gating"; one or more allow entries close the gate.
CREATE TABLE IF NOT EXISTS project_scope (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('host', 'ip', 'cidr', 'domain')),
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'human',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, kind, value)
);

CREATE INDEX IF NOT EXISTS project_scope_by_project ON project_scope (project_id);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    worker TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    step_id TEXT,
    summary TEXT,
    native_session TEXT,
    status TEXT NOT NULL DEFAULT 'failed',
    returncode INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    raw TEXT,
    PRIMARY KEY (session_id, project_id, seq),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT,
    error TEXT,
    worker TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT,
    answered_at TEXT,
    duration_ms INTEGER,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE INDEX IF NOT EXISTS sessions_by_project ON sessions (project_id, created_at);
CREATE INDEX IF NOT EXISTS findings_by_project ON findings (project_id, created_at);

-- Command-level guard. Rules are evaluated before a worker runs a shell command:
-- the first enabled rule by descending priority decides, otherwise the default
-- (allow) applies. `ask` suspends the command in `tool_approvals` until a human
-- decides or the rule's timeout expires.
CREATE TABLE IF NOT EXISTS tool_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    match_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    action TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 60,
    timeout_action TEXT NOT NULL DEFAULT 'deny',
    enabled INTEGER NOT NULL DEFAULT 1,
    -- NULL = platform-wide rule; a project id = task-level rule that only this
    -- project's workers see (ARTEX's task_intercept_rules equivalent).
    project_id TEXT
);

-- The project_id index is created by migration 006, not here: on a legacy DB
-- `tool_rules` already exists without that column, and executescript(SCHEMA)
-- runs BEFORE the migrations that add it.
CREATE TABLE IF NOT EXISTS tool_approvals (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT,
    worker TEXT,
    tool TEXT NOT NULL,
    command TEXT NOT NULL,
    digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    rule_id INTEGER,
    decision_source TEXT,
    decided_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    expires_at TEXT,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS tool_approvals_by_status ON tool_approvals (status, created_at);

CREATE TABLE IF NOT EXISTS tool_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT,
    worker TEXT,
    tool TEXT NOT NULL,
    command TEXT NOT NULL,
    digest TEXT NOT NULL,
    decision TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    rule_id INTEGER,
    actor TEXT,
    enforced INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tool_audit_by_project ON tool_audit (project_id, created_at);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

# Columns are additive so an existing Cairn 0.x database keeps working: create the new
# names, copy whatever the old version stored, and leave the old columns behind.
_LEGACY_COLUMN_COPIES: tuple[tuple[str, str, str], ...] = (
    ("settings", "step_timeout", "intent_timeout"),
    ("settings", "decide_timeout", "reason_timeout"),
    ("projects", "decide_worker", "reason_worker"),
    ("projects", "decide_trigger", "reason_trigger"),
    ("projects", "decide_started_at", "reason_started_at"),
    ("projects", "decide_last_heartbeat_at", "reason_last_heartbeat_at"),
)


def configure(path: Path) -> None:
    global _db_path, _db_generation
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_generation += 1
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Run each registered migration once, recording it in `schema_migrations`.

    Check-and-apply runs inside one IMMEDIATE transaction so two processes
    starting concurrently cannot both apply the same migration: the second one
    blocks on the write lock and, after acquiring it, re-reads
    `schema_migrations` and finds the work already recorded.
    """
    conn.commit()  # finish any implicit transaction so BEGIN IMMEDIATE is legal
    conn.execute("BEGIN IMMEDIATE")
    try:
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version, migrate in _MIGRATIONS:
            if version in applied:
                continue
            migrate(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migration_001_baseline(conn: sqlite3.Connection) -> None:
    """Columns and data transforms required by a pre-versioning (Cairn 0.x) database."""
    _ensure_columns(conn)
    _migrate_legacy_intents(conn)


def _migration_002_idempotency(conn: sqlite3.Connection) -> None:
    """Client-supplied dedupe keys so retried uploads cannot duplicate rows.

    The dispatcher retries session creates and message appends after network
    failures; `client_key` (per session object) and `client_msg_id` (per message)
    let the server land each upload exactly once. The unique indexes are partial
    so the many legacy rows without a key are unaffected.
    """
    if "client_key" not in _table_columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN client_key TEXT")
    if "client_msg_id" not in _table_columns(conn, "session_messages"):
        conn.execute("ALTER TABLE session_messages ADD COLUMN client_msg_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_client_key "
        "ON sessions (project_id, client_key) WHERE client_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_messages_client_msg_id "
        "ON session_messages (session_id, client_msg_id) WHERE client_msg_id IS NOT NULL"
    )


def _migration_003_finding_evidence(conn: sqlite3.Connection) -> None:
    """Findings get a confirmed/tentative status plus frozen evidence references.

    Older databases already have the `findings` table without `status`, so only the
    column needs adding; the two evidence tables come from `SCHEMA` and are created
    with `IF NOT EXISTS` on every start.
    """
    if "status" not in _table_columns(conn, "findings"):
        conn.execute("ALTER TABLE findings ADD COLUMN status TEXT NOT NULL DEFAULT 'tentative'")


def _migration_004_tool_guard(conn: sqlite3.Connection) -> None:
    """Seed the built-in command deny rules.

    Deliberately short: rules only cover actions no legitimate task needs (wiping
    the filesystem, dropping databases, destructive HTTP verbs) plus the usual
    "never touch government/education infrastructure" scope line. Anything else is
    left to the operator, because over-blocking silently breaks legitimate testing.
    """
    if conn.execute("SELECT COUNT(*) AS n FROM tool_rules").fetchone()["n"]:
        return
    conn.executemany(
        """
        INSERT INTO tool_rules (
            target, match_type, pattern, action, message, priority, timeout_seconds,
            timeout_action, enabled
        ) VALUES (?, ?, ?, 'deny', ?, ?, 60, 'deny', 1)
        """,
        [
            (
                "command",
                "regex",
                r"\brm\s+-(?:rf|fr)\s+/(?:\s|$)",
                "在容器内递归删除根目录，不属于任何测试步骤",
                100,
            ),
            (
                "command",
                "regex",
                r"\b(?:DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE)\b",
                "对数据库执行不可逆的清空操作",
                95,
            ),
            (
                "command",
                "contains",
                "curl -X DELETE",
                "使用 DELETE 动词删除远端数据，请改用只读验证",
                90,
            ),
            (
                "command",
                "regex",
                r"\.(?:gov|edu)(?:[./:]|$)",
                "目标属于禁止范围（政府/教育域名），需人工确认后由管理员处理",
                85,
            ),
        ],
    )


def _migration_005_token_budget(conn: sqlite3.Connection) -> None:
    """Per-project token budget and the machine reason a project got blocked."""
    columns = _table_columns(conn, "projects")
    if "token_budget" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN token_budget INTEGER")
    if "blocked_reason" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN blocked_reason TEXT")


def _migration_006_project_tool_rules(conn: sqlite3.Connection) -> None:
    """Let a project own tool rules without cloning the platform-wide rule set."""
    if "project_id" not in _table_columns(conn, "tool_rules"):
        conn.execute("ALTER TABLE tool_rules ADD COLUMN project_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS tool_rules_by_project ON tool_rules (project_id, enabled)"
    )


def _migration_007_steer_replies_and_scope(conn: sqlite3.Connection) -> None:
    """Steer answers, per-worker delivery receipts and task-level scope."""
    columns = _table_columns(conn, "steers")
    for name, ddl in (("answer", "TEXT"), ("answered_at", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE steers ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS steer_deliveries (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            steer_id TEXT NOT NULL,
            worker TEXT NOT NULL,
            session_id TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (project_id, steer_id, worker, session_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_scope (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('host', 'ip', 'cidr', 'domain')),
            value TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'human',
            created_at TEXT NOT NULL,
            UNIQUE (project_id, kind, value)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS project_scope_by_project ON project_scope (project_id)"
    )


def _migration_008_reviewer(conn: sqlite3.Connection) -> None:
    """Verdict cache for the command reviewer, plus the reason behind each decision.

    ``tool_audit`` recorded *what* was decided but not *why*, which made a denied
    command impossible to explain after the fact. The cache is keyed by the command
    and the project's scope digest, so editing the authorized assets invalidates
    every stale verdict instead of letting it survive a scope change.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_review_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            command_hash TEXT NOT NULL,
            scope_digest TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
            reason TEXT NOT NULL,
            rule TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE (project_id, command_hash, scope_digest)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS tool_review_cache_by_expiry ON tool_review_cache (expires_at)"
    )
    if "reason" not in _table_columns(conn, "tool_audit"):
        conn.execute("ALTER TABLE tool_audit ADD COLUMN reason TEXT")


_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_001_baseline),
    (2, _migration_002_idempotency),
    (3, _migration_003_finding_evidence),
    (4, _migration_004_tool_guard),
    (5, _migration_005_token_budget),
    (6, _migration_006_project_tool_rules),
    (7, _migration_007_steer_replies_and_scope),
    (8, _migration_008_reviewer),
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, new_column, legacy_column in _LEGACY_COLUMN_COPIES:
        columns = _table_columns(conn, table)
        if new_column in columns:
            continue
        if table == "settings":
            conn.execute(
                f"ALTER TABLE settings ADD COLUMN {new_column} INTEGER NOT NULL DEFAULT 20"
            )
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {new_column} TEXT")
        if legacy_column in columns:
            conn.execute(f"UPDATE {table} SET {new_column} = {legacy_column}")

    session_columns = _table_columns(conn, "sessions")
    for column, ddl in (
        ("native_session", "TEXT"),
        ("summary", "TEXT"),
        ("error", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("updated_at", "TEXT"),
        ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in session_columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")
    # Sessions created before `updated_at` existed have no heartbeat history.
    conn.execute("UPDATE sessions SET updated_at = created_at WHERE updated_at IS NULL")

    project_columns = _table_columns(conn, "projects")
    for column, ddl in (
        ("context_summary", "TEXT"),
        ("context_summary_facts", "INTEGER NOT NULL DEFAULT 0"),
        ("context_step_summary", "TEXT"),
        ("context_step_summary_facts", "INTEGER NOT NULL DEFAULT 0"),
        ("context_finding_summary", "TEXT"),
        ("context_finding_summary_facts", "INTEGER NOT NULL DEFAULT 0"),
        ("recall_plan", "TEXT"),
    ):
        if column not in project_columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {column} {ddl}")

    fact_columns = _table_columns(conn, "facts")
    if "step_id" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN step_id TEXT")
    if "created_at" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN created_at TEXT")
    if "title" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN title TEXT")

    step_columns = _table_columns(conn, "steps")
    if "title" not in step_columns:
        conn.execute("ALTER TABLE steps ADD COLUMN title TEXT")

    # Findings gained a severity grade; older rows have no opinion and stay `info`.
    finding_columns = _table_columns(conn, "findings")
    if "severity" not in finding_columns:
        conn.execute("ALTER TABLE findings ADD COLUMN severity TEXT NOT NULL DEFAULT 'info'")

    # A database created by Cairn 0.x has no goals table content: the root goal lived in a
    # reserved fact named "goal". Promote it to a real Goal node, then drop the fact.
    if "intents" not in _token_tables(conn):
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO goals (id, project_id, parent_id, description, status, creator, created_at)
        SELECT 'g001', p.id, NULL, f.description, 'active', 'legacy', p.created_at
        FROM projects p
        JOIN facts f ON f.project_id = p.id AND f.id = 'goal'
        WHERE NOT EXISTS (SELECT 1 FROM goals g WHERE g.project_id = p.id)
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scoped_counters (project_id, kind, value)
        SELECT project_id, 'goal', 1 FROM goals
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scoped_counters (project_id, kind, value)
        SELECT project_id, 'step', COUNT(*) FROM intents GROUP BY project_id
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scoped_counters (project_id, kind, value)
        SELECT id, 'finding', 0 FROM projects
        """
    )
    conn.execute("DELETE FROM facts WHERE id = 'goal'")


def _token_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _migrate_legacy_intents(conn: sqlite3.Connection) -> None:
    if (
        "intents" in _token_tables(conn)
        and not conn.execute("SELECT 1 FROM steps LIMIT 1").fetchone()
    ):
        _migrate_intents(conn)


def _migrate_intents(conn: sqlite3.Connection) -> None:
    """Copy Cairn 0.x intents into steps so old projects keep their history."""
    rows = conn.execute("SELECT * FROM intents").fetchall()
    if not rows:
        return
    for row in rows:
        if row["to_fact_id"] == "goal":
            status, result_fact_id, kind, note = "done", None, "completion", "legacy completion"
        elif row["to_fact_id"] is not None:
            status, result_fact_id, kind, note = "done", row["to_fact_id"], "action", None
        elif row["worker"] is not None:
            status, result_fact_id, kind, note = "running", None, "action", None
        else:
            status, result_fact_id, kind, note = "pending", None, "action", None
        conn.execute(
            """
            INSERT OR IGNORE INTO steps (
                id, project_id, goal_id, kind, description, rationale, priority, status,
                creator, worker, last_heartbeat_at, created_at, concluded_at, result_fact_id, note
            ) VALUES (?, ?, 'g001', ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["project_id"],
                kind,
                row["description"],
                status,
                row["creator"],
                row["worker"] if status == "running" else None,
                row["last_heartbeat_at"],
                row["created_at"],
                row["concluded_at"],
                result_fact_id,
                note,
            ),
        )
        for source in conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ?",
            (row["id"], row["project_id"]),
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (row["id"], row["project_id"], source["fact_id"]),
            )


_busy_lock = threading.Lock()
_busy_total = 0


def _note_busy() -> None:
    global _busy_total
    with _busy_lock:
        _busy_total += 1


def db_busy_total() -> int:
    """Hard lock failures (busy_timeout exhausted) since process start."""
    return _busy_total


class _InstrumentedConnection(sqlite3.Connection):
    """Connection that counts hard lock failures.

    `busy_timeout` makes SQLite wait out ordinary contention, so an
    OperationalError mentioning locked/busy means contention outlasted the
    timeout. Each occurrence bumps `db_busy_total()` so WAL and busy_timeout
    tuning can be judged from /metrics instead of guessed.
    """

    def execute(self, sql, parameters=()):
        try:
            return super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                _note_busy()
            raise

    def executemany(self, sql, parameters):
        try:
            return super().executemany(sql, parameters)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                _note_busy()
            raise

    def executescript(self, sql):
        try:
            return super().executescript(sql)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                _note_busy()
            raise


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """A per-thread connection, reused across requests.

    Opening a connection costs a WAL journal-mode round trip on every request, and the
    server's sync routes already run on a bounded thread pool, so one connection per
    thread is both simpler and cheaper than a pool.

    The context must not nest: committing happens per context, so an inner block
    would commit the outer block's partial work. Helpers take the connection as a
    parameter instead of opening their own.
    """
    if getattr(_local, "in_use", False):
        raise RuntimeError(
            "get_conn() must not be nested: the inner context would commit the "
            "outer context's partial work; pass the connection explicitly instead"
        )
    assert _db_path is not None
    path = str(_db_path)
    conn = getattr(_local, "conn", None)
    if conn is not None and (
        getattr(_local, "path", None) != path
        or getattr(_local, "generation", None) != _db_generation
    ):
        conn.close()
        conn = None
    if conn is None:
        conn = sqlite3.connect(path, timeout=5.0, factory=_InstrumentedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Writers are serialised by SQLite; wait instead of failing immediately.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.path = path
        _local.generation = _db_generation
    _local.in_use = True
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _local.in_use = False

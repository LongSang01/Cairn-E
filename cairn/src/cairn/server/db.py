from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

# Bumped only when SCHEMA changes shape. The number is recorded in ``PRAGMA
# user_version``: a file from an older build is patched in place by
# ``_patch_legacy_columns`` (additive columns only), and a file from a *newer* build
# is refused instead of being half-read.
SCHEMA_VERSION = 3

# Columns that were added to an existing table after the first release. Every entry
# is additive and carries a default, so an old file keeps working once the column is
# there — without it the first request fails deep in SQLite (``no such column``) or
# in the row lookup (``No item with that key``) instead of at startup.
_LEGACY_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("steps", "exclusive", "INTEGER NOT NULL DEFAULT 0"),
    ("tool_audit", "approval_id", "TEXT"),
)

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
    blocked_reason TEXT,
    -- Cached rolling summaries: full-run, step slice and finding slice, each with the
    -- fact count it was built from so a later run can tell whether it is stale.
    context_summary TEXT,
    context_summary_facts INTEGER NOT NULL DEFAULT 0,
    context_step_summary TEXT,
    context_step_summary_facts INTEGER NOT NULL DEFAULT 0,
    context_finding_summary TEXT,
    context_finding_summary_facts INTEGER NOT NULL DEFAULT 0,
    recall_plan TEXT
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
    -- Declared target (host, host:port, credential surface). Two steps sharing one
    -- still run in parallel; only `exclusive` claims it for a run.
    asset TEXT,
    -- 1 = do not run this step next to another step on the same asset.
    exclusive INTEGER NOT NULL DEFAULT 0,
    -- The Decide run that produced this step: only steps of one batch may run
    -- concurrently, since a planner never saw two batches at once.
    batch TEXT,
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
    -- Client-supplied dedupe key: a retried upload lands exactly once.
    client_key TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_client_key
    ON sessions (project_id, client_key) WHERE client_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    raw TEXT,
    client_msg_id TEXT,
    PRIMARY KEY (session_id, project_id, seq),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_messages_client_msg_id
    ON session_messages (session_id, client_msg_id) WHERE client_msg_id IS NOT NULL;

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
CREATE INDEX IF NOT EXISTS tool_rules_by_project ON tool_rules (project_id, enabled);

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
    timeout_seconds INTEGER,
    timeout_action TEXT,
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
    -- Set when the row is the `ask` queued for a human, or that approval's later
    -- timeout/manual verdict: it is what ties the two entries together.
    approval_id TEXT,
    actor TEXT,
    reason TEXT,
    enforced INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS tool_audit_by_project ON tool_audit (project_id, created_at);
CREATE INDEX IF NOT EXISTS tool_audit_by_approval ON tool_audit (approval_id);

-- Reviewer verdicts are cached per (command, scope digest) so editing the authorized
-- assets invalidates every stale verdict instead of letting it survive a scope change.
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
);

CREATE INDEX IF NOT EXISTS tool_review_cache_by_expiry ON tool_review_cache (expires_at);
"""


def configure(path: Path) -> None:
    global _db_path, _db_generation
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_generation += 1
    try:
        with get_conn() as conn:
            _refuse_newer_schema(conn, path)
            # Before SCHEMA on purpose: the script creates indexes over the new
            # columns, and on a table that predates them the index itself fails.
            _patch_legacy_columns(conn)
            conn.executescript(SCHEMA)
            _seed_tool_rules(conn)
            # Recorded so a later build can tell this file apart from one it predates.
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    except Exception:
        # A rejected file must not leave the module pointed at it: the caller that
        # catches the error has to be able to choose another path.
        _db_path = None
        raise


def _stored_schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _refuse_newer_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Fail loudly on a file written by a newer build.

    A file with version 0 predates the check and is patched up by
    ``_patch_legacy_columns``; anything above ``SCHEMA_VERSION`` was written by a
    build that knows columns this one does not, so reading it would silently answer
    from a half-understood schema.
    """
    stored = _stored_schema_version(conn)
    if stored > SCHEMA_VERSION:
        raise RuntimeError(
            f"database {path} uses schema version {stored}, this build expects "
            f"{SCHEMA_VERSION}: upgrade Cairn or point it at a fresh file"
        )


def _patch_legacy_columns(conn: sqlite3.Connection) -> None:
    """Add the columns an older DB file is missing, so it works instead of 500ing.

    A table that does not exist yet is left alone: ``SCHEMA`` creates it with the
    column already in place.
    """
    for table, column, ddl in _LEGACY_COLUMNS:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone():
            continue
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError as exc:
            # The server and the dispatcher may configure the same file at once; the
            # loser of that race finds the column already there.
            if "duplicate column" not in str(exc):
                raise


_BUILTIN_TOOL_RULES = (
    {
        "target": "command",
        "match_type": "regex",
        "pattern": r"\brm\s+-(?:rf|fr)\s+/(?:\s|$)",
        "message": "在容器内递归删除根目录，不属于任何测试步骤",
        "priority": 100,
    },
    {
        "target": "command",
        "match_type": "regex",
        "pattern": r"\b(?:DROP\s+(?:TABLE|DATABASE)|TRUNCATE\s+TABLE)\b",
        "message": "对数据库执行不可逆的清空操作",
        "priority": 95,
    },
    {
        "target": "command",
        "match_type": "contains",
        "pattern": "curl -X DELETE",
        "message": "使用 DELETE 动词删除远端数据，请改用只读验证",
        "priority": 90,
    },
    {
        "target": "host",
        "match_type": "regex",
        "pattern": r"\.(?:gov|edu)(?:\.[a-z]{2})?$",
        "message": "目标属于禁止范围（政府/教育域名），需人工确认后由管理员处理",
        "priority": 85,
    },
)


def _seed_tool_rules(conn: sqlite3.Connection) -> None:
    """Install the built-in platform rules, refreshing a table seeded by an older build.

    Rules are matched by ``message``: it is the one field the application owns and
    never varies, so a corrected target/pattern (the government/education rule moved
    from the command text to the host name) reaches every deployment instead of only
    a database whose rule table is still empty. Operator rules, project rules,
    priorities and timeouts are left untouched — only the shipped target, match type
    and pattern are rewritten.

    Deliberately short: rules only cover actions no legitimate task needs (wiping
    the filesystem, dropping databases, destructive HTTP verbs) plus the usual
    "never touch government/education infrastructure" scope line. Anything else is
    left to the operator, because over-blocking silently breaks legitimate testing.
    """
    for rule in _BUILTIN_TOOL_RULES:
        row = conn.execute(
            "SELECT id FROM tool_rules WHERE project_id IS NULL AND message = ?",
            (rule["message"],),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO tool_rules (
                    target, match_type, pattern, action, message, priority,
                    timeout_seconds, timeout_action, enabled
                ) VALUES (?, ?, ?, 'deny', ?, ?, 60, 'deny', 1)
                """,
                (
                    rule["target"],
                    rule["match_type"],
                    rule["pattern"],
                    rule["message"],
                    rule["priority"],
                ),
            )
            continue
        conn.execute(
            "UPDATE tool_rules SET target = ?, match_type = ?, pattern = ? WHERE id = ?",
            (rule["target"], rule["match_type"], rule["pattern"], row["id"]),
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

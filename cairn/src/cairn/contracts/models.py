"""Wire models shared by the server and the dispatcher.

Nothing here imports from ``cairn.server`` or ``cairn.dispatcher``: the dispatcher
reads these models straight off the HTTP responses, and the server builds them from
SQLite rows. HTTP request bodies (with their validation) live in ``server.models``.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

GoalStatus = Literal["active", "achieved", "abandoned"]
# `cancelled` is a planner kill: the running executor is stopped and its result
# is deliberately discarded, unlike `abandoned` (a pending step never started).
StepStatus = Literal["pending", "running", "done", "abandoned", "cancelled"]
# Closed forever: no lease renewal, no conclusion, no re-claim can revive them.
TERMINAL_STEP_STATUSES: tuple[str, ...] = ("done", "abandoned", "cancelled")
# Transcript roles: `tool` is the raw-output fallback, `tool_call`/`tool_result`
# are the individual tool invocations, `thinking` is model reasoning.
SessionRole = Literal["system", "user", "assistant", "thinking", "tool", "tool_call", "tool_result"]
# `running` marks a session that is still being appended to while the worker runs.
SessionStatus = Literal["running", "success", "failed", "cancelled", "rejected"]
# Side question lifecycle: asked by a human, claimed by the dispatcher, then answered.
QuestionStatus = Literal["pending", "running", "answered", "failed", "cancelled"]
# How much a finding matters. `info` is the neutral default so older rows keep loading.
FindingSeverity = Literal["critical", "high", "medium", "low", "info"]
# Derive the ordered tuple from the Literal so the two can never drift apart.
FINDING_SEVERITIES: tuple[str, ...] = get_args(FindingSeverity)
# A finding is confirmed only when it carries evidence; otherwise it is a suspicion.
FindingStatus = Literal["confirmed", "tentative"]
FINDING_STATUSES: tuple[str, ...] = get_args(FindingStatus)
# Where a finding's proof lives: a parsed HTTP exchange, an evidence document, or a
# raw transcript sequence number (last resort, when the parser understood nothing).
EvidenceKind = Literal["exchange", "document", "seq"]


# Storage ceiling for one evidence document (~200 KB). This is a safety rail, not a
# prompt budget: Decide still pages through documents with RecallPlan offsets.
MAX_DOCUMENT_CHARS = 200_000


class Settings(BaseModel):
    step_timeout: int = Field(ge=5)
    decide_timeout: int = Field(ge=5)


class Fact(BaseModel):
    """A confirmed fact about the world. Append-only.

    ``title`` is the one-line gist used by the snapshot; ``description`` stays the fuller
    summary, and bulk evidence lives in documents referenced from the export.
    """

    id: str
    description: str
    title: str | None = None


class Goal(BaseModel):
    """A completion condition. The root goal holds the project goal; goals form a tree."""

    id: str
    parent: str | None = None
    description: str
    status: GoalStatus
    creator: str
    created_at: str
    closed_at: str | None = None


class Step(BaseModel):
    """The next action: how to derive new facts from facts already on the board."""

    id: str
    from_: list[str] = Field(default_factory=list, alias="from")
    to: str | None = None
    goal: str | None = None
    kind: Literal["action", "completion"] = "action"
    description: str
    title: str | None = None
    rationale: str | None = None
    priority: int = 0
    status: StepStatus
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None
    note: str | None = None

    model_config = {"populate_by_name": True}


class EvidenceRef(BaseModel):
    """A pointer to frozen proof, so a report survives transcript cleanup."""

    kind: EvidenceKind
    ref: str
    digest: str | None = None


class Finding(BaseModel):
    """A structured product of the search, whose definition is task-defined.

    ``status`` is ``confirmed`` only when ``evidence`` is non-empty; the server
    downgrades a submission that claims otherwise, so a report never ships a
    confirmed finding that cannot be reproduced.
    """

    id: str
    kind: str
    description: str
    severity: FindingSeverity = "info"
    status: FindingStatus = "tentative"
    evidence: list[EvidenceRef] = Field(default_factory=list)
    step: str | None = None
    creator: str
    created_at: str


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class SessionMessage(BaseModel):
    """One turn of the conversation the dispatcher had with a model."""

    seq: int
    role: SessionRole
    content: str
    raw: str | None = None


class TokenUsage(BaseModel):
    """Token counters a worker reported for one run, or summed over many runs.

    Workers whose CLI does not expose usage leave everything at zero rather than
    guessing, so a project total is always made of measured values only.
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class Session(TokenUsage):
    """A single worker run: the prompt sent to the model and what it answered."""

    id: str
    worker: str
    worker_type: str
    phase: str
    step: str | None = None
    # One line describing what this run was asked to do, so a session list is readable
    # without opening the transcript.
    summary: str = ""
    native_session: str | None = None
    status: str
    returncode: int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    error: str | None = None
    created_at: str
    message_count: int = 0


class SessionDetail(Session):
    messages: list[SessionMessage] = Field(default_factory=list)


class Question(BaseModel):
    """A read-only side question: answered from a graph snapshot, never written back."""

    id: str
    project_id: str
    content: str
    status: QuestionStatus
    answer: str | None = None
    error: str | None = None
    worker: str | None = None
    created_at: str
    started_at: str | None = None
    answered_at: str | None = None
    duration_ms: int | None = None


class ContextSummary(BaseModel):
    """Rolling summaries of the history that left the recent window.

    Each channel carries both the text and how many items it covers, so a later run can
    tell whether new history still needs folding in.
    """

    summary: str = ""
    facts_covered: int = Field(ge=0, default=0)
    step_summary: str = ""
    steps_covered: int = Field(ge=0, default=0)
    finding_summary: str = ""
    findings_covered: int = Field(ge=0, default=0)


class ProjectDecide(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed", "blocked"]
    created_at: str
    decide: ProjectDecide | None = None
    # `blocked` is machine-set when measured token usage passes the budget; the reason
    # explains it in the UI instead of leaving an unexplained stopped project.
    token_budget: int | None = None
    blocked_reason: str | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    goal_count: int
    open_goal_count: int
    step_count: int
    running_step_count: int
    pending_step_count: int
    finding_count: int
    hint_count: int


class Constraint(BaseModel):
    """One rule the search must obey, ahead of its own goal.

    ``kind`` is ``allow`` (explicit authorization) or ``deny`` (prohibition).
    ``origin`` is ``human`` when entered through the API and ``planner`` when
    Decide derived it from the task; nothing enforces a constraint at the tool
    level, they are prompt-level authorization, same as ARTEX's task constraints.
    """

    id: str
    kind: Literal["allow", "deny"]
    text: str
    origin: str
    created_at: str


class Steer(BaseModel):
    """One message a human sent while the project was running, plus the planner's reply."""

    id: str
    text: str
    creator: str
    created_at: str
    answer: str | None = None
    answered_at: str | None = None


class ScopeEntry(BaseModel):
    """One authorized target. IPs/CIDRs are matched with ``ipaddress``, domains by suffix."""

    id: int
    kind: Literal["host", "ip", "cidr", "domain"]
    value: str
    source: str
    created_at: str


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    goals: list[Goal]
    steps: list[Step]
    findings: list[Finding]
    hints: list[Hint]
    constraints: list[Constraint] = Field(default_factory=list)
    steers: list[Steer] = Field(default_factory=list)
    scope: list[ScopeEntry] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)


class DocumentMeta(BaseModel):
    """Document header returned by list endpoints (content is fetched on demand)."""

    id: str
    title: str
    fact_id: str | None = None
    step_id: str | None = None
    size: int
    created_at: str


class Document(DocumentMeta):
    content: str


class RecallHit(BaseModel):
    """One keyword-recall hit: enough to decide whether to pull the full node."""

    kind: Literal["fact", "step", "document", "exchange"]
    id: str
    title: str | None = None
    description: str
    status: str | None = None
    score: int = 0
    doc_ids: list[str] = Field(default_factory=list)


class RecallResponse(BaseModel):
    query: str
    facts: list[RecallHit] = Field(default_factory=list)
    steps: list[RecallHit] = Field(default_factory=list)
    # Proof is searched too, so a Decide can find "the response that showed the header"
    # without reading every document or replaying a session.
    documents: list[RecallHit] = Field(default_factory=list)
    exchanges: list[RecallHit] = Field(default_factory=list)


class DocumentRequest(BaseModel):
    """A document the next Decide should read.

    ``from`` is the character offset to start at, so a read that ran out of injection
    budget can continue across Decide runs instead of losing the rest of the document.
    """

    id: str
    offset: int = Field(default=0, alias="from", ge=0)


class RecallPlan(BaseModel):
    """What the last Decide asked to look up; the next run injects the results."""

    queries: list[str] = Field(default_factory=list)
    doc_ids: list[DocumentRequest] = Field(default_factory=list)


class FindingsSummary(BaseModel):
    """Counts by grade and kind; the findings themselves live on their own endpoint."""

    project_id: str
    total: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_kind: dict[str, int] = Field(default_factory=dict)


class UsageBucket(TokenUsage):
    """One group of sessions: the totals a project, phase or worker spent."""

    sessions: int = 0


class ProjectUsage(UsageBucket):
    """Project-wide token totals, broken down by phase and by worker."""

    project_id: str
    by_phase: dict[str, UsageBucket] = Field(default_factory=dict)
    by_worker: dict[str, UsageBucket] = Field(default_factory=dict)

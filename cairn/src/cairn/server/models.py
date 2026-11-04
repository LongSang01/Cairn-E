from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

GoalStatus = Literal["active", "achieved", "abandoned"]
StepStatus = Literal["pending", "running", "done", "abandoned"]
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


class Finding(BaseModel):
    """A structured product of the search, whose definition is task-defined."""

    id: str
    kind: str
    description: str
    severity: FindingSeverity = "info"
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


class SubmittedSessionMessage(BaseModel):
    role: SessionRole
    content: str
    raw: str | None = None


class CreateSessionRequest(TokenUsage):
    worker: str
    worker_type: str
    phase: str
    step: str | None = None
    summary: str = Field(default="", max_length=300)
    native_session: str | None = None
    # Required on purpose: a live run creates the session with `running`, the
    # end-of-run fallback with the real outcome. Defaulting here would silently
    # record a wrong status when a caller forgets to choose one.
    status: SessionStatus
    returncode: int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    error: str | None = None
    messages: list[SubmittedSessionMessage] = Field(default_factory=list)

    @field_validator("worker", "worker_type", "phase")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        return " ".join(value.split())


class AppendSessionMessagesRequest(BaseModel):
    messages: list[SubmittedSessionMessage]


class FinishSessionRequest(TokenUsage):
    """Finalize a session that was created while the worker was still running."""

    status: SessionStatus
    returncode: int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    error: str | None = None
    native_session: str | None = None


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


class CreateQuestionRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ClaimQuestionRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_worker(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class AnswerQuestionRequest(BaseModel):
    status: Literal["answered", "failed"]
    answer: str | None = None
    error: str | None = None
    worker: str | None = None
    duration_ms: int | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> AnswerQuestionRequest:
        if self.status == "answered" and not (self.answer or "").strip():
            raise ValueError("answer is required when status is answered")
        if self.status == "failed" and not (self.error or "").strip():
            raise ValueError("error is required when status is failed")
        return self


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
    status: Literal["active", "stopped", "completed"]
    created_at: str
    decide: ProjectDecide | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    goal_count: int
    open_goal_count: int
    step_count: int
    running_step_count: int
    pending_step_count: int
    finding_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    goals: list[Goal]
    steps: list[Step]
    findings: list[Finding]
    hints: list[Hint]
    sessions: list[Session] = Field(default_factory=list)


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "origin", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateStepRequest(BaseModel):
    from_: list[str] = Field(default_factory=list, alias="from")
    goal: str | None = None
    title: str | None = None
    description: str
    rationale: str | None = None
    priority: int = 0
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "rationale", "worker", "goal")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class UpdateStepRequest(BaseModel):
    """Used by Decide: reprioritize or abandon a step. The step itself is never deleted."""

    priority: int | None = None
    status: Literal["abandoned"] | None = None
    note: str | None = None


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class DecideClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class SubmittedDocument(BaseModel):
    """Bulk evidence attached to a fact; the graph only carries its id."""

    title: str
    content: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 200:
            raise ValueError("must be at most 200 characters")
        return text

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        # Storage guard only, deliberately generous: it stops a runaway tool from
        # writing a hundred-megabyte row. How much of a document the model actually
        # reads is decided by the Decide injection budget and its resume offsets.
        if len(value) > MAX_DOCUMENT_CHARS:
            raise ValueError(f"must be at most {MAX_DOCUMENT_CHARS} characters")
        return value


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


class CreateDocumentRequest(BaseModel):
    title: str
    content: str
    fact_id: str | None = None
    step_id: str | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if len(text) > 200:
            raise ValueError("must be at most 200 characters")
        return text

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        # Storage guard only, deliberately generous: it stops a runaway tool from
        # writing a hundred-megabyte row. How much of a document the model actually
        # reads is decided by the Decide injection budget and its resume offsets.
        if len(value) > MAX_DOCUMENT_CHARS:
            raise ValueError(f"must be at most {MAX_DOCUMENT_CHARS} characters")
        return value


class RecallHit(BaseModel):
    """One keyword-recall hit: enough to decide whether to pull the full node."""

    kind: Literal["fact", "step"]
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


class SubmittedFinding(BaseModel):
    kind: str = "finding"
    description: str
    severity: FindingSeverity = "info"

    @field_validator("kind", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().lower()
            return text or "info"
        return value


class SubmittedFact(BaseModel):
    title: str | None = None
    description: str
    documents: list[SubmittedDocument] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeStepRequest(BaseModel):
    """Execute's submit_fact / submit_finding, plus closing the claimed step."""

    worker: str
    status: Literal["done", "abandoned"] = "done"
    fact: SubmittedFact | None = None
    facts: list[SubmittedFact] | None = None
    findings: list[SubmittedFinding] | None = None
    note: str | None = None

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    def submitted_facts(self) -> list[SubmittedFact]:
        items: list[SubmittedFact] = []
        if self.fact is not None:
            items.append(self.fact)
        if self.facts:
            items.extend(self.facts)
        return items


class ConcludeStepResponse(BaseModel):
    facts: list[Fact]
    findings: list[Finding]
    step: Step


class CreateGoalRequest(BaseModel):
    description: str
    parent: str | None = None
    creator: str

    @field_validator("description", "creator", "parent")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CloseGoalRequest(BaseModel):
    status: Literal["achieved", "abandoned"]
    note: str | None = None
    worker: str

    @field_validator("worker", "note")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned


class CompleteResponse(BaseModel):
    project: ProjectMeta
    goal: Goal
    step: Step


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    goal: Goal


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

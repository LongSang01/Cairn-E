from __future__ import annotations

from typing import Literal

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


class Settings(BaseModel):
    step_timeout: int = Field(ge=5)
    decide_timeout: int = Field(ge=5)


class Fact(BaseModel):
    """A confirmed fact about the world. Append-only."""

    id: str
    description: str


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


class Session(BaseModel):
    """A single worker run: the prompt sent to the model and what it answered."""

    id: str
    worker: str
    worker_type: str
    phase: str
    step: str | None = None
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


class CreateSessionRequest(BaseModel):
    worker: str
    worker_type: str
    phase: str
    step: str | None = None
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


class AppendSessionMessagesRequest(BaseModel):
    messages: list[SubmittedSessionMessage]


class FinishSessionRequest(BaseModel):
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
    """Rolling summary of the facts that left the recent window."""

    summary: str = ""
    facts_covered: int = Field(ge=0, default=0)


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


class SubmittedFinding(BaseModel):
    kind: str = "finding"
    description: str

    @field_validator("kind", "description")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class SubmittedFact(BaseModel):
    description: str

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

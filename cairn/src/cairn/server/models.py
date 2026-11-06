"""HTTP request bodies and validation for the server API.

The wire/domain models live in :mod:`cairn.contracts.models` and are re-exported here
so server code keeps one import site. The dispatcher imports the contracts package
directly, which removes its dependency on ``cairn.server``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.contracts.models import (
    FINDING_SEVERITIES,
    FINDING_STATUSES,
    MAX_DOCUMENT_CHARS,
    ContextSummary,
    Document,
    DocumentMeta,
    DocumentRequest,
    EvidenceRef,
    Fact,
    Finding,
    FindingSeverity,
    FindingsSummary,
    FindingStatus,
    Goal,
    GoalStatus,
    Hint,
    ProjectDecide,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ProjectUsage,
    Question,
    QuestionStatus,
    RecallHit,
    RecallPlan,
    RecallResponse,
    Session,
    SessionDetail,
    SessionMessage,
    SessionRole,
    SessionStatus,
    Settings,
    Step,
    StepStatus,
    TokenUsage,
    UsageBucket,
)

__all__ = [
    "FINDING_SEVERITIES",
    "MAX_DOCUMENT_CHARS",
    "AnswerQuestionRequest",
    "AppendSessionMessagesRequest",
    "ClaimQuestionRequest",
    "CloseGoalRequest",
    "CompleteRequest",
    "CompleteResponse",
    "ConcludeStepRequest",
    "ConcludeStepResponse",
    "ContextSummary",
    "CreateDocumentRequest",
    "CreateGoalRequest",
    "CreateHintInline",
    "CreateHintRequest",
    "CreateProjectRequest",
    "CreateQuestionRequest",
    "CreateSessionRequest",
    "CreateStepRequest",
    "DecideClaimRequest",
    "Document",
    "DocumentMeta",
    "DocumentRequest",
    "Fact",
    "Finding",
    "FindingSeverity",
    "FindingsSummary",
    "FinishSessionRequest",
    "Goal",
    "GoalStatus",
    "HeartbeatRequest",
    "Hint",
    "ProjectDecide",
    "ProjectDetail",
    "ProjectMeta",
    "ProjectSummary",
    "ProjectUsage",
    "Question",
    "QuestionStatus",
    "RecallHit",
    "RecallPlan",
    "RecallResponse",
    "ReopenRequest",
    "ReopenResponse",
    "Session",
    "SessionDetail",
    "SessionMessage",
    "SessionRole",
    "SessionStatus",
    "Settings",
    "Step",
    "StepStatus",
    "SubmittedDocument",
    "SubmittedFact",
    "SubmittedFinding",
    "SubmittedSessionMessage",
    "TokenUsage",
    "UpdateProjectStatusRequest",
    "UpdateProjectTitleRequest",
    "UpdateStepRequest",
    "UsageBucket",
]


class SubmittedSessionMessage(BaseModel):
    role: SessionRole
    content: str
    raw: str | None = None
    # Client-generated dedupe key: re-sending a message with the same msg_id is a
    # no-op, so upload retries cannot corrupt the transcript.
    msg_id: str | None = None


class CreateSessionRequest(TokenUsage):
    worker: str
    worker_type: str
    phase: str
    step: str | None = None
    summary: str = Field(default="", max_length=300)
    native_session: str | None = None
    # Client-generated dedupe key for the whole upload: a retried create converges
    # onto the session that already landed instead of recording a duplicate run.
    client_key: str | None = None
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


class SubmittedFinding(BaseModel):
    kind: str = "finding"
    description: str
    severity: FindingSeverity = "info"
    # `confirmed` is a promise the report can be reproduced; the server downgrades it
    # to `tentative` when no evidence reference resolves, instead of rejecting the run.
    status: FindingStatus = "tentative"
    evidence: list[EvidenceRef] = Field(default_factory=list)

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

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().lower()
            if text in FINDING_STATUSES:
                return text
            return "tentative"
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


class ToolGuardCheckRequest(BaseModel):
    """One shell command a worker is about to run."""

    tool: str = "bash"
    command: str
    worker: str | None = None
    session: str | None = None

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ToolApprovalDecision(BaseModel):
    approve: bool
    actor: str | None = None
    note: str | None = None


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

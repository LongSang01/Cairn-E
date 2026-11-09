from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cairn.contracts.models import (
    ContextSummary,
    Document,
    Fact,
    Finding,
    Goal,
    Hint,
    ProjectDetail,
    ProjectMeta,
    RecallHit,
    RecallPlan,
    RecallResponse,
    Step,
)
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.workers.base import DriverResult
from cairn.dispatcher.workers.health import HealthResult
from cairn.dispatcher.workers.usage import Usage
from cairn.server import db
from cairn.server.app import app


@pytest.fixture(autouse=True)
def _isolate_environment():
    """Keep ``CAIRN_*`` environment from leaking between tests.

    The CLI sets ``CAIRN_WEB_CONFIG`` for the middleware it is about to run; when a
    test calls that path while the repository root holds a ``dispatch.yaml``, the
    credentials stayed set for every later test. Restore the environment verbatim.
    """
    before = {key: value for key, value in os.environ.items() if key.startswith("CAIRN_")}
    yield
    for key in [key for key in os.environ if key.startswith("CAIRN_")]:
        del os.environ[key]
    os.environ.update(before)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    """Fresh API client over an isolated temporary database."""
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def bare_loop() -> DispatcherLoop:
    """A DispatcherLoop with only its in-memory bookkeeping wired.

    ``__init__`` loads the config, builds a server client and opens four pools; tests
    that exercise the mixins need none of that. Every attribute the mixins read lives
    here so a new one cannot be forgotten by one test file and remembered by another.
    """
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.question_futures = {}
    loop.summary_futures = {}
    loop.summary_inflight = set()
    loop._summary_lock = threading.Lock()
    loop.decide_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop.decide_retry_until = {}
    loop.decide_failures = {}
    loop.step_failures = {}
    loop._log_state = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.project_cursor = 0
    loop._settings_checked = False
    loop._startup_healthchecks_checked = False
    return loop


def make_config(runtime: dict | None = None) -> DispatchConfig:
    runtime_config = {
        "interval": 60,
        "max_workers": 2,
        "max_running_projects": 1,
        "max_project_workers": 2,
        "healthcheck_timeout": 5,
        "prompt_group": "default",
        **(runtime or {}),
    }
    return DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": runtime_config,
            "tasks": {
                "decide": {"timeout": 10, "max_steps": 3},
                "execute": {"timeout": 10, "conclude_timeout": 5},
            },
            "container": {
                "image": "test-image",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "test-worker",
                    "type": "mock",
                    "task_types": ["decide", "execute"],
                    "max_running": 1,
                    "priority": 0,
                }
            ],
        }
    )


def make_project(
    *,
    steps: list[Step] | None = None,
    findings: list[Finding] | None = None,
) -> ProjectDetail:
    return ProjectDetail(
        project=ProjectMeta(
            id="proj_001",
            title="test",
            status="active",
            created_at="2026-01-01T00:00:00Z",
        ),
        facts=[
            Fact(id="origin", description="start"),
            Fact(id="f001", description="known fact"),
        ],
        goals=[
            Goal(
                id="g001",
                parent=None,
                description="finish the task",
                status="active",
                creator="system",
                created_at="2026-01-01T00:00:00Z",
            )
        ],
        steps=steps or [],
        findings=findings or [],
        hints=[
            Hint(
                id="h001",
                content="use the clue",
                creator="human",
                created_at="2026-01-01T00:00:01Z",
            )
        ],
    )


def make_step(
    step_id: str = "s001",
    *,
    status: str = "pending",
    worker: str | None = None,
    asset: str | None = None,
    batch: str | None = None,
) -> Step:
    return Step(
        id=step_id,
        from_=["f001"],
        description="investigate",
        goal="g001",
        asset=asset,
        batch=batch,
        priority=0,
        status=status,  # type: ignore[arg-type]
        creator="decider",
        worker=worker,
        created_at="2026-01-01T00:00:02Z",
    )


class FakeLease:
    def __init__(self) -> None:
        self.failure = None
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def attach_process(self, _process) -> None:
        return None


@dataclass
class FakeContainerManager:
    writes: list[tuple[str, str, str]] = field(default_factory=list)

    def ensure_running(self, project_id: str) -> str:
        return f"container-{project_id}"

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))


@dataclass
class FakeClient:
    project: ProjectDetail
    concluded: list[tuple[str, str, str, list[str], list[dict]]] = field(default_factory=list)
    concluded_details: list[list[dict]] = field(default_factory=list)
    completed: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_steps: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_step_titles: list[str | None] = field(default_factory=list)
    created_step_assets: list[str | None] = field(default_factory=list)
    created_step_batches: list[str | None] = field(default_factory=list)
    updated_steps: list[tuple[str, str, object, object]] = field(default_factory=list)
    created_goals: list[tuple[str, str, str]] = field(default_factory=list)
    closed_goals: list[tuple[str, str, str]] = field(default_factory=list)
    released: list[tuple[str, str, str]] = field(default_factory=list)
    released_decide: list[tuple[str, str]] = field(default_factory=list)
    created_sessions: list[tuple[str, dict]] = field(default_factory=list)
    appended_sessions: list[tuple[str, str, list[dict]]] = field(default_factory=list)
    finished_sessions: list[tuple[str, str, dict]] = field(default_factory=list)
    session_heartbeats: list[tuple[str, str]] = field(default_factory=list)
    conclude_status: int = 200
    conclude_fact_ids: list[str] = field(default_factory=lambda: ["f002"])
    context_summary: ContextSummary = field(default_factory=ContextSummary)
    saved_summaries: list[ContextSummary] = field(default_factory=list)
    recall_plan: RecallPlan = field(default_factory=RecallPlan)
    recall_hits: list[RecallHit] = field(default_factory=list)
    documents: dict[str, Document] = field(default_factory=dict)
    recalled: list[tuple[str, str]] = field(default_factory=list)
    fetched_documents: list[str] = field(default_factory=list)

    def get_context_summary(self, _project_id: str) -> ContextSummary:
        return self.context_summary

    def get_recall_plan(self, _project_id: str) -> RecallPlan:
        return self.recall_plan

    def save_recall_plan(
        self, _project_id: str, *, queries: list[str], doc_ids: list[str]
    ) -> ApiResult:
        self.recall_plan = RecallPlan(queries=list(queries), doc_ids=list(doc_ids))
        return ApiResult(200, self.recall_plan.model_dump())

    def recall(self, _project_id: str, query: str, *, limit: int = 5) -> RecallResponse:
        self.recalled.append((_project_id, query))
        hits = [
            hit
            for hit in self.recall_hits
            if query.lower() in (hit.title or hit.description).lower()
        ]
        return RecallResponse(query=query, facts=hits[:limit], steps=[])

    def get_document(self, _project_id: str, document_id: str) -> Document:
        self.fetched_documents.append(document_id)
        if document_id not in self.documents:
            raise ValueError(f"unknown document: {document_id}")
        return self.documents[document_id]

    def save_context_summary(self, _project_id: str, **fields) -> ApiResult:
        self.context_summary = ContextSummary(**fields)
        self.saved_summaries.append(self.context_summary)
        return ApiResult(200, self.context_summary.model_dump())

    def get_project(self, _project_id: str) -> ProjectDetail:
        return self.project

    def conclude_step(
        self,
        project_id: str,
        step_id: str,
        worker: str,
        *,
        facts: list[dict[str, Any]],
        findings: list[dict[str, str]],
        status: str = "done",
        note: str | None = None,
    ) -> ApiResult:
        normalized = [item if isinstance(item, dict) else {"description": item} for item in facts]
        self.concluded_details.append(normalized)
        self.concluded.append(
            (project_id, step_id, worker, [item["description"] for item in normalized], findings)
        )
        if self.conclude_status != 200:
            return ApiResult(self.conclude_status, text="inactive")
        return ApiResult(
            200,
            {
                "facts": [
                    {"id": fid, "description": description}
                    for fid, description in zip(self.conclude_fact_ids, facts, strict=False)
                ],
                "findings": findings,
                "step": {"id": step_id, "status": status},
            },
        )

    def complete(
        self, project_id: str, from_ids: list[str], description: str, worker: str
    ) -> ApiResult:
        self.completed.append((project_id, from_ids, description, worker))
        return ApiResult(200, {})

    def create_step(
        self,
        project_id: str,
        *,
        from_ids: list[str],
        description: str,
        creator: str,
        title: str | None = None,
        goal: str | None = None,
        asset: str | None = None,
        batch: str | None = None,
        rationale: str | None = None,
        priority: int = 0,
    ) -> ApiResult:
        self.created_steps.append((project_id, from_ids, description, creator))
        self.created_step_titles.append(title)
        self.created_step_assets.append(asset)
        self.created_step_batches.append(batch)
        return ApiResult(201, {})

    def update_step(
        self,
        project_id: str,
        step_id: str,
        *,
        priority: int | None = None,
        status: str | None = None,
        note: str | None = None,
    ) -> ApiResult:
        self.updated_steps.append((project_id, step_id, priority, status))
        return ApiResult(200, {})

    def create_goal(
        self, project_id: str, *, description: str, creator: str, parent: str | None = None
    ) -> ApiResult:
        self.created_goals.append((project_id, description, creator))
        return ApiResult(201, {})

    def close_goal(
        self, project_id: str, goal_id: str, *, status: str, worker: str, note: str | None = None
    ) -> ApiResult:
        self.closed_goals.append((project_id, goal_id, status))
        return ApiResult(200, {})

    def release_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        self.released.append((project_id, step_id, worker))
        return ApiResult(200, {})

    def release_decide(self, project_id: str, worker: str) -> ApiResult:
        self.released_decide.append((project_id, worker))
        return ApiResult(200, {})

    def heartbeat_step(self, _project_id: str, _step_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})

    def create_session(self, project_id: str, payload: dict) -> ApiResult:
        self.created_sessions.append((project_id, payload))
        return ApiResult(201, {"id": f"x{len(self.created_sessions):03d}"})

    def append_session_messages(
        self, project_id: str, session_id: str, messages: list[dict]
    ) -> ApiResult:
        self.appended_sessions.append((project_id, session_id, messages))
        return ApiResult(200, {})

    def finish_session(self, project_id: str, session_id: str, payload: dict) -> ApiResult:
        self.finished_sessions.append((project_id, session_id, payload))
        return ApiResult(200, {})

    def session_heartbeat(self, project_id: str, session_id: str) -> ApiResult:
        self.session_heartbeats.append((project_id, session_id))
        return ApiResult(200, {})

    def decide_heartbeat(self, _project_id: str, _worker: str) -> ApiResult:
        return ApiResult(200, {})


class FakeDriver:
    def __init__(self) -> None:
        self.execute_prompts: list[str] = []
        self.conclude_prompts: list[str] = []
        self.health = HealthResult(ok=True, status=200, detail="")

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str:
        return "session-001"

    def extract_usage(self, _stdout: str, _stderr: str) -> Usage | None:
        return Usage(input_tokens=10, output_tokens=5)

    def check_health(self, _worker, *, timeout: float) -> HealthResult:
        return self.health

    def build_execute(self, _worker, prompt: str, session: str | None) -> DriverResult:
        self.execute_prompts.append(prompt)
        return DriverResult(["execute"], session=session)

    def build_conclude(self, _worker, prompt: str, _session: str) -> list[str]:
        self.conclude_prompts.append(prompt)
        return ["conclude"]

    def extract_session(self, session: str | None, _stdout: str, _stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, _stderr: str) -> str:
        return stdout

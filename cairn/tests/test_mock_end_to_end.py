from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from conftest import bare_loop
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks.summary import SummaryRequest
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import (
    ContextSummary,
    Document,
    ProjectDetail,
    ProjectSummary,
    Question,
    RecallPlan,
    RecallResponse,
    Settings,
)


class InProcessClient:
    def __init__(self, http: TestClient):
        self.http = http
        self._summaries = TypeAdapter(list[ProjectSummary])

    def close(self) -> None:
        return None

    def list_projects(self) -> list[ProjectSummary]:
        response = self.http.get("/projects")
        response.raise_for_status()
        return self._summaries.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self.http.get(f"/projects/{project_id}")
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self.http.get("/settings")
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self.http.get(f"/projects/{project_id}/export?format=yaml")
        response.raise_for_status()
        return response.text

    def heartbeat_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/steps/{step_id}/heartbeat", {"worker": worker})

    def release_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/steps/{step_id}/release", {"worker": worker})

    def conclude_step(
        self,
        project_id: str,
        step_id: str,
        worker: str,
        *,
        facts: list[Any],
        findings: list[dict[str, str]],
        status: str = "done",
        note: str | None = None,
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/steps/{step_id}/conclude",
            {
                "worker": worker,
                "status": status,
                "facts": [
                    fact if isinstance(fact, dict) else {"description": fact} for fact in facts
                ],
                "findings": findings,
                "note": note,
            },
        )

    def claim_decide(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/decide/claim", {"worker": worker, "trigger": trigger}
        )

    def decide_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/decide/heartbeat", {"worker": worker})

    def release_decide(self, project_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/decide/release", {"worker": worker})

    def complete(
        self, project_id: str, from_ids: list[str], description: str, worker: str
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/complete",
            {"from": from_ids, "description": description, "worker": worker},
        )

    def create_step(
        self,
        project_id: str,
        *,
        from_ids: list[str],
        description: str,
        creator: str,
        title: str | None = None,
        goal: str | None = None,
        rationale: str | None = None,
        priority: int = 0,
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/steps",
            {
                "from": from_ids,
                "description": description,
                "title": title,
                "creator": creator,
                "goal": goal,
                "rationale": rationale,
                "priority": priority,
                "worker": None,
            },
        )

    def update_step(
        self,
        project_id: str,
        step_id: str,
        *,
        priority: int | None = None,
        status: str | None = None,
        note: str | None = None,
    ) -> ApiResult:
        payload: dict[str, Any] = {}
        if priority is not None:
            payload["priority"] = priority
        if status is not None:
            payload["status"] = status
        if note is not None:
            payload["note"] = note
        return self._post(f"/projects/{project_id}/steps/{step_id}/update", payload)

    def create_goal(
        self, project_id: str, *, description: str, creator: str, parent: str | None = None
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/goals",
            {"description": description, "creator": creator, "parent": parent},
        )

    def close_goal(
        self, project_id: str, goal_id: str, *, status: str, worker: str, note: str | None = None
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/goals/{goal_id}/close",
            {"status": status, "worker": worker, "note": note},
        )

    def create_session(self, project_id: str, payload: dict[str, Any]) -> ApiResult:
        return self._post(f"/projects/{project_id}/sessions", payload)

    def append_session_messages(
        self, project_id: str, session_id: str, messages: list[dict[str, Any]]
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/sessions/{session_id}/messages", {"messages": messages}
        )

    def finish_session(
        self, project_id: str, session_id: str, payload: dict[str, Any]
    ) -> ApiResult:
        response = self.http.patch(f"/projects/{project_id}/sessions/{session_id}", json=payload)
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        return ApiResult(response.status_code, data, response.text)

    def session_heartbeat(self, project_id: str, session_id: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/sessions/{session_id}/heartbeat", {})

    def list_pending_questions(self, limit: int = 5) -> list[Question]:
        response = self.http.get("/questions/pending", params={"limit": limit})
        response.raise_for_status()
        return TypeAdapter(list[Question]).validate_python(response.json())

    def get_question(self, project_id: str, question_id: str) -> Question:
        response = self.http.get(f"/projects/{project_id}/questions/{question_id}")
        response.raise_for_status()
        return Question.model_validate(response.json())

    def claim_question(self, project_id: str, question_id: str, worker: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/questions/{question_id}/claim", {"worker": worker}
        )

    def answer_question(
        self,
        project_id: str,
        question_id: str,
        *,
        status: str,
        answer: str | None = None,
        error: str | None = None,
        worker: str | None = None,
        duration_ms: int | None = None,
    ) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/questions/{question_id}/answer",
            {
                "status": status,
                "answer": answer,
                "error": error,
                "worker": worker,
                "duration_ms": duration_ms,
            },
        )

    def get_context_summary(self, project_id: str) -> ContextSummary:
        response = self.http.get(f"/projects/{project_id}/context")
        response.raise_for_status()
        return ContextSummary.model_validate(response.json())

    def save_context_summary(
        self,
        project_id: str,
        *,
        summary: str = "",
        facts_covered: int = 0,
        step_summary: str = "",
        steps_covered: int = 0,
        finding_summary: str = "",
        findings_covered: int = 0,
    ) -> ApiResult:
        response = self.http.put(
            f"/projects/{project_id}/context",
            json={
                "summary": summary,
                "facts_covered": facts_covered,
                "step_summary": step_summary,
                "steps_covered": steps_covered,
                "finding_summary": finding_summary,
                "findings_covered": findings_covered,
            },
        )
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        return ApiResult(response.status_code, data, response.text)

    def get_recall_plan(self, project_id: str) -> RecallPlan:
        response = self.http.get(f"/projects/{project_id}/recall-plan")
        response.raise_for_status()
        return RecallPlan.model_validate(response.json())

    def save_recall_plan(
        self, project_id: str, *, queries: list[str], doc_ids: list[str]
    ) -> ApiResult:
        response = self.http.put(
            f"/projects/{project_id}/recall-plan",
            json={"queries": queries, "doc_ids": doc_ids},
        )
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        return ApiResult(response.status_code, data, response.text)

    def recall(self, project_id: str, query: str, *, limit: int = 5) -> RecallResponse:
        response = self.http.get(
            f"/projects/{project_id}/recall", params={"q": query, "limit": limit}
        )
        response.raise_for_status()
        return RecallResponse.model_validate(response.json())

    def get_document(self, project_id: str, document_id: str) -> Document:
        response = self.http.get(f"/projects/{project_id}/documents/{document_id}")
        response.raise_for_status()
        return Document.model_validate(response.json())

    def _post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        response = self.http.post(path, json=payload)
        data = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        return ApiResult(response.status_code, data, response.text)


class LocalProcess:
    def __init__(self, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._process: subprocess.Popen[str] | None = None
        self._cancel_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._process = subprocess.Popen(
                self.command,
                env={**os.environ, **self.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        timed_out = False
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill()
            stdout, stderr = self._process.communicate()
        return ProcessResult(
            returncode=self._process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()


class LocalContainerManager:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return f"local-{project_id}"

    def ensure_running(self, project_id: str) -> str:
        return self.container_name(project_id)

    def build_exec_process(
        self,
        _container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        assert timeout_seconds is not None
        assert kill_after_seconds == 5
        return LocalProcess(command, env)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))

    def needs_completed_cleanup(self, _project_id: str) -> bool:
        return False

    def needs_stopped_cleanup(self, _project_id: str) -> bool:
        return False


@pytest.fixture
def http_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


def _phase(
    outcome: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    zero_outcomes: list[str] | None = None,
) -> str:
    outcomes = dict.fromkeys(zero_outcomes or [], 0)
    outcomes[outcome] = 1
    payload: dict[str, Any] = {"delay": [0, 0], "outcomes": outcomes}
    if rules is not None:
        payload["rules"] = rules
    return json.dumps(payload)


def _config(
    *,
    decide: str,
    execute: str,
    task_types: list[str] | None = None,
    worker_healthcheck: str = "startup_only",
    healthcheck: str | None = None,
    question: str | None = None,
) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": worker_healthcheck,
                "prompt_group": "mock",
            },
            "tasks": {
                "decide": {"timeout": 2, "max_steps": 1},
                "execute": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {
                "image": "unused",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "mock-worker",
                    "type": "mock",
                    "task_types": task_types or ["decide", "execute"],
                    "max_running": 1,
                    "priority": 0,
                    "env": {
                        "MOCK_HEALTHCHECK": healthcheck or _phase("ok"),
                        "MOCK_DECIDE": decide,
                        "MOCK_EXECUTE": execute,
                        "MOCK_EXECUTE_CONCLUDE": execute,
                        "MOCK_QUESTION": question or _phase("answer"),
                    },
                }
            ],
        }
    )


def _loop(
    config: DispatchConfig, client: InProcessClient, containers: LocalContainerManager
) -> DispatcherLoop:
    loop = bare_loop()
    loop.config = config
    loop.client = client
    loop.container_manager = containers
    loop.executor = ThreadPoolExecutor(max_workers=config.runtime.max_workers)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.question_executor = ThreadPoolExecutor(max_workers=config.runtime.max_questions)
    loop.summary_executor = ThreadPoolExecutor(max_workers=config.runtime.max_summaries)
    return loop


def _dispatch_and_wait(loop: DispatcherLoop) -> None:
    loop._reap_futures()
    summaries = loop.client.list_projects()
    loop._initialize_decide_checkpoints(summaries)
    loop._refresh_runtime_projects(summaries)
    loop._cancel_inactive_tasks(summaries)
    loop._queue_container_cleanups(summaries)
    loop._reap_summary_futures()
    loop._dispatch_questions()
    loop._dispatch_available(summaries)
    assert loop.futures
    for future in list(loop.futures):
        future.result(timeout=5)
    loop._reap_futures()


def _create_project(http: TestClient) -> str:
    response = http.post(
        "/projects",
        json={"title": "integration", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_mock_decide_completes_project_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            decide=_phase("complete", zero_outcomes=["steps"]),
            execute=_phase("facts"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin"]
    assert project.goals[0].status == "achieved"
    assert [step.kind for step in project.steps] == ["completion"]
    assert any("/decide-" in path for _, path, _ in containers.writes)


def test_mock_decide_execute_decide_complete_chain(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            decide=_phase("steps", rules=[{"fact_ids_gte": 2, "force": "complete"}]),
            execute=_phase("facts"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)  # decide: one step from origin
        project = client.get_project(project_id)
        assert project.project.status == "active"
        assert [(step.id, step.status) for step in project.steps] == [("s001", "pending")]

        _dispatch_and_wait(loop)  # execute: fact + finding
        project = client.get_project(project_id)
        assert [fact.id for fact in project.facts] == ["origin", "f001"]
        assert [(finding.kind, finding.severity) for finding in project.findings] == [
            ("mock", "medium")
        ]

        _dispatch_and_wait(loop)  # decide: facts grew -> complete
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [step.kind for step in project.steps] == ["action", "completion"]
    assert any("/decide-" in path and "s001" in content for _, path, content in containers.writes)
    assert any("/execute-" in path for _, path, _ in containers.writes)

    # The mock execute run appends a usage event; it must land in the project totals.
    usage = http_client.get(f"/projects/{project_id}/usage").json()
    assert usage["by_phase"]["execute"]["total_tokens"] == 205
    assert usage["by_phase"]["execute"]["input_tokens"] == 120
    assert usage["total_tokens"] >= 205


def test_mock_execute_only_worker_keeps_project_active(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            decide=_phase("steps"),
            execute=_phase("facts"),
            task_types=["execute"],
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        summaries = client.list_projects()
        loop._dispatch_available(summaries)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "active"
    assert project.steps == []
    assert not loop.futures


def test_task_healthcheck_failure_aborts_decide_and_cools_down_worker(
    http_client: TestClient,
) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            decide=_phase("complete", zero_outcomes=["steps"]),
            execute=_phase("facts"),
            worker_healthcheck="startup_and_task",
            healthcheck=_phase("fail", zero_outcomes=["ok"]),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "active"
    assert [fact.id for fact in project.facts] == ["origin"]
    assert "mock-worker" in loop.worker_unhealthy_until


def test_task_healthcheck_healthy_worker_completes_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            decide=_phase("complete", zero_outcomes=["steps"]),
            execute=_phase("facts"),
            worker_healthcheck="startup_and_task",
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"


def _failover_config() -> DispatchConfig:
    def worker(name: str, priority: int, healthcheck: str) -> dict:
        return {
            "name": name,
            "type": "mock",
            "task_types": ["decide", "execute"],
            "max_running": 1,
            "priority": priority,
            "env": {
                "MOCK_HEALTHCHECK": healthcheck,
                "MOCK_DECIDE": _phase("complete", zero_outcomes=["steps"]),
                "MOCK_EXECUTE": _phase("facts"),
                "MOCK_EXECUTE_CONCLUDE": _phase("facts"),
            },
        }

    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": "startup_and_task",
                "prompt_group": "mock",
            },
            "tasks": {
                "decide": {"timeout": 2, "max_steps": 1},
                "execute": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {"image": "unused", "network_mode": "host", "completed_action": "stop"},
            "workers": [
                worker("bad", 0, _phase("fail", zero_outcomes=["ok"])),
                worker("good", 1, _phase("ok")),
            ],
        }
    )


def test_unhealthy_worker_fails_over_to_healthy_worker(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(_failover_config(), client, containers)
    project_id = _create_project(http_client)

    try:
        # round 1: 'bad' (priority 0) is chosen first, its health check fails -> cooldown
        _dispatch_and_wait(loop)
        assert "bad" in loop.worker_unhealthy_until
        assert client.get_project(project_id).project.status == "active"

        # round 2: 'bad' still cooling down -> 'good' takes over and completes the project
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"


def test_decide_checkpoint_tracks_graph_after_decide_run(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(decide=_phase("steps"), execute=_phase("facts")),
        client,
        LocalContainerManager(),
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        checkpoint = loop.decide_checkpoints[project_id]
        summary = client.list_projects()[0]
    finally:
        loop.close()

    assert checkpoint.open_step_count == summary.pending_step_count == 1
    assert checkpoint.fact_count == summary.fact_count


def test_mock_question_is_answered_without_touching_the_graph(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(decide=_phase("noop", zero_outcomes=["steps"]), execute=_phase("facts")),
        client,
        LocalContainerManager(),
    )
    project_id = _create_project(http_client)
    question = http_client.post(
        f"/projects/{project_id}/questions", json={"content": "what is this CVE?"}
    ).json()

    try:
        loop._dispatch_questions()
        assert loop.question_futures
        for future in list(loop.question_futures):
            future.result(timeout=5)
        loop._reap_question_futures()
    finally:
        loop.close()

    answered = http_client.get(f"/projects/{project_id}/questions/{question['id']}").json()
    assert answered["status"] == "answered"
    assert answered["answer"] == "mock answer: derived from the current graph snapshot"
    assert answered["worker"] == "mock-worker"
    assert answered["duration_ms"] is not None

    # A side answer is read-only: no facts, no steps, no hints were created.
    project = client.get_project(project_id)
    assert [fact.id for fact in project.facts] == ["origin"]
    assert project.steps == []
    assert http_client.get("/questions/pending").json() == []


def test_background_summary_runs_off_the_decide_path(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    loop = _loop(
        _config(decide=_phase("noop", zero_outcomes=["steps"]), execute=_phase("facts")),
        client,
        LocalContainerManager(),
    )
    project_id = _create_project(http_client)
    request = SummaryRequest(
        project_id=project_id,
        worker="mock-worker",
        facts=[{"id": "f001", "description": "db reachable"}],
        facts_covered=1,
        steps=[
            {
                "id": "s001",
                "description": "sqlmap /login",
                "status": "done",
                "result": "no injection",
            }
        ],
        steps_covered=1,
        findings=[{"id": "d001", "description": "openssh 8.2"}],
        findings_covered=1,
    )

    try:
        assert loop.request_summary(request) is True
        # A second Decide for the same project must not queue a duplicate.
        assert loop.request_summary(request) is False
        for future in list(loop.summary_futures):
            future.result(timeout=10)
        loop._reap_summary_futures()
    finally:
        loop.close()

    assert loop.summary_inflight == set()
    stored = client.get_context_summary(project_id)
    assert stored.summary == "- mock fact summary (f001)"
    assert stored.facts_covered == 1
    assert stored.step_summary == "- mock step summary (s001)"
    assert stored.steps_covered == 1
    assert stored.finding_summary == "- mock finding summary (d001)"
    assert stored.findings_covered == 1

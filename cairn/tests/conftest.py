from __future__ import annotations

from dataclasses import dataclass, field

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.workers.base import DriverResult
from cairn.dispatcher.workers.health import HealthResult
from cairn.server.models import Fact, Finding, Goal, Hint, ProjectDetail, ProjectMeta, Step


def make_config() -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "http://127.0.0.1:8000",
            "runtime": {
                "interval": 60,
                "max_workers": 2,
                "max_running_projects": 1,
                "max_project_workers": 2,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
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


def make_step(step_id: str = "s001", *, status: str = "pending", worker: str | None = None) -> Step:
    return Step(
        id=step_id,
        from_=["f001"],
        description="investigate",
        goal="g001",
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
    completed: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    created_steps: list[tuple[str, list[str], str, str]] = field(default_factory=list)
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

    def get_project(self, _project_id: str) -> ProjectDetail:
        return self.project

    def conclude_step(
        self,
        project_id: str,
        step_id: str,
        worker: str,
        *,
        facts: list[str],
        findings: list[dict[str, str]],
        status: str = "done",
        note: str | None = None,
    ) -> ApiResult:
        self.concluded.append((project_id, step_id, worker, facts, findings))
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

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        self.completed.append((project_id, from_ids, description, worker))
        return ApiResult(200, {})

    def create_step(
        self,
        project_id: str,
        *,
        from_ids: list[str],
        description: str,
        creator: str,
        goal: str | None = None,
        rationale: str | None = None,
        priority: int = 0,
    ) -> ApiResult:
        self.created_steps.append((project_id, from_ids, description, creator))
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

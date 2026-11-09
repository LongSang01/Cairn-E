"""Dispatch gating and lost-claim handling for Decide / Execute."""

from __future__ import annotations

from types import SimpleNamespace

from conftest import bare_loop, make_config, make_project, make_step

from cairn.contracts.models import ProjectSummary
from cairn.dispatcher.models import DecideCheckpoint
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop


class _Client:
    def __init__(self, project, *, claim_status: int = 200, execute_status: int = 200) -> None:
        self.project = project
        self.claim_status = claim_status
        self.execute_status = execute_status
        self.claimed_decide: list[tuple[str, str, str]] = []
        self.claimed_steps: list[tuple[str, str, str]] = []
        self.released_decide: list[tuple[str, str]] = []
        self.released_steps: list[tuple[str, str, str]] = []

    def get_project(self, _project_id: str):
        return self.project

    def export_project(self, _project_id: str) -> str:
        return "graph"

    def claim_decide(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        self.claimed_decide.append((project_id, worker, trigger))
        return ApiResult(self.claim_status, {})

    def heartbeat_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        self.claimed_steps.append((project_id, step_id, worker))
        return ApiResult(self.execute_status, {})

    def release_decide(self, project_id: str, worker: str) -> ApiResult:
        self.released_decide.append((project_id, worker))
        return ApiResult(200, {})

    def release_step(self, project_id: str, step_id: str, worker: str) -> ApiResult:
        self.released_steps.append((project_id, step_id, worker))
        return ApiResult(200, {})


class _BrokenPool:
    def submit(self, *_args, **_kwargs):
        raise RuntimeError("executor is shut down")


def _summary(project_id: str, status: str = "active") -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        created_at="2026-01-01T00:00:00Z",
        fact_count=2,
        goal_count=1,
        open_goal_count=1,
        step_count=0,
        running_step_count=0,
        pending_step_count=0,
        finding_count=0,
        hint_count=0,
    )


def _loop(project=None, client=None, *, runtime: dict | None = None) -> DispatcherLoop:
    loop = bare_loop()
    loop.config = make_config(runtime)
    loop.container_manager = SimpleNamespace(container_name=lambda project_id: project_id)
    loop.client = client or _Client(project or make_project())
    return loop


def _forbid_try_dispatch(loop: DispatcherLoop) -> None:
    def boom(_summary: ProjectSummary) -> bool:
        raise AssertionError("no project should be dispatched")

    loop._try_dispatch_project = boom


def test_dispatch_stops_when_the_global_capacity_is_full() -> None:
    loop = _loop(runtime={"max_workers": 1, "max_project_workers": 1})
    loop.futures[object()] = SimpleNamespace()  # only the length matters here
    _forbid_try_dispatch(loop)

    loop._dispatch_available([_summary("proj_001")])

    assert "dispatch/global" in loop._log_state


def test_dispatch_skips_when_no_project_is_active() -> None:
    loop = _loop()
    _forbid_try_dispatch(loop)

    loop._dispatch_available([_summary("proj_001", "completed")])

    assert "no active projects" in loop._log_state["dispatch/global"][1]


def test_idle_dispatch_stops_at_the_running_project_limit() -> None:
    loop = _loop(runtime={"max_running_projects": 1})
    loop.runtime_project_ids = {"proj_001"}
    loop._try_dispatch_project = lambda _summary: False

    loop._dispatch_available([_summary("proj_001"), _summary("proj_002")])

    assert "dispatch/idle-limit" in loop._log_state


def test_decide_is_not_dispatched_without_a_capable_worker() -> None:
    project = make_project()
    loop = _loop(project)
    loop.config.workers[0].task_types = ["execute"]

    assert loop._dispatch_decide(project, "graph", "initial") is False
    assert "project:proj_001:worker:decide" in loop._log_state


def test_a_lost_decide_claim_is_logged_and_not_submitted() -> None:
    project = make_project()
    client = _Client(project, claim_status=409)
    loop = _loop(project, client)

    assert loop._dispatch_decide(project, "graph", "initial") is False
    assert client.claimed_decide == [("proj_001", "test-worker", "initial")]
    assert loop.futures == {}


def test_a_forbidden_decide_claim_is_only_informational() -> None:
    project = make_project()
    client = _Client(project, claim_status=403)
    loop = _loop(project, client)

    assert loop._dispatch_decide(project, "graph", "initial") is False
    assert loop.futures == {}


def test_a_failed_decide_submit_releases_the_claim() -> None:
    project = make_project()
    client = _Client(project)
    loop = _loop(project, client)
    loop.executor = _BrokenPool()

    assert loop._dispatch_decide(project, "graph", "initial") is False
    assert client.released_decide == [("proj_001", "test-worker")]
    assert loop.futures == {}


def test_a_lost_execute_claim_is_not_submitted() -> None:
    project = make_project(steps=[make_step()])
    client = _Client(project, execute_status=409)
    loop = _loop(project, client)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )

    assert loop._try_dispatch_project(_summary("proj_001")) is False
    assert client.claimed_steps == [("proj_001", "s001", "test-worker")]
    assert loop.futures == {}


def test_a_failed_execute_submit_releases_the_step() -> None:
    project = make_project(steps=[make_step()])
    client = _Client(project)
    loop = _loop(project, client)
    loop.executor = _BrokenPool()
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )

    assert loop._try_dispatch_project(_summary("proj_001")) is False
    assert client.released_steps == [("proj_001", "s001", "test-worker")]
    assert loop.futures == {}

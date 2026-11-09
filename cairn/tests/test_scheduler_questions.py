"""Side questions, background summaries and container cleanup passes."""

from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace

from conftest import bare_loop, make_config, make_project

from cairn.contracts.models import ProjectSummary, Question
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.tasks.summary import SummaryRequest


class _RecordingPool:
    def __init__(self) -> None:
        self.submitted: list[tuple[object, tuple, dict]] = []
        self.error: Exception | None = None

    def submit(self, fn, *args, **kwargs) -> Future:
        self.submitted.append((fn, args, kwargs))
        if self.error is not None:
            raise self.error
        return Future()


class _QuestionClient:
    def __init__(self, questions: list[Question], *, claim_ok: bool = True) -> None:
        self.questions = questions
        self.claim_ok = claim_ok
        self.project = make_project()
        self.claimed: list[tuple[str, str, str]] = []
        self.answered: list[tuple[str, str, str, str | None]] = []
        self.export_calls = 0
        self.export_error: Exception | None = None
        self.list_calls = 0

    def list_pending_questions(self, limit: int = 5) -> list[Question]:
        self.list_calls += 1
        return self.questions[:limit]

    def get_project(self, _project_id: str):
        return self.project

    def claim_question(self, project_id: str, question_id: str, worker: str) -> ApiResult:
        self.claimed.append((project_id, question_id, worker))
        return ApiResult(200 if self.claim_ok else 409, {})

    def export_project(self, _project_id: str) -> str:
        self.export_calls += 1
        if self.export_error is not None:
            raise self.export_error
        return "graph"

    def answer_question(
        self,
        project_id: str,
        question_id: str,
        *,
        status: str,
        error: str | None = None,
        worker: str | None = None,
    ) -> ApiResult:
        self.answered.append((project_id, question_id, status, error))
        return ApiResult(200, {})


class _CleanupContainers:
    def __init__(self, *, needs_completed: bool = True, needs_stopped: bool = True) -> None:
        self.needs_completed = needs_completed
        self.needs_stopped = needs_stopped
        self.cleaned: list[tuple[str, str]] = []

    def container_name(self, project_id: str) -> str:
        return f"container-{project_id}"

    def needs_completed_cleanup(self, _project_id: str) -> bool:
        return self.needs_completed

    def needs_stopped_cleanup(self, _project_id: str) -> bool:
        return self.needs_stopped

    def cleanup_completed(self, project_id: str) -> bool:
        self.cleaned.append(("completed", project_id))
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        self.cleaned.append(("stopped", project_id))
        return True


def _question_loop(questions: list[Question], **kwargs) -> tuple[DispatcherLoop, _QuestionClient]:
    loop = bare_loop()
    loop.config = make_config({"max_questions": 1, "max_summaries": 1})
    client = _QuestionClient(questions, **kwargs)
    loop.client = client
    loop.container_manager = SimpleNamespace()
    loop.question_executor = _RecordingPool()
    loop.summary_executor = _RecordingPool()
    return loop, client


def _question(question_id: str = "q001", project_id: str = "proj_001") -> Question:
    return Question(
        id=question_id,
        project_id=project_id,
        content="what is known?",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
    )


def test_a_pending_question_is_claimed_and_submitted() -> None:
    loop, client = _question_loop([_question()])

    loop._dispatch_questions()

    assert client.claimed == [("proj_001", "q001", "test-worker")]
    assert len(loop.question_executor.submitted) == 1
    assert len(loop.question_futures) == 1


def test_question_dispatch_stops_at_the_configured_concurrency() -> None:
    loop, client = _question_loop([_question()])
    loop.question_futures[Future()] = "proj_001/q000"

    loop._dispatch_questions()

    assert client.list_calls == 0
    assert client.claimed == []


def test_a_completed_project_question_is_never_answered() -> None:
    loop, client = _question_loop([_question()])
    client.project.project.status = "completed"

    loop._dispatch_questions()

    assert client.claimed == []
    assert loop.question_executor.submitted == []


def test_a_lost_question_claim_is_skipped() -> None:
    loop, client = _question_loop([_question()], claim_ok=False)

    loop._dispatch_questions()

    assert client.claimed == [("proj_001", "q001", "test-worker")]
    assert loop.question_executor.submitted == []


def test_an_unreadable_project_does_not_stop_the_question_pass() -> None:
    loop, client = _question_loop([_question("q001"), _question("q002")])
    loop.config.runtime.max_questions = 2
    original = client.get_project
    calls = {"count": 0}

    def get_project(project_id: str):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("project gone")
        return original(project_id)

    client.get_project = get_project

    loop._dispatch_questions()

    assert len(loop.question_executor.submitted) == 1
    assert client.claimed == [("proj_001", "q002", "test-worker")]


def test_a_missing_snapshot_fails_the_question_instead_of_running_it() -> None:
    loop, client = _question_loop([_question()])
    client.export_error = ValueError("snapshot unavailable")

    loop._dispatch_questions()

    assert loop.question_executor.submitted == []
    assert client.answered == [("proj_001", "q001", "failed", "could not read the graph snapshot")]


def test_a_failed_question_submit_does_not_stop_the_pass() -> None:
    loop, client = _question_loop([_question()])
    loop.question_executor.error = RuntimeError("pool closed")

    loop._dispatch_questions()

    assert client.claimed == [("proj_001", "q001", "test-worker")]
    assert loop.question_futures == {}


def test_questions_without_a_decide_capable_worker_are_left_pending() -> None:
    loop, client = _question_loop([_question()])
    loop.config.workers[0].task_types = ["execute"]

    loop._dispatch_questions()

    assert client.claimed == []
    assert "questions/worker" in loop._log_state


def test_no_pending_questions_is_logged_once() -> None:
    loop, _client = _question_loop([])

    loop._dispatch_questions()
    loop._dispatch_questions()

    state = loop._log_state["questions/idle"]
    assert "no pending side questions" in state[1]


def test_reaping_a_question_reports_a_non_success_outcome(caplog) -> None:
    loop, _ = _question_loop([])
    future: Future = Future()
    future.set_result("failed")
    loop.question_futures[future] = "proj_001/q001"

    with caplog.at_level("INFO"):
        loop._reap_question_futures()

    assert loop.question_futures == {}
    assert "question finished key=proj_001/q001 outcome=failed" in caplog.text


def test_reaping_a_crashing_question_keeps_the_loop_alive(caplog) -> None:
    loop, _ = _question_loop([])
    future: Future = Future()
    future.set_exception(RuntimeError("worker exploded"))
    loop.question_futures[future] = "proj_001/q001"

    with caplog.at_level("ERROR"):
        loop._reap_question_futures()

    assert loop.question_futures == {}
    assert "question task crashed key=proj_001/q001" in caplog.text


def _summary_request(**overrides) -> SummaryRequest:
    fields = {
        "project_id": "proj_001",
        "worker": "test-worker",
        "facts": [{"id": "f001"}],
    }
    fields.update(overrides)
    return SummaryRequest(**fields)


def test_an_empty_summary_request_is_rejected() -> None:
    loop, _ = _question_loop([])

    assert (
        loop.request_summary(SummaryRequest(project_id="proj_001", worker="test-worker")) is False
    )


def test_a_summary_for_a_removed_worker_is_rejected(caplog) -> None:
    loop, _ = _question_loop([])

    with caplog.at_level("WARNING"):
        accepted = loop.request_summary(_summary_request(worker="gone"))

    assert accepted is False
    assert "summary worker is gone worker=gone project=proj_001" in caplog.text


def test_only_one_summary_per_project_is_in_flight() -> None:
    loop, _ = _question_loop([])

    assert loop.request_summary(_summary_request()) is True
    assert loop.request_summary(_summary_request()) is False
    assert loop.summary_inflight == {"proj_001"}
    assert len(loop.summary_executor.submitted) == 1


def test_a_failed_summary_submit_releases_the_project() -> None:
    loop, _ = _question_loop([])
    loop.summary_executor.error = RuntimeError("pool closed")

    assert loop.request_summary(_summary_request()) is False
    assert loop.summary_inflight == set()


def test_reaping_a_summary_clears_the_in_flight_marker() -> None:
    loop, _ = _question_loop([])
    future: Future = Future()
    future.set_result("facts folded")
    loop.summary_futures[future] = "proj_001"
    loop.summary_inflight.add("proj_001")

    loop._reap_summary_futures()

    assert loop.summary_inflight == set()
    assert loop.summary_futures == {}
    assert "summary:proj_001" in loop._log_state


def test_reaping_a_crashing_summary_clears_the_in_flight_marker(caplog) -> None:
    loop, _ = _question_loop([])
    future: Future = Future()
    future.set_exception(RuntimeError("summary exploded"))
    loop.summary_futures[future] = "proj_001"
    loop.summary_inflight.add("proj_001")

    with caplog.at_level("ERROR"):
        loop._reap_summary_futures()

    assert loop.summary_inflight == set()
    assert "summary task crashed project=proj_001" in caplog.text


def _summary_summary(project_id: str, status: str) -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        created_at="2026-01-01T00:00:00Z",
        fact_count=0,
        goal_count=0,
        open_goal_count=0,
        step_count=0,
        running_step_count=0,
        pending_step_count=0,
        finding_count=0,
        hint_count=0,
    )


def _cleanup_loop(containers: _CleanupContainers) -> tuple[DispatcherLoop, _RecordingPool]:
    loop = bare_loop()
    loop.container_manager = containers
    loop.cleanup_executor = _RecordingPool()
    return loop, loop.cleanup_executor


def test_completed_and_stopped_containers_are_queued_for_cleanup() -> None:
    containers = _CleanupContainers()
    loop, pool = _cleanup_loop(containers)

    loop._queue_container_cleanups(
        [_summary_summary("proj_done", "completed"), _summary_summary("proj_stop", "stopped")]
    )

    assert [args for _fn, args, _kwargs in pool.submitted] == [("proj_done",), ("proj_stop",)]
    assert loop._cleanup_pending == {"container-proj_done", "container-proj_stop"}
    assert set(loop.cleanup_futures.values()) == {
        ("container-proj_done", "proj_done", "completed"),
        ("container-proj_stop", "proj_stop", "stopped"),
    }


def test_containers_that_need_no_cleanup_are_marked_done_without_submitting() -> None:
    containers = _CleanupContainers(needs_completed=False, needs_stopped=False)
    loop, pool = _cleanup_loop(containers)

    loop._queue_container_cleanups(
        [_summary_summary("proj_done", "completed"), _summary_summary("proj_stop", "stopped")]
    )

    assert pool.submitted == []
    assert loop._inactive_cleanup_done == {"proj_done": "completed", "proj_stop": "stopped"}


def test_an_active_project_is_never_cleaned_up() -> None:
    containers = _CleanupContainers()
    loop, pool = _cleanup_loop(containers)

    loop._queue_container_cleanups([_summary_summary("proj_active", "active")])

    assert pool.submitted == []
    assert loop._cleanup_pending == set()


def test_a_crashing_cleanup_is_logged_and_retried_next_tick(caplog) -> None:
    containers = _CleanupContainers()
    loop, _ = _cleanup_loop(containers)
    loop._queue_container_cleanups([_summary_summary("proj_done", "completed")])
    future = next(iter(loop.cleanup_futures))
    future.set_exception(RuntimeError("docker gone"))
    loop._inactive_cleanup_done["proj_done"] = "completed"

    with caplog.at_level("ERROR"):
        loop._reap_cleanup_futures()

    assert "container cleanup failed container=container-proj_done" in caplog.text
    assert "proj_done" not in loop._inactive_cleanup_done
    assert loop._cleanup_pending == set()

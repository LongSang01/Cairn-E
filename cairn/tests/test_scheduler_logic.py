from __future__ import annotations

import time
from concurrent.futures import Future

from conftest import FakeClient, bare_loop, make_config, make_project, make_step

from cairn.dispatcher.models import DecideCheckpoint, RunningTask, StepFailure
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.scheduler.loop._common import (
    DECIDE_RETRY_AFTER_SECONDS,
    MAX_CONSECUTIVE_DECIDE_FAILURES,
)
from cairn.dispatcher.scheduler.loop.dispatch import (
    MAX_STEP_EXECUTE_ATTEMPTS,
    MAX_UNPROMPTED_DECIDE_RUNS,
)
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.server.models import Fact, Finding, ProjectSummary


def _loop() -> DispatcherLoop:
    loop = bare_loop()
    loop.container_manager = type(
        "Containers", (), {"container_name": lambda _self, project_id: project_id}
    )()
    return loop


def _summary(project_id: str, status: str) -> ProjectSummary:
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


def _dispatchable_loop(project, dispatched: list[tuple[str, str]]) -> DispatcherLoop:
    """A loop that records Decide / Execute dispatches instead of running them."""
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    loop._dispatch_decide = lambda _project, _graph, trigger: (
        dispatched.append(("decide", trigger)) or True
    )
    loop._dispatch_execute = lambda _project, _graph, step: (
        dispatched.append(("execute", step.id)) or True
    )
    return loop


def test_decide_trigger_detects_new_facts_and_drained_steps() -> None:
    loop = _loop()
    project = make_project(steps=[make_step()])
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )
    project.facts.append(Fact(id="f002", description="new"))
    project.steps = []

    assert loop._decide_trigger(project) == "facts:2->3,open_steps:1->0"


def test_decide_trigger_detects_new_findings() -> None:
    loop = _loop()
    project = make_project()
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=0
    )
    project.findings.append(
        Finding(
            id="d001",
            kind="flaw",
            description="found",
            step="s001",
            creator="w",
            created_at="2026-01-01T00:00:00Z",
        )
    )

    assert loop._decide_trigger(project) == "findings:0->1"


def test_decide_trigger_returns_none_when_graph_is_unchanged() -> None:
    loop = _loop()
    project = make_project(steps=[make_step()])
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )

    assert loop._decide_trigger(project) is None


def test_decide_trigger_rewakes_a_terminal_graph_until_the_budget_is_spent() -> None:
    """Nothing left to run but the project is still active: Decide must still be asked.

    This is the proj_008 dead end: every step closed, every sub goal achieved, and no
    graph change left to trigger the Decide that declares completion.
    """
    loop = _loop()
    project = make_project()
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=0
    )

    assert loop._decide_trigger(project) == "idle:1"

    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=0, unprompted_attempts=3
    )
    assert loop._decide_trigger(project) is None


def test_refresh_decide_checkpoint_keeps_the_budget_only_while_nothing_changed() -> None:
    loop = _loop()
    loop.client = FakeClient(make_project())
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=0, unprompted_attempts=2
    )

    loop._refresh_decide_checkpoint("proj_001")
    assert loop.decide_checkpoints["proj_001"].unprompted_attempts == 2

    loop.client = FakeClient(make_project(steps=[make_step()]))
    loop._refresh_decide_checkpoint("proj_001")
    assert loop.decide_checkpoints["proj_001"].unprompted_attempts == 0


def test_refresh_runtime_projects_discards_active_and_changed_cleanup_markers() -> None:
    loop = _loop()
    loop.runtime_project_ids = {"active", "stopped", "deleted"}
    loop._inactive_cleanup_done = {
        "active": "stopped",
        "stopped": "stopped",
        "changed": "completed",
        "deleted": "completed",
    }

    loop._refresh_runtime_projects(
        [
            _summary("active", "active"),
            _summary("stopped", "stopped"),
            _summary("changed", "stopped"),
        ]
    )

    assert loop.runtime_project_ids == {"active"}
    assert loop._inactive_cleanup_done == {"stopped": "stopped"}


def test_reap_cleanup_future_records_only_successful_inactive_cleanup() -> None:
    loop = _loop()
    succeeded: Future[bool] = Future()
    failed: Future[bool] = Future()
    succeeded.set_result(True)
    failed.set_result(False)
    loop.cleanup_futures = {
        succeeded: ("container-success", "proj-success", "completed"),
        failed: ("container-failed", "proj-failed", "stopped"),
    }
    loop._cleanup_pending = {"container-success", "container-failed"}
    loop._inactive_cleanup_done = {"proj-failed": "stopped"}

    loop._reap_cleanup_futures()

    assert loop.cleanup_futures == {}
    assert loop._cleanup_pending == set()
    assert loop._inactive_cleanup_done == {"proj-success": "completed"}


def test_choose_worker_prefers_priority_then_lower_running_count() -> None:
    workers = make_config().workers
    first = workers[0].model_copy(update={"name": "first", "priority": 0})
    busy = workers[0].model_copy(update={"name": "busy", "priority": 0})
    lower_priority = workers[0].model_copy(update={"name": "lower", "priority": 1})

    ordered = choose_worker(
        [lower_priority, busy, first],
        {"busy": 2, "first": 0, "lower": 0},
    )

    assert [worker.name for worker in ordered] == ["first", "busy", "lower"]


def test_new_fact_dispatches_decide_before_pending_step() -> None:
    project = make_project(steps=[make_step()])
    project.facts.append(Fact(id="f002", description="new"))
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("decide", "facts:2->3")]


def test_initial_project_dispatches_decide() -> None:
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(make_project(), dispatched)

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("decide", "initial")]


def test_project_with_pending_step_and_unchanged_graph_dispatches_execute_by_priority() -> None:
    low = make_step("s001").model_copy(update={"priority": 1})
    high = make_step("s002").model_copy(update={"priority": 8})
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(make_project(steps=[low, high]), dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=2
    )

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("execute", "s002")]


def test_cancel_inactive_tasks_marks_stopped_and_deleted_projects() -> None:
    loop = _loop()
    stopped = TaskCancellation()
    deleted = TaskCancellation()
    loop.futures = {
        Future(): RunningTask("stopped", "execute", "worker", stopped),
        Future(): RunningTask("deleted", "decide", "worker", deleted),
    }

    loop._cancel_inactive_tasks([_summary("stopped", "stopped")])

    assert stopped.reason == "stopped"
    assert deleted.reason == "deleted"


def test_initialize_decide_checkpoint_only_for_active_projects_with_open_steps() -> None:
    loop = _loop()
    active = _summary("active", "active")
    active.pending_step_count = 1

    loop._initialize_decide_checkpoints(
        [
            active,
            _summary("idle", "active"),
            _summary("stopped", "stopped"),
        ]
    )

    assert loop.decide_checkpoints == {
        "active": DecideCheckpoint(fact_count=2, hint_count=0, finding_count=0, open_step_count=1)
    }


def test_select_worker_reports_busy_unhealthy_rejected_and_unsupported_workers(monkeypatch) -> None:
    loop = _loop()
    base = make_config()
    busy = base.workers[0].model_copy(update={"name": "busy", "task_types": ["decide"]})
    unhealthy = base.workers[0].model_copy(update={"name": "unhealthy", "task_types": ["decide"]})
    rejected = base.workers[0].model_copy(update={"name": "rejected", "task_types": ["decide"]})
    unsupported = base.workers[0].model_copy(
        update={"name": "unsupported", "task_types": ["execute"]}
    )
    loop.config = base.model_copy(update={"workers": [busy, unhealthy, rejected, unsupported]})
    loop.futures = {Future(): RunningTask("proj", "decide", "busy", TaskCancellation())}
    loop.worker_unhealthy_until = {"unhealthy": 110.0}
    loop.worker_rejected_until = {("proj", "decide", "rejected"): 120.0}
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.time", lambda: 100.0)

    selection = loop._select_worker("proj", "decide")

    assert selection.worker is None
    assert selection.blocked_busy == ["busy(1/1)"]
    assert selection.blocked_unhealthy == ["unhealthy(10.0s)"]
    assert selection.blocked_rejected == ["rejected(20.0s)"]
    assert selection.blocked_task_type == ["unsupported"]


def test_disabled_worker_healthcheck_skips_automatic_startup_but_force_runs_diagnostic() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"worker_healthcheck": "disabled"})}
    )
    calls: list[bool] = []
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(show_commands)
    loop._startup_healthchecks_checked = False

    loop.run_startup_healthchecks()

    assert calls == []
    assert loop._startup_healthchecks_checked

    loop._startup_healthchecks_checked = False
    loop.run_startup_healthchecks(show_commands=True, force=True)

    assert calls == [True]


def test_startup_only_worker_healthcheck_runs_automatic_startup_check() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={"runtime": config.runtime.model_copy(update={"worker_healthcheck": "startup_only"})}
    )
    calls: list[bool] = []
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(show_commands)
    loop._startup_healthchecks_checked = False

    loop.run_startup_healthchecks()

    assert calls == [False]


def _reap_decide(loop: DispatcherLoop, result: str | Exception, trigger: str = "initial") -> None:
    future: Future = Future()
    if isinstance(result, Exception):
        future.set_exception(result)
    else:
        future.set_result(result)
    loop.futures[future] = RunningTask(
        project_id="proj_001",
        task_type="decide",
        worker_name="test-worker",
        cancellation=TaskCancellation(),
        trigger=trigger,
    )
    loop._reap_futures()


def test_failed_decide_cools_down_before_the_same_trigger_retries() -> None:
    """A failed Decide leaves the checkpoint untouched, so retry has to cool down.

    proj_009: every noop Decide crashed, the checkpoint never advanced, and the same
    trigger was dispatched again on the next tick — 71 runs, seconds apart.
    """
    loop = _loop()
    loop.futures = {}

    before = time.time()
    _reap_decide(loop, "failed")

    assert loop.decide_retry_until["proj_001"] >= before + DECIDE_RETRY_AFTER_SECONDS - 1
    assert loop.decide_retry_until["proj_001"] <= time.time() + DECIDE_RETRY_AFTER_SECONDS


def test_crashed_decide_also_cools_down() -> None:
    loop = _loop()
    loop.futures = {}

    _reap_decide(loop, RuntimeError("boom"))

    assert "proj_001" in loop.decide_retry_until
    assert loop.decide_failures[("proj_001", "initial")] == 1


def test_successful_decide_clears_the_cooldown() -> None:
    loop = _loop()
    loop.futures = {}
    loop.client = FakeClient(make_project())
    loop.decide_retry_until["proj_001"] = time.time() + 60

    _reap_decide(loop, "success")

    assert "proj_001" not in loop.decide_retry_until


def test_repeated_decide_failures_stop_the_same_trigger_from_retrying() -> None:
    """The cooldown only spaces the runs out; a counter has to stop them altogether.

    proj_009 burned 71 noop runs on a graph that never moved: cooldown-free until the
    fix, then one run every 10s — still unbounded.
    """
    project = make_project(steps=[make_step()])
    project.facts.append(Fact(id="f002", description="new"))
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )
    for _ in range(MAX_CONSECUTIVE_DECIDE_FAILURES):
        _reap_decide(loop, "failed", trigger="facts:2->3")

    loop.decide_retry_until.pop("proj_001")  # the cooldown has expired again

    # The step still runs, but no further Decide is spent on the dead end.
    assert loop._try_dispatch_project(_summary("proj_001", "active")) is True
    assert dispatched == [("execute", "s001")]


def test_a_new_graph_change_reopens_the_decide_budget() -> None:
    project = make_project(steps=[make_step()])
    project.facts.append(Fact(id="f002", description="new"))
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )
    for _ in range(MAX_CONSECUTIVE_DECIDE_FAILURES):
        _reap_decide(loop, "failed", trigger="facts:2->3")
    loop.decide_retry_until.pop("proj_001")

    project.facts.append(Fact(id="f003", description="newer"))

    assert loop._try_dispatch_project(_summary("proj_001", "active")) is True
    assert dispatched == [("decide", "facts:2->4")]


def test_successful_decide_clears_the_failure_counters() -> None:
    loop = _loop()
    loop.futures = {}
    loop.client = FakeClient(make_project())
    loop.decide_failures[("proj_001", "facts:2->3")] = 4

    _reap_decide(loop, "success", trigger="facts:2->3")

    assert loop.decide_failures == {}


def test_runtime_refresh_drops_decide_failures_of_inactive_projects() -> None:
    loop = _loop()
    loop.decide_failures = {("active", "t"): 1, ("stopped", "t"): 2}

    loop._refresh_runtime_projects([_summary("active", "active"), _summary("stopped", "stopped")])

    assert set(loop.decide_failures) == {("active", "t")}


def test_decide_cooldown_skips_decide_but_still_dispatches_steps() -> None:
    loop = _loop()
    loop.futures = {}
    loop.config = make_config()
    loop.client = FakeClient(make_project(steps=[make_step()]))
    loop.client.export_project = lambda _project_id: "graph"  # type: ignore[method-assign]
    loop.decide_retry_until["proj_001"] = time.time() + 60
    dispatched: list[str] = []
    loop._dispatch_decide = lambda *_a, **_k: dispatched.append("decide") or True
    loop._dispatch_execute = lambda *_a, **_k: dispatched.append("execute") or True

    assert loop._try_dispatch_project(_summary("proj_001", "active")) is True
    assert dispatched == ["execute"]


def _reap_execute(loop: DispatcherLoop, outcome: str, reason: str | None = None) -> None:
    future: Future = Future()
    future.set_result(outcome)
    task = RunningTask(
        project_id="proj_001",
        task_type="execute",
        worker_name="test-worker",
        cancellation=TaskCancellation(),
        step_id="s001",
    )
    task.failure_reason = reason
    loop.futures[future] = task
    loop._reap_futures()


def test_failed_execute_accumulates_the_step_history() -> None:
    """A timeout never reaches the graph, so Decide can only learn it from here."""
    loop = _loop()
    loop.futures = {}

    _reap_execute(loop, "failed", "timed out after 600s")
    _reap_execute(loop, "failed", "worker exited with code 1")

    failure = loop.step_failures[("proj_001", "s001")]
    assert failure.count == 2
    assert failure.reason == "worker exited with code 1"
    assert failure.at


def test_a_provider_refusal_cools_the_worker_instead_of_the_step() -> None:
    """Quota/auth failures belong to the platform: the step must not burn an attempt."""
    loop = _loop()
    loop.futures = {}

    _reap_execute(loop, "failed", "quota: worker exited with code 1")

    assert loop.step_failures == {}
    assert ("proj_001", "execute", "test-worker") in loop.worker_rejected_until


def test_failure_counters_split_budget_from_errors() -> None:
    """Decide must tell "ran out of time" from "the run broke"."""
    loop = _loop()
    loop.futures = {}

    _reap_execute(loop, "failed", "budget_exhausted: no answer within 510s of 600s")
    _reap_execute(loop, "failed", "budget_exhausted: no answer within 510s of 600s")
    _reap_execute(loop, "failed", "parse failed: boom")

    failure = loop.step_failures[("proj_001", "s001")]
    assert failure.count == 3
    assert failure.budget_exhausted == 2
    assert failure.error == 1


def test_noop_success_is_recorded_without_clearing_the_history() -> None:
    loop = _loop()
    loop.futures = {}
    _reap_execute(loop, "failed", "budget_exhausted: no answer within 510s of 600s")

    _reap_execute(loop, "success", "noop: step closed without adding a fact or a finding")

    failure = loop.step_failures[("proj_001", "s001")]
    assert failure.noop == 1
    # The step produced nothing, so it did not "recover" either.
    assert failure.count == 1


def test_successful_execute_clears_the_step_history() -> None:
    loop = _loop()
    loop.futures = {}
    _reap_execute(loop, "failed", "timed out after 600s")

    _reap_execute(loop, "success")

    assert ("proj_001", "s001") not in loop.step_failures


def test_cancelled_execute_does_not_count_as_a_step_failure() -> None:
    loop = _loop()
    loop.futures = {}

    _reap_execute(loop, "cancelled")

    assert loop.step_failures == {}


def _checkpoint_matching(project) -> DecideCheckpoint:
    """A checkpoint that matches the project, so the tick goes to Execute, not Decide."""
    return DecideCheckpoint(
        fact_count=len(project.facts),
        hint_count=len(project.hints),
        finding_count=len(project.findings),
        open_step_count=len([step for step in project.steps if step.kind == "action"]),
    )


def test_one_step_is_dispatched_per_tick_by_default() -> None:
    """max_running_steps=1 keeps the historical serial behaviour."""
    project = make_project(steps=[make_step("s001"), make_step("s002")])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    assert len(dispatched) == 1


def test_slots_fill_up_to_max_running_steps_within_one_batch() -> None:
    project = make_project(
        steps=[
            make_step("s001", batch="x001"),
            make_step("s002", batch="x001"),
            make_step("s003", batch="x001"),
        ]
    )
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    # Highest priority first; equal priority falls back to the newest id.
    assert [step_id for _kind, step_id in dispatched] == ["s003", "s002"]


def test_steps_from_different_batches_never_overlap() -> None:
    """A planner saw one snapshot per run: two runs must not act at the same time."""
    project = make_project(steps=[make_step("s001", batch="x001"), make_step("s002", batch="x002")])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    # s002 wins on priority order, and its batch has nothing else to fill the slot.
    assert dispatched == [("execute", "s002")]


def test_a_running_batch_blocks_other_batches() -> None:
    running = make_step("s001", status="running", worker="w", batch="x001")
    sibling = make_step("s002", batch="x001")
    other = make_step("s003", batch="x002", asset=None)
    project = make_project(steps=[running, sibling, other])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    assert dispatched == [("execute", "s002")]


def test_a_running_step_without_a_batch_runs_alone() -> None:
    running = make_step("s001", status="running", worker="w")
    other = make_step("s002", batch="x002")
    project = make_project(steps=[running, other])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    assert dispatched == []


def test_steps_without_a_batch_run_one_at_a_time() -> None:
    project = make_project(steps=[make_step("s001"), make_step("s002")])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    assert len(dispatched) == 1


def test_slots_skip_a_step_whose_asset_is_already_running() -> None:
    """Two concurrent runs on one target duplicate each other, so the slot goes elsewhere."""
    running = make_step("s001", status="running", worker="w", asset="10.0.0.1:8080", batch="x001")
    same_asset = make_step("s002", asset="10.0.0.1:8080", batch="x001")
    other_asset = make_step("s003", asset="10.0.0.1:3389", batch="x001")
    project = make_project(steps=[running, same_asset, other_asset])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    assert dispatched == [("execute", "s003")]


def test_slots_do_not_repeat_an_asset_dispatched_in_the_same_tick() -> None:
    first = make_step("s002", asset="10.0.0.1:8080", batch="x001")
    second = make_step("s003", asset="10.0.0.1:8080", batch="x001")
    project = make_project(steps=[first, second])
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(project, dispatched)
    loop.config = make_config({"max_running_steps": 2})
    loop.decide_checkpoints["proj_001"] = _checkpoint_matching(project)

    loop._try_dispatch_project(_summary("proj_001", "active"))

    assert dispatched == [("execute", "s003")]


def test_a_stuck_step_is_handed_back_to_decide_instead_of_being_rerun() -> None:
    """proj_009: s062 timed out four times because nothing capped the blind retries.

    A failure never changes the graph, so the checkpoint saw no reason to wake Decide:
    the step was released to ``pending`` and re-claimed on the very next tick, burning
    one 600s model run each time.
    """
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(make_project(steps=[make_step()]), dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1
    )
    for _ in range(MAX_STEP_EXECUTE_ATTEMPTS - 1):
        _reap_execute(loop, "failed", "timed out after 600s")

    # Below the cap the step is retried; no Decide is spent on it yet.
    assert loop._decide_trigger(loop.client.get_project("proj_001")) is None

    _reap_execute(loop, "failed", "timed out after 600s")

    assert loop._decide_trigger(loop.client.get_project("proj_001")) == "stuck:s001"
    assert loop._try_dispatch_project(_summary("proj_001", "active")) is True
    assert dispatched == [("decide", "stuck:s001")]


def test_a_stuck_step_is_not_rerun_once_the_decide_budget_is_spent() -> None:
    """The escalation itself must be bounded, otherwise the dead end just moves to Decide."""
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(make_project(steps=[make_step()]), dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2,
        hint_count=1,
        finding_count=0,
        open_step_count=1,
        unprompted_attempts=MAX_UNPROMPTED_DECIDE_RUNS,
    )
    for _ in range(MAX_STEP_EXECUTE_ATTEMPTS):
        _reap_execute(loop, "failed", "timed out after 600s")

    assert loop._decide_trigger(loop.client.get_project("proj_001")) is None
    assert loop._try_dispatch_project(_summary("proj_001", "active")) is False
    assert dispatched == []


def test_a_healthy_step_still_runs_while_another_step_is_stuck() -> None:
    stuck = make_step("s001")
    healthy = make_step("s002")
    dispatched: list[tuple[str, str]] = []
    loop = _dispatchable_loop(make_project(steps=[stuck, healthy]), dispatched)
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=2
    )
    loop.step_failures[("proj_001", "s001")] = StepFailure(
        count=MAX_STEP_EXECUTE_ATTEMPTS, reason="timed out after 600s"
    )

    assert loop._decide_trigger(loop.client.get_project("proj_001")) is None
    assert loop._try_dispatch_project(_summary("proj_001", "active")) is True
    assert dispatched == [("execute", "s002")]


def test_step_failure_history_is_dropped_once_the_step_closes() -> None:
    project = make_project(steps=[make_step(status="abandoned")])
    loop = _dispatchable_loop(project, [])
    loop.step_failures[("proj_001", "s001")] = StepFailure(count=2, reason="timed out")
    loop.step_failures[("proj_002", "s001")] = StepFailure(count=2, reason="timed out")

    loop._forget_closed_step_failures(project)

    assert set(loop.step_failures) == {("proj_002", "s001")}

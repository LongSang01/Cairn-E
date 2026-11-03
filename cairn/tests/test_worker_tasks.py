from __future__ import annotations

from collections.abc import Iterator

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.health import HealthResult
from cairn.server.models import Goal
from cairn.dispatcher.tasks import decide, execute

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_project,
    make_step,
)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


def test_decide_writes_graph_snapshot_and_creates_steps(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    graph_yaml = "project:\n  title: huge\n" + ("x" * 100_000)

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":['
            '{"from":["f001"],"description":"next step","priority":4}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        containers,
        project,
        graph_yaml,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_steps == [("proj_001", ["f001"], "next step", "test-worker")]
    assert client.released_decide == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert len(containers.writes) == 1
    container_name, path, content = containers.writes[0]
    assert container_name == "container-proj_001"
    assert path.startswith("/tmp/cairn-prompts/decide-")
    assert path.endswith("/graph.yaml")
    assert content == graph_yaml
    assert graph_yaml not in driver.execute_prompts[0]
    assert path in driver.execute_prompts[0]


def test_decide_applies_goal_add_update_and_complete(monkeypatch) -> None:
    config = make_config()
    project = make_project(steps=[make_step("s002")])
    project.goals.append(
        Goal(
            id="g002",
            parent="g001",
            description="stage",
            status="active",
            creator="decider",
            created_at="2026-01-01T00:00:03Z",
        )
    )
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"goals":{"add":[{"description":"sub goal"}],'
            '"close":[{"id":"g002","status":"achieved"}]},'
            '"steps":{"add":[{"from":["origin"],"description":"new step"}],'
            '"update":[{"id":"s002","priority":9},{"id":"s002","status":"abandoned"}]}}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config, client, FakeContainerManager(), project, "graph", config.workers[0], TaskCancellation()
    )

    assert outcome == "success"
    assert client.created_goals == [("proj_001", "sub goal", "test-worker")]
    assert client.closed_goals == [("proj_001", "g002", "achieved")]
    assert client.created_steps == [("proj_001", ["origin"], "new step", "test-worker")]
    assert client.updated_steps == [
        ("proj_001", "s002", 9, None),
        ("proj_001", "s002", None, "abandoned"),
    ]


def test_decide_complete_writes_completion_and_releases(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config, client, FakeContainerManager(), project, "graph", config.workers[0], TaskCancellation()
    )

    assert outcome == "success"
    assert client.completed == [("proj_001", ["f001"], "done", "test-worker")]
    assert client.released_decide == [("proj_001", "test-worker")]


def test_decide_complete_treats_inactive_project_as_success(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    lease = FakeLease()

    def complete(*_args, **_kwargs) -> ApiResult:
        return ApiResult(403, text="inactive")

    client.complete = complete  # type: ignore[method-assign]
    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config, client, FakeContainerManager(), project, "graph", config.workers[0], TaskCancellation()
    )

    assert outcome == "success"
    assert client.released_decide == [("proj_001", "test-worker")]


def test_decide_without_open_steps_must_add_steps(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(0, '{"accepted":true,"data":{}}', ""),
    )

    outcome = decide.run_decide_task(
        config, client, FakeContainerManager(), project, "graph", config.workers[0], TaskCancellation()
    )

    assert outcome == "failed"
    assert client.created_steps == []


def test_execute_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(0, '{"accepted":true,"data":{"facts":[{"description":"confirmed fact"}]}}', ""),
        ]
    )

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(lease))
    monkeypatch.setattr(execute, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = execute.run_execute_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "s001", "test-worker", ["confirmed fact"], [])]
    assert len(containers.writes) == 2
    assert "/execute-" in containers.writes[0][1]
    assert "/execute_conclude-" in containers.writes[1][1]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert lease.started and lease.stopped


def test_execute_writes_findings_and_status(monkeypatch) -> None:
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        execute,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"facts":[{"description":"f"}],'
            '"findings":[{"kind":"flaw","description":"a product"}],"status":"done"}}',
            "",
        ),
    )

    outcome = execute.run_execute_task(
        config, client, FakeContainerManager(), project, "graph", step, config.workers[0], TaskCancellation()
    )

    assert outcome == "success"
    assert client.concluded == [
        ("proj_001", "s001", "test-worker", ["f"], [{"kind": "flaw", "description": "a product"}])
    ]


def test_execute_healthcheck_failure_releases_claim(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_and_task"
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)
    containers = FakeContainerManager()

    driver = FakeDriver()
    driver.health = HealthResult(ok=False, status=401, detail="unauthorized")
    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))

    outcome = execute.run_execute_task(
        config, client, containers, project, "graph", step, config.workers[0], TaskCancellation()
    )

    assert outcome == "unhealthy"
    assert client.released == [("proj_001", "s001", "test-worker")]
    assert containers.writes == []


def test_execute_timeout_without_session_releases_claim(monkeypatch) -> None:
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)
    driver = FakeDriver()

    class NoSessionDriver(FakeDriver):
        def prepare_session(self) -> str | None:
            return None

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: NoSessionDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        execute,
        "_run_process",
        lambda *_args, **_kwargs: ProcessResult(124, "", "", timed_out=True),
    )

    outcome = execute.run_execute_task(
        config, client, FakeContainerManager(), project, "graph", step, config.workers[0], TaskCancellation()
    )

    assert outcome == "failed"
    assert client.released == [("proj_001", "s001", "test-worker")]


def test_decide_startup_only_mode_skips_task_healthcheck(monkeypatch) -> None:
    config = make_config()
    config.runtime.worker_healthcheck = "startup_only"
    project = make_project()
    client = FakeClient(project)
    driver = FakeDriver()

    def _boom(*_a, **_k):
        raise AssertionError("task healthcheck should be skipped")

    driver.check_health = _boom  # type: ignore[method-assign]
    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0, '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next"}]}}', ""
        ),
    )

    outcome = decide.run_decide_task(
        config, client, FakeContainerManager(), project, "graph", config.workers[0], TaskCancellation()
    )

    assert outcome == "success"
    assert client.created_steps == [("proj_001", ["f001"], "next", "test-worker")]

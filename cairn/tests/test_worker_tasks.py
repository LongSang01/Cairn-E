from __future__ import annotations

import re
from collections.abc import Iterator

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_project,
    make_step,
)

from cairn.contracts.models import (
    Document,
    Goal,
    RecallHit,
    RecallPlan,
)
from cairn.dispatcher.models import StepFailure
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import decide, execute
from cairn.dispatcher.workers.health import HealthResult


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


def test_decide_noop_is_a_success_and_still_stores_the_recall_plan(monkeypatch) -> None:
    """A "wait" answer carries no graph data and must not crash the task.

    proj_009: 66 of the 71 `task crashed` Decide runs were exactly this — an empty
    `data` object while every step was running — because a type-narrowing assert
    indexed into the payload before the noop branch could return.
    """
    config = make_config()
    project = make_project(steps=[make_step()])
    client = FakeClient(project)
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0, '{"accepted":true,"data":{"recall":["uac"]}}', ""
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.recall_plan.queries == ["uac"]
    assert client.released_decide == [("proj_001", "test-worker")]


def test_decide_sees_how_many_times_a_step_already_failed(monkeypatch) -> None:
    """The step's failure history must reach the prompt, not just the scheduler log."""
    config = make_config()
    project = make_project(steps=[make_step()])
    client = FakeClient(project)
    driver = FakeDriver()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(0, '{"accepted":true,"data":{}}', ""),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
        step_failures={
            "s001": StepFailure(count=3, reason="timed out after 600s", at="2026-11-06T01:24:27Z")
        },
    )

    assert outcome == "success"
    prompt = driver.execute_prompts[0]
    assert '"failures"' in prompt
    assert '"count": 3' in prompt
    assert "timed out after 600s" in prompt
    assert "2026-11-06T01:24:27Z" in prompt


def test_decide_injects_and_clears_the_recall_plan(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    client.recall_plan = RecallPlan(queries=["redis"], doc_ids=[{"id": "doc001"}])
    client.recall_hits = [
        RecallHit(
            kind="fact",
            id="f900",
            title="redis reachable",
            description="port 6379 open",
            score=9,
            doc_ids=["doc003"],
        )
    ]
    client.documents = {
        "doc001": Document(
            id="doc001",
            title="nmap",
            content="PORT 6379 open\n" + "y" * 6_000 + "CONCLUSION AT THE END",
            size=6_036,
            created_at="2026-01-01T00:00:00Z",
        )
    }
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    prompt = driver.execute_prompts[0]
    assert "redis reachable" in prompt
    assert "docs=doc003" in prompt
    assert "PORT 6379 open" in prompt
    # The document is cut in the middle: head and tail both survive, and the gap tells
    # the model exactly where to resume.
    assert "CONCLUSION AT THE END" in prompt
    marker = re.search(r'\{"id": "doc001", "from": (\d+)\}', prompt)
    assert marker, prompt[-400:]
    offset = int(marker.group(1))
    assert 0 < offset < 6_036
    # Read once: the plan is cleared so the next run does not re-spend tokens.
    assert client.recall_plan == RecallPlan()
    assert client.recalled == [("proj_001", "redis")]
    assert client.fetched_documents == ["doc001"]


def test_decide_resumes_a_document_from_the_reported_offset(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    client.recall_plan = RecallPlan(doc_ids=[{"id": "doc001", "from": 4_000}])
    client.documents = {
        "doc001": Document(
            id="doc001",
            title="scan",
            content="HEAD" + "m" * 3_996 + "TAIL" + "z" * 8_000,
            size=12_004,
            created_at="2026-01-01T00:00:00Z",
        )
    }
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    prompt = driver.execute_prompts[0]
    # Resuming skips the first 4000 characters, and because the remaining 8004 still
    # exceed the budget the next offset moves forward instead of starting over.
    assert "HEAD" not in prompt
    assert "TAIL" in prompt
    marker = re.search(r'\{"id": "doc001", "from": (\d+)\}', prompt)
    assert marker, prompt[-400:]
    assert int(marker.group(1)) > 4_000


def test_decide_stores_the_next_lookups_and_step_title(monkeypatch) -> None:
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
            '{"accepted":true,"data":{"recall":["ldap"],"doc_ids":[{"id":"doc007"}],'
            '"steps":[{"from":["f001"],"description":"probe ldap","title":"probe ldap",'
            '"asset":"ldap.example:389"}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_step_titles == ["probe ldap"]
    assert client.created_step_assets == ["ldap.example:389"]
    # The Decide session is the batch: the scheduler only parallelises inside it.
    assert client.created_step_batches == ["x001"]
    assert client.recall_plan == RecallPlan(queries=["ldap"], doc_ids=[{"id": "doc007"}])


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
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
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
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
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
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
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
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
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
            ProcessResult(
                0, '{"accepted":true,"data":{"facts":[{"description":"confirmed fact"}]}}', ""
            ),
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


def test_conclude_prompt_carries_only_the_step_and_its_sources(monkeypatch) -> None:
    """The settlement round already holds its transcript; the whole graph was overhead."""
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)
    driver = FakeDriver()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(
                0, '{"accepted":true,"data":{"facts":[{"description":"confirmed fact"}]}}', ""
            ),
        ]
    )

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))
    monkeypatch.setattr(execute, "_run_process", lambda *_args, **_kwargs: next(results))

    containers = FakeContainerManager()
    execute.run_execute_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: origin\n- id: f001\nhints:\n- use the clue\n",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    prompt = driver.conclude_prompts[0]
    # The snapshot is written into the container and referenced by path.
    assert "/execute_conclude-" in prompt
    snapshot = next(content for _, path, content in containers.writes if "execute_conclude" in path)
    assert "investigate" in snapshot  # the step itself
    assert "f001" in snapshot  # the fact it started from
    assert "origin" not in snapshot  # unrelated graph content is gone
    assert "use the clue" not in snapshot


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
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [
        (
            "proj_001",
            "s001",
            "test-worker",
            ["f"],
            [
                {
                    "kind": "flaw",
                    "description": "a product",
                    "severity": "info",
                    "status": "tentative",
                    "evidence": [],
                }
            ],
        )
    ]
    # The driver's reported usage rides along with the session finish payload.
    finished = [
        payload
        for _project, _session, payload in client.finished_sessions
        if payload.get("status") == "success"
    ]
    assert finished
    assert finished[-1]["input_tokens"] == 10
    assert finished[-1]["output_tokens"] == 5
    assert finished[-1]["total_tokens"] == 15


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

    reasons: list[str] = []
    outcome = execute.run_execute_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        step,
        config.workers[0],
        TaskCancellation(),
        on_failure=reasons.append,
    )

    assert outcome == "failed"
    assert client.released == [("proj_001", "s001", "test-worker")]
    # The scheduler needs the reason to tell the next Decide why this step is stuck:
    # a spent budget is classified apart from a crash.
    assert len(reasons) == 1
    assert reasons[0].startswith("budget_exhausted:")
    assert f"of {config.tasks.execute.timeout}s" in reasons[0]


def test_execute_reports_a_provider_refusal_as_a_worker_fault(monkeypatch) -> None:
    """A dead key or empty balance is the worker's problem, not the step's."""
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))
    monkeypatch.setattr(
        execute,
        "_run_process",
        lambda *_args, **_kwargs: ProcessResult(1, "", "Error: insufficient_quota"),
    )

    reasons: list[str] = []
    outcome = execute.run_execute_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        step,
        config.workers[0],
        TaskCancellation(),
        on_failure=reasons.append,
    )

    assert outcome == "failed"
    assert reasons == ["quota: worker exited with code 1"]


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
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_steps == [("proj_001", ["f001"], "next", "test-worker")]


def test_decide_records_model_session(monkeypatch) -> None:
    config = make_config()
    client = FakeClient(make_project())
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(
        decide,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next","priority":1}]}}',
            "",
        ),
    )

    decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        make_project(),
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert len(client.created_sessions) == 1
    project_id, payload = client.created_sessions[0]
    assert project_id == "proj_001"
    assert payload["phase"] == "decide"
    # The session is visible while the worker runs, then finalized with the outcome.
    assert payload["status"] == "running"
    assert payload["worker"] == "test-worker"
    assert payload["summary"].startswith("决策下一步")
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"] == driver.execute_prompts[0]
    assert payload["messages"][0]["msg_id"]

    assert len(client.finished_sessions) == 1
    _project_id, session_id, finished = client.finished_sessions[0]
    assert session_id == "x001"
    assert finished["status"] == "success"
    assert finished["returncode"] == 0


def test_execute_records_session_for_execute_and_conclude(monkeypatch) -> None:
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Still exploring, no json yet.", ""),
            ProcessResult(0, '{"accepted":true,"data":{"facts":[{"description":"f"}]}}', ""),
        ]
    )

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))
    monkeypatch.setattr(execute, "_run_process", lambda *_args, **_kwargs: next(results))

    execute.run_execute_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    phases = [payload["phase"] for _pid, payload in client.created_sessions]
    assert phases == ["execute", "execute_conclude"]
    assert all(payload["status"] == "running" for _pid, payload in client.created_sessions)
    assert all(payload["step"] == "s001" for _pid, payload in client.created_sessions)
    # Every execute session carries the step text, so the list shows what ran.
    assert {payload["summary"] for _pid, payload in client.created_sessions} == {"investigate"}

    assert [payload["status"] for _pid, _sid, payload in client.finished_sessions] == [
        "failed",
        "success",
    ]


def test_execute_streams_transcript_while_the_worker_runs(monkeypatch) -> None:
    """stdout chunks reach the live session before the process finishes."""
    config = make_config()
    step = make_step(status="running", worker="test-worker")
    project = make_project(steps=[step])
    client = FakeClient(project)

    def fake_run(_manager, _name, _worker, _argv, *, on_stdout=None, **_kwargs):
        assert on_stdout is not None
        for line in (
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}',
            '{"type":"tool_execution_start","toolName":"bash","args":{"command":"ls"}}',
        ):
            on_stdout(line + "\n")
        return ProcessResult(0, '{"accepted":true,"data":{"facts":[{"description":"f"}]}}', "")

    monkeypatch.setattr(execute, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(execute.HeartbeatLease, "for_step", _lease_factory(FakeLease()))
    monkeypatch.setattr(execute, "_run_process", fake_run)

    execute.run_execute_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        step,
        config.workers[0],
        TaskCancellation(),
    )

    created_project, created = client.created_sessions[0]
    assert created_project == "proj_001"
    assert created["status"] == "running"
    appended = [messages for _pid, _sid, messages in client.appended_sessions]
    roles = [message["role"] for batch in appended for message in batch]
    assert "assistant" in roles
    assert "tool_call" in roles
    assert client.finished_sessions[0][2]["status"] == "success"


def _wide_graph(facts: int) -> str:
    lines = ["project:", "  title: engagement", "  status: active", "facts:"]
    for index in range(1, facts + 1):
        lines.append(f"  - id: f{index:03d}")
        lines.append("    description: fact " + str(index) + " " + "x" * 300)
        lines.append("    from_step: null")
    lines.append("steps: []")
    return "\n".join(lines) + "\n"


def test_decide_hands_expired_history_to_the_scheduler(monkeypatch) -> None:
    config = make_config(
        runtime={
            "context_budget": {
                "context_window": 2_000,
                "max_output_tokens": 200,
                "safety_min_tokens": 50,
                "safety_max_tokens": 50,
                "recent_items": 2,
                "min_snapshot_tokens": 300,
            }
        }
    )
    project = make_project()
    client = FakeClient(project)
    containers = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    calls: list[str] = []
    requests = []

    def fake_run(*_args, **_kwargs):
        calls.append("worker")
        return ProcessResult(
            0,
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"next","priority":4}]}}',
            "",
        )

    def sink(request) -> bool:
        requests.append(request)
        return True

    monkeypatch.setattr(decide, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(decide.HeartbeatLease, "for_decide", _lease_factory(lease))
    monkeypatch.setattr(decide, "run_worker_process", fake_run)

    outcome = decide.run_decide_task(
        config,
        client,
        containers,
        project,
        _wide_graph(20),
        config.workers[0],
        TaskCancellation(),
        summary_sink=sink,
    )

    assert outcome == "success"
    assert len(requests) == 1
    request = requests[0]
    assert request.project_id == "proj_001"
    assert request.facts_covered == 18  # 20 facts minus the 2 kept verbatim
    assert len(request.facts) == 18
    # The Decide itself stays one model call: the summary is not run here.
    assert calls == ["worker"]
    assert client.saved_summaries == []


def test_slice_document_keeps_head_and_tail_with_a_resume_pointer() -> None:
    text = "H" * 3_000 + "MIDDLE" + "T" * 3_000

    out = decide._slice_document(text, 0, 1_000, "doc009")

    assert out.startswith("H")
    assert out.endswith("T")
    assert '"id": "doc009"' in out
    assert len(out) <= 1_000


def test_slice_document_falls_back_to_a_plain_cut_in_a_tiny_budget() -> None:
    out = decide._slice_document("z" * 500, 0, 40, "doc009")

    assert len(out) <= 40
    assert out.startswith("z")


def test_decide_passes_the_exclusive_flag_to_the_step(monkeypatch) -> None:
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
            '{"accepted":true,"data":{"steps":[{"from":["f001"],"description":"spray",'
            '"asset":"10.0.0.1:8080/login","exclusive":true}]}}',
            "",
        ),
    )

    outcome = decide.run_decide_task(
        config,
        client,
        FakeContainerManager(),
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_step_exclusive == [True]

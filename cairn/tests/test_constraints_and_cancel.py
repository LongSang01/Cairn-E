"""Constraint propagation (ARTEX-style) and the planner kill path.

Two guarantees are pinned here:

* a constraint outranks the goal in both Decide and Execute prompts, but never
  rewrites the platform-owned tool rules (no prompt-level widening of powers);
* Decide can stop a running worker by cancelling its step; the worker's lease
  then fails, the process is killed, and its unsubmitted result is discarded.
"""

from __future__ import annotations

import pytest
from conftest import bare_loop, make_project, make_step
from fastapi.testclient import TestClient

from cairn.contracts.models import Steer
from cairn.dispatcher.constraints import (
    EMPTY,
    HEADER,
    MAX_CONSTRAINT_CHARS,
    MAX_CONSTRAINTS,
    render_constraints_block,
    render_steers_block,
)
from cairn.dispatcher.models import DecideCheckpoint
from cairn.dispatcher.output_validation import validate_decide_payload
from cairn.server import db


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "eng", "origin": "starting point", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _decide_kwargs(**overrides):
    return {
        "open_steps_empty": False,
        "max_steps": 3,
        "fact_ids": ["f001"],
        "goal_ids": ["g001"],
        "active_sub_goal_ids": [],
        "step_ids": ["s001"],
        "pending_step_ids": [],
        "running_step_ids": [],
        **overrides,
    }


# --- constraints: API and graph -------------------------------------------------


def test_constraints_crud_reaches_project_detail(client: TestClient) -> None:
    pid = _project(client)
    deny = client.post(f"/projects/{pid}/constraints", json={"text": "不要进行增删改查"})
    assert deny.status_code == 201
    assert deny.json()["kind"] == "deny"
    assert deny.json()["origin"] == "human"

    allow = client.post(
        f"/projects/{pid}/constraints",
        json={"text": "只测试 10.0.0.0/24", "kind": "allow"},
    )
    assert allow.status_code == 201
    assert allow.json()["kind"] == "allow"

    listed = client.get(f"/projects/{pid}/constraints").json()
    assert [row["text"] for row in listed] == ["不要进行增删改查", "只测试 10.0.0.0/24"]
    assert client.get(f"/projects/{pid}").json()["constraints"] == listed

    assert client.delete(f"/projects/{pid}/constraints/{deny.json()['id']}").status_code == 204
    assert len(client.get(f"/projects/{pid}/constraints").json()) == 1
    assert client.delete(f"/projects/{pid}/constraints/{deny.json()['id']}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"text": "   "},
        {"text": "ok", "kind": "maybe"},
        {"text": "ok", "origin": "system"},
    ],
)
def test_constraint_request_validation(client: TestClient, body: dict) -> None:
    pid = _project(client)
    assert client.post(f"/projects/{pid}/constraints", json=body).status_code == 422


def test_constraints_do_not_change_tool_rules(client: TestClient) -> None:
    """A constraint is prompt-level authorization, not a command rule."""
    pid = _project(client)
    assert (
        client.post(f"/projects/{pid}/constraints", json={"text": "禁止所有命令"}).status_code
        == 201
    )
    response = client.post(
        f"/projects/{pid}/tool-guard/check",
        json={"tool": "bash", "command": "nmap -sV 10.0.0.5", "worker": "local-pi"},
    )
    assert response.status_code == 200
    assert response.json() == {"decision": "allow"}


def test_constraints_survive_context_compaction(client: TestClient) -> None:
    """The block is built from ProjectDetail, not from the compacted YAML."""
    pid = _project(client)
    client.post(f"/projects/{pid}/constraints", json={"text": "不要碰支付系统"})
    detail = client.get(f"/projects/{pid}").json()
    block = render_constraints_block(detail["constraints"])
    assert "禁止：不要碰支付系统" in block
    assert "out-of-scope" in block


# --- constraints: renderer -------------------------------------------------------


def test_render_constraints_block_orders_deny_first_and_bounds_text() -> None:
    rules = [
        {"kind": "allow", "text": "允许只读探测"},
        {"kind": "deny", "text": "禁止改数据"},
        "没有分类的裸文本",
    ]
    block = render_constraints_block(rules)
    assert block.startswith(HEADER)
    assert block.index("禁止：禁止改数据") < block.index("必须：允许只读探测")
    # A bare string is a prohibition; assuming "allow" would widen the task.
    assert "禁止：没有分类的裸文本" in block
    assert "out-of-scope" in block

    long_block = render_constraints_block([{"kind": "deny", "text": "禁" * 1000}])
    assert len(long_block) < 1000 + len(HEADER) + 200
    assert "…" in long_block

    many = render_constraints_block(
        [{"kind": "deny", "text": f"规则{i}"} for i in range(MAX_CONSTRAINTS + 5)]
    )
    assert f"规则{MAX_CONSTRAINTS - 1}" in many
    assert f"规则{MAX_CONSTRAINTS}" not in many
    assert render_constraints_block([]) == f"{HEADER}\n{EMPTY}"
    assert (
        len(render_constraints_block([{"kind": "deny", "text": "x" * 500}]))
        < len(HEADER) + MAX_CONSTRAINT_CHARS + 400
    )


# --- constraints: Decide action --------------------------------------------------


def test_decide_may_only_tighten_constraints() -> None:
    kind, data, _ = validate_decide_payload(
        {"accepted": True, "data": {"constraints": {"add": [{"text": "禁止访问支付系统"}]}}},
        **_decide_kwargs(open_steps_empty=True),
    )
    assert kind == "actions"
    assert data["constraints"]["add"] == [{"kind": "deny", "text": "禁止访问支付系统"}]

    with pytest.raises(ValueError, match="only a human may allow"):
        validate_decide_payload(
            {
                "accepted": True,
                "data": {"constraints": {"add": [{"kind": "allow", "text": "扩大范围"}]}},
            },
            **_decide_kwargs(open_steps_empty=True),
        )


def test_constraint_add_alone_is_a_valid_decide_response() -> None:
    """A rule-only payload must not be mistaken for a no-op when the graph is empty."""
    kind, data, _ = validate_decide_payload(
        {"accepted": True, "data": {"constraints": {"add": [{"text": "别碰其他主机"}]}}},
        **_decide_kwargs(open_steps_empty=True),
    )
    assert kind == "actions"
    assert data["constraints"]["add"]


# --- planner kill: validation ----------------------------------------------------


def test_decide_can_cancel_a_running_step_but_not_a_pending_one() -> None:
    kind, data, _ = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "steps": {
                    "update": [
                        {"id": "s001", "status": "cancelled", "note": "crossed a constraint"}
                    ]
                }
            },
        },
        **_decide_kwargs(running_step_ids=["s001"]),
    )
    assert kind == "actions"
    assert data["steps"]["update"][0]["status"] == "cancelled"

    with pytest.raises(ValueError, match="only cancel a running step"):
        validate_decide_payload(
            {
                "accepted": True,
                "data": {"steps": {"update": [{"id": "s001", "status": "cancelled"}]}},
            },
            **_decide_kwargs(pending_step_ids=["s001"]),
        )


def test_cancelling_the_last_running_step_allows_completion() -> None:
    kind, data, _ = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "complete": {"from": ["f001"], "description": "done"},
                "steps": {
                    "update": [{"id": "s001", "status": "cancelled", "note": "out of scope"}]
                },
            },
        },
        **_decide_kwargs(running_step_ids=["s001"]),
    )
    assert kind == "complete"
    assert data["step_update"][0]["status"] == "cancelled"


# --- planner kill: server --------------------------------------------------------


def _running_step(client: TestClient, pid: str) -> str:
    response = client.post(
        f"/projects/{pid}/steps",
        json={
            "from": ["origin"],
            "description": "probe out-of-scope host",
            "creator": "executor",
            "worker": "executor",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "running"
    return response.json()["id"]


def test_cancel_running_step_kills_lease_and_discards_result(client: TestClient) -> None:
    pid = _project(client)
    sid = _running_step(client, pid)

    cancelled = client.post(
        f"/projects/{pid}/steps/{sid}/update",
        json={"status": "cancelled", "note": "crossed a constraint"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    # The owner stays on the row: the next heartbeat must see a terminal step and
    # 409, which is what makes the dispatcher kill the process.
    assert cancelled.json()["worker"] == "executor"
    assert cancelled.json()["note"] == "crossed a constraint"

    heartbeat = client.post(f"/projects/{pid}/steps/{sid}/heartbeat", json={"worker": "executor"})
    assert heartbeat.status_code == 409

    # No write-back: ARTEX's kill_work marks `stopped` without landing its result.
    conclude = client.post(
        f"/projects/{pid}/steps/{sid}/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "facts": [{"description": "late result"}],
        },
    )
    assert conclude.status_code == 409
    with db.get_conn() as conn:
        assert (
            conn.execute("SELECT COUNT(*) AS n FROM facts WHERE step_id = ?", (sid,)).fetchone()[
                "n"
            ]
            == 0
        )

    # A cancelled step is closed: it must not keep the project from completing.
    assert client.get(f"/projects/{pid}").json()["steps"][0]["status"] == "cancelled"
    assert "status: cancelled" in client.get(f"/projects/{pid}/export?format=yaml").text
    assert "STEP CANCELLED" in client.get(f"/projects/{pid}/export?format=timeline").text


def test_cancel_does_not_resurrect_or_cancel_pending_steps(client: TestClient) -> None:
    pid = _project(client)
    sid = _running_step(client, pid)

    pending = client.post(
        f"/projects/{pid}/steps",
        json={
            "from": ["origin"],
            "description": "not started",
            "creator": "decider",
            "worker": None,
        },
    ).json()["id"]
    assert (
        client.post(
            f"/projects/{pid}/steps/{pending}/update", json={"status": "cancelled"}
        ).status_code
        == 409
    )

    client.post(
        f"/projects/{pid}/steps/{sid}/update",
        json={"status": "cancelled", "note": "stop"},
    )
    # Abandon after a cancel is a no-op, not a resurrection of the step.
    again = client.post(f"/projects/{pid}/steps/{sid}/update", json={"status": "abandoned"})
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


# --- in-flight steers ------------------------------------------------------------


def test_steers_are_append_only_and_reach_project_detail(client: TestClient) -> None:
    pid = _project(client)
    first = client.post(
        f"/projects/{pid}/steers", json={"text": "停止测试 10.0.0.9", "creator": "Human"}
    )
    assert first.status_code == 201
    assert first.json()["creator"] == "Human"

    second = client.post(
        f"/projects/{pid}/steers", json={"text": "只做只读收集", "creator": "Human"}
    )
    assert second.status_code == 201

    listed = client.get(f"/projects/{pid}/steers").json()
    assert [row["text"] for row in listed] == ["停止测试 10.0.0.9", "只做只读收集"]
    assert client.get(f"/projects/{pid}").json()["steers"] == listed
    assert (
        client.post(f"/projects/{pid}/steers", json={"text": "  ", "creator": "Human"}).status_code
        == 422
    )


def test_render_steers_block_keeps_latest_last_and_bounds_history() -> None:
    assert render_steers_block([]) == "（无运行中指令）"
    steers = [
        Steer(id=f"st{i:03d}", text=f"指令{i}", creator="Human", created_at="now")
        for i in range(12)
    ]
    block = render_steers_block(steers)
    assert "指令11" in block
    assert "指令3" not in block
    assert "最后一条是本轮最新指令" in block


def test_a_new_steer_wakes_decide_even_without_a_graph_change() -> None:
    loop = bare_loop()
    project = make_project(steps=[make_step()])
    loop.decide_checkpoints["proj_001"] = DecideCheckpoint(
        fact_count=2, hint_count=1, finding_count=0, open_step_count=1, steer_count=0
    )
    assert loop._decide_trigger(project) is None

    project.steers.append(
        Steer(id="st001", text="停止测试 10.0.0.9", creator="Human", created_at="now")
    )
    # A new steer is a legitimate wake-up; it must not consume the retry budget.
    assert loop._decide_trigger(project) == "steers:0->1"

    # No new steer but still unanswered after a failed run -> bounded retry.
    loop.decide_checkpoints["proj_001"].steer_count = 1
    assert loop._decide_trigger(project) == "steers:pending:1"

    # Once answered, no retry and no wake-up.
    project.steers[0].answer = "已停止。"
    assert loop._decide_trigger(project) is None


def test_apply_actions_does_not_report_a_cancel_the_server_ignored() -> None:
    """A 200 no-op on an already-terminal step is not an applied action."""
    from cairn.dispatcher.protocol.client import ApiResult
    from cairn.dispatcher.tasks.decide import _apply_actions

    class FakeClient:
        def __init__(self, status: str) -> None:
            self.status = status

        def update_step(self, *_args, **_kwargs):
            return ApiResult(status_code=200, data={"status": self.status})

        def create_constraint(self, *_args, **_kwargs):
            return ApiResult(status_code=201, data={})

    payload = {
        "goals": {"add": [], "close": []},
        "steps": {
            "add": [],
            "update": [{"id": "s001", "priority": None, "status": "cancelled", "note": "stop"}],
        },
        "constraints": {"add": []},
    }
    assert _apply_actions(FakeClient("done"), "proj_001", "decider", payload)["steps_updated"] == 0
    assert (
        _apply_actions(FakeClient("cancelled"), "proj_001", "decider", payload)["steps_updated"]
        == 1
    )

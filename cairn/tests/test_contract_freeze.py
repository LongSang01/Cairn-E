"""契约冻结测试:锁定对外字段名,防止无意的契约漂移。

1.0 契约以 additive-only 为演进策略:字段只增不删不改名。任何字段变更都必须
显式修改本文件的冻结清单,并同步 docs/protocol.md 与 CONTRACT_VERSION 的版本决策。
"""

from fastapi.testclient import TestClient

from cairn.contracts import CONTRACT_VERSION
from cairn.contracts.models import (
    Constraint,
    ProjectDetail,
    ScopeEntry,
    Session,
    SessionDetail,
    SessionMessage,
    Steer,
)
from cairn.server import db
from cairn.server.models import (
    AnswerSteerRequest,
    CreateConstraintRequest,
    CreateScopeEntryRequest,
    CreateSessionRequest,
    CreateSteerRequest,
    CreateToolRuleRequest,
    ScopeCheckRequest,
    SubmittedSessionMessage,
)


def test_contract_version_is_frozen() -> None:
    assert CONTRACT_VERSION == "1.0"


def test_project_detail_fields_are_frozen() -> None:
    assert set(ProjectDetail.model_fields) == {
        "project",
        "facts",
        "goals",
        "steps",
        "findings",
        "hints",
        "constraints",
        "steers",
        "scope",
        "sessions",
    }


def test_constraint_fields_are_frozen() -> None:
    assert set(Constraint.model_fields) == {"id", "kind", "text", "origin", "created_at"}


def test_scope_entry_fields_are_frozen() -> None:
    assert set(ScopeEntry.model_fields) == {"id", "kind", "value", "source", "created_at"}


def test_steer_fields_are_frozen() -> None:
    assert set(Steer.model_fields) == {
        "id",
        "text",
        "creator",
        "created_at",
        "answer",
        "answered_at",
    }


def test_session_message_fields_are_frozen() -> None:
    assert set(SessionMessage.model_fields) == {"seq", "role", "content", "raw"}


def test_submitted_session_message_fields_are_frozen() -> None:
    assert set(SubmittedSessionMessage.model_fields) == {
        "role",
        "content",
        "raw",
        "msg_id",
    }


def test_session_fields_are_frozen() -> None:
    assert set(Session.model_fields) == {
        "id",
        "worker",
        "worker_type",
        "phase",
        "step",
        "summary",
        "native_session",
        "status",
        "returncode",
        "timed_out",
        "duration_ms",
        "error",
        "created_at",
        "message_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    }


def test_session_detail_fields_are_frozen() -> None:
    assert set(SessionDetail.model_fields) == set(Session.model_fields) | {"messages"}


def test_create_session_request_fields_are_frozen() -> None:
    assert set(CreateSessionRequest.model_fields) == {
        "client_key",
        "worker",
        "worker_type",
        "phase",
        "step",
        "summary",
        "native_session",
        "status",
        "returncode",
        "timed_out",
        "duration_ms",
        "error",
        "messages",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    }


def test_healthz_exposes_contract_version(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["contract_version"] == CONTRACT_VERSION


def test_schema_migrations_are_registered() -> None:
    # 契约兼容性依赖迁移台账:新版本只能追加,不能改写已发布的迁移。
    assert [version for version, _ in db._MIGRATIONS] == [1, 2, 3, 4, 5, 6, 7]


def test_boundary_request_bodies_are_frozen() -> None:
    assert set(CreateConstraintRequest.model_fields) == {"text", "kind", "origin"}
    assert set(AnswerSteerRequest.model_fields) == {"text"}
    assert set(CreateSteerRequest.model_fields) == {"text", "creator"}
    assert set(CreateScopeEntryRequest.model_fields) == {"kind", "value"}
    assert set(ScopeCheckRequest.model_fields) == {"command"}
    assert set(CreateToolRuleRequest.model_fields) == {
        "target",
        "match_type",
        "pattern",
        "action",
        "priority",
        "message",
        "timeout_seconds",
        "timeout_action",
    }

from __future__ import annotations

import json

import pytest

from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_decide_payload,
    validate_execute_payload,
)
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver

FACTS = ["origin", "f001"]
GOALS = ["g001", "g002"]
STEPS = ["s001"]


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_decide_payload_limits_number_of_steps() -> None:
    kind, data, _lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "steps": [
                    {"from": ["f001"], "description": "one"},
                    {"from": ["f001"], "description": "two"},
                ]
            },
        },
        open_steps_empty=False,
        max_steps=1,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data["steps"]["add"] == [
        {
            "from": ["f001"],
            "description": "one",
            "title": None,
            "goal": None,
            "priority": 0,
            "rationale": None,
        }
    ]


def test_decide_payload_collects_title_recall_and_doc_ids() -> None:
    kind, data, lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "recall": ["redis", "  ", "kerberos"],
                "doc_ids": ["doc001", "doc002"],
                "steps": [{"from": ["f001"], "description": "one", "title": "probe redis"}],
            },
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert lookups == {"recall": ["redis", "kerberos"], "doc_ids": ["doc001", "doc002"]}
    assert data["steps"]["add"][0]["title"] == "probe redis"


def test_decide_payload_caps_recall_queries_and_documents() -> None:
    _kind, _data, lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "recall": ["a", "b", "c", "d"],
                "doc_ids": [f"doc{index:03d}" for index in range(1, 9)],
                "steps": [{"from": ["f001"], "description": "one"}],
            },
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert lookups["recall"] == ["a", "b", "c"]
    assert lookups["doc_ids"] == [f"doc{index:03d}" for index in range(1, 6)]


def test_decide_payload_rejects_non_array_recall() -> None:
    with pytest.raises(ValueError, match="recall must be an array"):
        validate_decide_payload(
            {"accepted": True, "data": {"recall": "redis", "steps": []}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_requires_action_when_no_step_is_open() -> None:
    with pytest.raises(ValueError, match="must add steps"):
        validate_decide_payload(
            {"accepted": True, "data": {}},
            open_steps_empty=True,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_rejects_unknown_fact_reference() -> None:
    with pytest.raises(ValueError, match="unknown id"):
        validate_decide_payload(
            {"accepted": True, "data": {"steps": [{"from": ["f999"], "description": "x"}]}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_supports_completion_and_sub_goals() -> None:
    kind, data, _lookups = validate_decide_payload(
        {
            "data": {
                "complete": {"from": ["f001"], "description": "goal met"},
                "goals": {"add": [{"description": "sub", "parent": "g001"}]},
            }
        },
        open_steps_empty=True,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert kind == "complete"
    assert data == {"from": ["f001"], "description": "goal met"}


def test_execute_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_execute_payload(parse_json_output("Need inspect files and keep working."))


def test_execute_payload_requires_evidence_for_done_steps() -> None:
    with pytest.raises(ValueError, match="a fact, a finding or a note"):
        validate_execute_payload({"accepted": True, "data": {"status": "done"}})


def test_execute_payload_accepts_findings_only_done_step() -> None:
    kind, data = validate_execute_payload(
        {
            "accepted": True,
            "data": {
                "status": "done",
                "findings": [{"kind": "info-leak", "description": "config bucket readable"}],
            },
        }
    )

    assert kind == "result"
    assert data["facts"] == []
    assert data["findings"][0]["kind"] == "info-leak"


def test_decide_payload_closing_a_goal_without_adding_one() -> None:
    """A `goals` object that only carries `close` must not look like a bad `add`."""
    kind, data, _lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "goals": {"close": [{"id": "g002", "status": "achieved"}]},
                "steps": {"update": [{"id": "s001", "priority": 5}]},
            },
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data["goals"] == {"add": [], "close": [{"id": "g002", "status": "achieved", "note": None}]}


def test_decide_payload_accepts_note_only_step_update() -> None:
    kind, data, _lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {"steps": {"update": [{"id": "s001", "note": "wait for the lockout window"}]}},
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data["steps"]["update"] == [
        {"id": "s001", "priority": None, "status": None, "note": "wait for the lockout window"}
    ]


def test_decide_payload_rejects_empty_step_update() -> None:
    with pytest.raises(ValueError, match="must set priority, status or note"):
        validate_decide_payload(
            {"accepted": True, "data": {"steps": {"update": [{"id": "s001"}]}}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_ignores_empty_complete_placeholder() -> None:
    """The example's `{"from": [], "description": ""}` is not a completion attempt."""
    kind, data, _lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "complete": {"from": [], "description": ""},
                "steps": {"add": [{"from": ["f001"], "description": "keep going"}]},
            },
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data["steps"]["add"][0]["description"] == "keep going"


def test_execute_payload_accepts_findings_and_abandon() -> None:
    kind, data = validate_execute_payload(
        {
            "accepted": True,
            "data": {
                "status": "abandoned",
                "findings": [{"kind": "flaw", "description": "product"}],
            },
        }
    )

    assert kind == "result"
    assert data == {
        "facts": [],
        "findings": [{"kind": "flaw", "description": "product"}],
        "status": "abandoned",
        "note": None,
    }


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_claudecode_driver_extracts_answer_from_stream_json() -> None:
    driver = ClaudeCodeDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "result": '{"accepted":true}', "is_error": False}),
        ]
    )

    assert driver.extract_response_text(stdout, "") == '{"accepted":true}'
    # Plain mode output must keep working: stdout is the answer.
    assert driver.extract_response_text("plain answer", "") == "plain answer"


def test_execute_payload_keeps_fact_title_and_documents() -> None:
    kind, data = validate_execute_payload(
        {
            "accepted": True,
            "data": {
                "facts": [
                    {
                        "title": "creds found",
                        "description": "admin:admin works on /login",
                        "documents": [{"title": "http dump", "content": "HTTP/1.1 200 OK"}],
                    }
                ],
                "status": "done",
            }
        }
    )

    assert kind == "result"
    assert data["facts"] == [
        {
            "description": "admin:admin works on /login",
            "title": "creds found",
            "documents": [{"title": "http dump", "content": "HTTP/1.1 200 OK"}],
        }
    ]


def test_execute_payload_rejects_overlong_fact_title() -> None:
    with pytest.raises(ValueError, match="at most 200 characters"):
        validate_execute_payload(
            {"accepted": True, "data": {"facts": [{"description": "x", "title": "t" * 201}]}}
        )


def test_codex_driver_extracts_thread_and_agent_messages() -> None:
    driver = CodexDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "command_execution", "command": "ls", "aggregated_output": "a.txt"},
                }
            ),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "second"}}),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "thread-1"
    assert driver.extract_response_text(stdout, "") == "first\nsecond"


def test_close_stream_closes_response_even_when_stream_close_fails() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Stream:
        def __init__(self) -> None:
            self._response = Response()

        def close(self) -> None:
            raise ValueError("already closed")

    stream = Stream()
    ManagedProcess._close_stream(stream)

    assert stream._response.closed

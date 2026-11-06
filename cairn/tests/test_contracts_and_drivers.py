from __future__ import annotations

import json

import pytest

from cairn.dispatcher.output_validation import (
    parse_json_output,
    validate_decide_payload,
    validate_execute_payload,
)
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.workers.adapters.pi import PiDriver

FACTS = ["origin", "f001"]
GOALS = ["g001", "g002"]
STEPS = ["s001"]
ACTIVE_SUB_GOALS = ["g002"]


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
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
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
                "doc_ids": [{"id": "doc001"}, {"id": "doc002", "from": 10}],
                "steps": [{"from": ["f001"], "description": "one", "title": "probe redis"}],
            },
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert lookups == {
        "recall": ["redis", "kerberos"],
        "doc_ids": [{"id": "doc001", "from": 0}, {"id": "doc002", "from": 10}],
    }
    assert data["steps"]["add"][0]["title"] == "probe redis"


def test_decide_payload_accepts_document_resume_offsets() -> None:
    _kind, _data, lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {"doc_ids": [{"id": "doc001"}, {"id": "doc002", "from": 4_000}]},
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
    )

    assert lookups["doc_ids"] == [
        {"id": "doc001", "from": 0},
        {"id": "doc002", "from": 4_000},
    ]


def test_decide_payload_rejects_a_bare_document_id() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        validate_decide_payload(
            {"accepted": True, "data": {"doc_ids": ["doc001"]}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_rejects_a_negative_resume_offset() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        validate_decide_payload(
            {"accepted": True, "data": {"doc_ids": [{"id": "doc001", "from": -1}]}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_rejects_a_non_integer_resume_offset() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        validate_decide_payload(
            {"accepted": True, "data": {"doc_ids": [{"id": "doc001", "from": "4000"}]}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_caps_recall_queries_and_documents() -> None:
    _kind, _data, lookups = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "recall": ["a", "b", "c", "d"],
                "doc_ids": [{"id": f"doc{index:03d}"} for index in range(1, 9)],
                "steps": [{"from": ["f001"], "description": "one"}],
            },
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
    )

    assert lookups["recall"] == ["a", "b", "c"]
    assert lookups["doc_ids"] == [{"id": f"doc{index:03d}", "from": 0} for index in range(1, 6)]


def test_decide_payload_rejects_non_array_recall() -> None:
    with pytest.raises(ValueError, match="recall must be an array"):
        validate_decide_payload(
            {"accepted": True, "data": {"recall": "redis", "steps": []}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
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
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
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
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
            step_ids=STEPS,
        )


def test_decide_payload_drops_completion_that_queues_work() -> None:
    """A completion arriving with graph actions keeps the actions and does not end.

    This is the shape that falsely completed proj_008: the model mixed a complete
    with add/update/close work. All actions must be preserved.
    """
    kind, data, _lookups = validate_decide_payload(
        {
            "data": {
                "complete": {"from": ["f001"], "description": "goal met"},
                "goals": {
                    "add": [{"description": "sub", "parent": "g001"}],
                    "close": [{"id": "g002", "status": "achieved"}],
                },
                "steps": {
                    "add": [{"from": ["f001"], "description": "keep going"}],
                    "update": [{"id": "s001", "priority": 5}],
                },
            }
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data is not None
    assert data["goals"]["add"] == [{"description": "sub", "parent": "g001"}]
    assert data["goals"]["close"] == [{"id": "g002", "status": "achieved", "note": None}]
    assert data["steps"]["add"][0]["description"] == "keep going"
    assert data["steps"]["update"][0]["id"] == "s001"


def test_decide_payload_completes_with_final_goal_close() -> None:
    """Closing the last sub goals and completing in one payload is the atomic finale."""
    kind, data, _lookups = validate_decide_payload(
        {
            "data": {
                "complete": {"from": ["f001"], "description": "goal met"},
                "goals": {"close": [{"id": "g002", "status": "achieved"}]},
            }
        },
        open_steps_empty=True,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=[],
    )

    assert kind == "complete"
    assert data == {
        "from": ["f001"],
        "description": "goal met",
        "goal_close": [{"id": "g002", "status": "achieved", "note": None}],
    }


def test_decide_payload_drops_completion_while_steps_stay_open() -> None:
    kind, data, _lookups = validate_decide_payload(
        {
            "data": {
                "complete": {"from": ["f001"], "description": "premature"},
                "goals": {"close": [{"id": "g002", "status": "achieved"}]},
            }
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data is not None
    assert data["goals"]["close"] == [{"id": "g002", "status": "achieved", "note": None}]


def test_decide_payload_completes_with_abandoned_pending_steps() -> None:
    """Abandoning the last *pending* steps as part of the finale closes the graph."""
    kind, data, _lookups = validate_decide_payload(
        {
            "data": {
                "complete": {"from": ["f001"], "description": "goal met"},
                "goals": {"close": [{"id": "g002", "status": "achieved"}]},
                "steps": {
                    "update": [{"id": "s001", "status": "abandoned", "note": "covered by f001"}]
                },
            }
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
        pending_step_ids=STEPS,
    )

    assert kind == "complete"
    assert data is not None
    assert data["goal_close"] == [{"id": "g002", "status": "achieved", "note": None}]
    assert data["step_update"] == [
        {"id": "s001", "priority": None, "status": "abandoned", "note": "covered by f001"}
    ]


def test_decide_payload_keeps_running_step_open_for_its_executor() -> None:
    """A running step cannot be abandoned, so the completion must not fire.

    proj_008: Decide abandoned the running final verification in the same payload as a
    completion; the executor's finished result was thrown away and the project never
    closed. The payload must lose the completion and leave the step to its executor.
    """
    kind, data, _lookups = validate_decide_payload(
        {
            "data": {
                "complete": {"from": ["f001"], "description": "goal met"},
                "goals": {"close": [{"id": "g002", "status": "achieved"}]},
                "steps": {"update": [{"id": "s001", "status": "abandoned", "note": "duplicate"}]},
            }
        },
        open_steps_empty=False,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
        pending_step_ids=[],
    )

    assert kind == "actions"
    assert data is not None
    assert data["goals"]["close"] == [{"id": "g002", "status": "achieved", "note": None}]


def test_decide_payload_rejects_completion_with_open_steps() -> None:
    with pytest.raises(ValueError, match="complete is only allowed"):
        validate_decide_payload(
            {"data": {"complete": {"from": ["f001"], "description": "goal met"}}},
            open_steps_empty=False,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            active_sub_goal_ids=[],
            step_ids=STEPS,
        )


def test_decide_payload_rejects_completion_with_active_sub_goals() -> None:
    with pytest.raises(ValueError, match="active sub-goals"):
        validate_decide_payload(
            {"data": {"complete": {"from": ["f001"], "description": "goal met"}}},
            open_steps_empty=True,
            max_steps=3,
            fact_ids=FACTS,
            goal_ids=GOALS,
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
            step_ids=[],
        )


def test_decide_payload_accepts_completion_on_a_clean_graph() -> None:
    kind, data, _lookups = validate_decide_payload(
        {"data": {"complete": {"from": ["f001"], "description": "goal met"}}},
        open_steps_empty=True,
        max_steps=3,
        fact_ids=FACTS,
        goal_ids=GOALS,
        active_sub_goal_ids=[],
        step_ids=[],
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
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
        step_ids=STEPS,
    )

    assert kind == "actions"
    assert data["goals"] == {
        "add": [],
        "close": [{"id": "g002", "status": "achieved", "note": None}],
    }


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
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
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
            active_sub_goal_ids=ACTIVE_SUB_GOALS,
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
        active_sub_goal_ids=ACTIVE_SUB_GOALS,
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
        "findings": [
            {
                "kind": "flaw",
                "description": "product",
                "severity": "info",
                "status": "tentative",
                "evidence": [],
            }
        ],
        "status": "abandoned",
        "note": None,
    }


def test_execute_payload_normalises_finding_severity() -> None:
    _kind, data = validate_execute_payload(
        {
            "accepted": True,
            "data": {
                "findings": [
                    {"kind": "flaw", "severity": "HIGH", "description": "product"},
                    {"kind": "flaw", "severity": " medium ", "description": "product"},
                ],
                "status": "abandoned",
            },
        }
    )

    assert [item["severity"] for item in data["findings"]] == ["high", "medium"]


def test_execute_payload_rejects_unknown_severity() -> None:
    with pytest.raises(ValueError, match="severity must be one of"):
        validate_execute_payload(
            {
                "accepted": True,
                "data": {
                    "findings": [{"kind": "flaw", "severity": "urgent", "description": "product"}],
                    "status": "abandoned",
                },
            }
        )


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
            },
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


def test_pi_driver_sums_token_usage_across_turns() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "usage": {"input": 10, "output": 4, "cacheRead": 3, "cacheWrite": 1},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {"role": "assistant", "usage": {"input": 5, "output": 2}},
                }
            ),
        ]
    )

    usage = PiDriver().extract_usage(stdout, "")

    assert usage is not None
    assert usage.payload() == {
        "input_tokens": 15,
        "output_tokens": 6,
        "cache_read_tokens": 3,
        "cache_write_tokens": 1,
        "total_tokens": 25,
    }


def test_pi_driver_prefers_agent_end_totals() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "turn_end", "message": {"usage": {"input": 10, "output": 4}}}),
            json.dumps(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "usage": {"input": 10, "output": 4}},
                        {"role": "assistant", "usage": {"input": 2, "output": 1}},
                    ],
                }
            ),
        ]
    )

    usage = PiDriver().extract_usage(stdout, "")

    assert usage is not None
    assert usage.total_tokens == 17


def test_driver_without_usage_metadata_reports_nothing() -> None:
    assert PiDriver().extract_usage("plain answer", "") is None
    assert PiDriver().extract_usage('{"type":"session","id":"s"}', "") is None


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

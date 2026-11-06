from __future__ import annotations

from typing import Any

from cairn.contracts.models import FINDING_SEVERITIES, FINDING_STATUSES
from cairn.dispatcher.output_parser import extract_json_object

MAX_PRIORITY = 9
EVIDENCE_KINDS = ("exchange", "document", "seq")


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    data = payload.get("data")
    if isinstance(data, dict) and not any(key in payload for key in ("complete", "steps", "goals")):
        return True, data
    return None, None


def _fact_entry(value: Any, field: str) -> dict[str, Any]:
    """A submitted fact: description plus optional short title and evidence documents."""
    if isinstance(value, str):
        return {"description": _text(value, field)}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a string or object")
    entry: dict[str, Any] = {"description": _text(value.get("description"), f"{field}.description")}
    title = _short_text(value.get("title"), f"{field}.title", 200)
    if title:
        entry["title"] = title
    documents = value.get("documents")
    if documents is not None:
        if not isinstance(documents, list):
            raise ValueError(f"{field}.documents must be an array")
        items: list[dict[str, str]] = []
        for index, document in enumerate(documents):
            if not isinstance(document, dict):
                raise ValueError(f"{field}.documents[{index}] must be an object")
            items.append(
                {
                    "title": _text(document.get("title"), f"{field}.documents[{index}].title"),
                    "content": _text(
                        document.get("content"), f"{field}.documents[{index}].content"
                    ),
                }
            )
        if items:
            entry["documents"] = items
    return entry


def _short_text(value: Any, field: str, limit: int) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    if len(text) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return text


def _string_list(value: Any, field: str, limit: int, item_limit: int) -> list[str]:
    """A bounded list of short strings, used for recall queries and document ids."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    items: list[str] = []
    for index, entry in enumerate(value):
        items.append(_short_text(entry, f"{field}[{index}]", item_limit) or "")
    items = [item for item in items if item]
    return items[:limit]


def _document_requests(value: Any, field: str, limit: int) -> list[dict[str, Any]]:
    """Document reads: ``{"id": "doc003", "from": 4000}``, one entry per document.

    ``from`` is the offset that lets a truncated document continue next round, so it is
    validated rather than silently dropped.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    requests: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        name = f"{field}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f'{name} must be an object like {{"id": "doc003"}}')
        document_id = _short_text(entry.get("id"), f"{name}.id", 64)
        raw_offset = entry.get("from")
        if raw_offset is None:
            offset = 0
        elif isinstance(raw_offset, bool) or not isinstance(raw_offset, int):
            raise ValueError(f"{name}.from must be an integer")
        elif raw_offset < 0:
            raise ValueError(f"{name}.from must not be negative")
        else:
            offset = raw_offset
        if not document_id:
            continue
        requests.append({"id": document_id, "from": offset})
        if len(requests) >= limit:
            break
    return requests


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    return text or None


def _id_list(value: Any, field: str, allowed: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    cleaned: list[str] = []
    for item in value:
        text = _text(item, f"{field}[]")
        if text not in allowed:
            raise ValueError(f"{field} references unknown id: {text}")
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _severity(value: Any, field: str) -> str:
    """A finding's grade; missing means the neutral `info`."""
    if value is None:
        return "info"
    text = _optional_text(value, field)
    if text is None:
        return "info"
    severity = text.lower()
    if severity not in FINDING_SEVERITIES:
        raise ValueError(f"{field} must be one of: {', '.join(FINDING_SEVERITIES)}")
    return severity


def _finding_status(value: Any, field: str) -> str:
    """`confirmed` promises reproducibility; otherwise the finding is tentative."""
    if value is None:
        return "tentative"
    text = _optional_text(value, field)
    if text is None:
        return "tentative"
    status = text.lower()
    if status not in FINDING_STATUSES:
        raise ValueError(f"{field} must be one of: {', '.join(FINDING_STATUSES)}")
    return status


def _evidence_list(value: Any, field: str) -> list[dict[str, str | None]]:
    """Evidence pointers a worker can produce: a URL, a document id or a seq.

    A bare string is accepted as an exchange URL because that is what the model
    naturally writes after running `curl`.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    cleaned: list[dict[str, str | None]] = []
    for index, item in enumerate(value):
        item_field = f"{field}[{index}]"
        if isinstance(item, str):
            cleaned.append({"kind": "exchange", "ref": _text(item, item_field), "digest": None})
            continue
        if not isinstance(item, dict):
            raise ValueError(f"{item_field} must be an object or a string")
        kind = (_optional_text(item.get("kind"), f"{item_field}.kind") or "exchange").lower()
        if kind not in EVIDENCE_KINDS:
            raise ValueError(f"{item_field}.kind must be one of: {', '.join(EVIDENCE_KINDS)}")
        ref = _text(item.get("ref") or item.get("id"), f"{item_field}.ref")
        cleaned.append(
            {
                "kind": kind,
                "ref": ref,
                "digest": _optional_text(item.get("digest"), f"{item_field}.digest"),
            }
        )
    return cleaned


def _priority(value: Any, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return max(0, min(MAX_PRIORITY, value))


def _section(payload: dict[str, Any], key: str, action_key: str) -> list[Any]:
    """Accept both {"steps": [...]} and {"steps": {"add": [...]}}."""
    value = payload.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        items = value.get(action_key)
        if items is None:
            return []
        if not isinstance(items, list):
            raise ValueError(f"{key}.{action_key} must be an array")
        return items
    raise ValueError(f"{key} must be an object or an array")


def _complete_block(value: Any, fact_ids: list[str]) -> dict[str, Any] | None:
    """Parse the completion declaration, ignoring the empty template placeholder.

    Models often echo `{"complete": {"from": [], "description": ""}}` from the example
    while the real work is in `steps`; that is an artifact, not a malformed completion.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("complete must be an object")
    from_ids = _id_list(value.get("from"), "complete.from", fact_ids)
    description = _optional_text(value.get("description"), "complete.description")
    if not from_ids and description is None:
        return None
    if not from_ids:
        raise ValueError("complete.from must not be empty")
    if description is None:
        raise ValueError("complete.description is required")
    return {"from": from_ids, "description": description}


def validate_decide_payload(
    payload: dict[str, Any],
    *,
    open_steps_empty: bool,
    max_steps: int,
    fact_ids: list[str],
    goal_ids: list[str],
    active_sub_goal_ids: list[str],
    step_ids: list[str],
    pending_step_ids: list[str] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Decide only touches the graph: goals, steps, and the completion declaration.

    ``complete`` ends the whole project, so it must never silently swallow the
    actions it arrives with:

    - ``complete`` combined with ``goals.add``, ``steps.add`` or ``steps.update``
      keeps those graph actions and drops the premature completion.
    - ``complete`` combined with only ``goals.close`` applies the closes and
      completes atomically (the closes are carried in ``goal_close``).
    - A bare ``complete`` is refused while steps are open
      (``open_steps_empty`` is false) or ``active_sub_goal_ids`` is non-empty.
    - Only a ``pending`` step can be closed by the payload. A ``running`` step
      belongs to its executor, which alone may conclude it; abandoning it is
      impossible, so it keeps the completion from firing until its result lands
      and the next Decide sees the closed step.

    ``pending_step_ids`` (defaults to ``step_ids``, i.e. "nothing is running") is the
    abandonable subset of ``step_ids``.

    The third element carries what the run wants looked up next (keyword recall and
    document ids), which the scheduler persists for the following run.
    """
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None, {"recall": [], "doc_ids": []}
    if accepted is None:
        data = payload
    if data is None:  # pragma: no cover - a wrapped payload always carries an object
        raise ValueError("data must be an object")

    lookups = {
        "recall": _string_list(data.get("recall"), "recall", 3, 200),
        "doc_ids": _document_requests(data.get("doc_ids"), "doc_ids", 5),
    }

    complete_payload = _complete_block(data.get("complete"), fact_ids)

    goals_section = data.get("goals")
    goal_add: list[dict[str, Any]] = []
    for index, item in enumerate(_section(data, "goals", "add")):
        if not isinstance(item, dict):
            raise ValueError(f"goals.add[{index}] must be an object")
        parent = item.get("parent")
        if parent is not None:
            parent = _text(parent, f"goals.add[{index}].parent")
            if parent not in goal_ids:
                raise ValueError(f"goals.add[{index}] references unknown goal: {parent}")
        goal_add.append(
            {
                "description": _text(item.get("description"), f"goals.add[{index}].description"),
                "parent": parent,
            }
        )

    goal_close: list[dict[str, Any]] = []
    if isinstance(goals_section, dict):
        raw_close = goals_section.get("close")
        if raw_close is not None:
            if not isinstance(raw_close, list):
                raise ValueError("goals.close must be an array")
            for index, item in enumerate(raw_close):
                if not isinstance(item, dict):
                    raise ValueError(f"goals.close[{index}] must be an object")
                gid = _text(item.get("id"), f"goals.close[{index}].id")
                if gid not in goal_ids:
                    raise ValueError(f"goals.close[{index}] references unknown goal: {gid}")
                status = item.get("status", "abandoned")
                if status not in ("achieved", "abandoned"):
                    raise ValueError(f"goals.close[{index}].status must be achieved or abandoned")
                goal_close.append(
                    {
                        "id": gid,
                        "status": status,
                        "note": _optional_text(item.get("note"), f"goals.close[{index}].note"),
                    }
                )

    step_add: list[dict[str, Any]] = []
    for index, item in enumerate(_section(data, "steps", "add")):
        if not isinstance(item, dict):
            raise ValueError(f"steps.add[{index}] must be an object")
        goal = item.get("goal")
        if goal is not None:
            goal = _text(goal, f"steps.add[{index}].goal")
            if goal not in goal_ids:
                raise ValueError(f"steps.add[{index}] references unknown goal: {goal}")
        step_add.append(
            {
                "description": _text(item.get("description"), f"steps.add[{index}].description"),
                "title": _short_text(item.get("title"), f"steps.add[{index}].title", 200),
                "from": _id_list(item.get("from"), f"steps.add[{index}].from", fact_ids),
                "goal": goal,
                "priority": _priority(item.get("priority"), f"steps.add[{index}].priority"),
                "rationale": _optional_text(item.get("rationale"), f"steps.add[{index}].rationale"),
            }
        )
    step_add = step_add[:max_steps]

    step_update: list[dict[str, Any]] = []
    if isinstance(data.get("steps"), dict):
        raw_update = data["steps"].get("update")
        if raw_update is not None:
            if not isinstance(raw_update, list):
                raise ValueError("steps.update must be an array")
            for index, item in enumerate(raw_update):
                if not isinstance(item, dict):
                    raise ValueError(f"steps.update[{index}] must be an object")
                sid = _text(item.get("id"), f"steps.update[{index}].id")
                if sid not in step_ids:
                    raise ValueError(f"steps.update[{index}] references unknown step: {sid}")
                status = item.get("status")
                if status is not None and status != "abandoned":
                    raise ValueError(f"steps.update[{index}].status only supports abandoned")
                priority = item.get("priority")
                note = _optional_text(item.get("note"), f"steps.update[{index}].note")
                if priority is None and status is None and note is None:
                    # A note-only update is how Decide pins guidance for the executor.
                    raise ValueError(f"steps.update[{index}] must set priority, status or note")
                step_update.append(
                    {
                        "id": sid,
                        "priority": None
                        if priority is None
                        else _priority(priority, f"steps.update[{index}].priority"),
                        "status": status,
                        "note": note,
                    }
                )

    if complete_payload is not None:
        # Abandoning a step closes it instead of queuing work: the wrap-up payload
        # (proj_008 shape) is complete + goals.close + steps.update(abandon). Only a
        # pending step can be closed this way; a running one keeps its own conclusion.
        closable = set(step_ids if pending_step_ids is None else pending_step_ids)
        abandoned_now = {
            u["id"] for u in step_update if u.get("status") == "abandoned" and u["id"] in closable
        }
        queued_work = bool(goal_add or step_add) or any(
            u["id"] not in abandoned_now for u in step_update
        )
        open_now = (
            () if open_steps_empty else tuple(sid for sid in step_ids if sid not in abandoned_now)
        )
        closed_now = {entry["id"] for entry in goal_close}
        active_now = [gid for gid in active_sub_goal_ids if gid not in closed_now]
        if not queued_work and not open_now and not active_now:
            # Finale: this payload leaves nothing open — ends the project atomically.
            if goal_close:
                complete_payload = {**complete_payload, "goal_close": goal_close}
            if step_update:
                complete_payload = {**complete_payload, "step_update": step_update}
            return "complete", complete_payload, lookups
        if goal_add or goal_close or step_add or step_update:
            # Completing cancels every running step: a payload that still queues
            # work keeps its graph actions and drops the premature completion.
            complete_payload = None
        else:
            raise ValueError(
                "complete is only allowed when nothing remains to run: "
                f"open steps {list(open_now) or '-'}; active sub-goals {active_now or '-'}"
            )

    if not (goal_add or goal_close or step_add or step_update):
        if open_steps_empty:
            raise ValueError("decide must add steps, add goals, or complete when no step is open")
        return "noop", None, lookups

    return (
        "actions",
        {
            "goals": {"add": goal_add, "close": goal_close},
            "steps": {"add": step_add, "update": step_update},
        },
        lookups,
    )


def validate_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Execute submits facts (submit_fact), findings, and closes its step."""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        data = payload
    if data is None:  # pragma: no cover - a wrapped payload always carries an object
        raise ValueError("data must be an object")

    status = data.get("status", "done")
    if status not in ("done", "abandoned"):
        raise ValueError("status must be done or abandoned")

    facts: list[dict[str, Any]] = []
    raw_facts = data.get("facts")
    if isinstance(raw_facts, list):
        for index, item in enumerate(raw_facts):
            facts.append(_fact_entry(item, f"facts[{index}]"))
    singular = data.get("fact")
    if singular is not None:
        facts.append(_fact_entry(singular, "fact"))

    findings: list[dict[str, Any]] = []
    raw_findings = data.get("findings")
    if raw_findings is not None:
        if not isinstance(raw_findings, list):
            raise ValueError("findings must be an array")
        for index, item in enumerate(raw_findings):
            if not isinstance(item, dict):
                raise ValueError(f"findings[{index}] must be an object")
            findings.append(
                {
                    "kind": _optional_text(item.get("kind"), f"findings[{index}].kind")
                    or "finding",
                    "description": _text(item.get("description"), f"findings[{index}].description"),
                    "severity": _severity(item.get("severity"), f"findings[{index}].severity"),
                    "status": _finding_status(item.get("status"), f"findings[{index}].status"),
                    "evidence": _evidence_list(
                        item.get("evidence"), f"findings[{index}].evidence"
                    ),
                }
            )

    note = _optional_text(data.get("note"), "note")
    if status == "done" and not (facts or findings or note):
        # Never force a fabricated fact: findings or a note are a valid closure.
        raise ValueError("a done step must submit a fact, a finding or a note")

    return "result", {"facts": facts, "findings": findings, "status": status, "note": note}

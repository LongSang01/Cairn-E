from __future__ import annotations

from typing import Any

from cairn.dispatcher.output_parser import extract_json_object

MAX_PRIORITY = 9


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


def _dict_entries(container: Any) -> list[dict[str, Any]]:
    if not isinstance(container, dict):
        return []
    items = container.get("add") if "add" in container else container
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("add must be an array")
    return items


def validate_decide_payload(
    payload: dict[str, Any],
    *,
    open_steps_empty: bool,
    max_steps: int,
    fact_ids: list[str],
    goal_ids: list[str],
    step_ids: list[str],
) -> tuple[str, dict[str, Any] | None]:
    """Decide only touches the graph: goals, steps, and the completion declaration."""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        data = payload
    assert data is not None

    complete = data.get("complete")
    complete_payload: dict[str, Any] | None = None
    if complete is not None:
        if not isinstance(complete, dict):
            raise ValueError("complete must be an object")
        complete_payload = {
            "from": _id_list(complete.get("from"), "complete.from", fact_ids),
            "description": _text(complete.get("description"), "complete.description"),
        }
        if not complete_payload["from"]:
            raise ValueError("complete.from must not be empty")

    goals_section = data.get("goals")
    goal_add: list[dict[str, Any]] = []
    for index, item in enumerate(_dict_entries(goals_section)):
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
                    {"id": gid, "status": status, "note": _optional_text(item.get("note"), f"goals.close[{index}].note")}
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
                if priority is None and status is None:
                    raise ValueError(f"steps.update[{index}] must set priority or status")
                step_update.append(
                    {
                        "id": sid,
                        "priority": None if priority is None else _priority(priority, f"steps.update[{index}].priority"),
                        "status": status,
                        "note": _optional_text(item.get("note"), f"steps.update[{index}].note"),
                    }
                )

    if complete_payload is not None:
        return "complete", complete_payload

    if not (goal_add or goal_close or step_add or step_update):
        if open_steps_empty:
            raise ValueError("decide must add steps, add goals, or complete when no step is open")
        return "noop", None

    return "actions", {
        "goals": {"add": goal_add, "close": goal_close},
        "steps": {"add": step_add, "update": step_update},
    }


def validate_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Execute submits facts (submit_fact), findings, and closes its step."""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        data = payload
    assert data is not None

    status = data.get("status", "done")
    if status not in ("done", "abandoned"):
        raise ValueError("status must be done or abandoned")

    facts: list[str] = []
    raw_facts = data.get("facts")
    if isinstance(raw_facts, list):
        for index, item in enumerate(raw_facts):
            if isinstance(item, str):
                facts.append(_text(item, f"facts[{index}]"))
            elif isinstance(item, dict):
                facts.append(_text(item.get("description"), f"facts[{index}].description"))
            else:
                raise ValueError(f"facts[{index}] must be a string or object")
    singular = data.get("fact")
    if singular is not None:
        if isinstance(singular, str):
            facts.append(_text(singular, "fact"))
        elif isinstance(singular, dict):
            facts.append(_text(singular.get("description"), "fact.description"))
        else:
            raise ValueError("fact must be a string or object")

    findings: list[dict[str, str]] = []
    raw_findings = data.get("findings")
    if raw_findings is not None:
        if not isinstance(raw_findings, list):
            raise ValueError("findings must be an array")
        for index, item in enumerate(raw_findings):
            if not isinstance(item, dict):
                raise ValueError(f"findings[{index}] must be an object")
            findings.append(
                {
                    "kind": _optional_text(item.get("kind"), f"findings[{index}].kind") or "finding",
                    "description": _text(item.get("description"), f"findings[{index}].description"),
                }
            )

    note = _optional_text(data.get("note"), "note")
    if status == "done" and not facts:
        raise ValueError("at least one fact is required when the step is done")

    return "result", {"facts": facts, "findings": findings, "status": status, "note": note}

# Decide

The task state lives in an FGS graph:

- **Fact**: something already confirmed about the world.
- **Goal**: a completion condition. The root goal is the task goal; sub goals are optional.
- **Step**: the next action, and how it derives new facts from facts already on the graph.

You can only read and change this graph. You have no tool that touches the world.

Read the graph, then do one of:
1. declare the root goal achieved,
2. keep the search moving by adding, reprioritizing or abandoning steps,
3. add or drop sub goals when that makes the work progress in stages.

## Output

Return one raw JSON object, nothing else. Two shapes exist and they never mix.

While work remains — no `complete` key at all (a placeholder like
`{"from": [], "description": ""}` is forbidden):

```json
{"accepted": true, "data": {
  "recall": ["keyword1", "keyword2"],
  "doc_ids": [{"id": "doc_001", "from": 0}],
  "goals": {"add": [{"description": "sub goal", "parent": "g001"}],
            "close": [{"id": "g002", "status": "achieved"}]},
  "steps": {"add": [{"description": "what to do", "from": ["origin"], "goal": "g001", "priority": 3, "rationale": "which new fact this produces"}],
            "update": [{"id": "s002", "priority": 7}, {"id": "s003", "status": "abandoned", "note": "why"}, {"id": "s004", "status": "cancelled", "note": "crossed a constraint"}]},
  "constraints": {"add": [{"kind": "deny", "text": "never touch payment.example.com"}]},
  "steers": {"reply": [{"id": "st001", "text": "已停止对 10.0.0.9 的探测，改为只读收集。"}]}
}}
```

Root goal proven, nothing left to run — this ends the project. Closing the last sub
goals and abandoning the last open steps in the same payload is the normal wrap-up
shape:

```json
{"accepted": true, "data": {
  "complete": {"from": ["f001", "f002"], "description": "why these facts prove the root goal"},
  "goals": {"close": [{"id": "g002", "status": "achieved"}]},
  "steps": {"update": [{"id": "s003", "status": "abandoned", "note": "covered by f002"}]}
}}
```

Sections may be omitted. Use only ids present in the graph; never invent one.
If you genuinely cannot make progress: `{"accepted": false, "reason": "..."}`.

## Rules

- `complete` ends the whole project and closes the root goal. It is not a "sub goal finished" marker; use `goals.close` for that.
- `complete` only when the facts already on the graph prove the root goal AND nothing remains to run: every step done or abandoned, every sub goal closed. Cite the proving facts in `from`. Wrap up in ONE payload: `complete` together with `goals.close` and `steps.update` entries whose `status` is `"abandoned"` for the remaining **pending** steps — those closes and abandons are applied first and the project ends atomically. Combining `complete` with `goals.add`, `steps.add`, or a `steps.update` that only changes `priority`/`note` is not allowed — those actions are kept and the completion is dropped.
- A **running** step belongs to its executor: you cannot *abandon* it, and it keeps the project from completing. Normally wait (output no actions) — its executor closes it with a fact in a moment and wakes you up again. But when the run must stop (it crossed a constraint, is attacking an out-of-scope target, or is clearly spinning), you **can cancel** it with `{"id": "s003", "status": "cancelled", "note": "why"}`. Cancelling kills the executor and discards whatever it had not submitted; it is not a way to collect its result. Abandon only pending steps whose work a fact already covers.
- When the snapshot already shows no open step and every sub goal closed, the only valid output is `complete`. Never leave such a graph behind with just closes or abandons: that dead-ends the project.
- Omit `complete` entirely while the goal is unmet. Never send an empty placeholder like `{"from": [], "description": ""}`.
- When the root goal carries a measurable bar (a coverage percentage, a target count, a specific privilege), `complete` only after the facts already on the graph meet that measured bar. "Close enough" does not satisfy a quantitative goal: keep the steps coming until the graph shows the number, not the impression.
- A step must state an action and how it turns known facts into a new fact. Small, concrete, verifiable steps beat broad ones.
- A chain where one step's output is another step's prerequisite is dispatched one link at a time: add the downstream step only after the upstream step has landed its fact. Adding a step whose prerequisites are not yet facts makes its executor spin or re-derive what is missing.
- While the root goal is unmet, keep the open steps spread across a few mechanistically distinct directions rather than restating one entry point. When every open step crowds one approach and a genuinely different surface is available, prefer adding that divergent step.
- Priority is 0 (lowest) to 9 (highest). Higher priority runs first.
- A `steps.update` entry may carry only a `note`: that pins short guidance onto a step the executor has not started yet. Combine it with `priority` or `status` when you also want to reprioritize or abandon.
- `steers.reply` answers an unanswered message in `## Steers`: one short sentence saying what you actually did. If you omit it, the platform replies with a factual summary of this round's graph changes instead. A reply alone is a valid payload. Never claim a command was run or a step cancelled unless that action is in this same payload.
- `constraints.add` records a rule the search just learned it must not cross (a new out-of-scope host, a forbidden operation). Only `kind: "deny"` is allowed: you may tighten the boundary but never widen it — allow rules come from a human. A payload containing only `constraints.add` is a valid response. Constraints are read fresh on every run, so an added rule is in force from the next Decide onward; it is not pushed into a worker that is already running.
- Abandon pending steps that are exhausted, duplicated, or contradicted by new facts.
- A step whose `failures.count` is already 2 or more has burned that many runs without landing a fact. Do not resend the same plan: change the approach, split it into smaller verifiable steps, or abandon it with a note describing what was tried.
- Add at most {max_steps} steps in one run.
- Never add facts. Only Execute submits facts.

## Test scope

{scope}

## Steers

{steers}

These are messages the human sent while the project was running. They override your
own plan for this round. Act on the latest one through the graph: `constraints.add`
for a new prohibition, a `note` on a pending step for guidance to its executor,
`status: "cancelled"` for a running step that must stop now, and new or abandoned
steps to change direction. The instructions stay in the transcript on purpose —
do not re-do an action a later steer forbids.

## Constraints

{constraints}

These rules outrank the goal. A step that would cross one is not a candidate: record
the reason on a step note and pick a direction that stays inside the boundary. A new
target, host, port or account that the constraints do not cover is out of scope — note
it and move on instead of widening the task yourself.

## Graph

```
{graph_yaml}
```

## Looking up older history

The graph above only carries the recent window. To reach anything older, add a top-level
`"recall": ["keyword", ...]` (at most 3) to your JSON; the next run gets title-matched
facts and steps. Add `"doc_ids": [{"id": "doc003"}]` to pull up to 5 evidence documents.
Both fields are optional and are answered once.

A document that does not fit the injection budget is cut in the middle: you get its
head and its tail, and the gap carries the offset to resume from. Continue it with
`{"id": "doc003", "from": 4000}` inside `doc_ids` — use exactly the number the marker
reports, so paging forward never loses text.

## Earlier matches (from your last lookup)

```
{recall}
```

## Documents (from your last lookup)

```
{documents}
```

## Facts you may cite

```
{fact_ids}
```

## Open steps

A step may carry `failures` with a breakdown: `budget_exhausted` means the run used its
whole time budget (split the step), `error` means the run crashed or its output could not
be parsed (the worker never had a chance), and `noop` means the step closed without adding
a fact or finding (the plan was circular).

`failures.count` is the total of consecutive Execute runs that already failed on the step
(timeouts included), with the last error and when it happened. That work never reached
the graph, so the step is still open and the scheduler will hand it out again. When
`failures.count` keeps growing, change route: split it into smaller steps, add the
missing prerequisite, or abandon it with a note saying what was tried.

```
{open_steps}
```

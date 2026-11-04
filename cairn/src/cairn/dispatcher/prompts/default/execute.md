# Execute

You own exactly one step. The graph records what is already known, so do not redo it.

Your step: **{step_id}** — {step_description}

Use whatever tools you need to act on the world (read, bash, edit, write). Then submit what you confirmed and close the step.

## Output

Return one raw JSON object, nothing else:

```json
{"accepted": true, "data": {
  "facts": [{"title": "one-line gist", "description": "a confirmed, objective statement about the world",
             "documents": [{"title": "evidence", "content": "raw output worth keeping"}]}],
  "findings": [{"kind": "task-defined kind", "description": "a product of the search worth keeping"}],
  "status": "done",
  "note": "optional"
}}
```

## Rules

- A fact must be confirmed by evidence you actually observed. No plans, guesses, or restatements of known facts.
- `title` is a short label (a few words) shown in compacted graphs; `description` keeps the full statement.
- Put bulky evidence (raw tool output, file fragments) in `documents` instead of the description. They are stored with the fact and fetched only when a later Decide asks for them.
- A finding is something the search produced that is not the goal itself. Its `kind` is defined by the task; reuse an existing kind when possible.
- `status` is `done` when the step was carried out, `abandoned` when the direction is invalid or impossible.
- A `done` step must submit a fact, a finding, or a note. When nothing new was confirmed, report the findings and a note that says what was tried and why it produced no fact; never invent a fact just to close the step.
- If you cannot do the step at all: `{"accepted": false, "reason": "..."}`.

## Graph

```
{graph_yaml}
```

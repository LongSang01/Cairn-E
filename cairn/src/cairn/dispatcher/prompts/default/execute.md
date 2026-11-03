# Execute

You own exactly one step. The graph records what is already known, so do not redo it.

Your step: **{step_id}** — {step_description}

Use whatever tools you need to act on the world (read, bash, edit, write). Then submit what you confirmed and close the step.

## Output

Return one raw JSON object, nothing else:

```json
{"accepted": true, "data": {
  "facts": [{"description": "a confirmed, objective statement about the world"}],
  "findings": [{"kind": "task-defined kind", "description": "a product of the search worth keeping"}],
  "status": "done",
  "note": "optional"
}}
```

## Rules

- A fact must be confirmed by evidence you actually observed. No plans, guesses, or restatements of known facts.
- A finding is something the search produced that is not the goal itself. Its `kind` is defined by the task; reuse an existing kind when possible.
- `status` is `done` when the step was carried out, `abandoned` when the direction is invalid or impossible.
- Submit at least one fact when the status is `done`.
- If you cannot do the step at all: `{"accepted": false, "reason": "..."}`.

## Graph

```
{graph_yaml}
```

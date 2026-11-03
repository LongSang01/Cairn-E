# Execute — conclude now

The step below ran out of time. Stop immediately: no more tool calls, no waiting for unfinished commands, no further inspection.

Your step: **{step_id}** — {step_description}

Report only what is already confirmed. If nothing is confirmed, abandon the step.

```json
{"accepted": true, "data": {
  "facts": [{"description": "confirmed statement"}],
  "findings": [{"kind": "task-defined kind", "description": "product of the search"}],
  "status": "done"
}}
```

- Do not invent results you did not observe.
- Use `"status": "abandoned"` if the step produced nothing.
- Output this JSON and stop.

## Graph

```
{graph_yaml}
```

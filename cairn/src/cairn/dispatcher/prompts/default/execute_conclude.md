# Execute — conclude now

The step below ran out of time. Stop immediately: no more tool calls, no waiting for unfinished commands, no further inspection.

Your step: **{step_id}** — {step_description}

Report only what is already confirmed. If nothing is confirmed, abandon the step.

```json
{"accepted": true, "data": {
  "facts": [{"description": "confirmed statement"}],
  "findings": [{"kind": "task-defined kind", "severity": "critical|high|medium|low|info",
                "description": "product of the search"}],
  "status": "done"
}}
```

- Do not invent results you did not observe.
- Grade each finding with `severity` = critical, high, medium, low or info, based only on what was confirmed.
- Use `"status": "abandoned"` if the step produced nothing.
- Output this JSON and stop.

## Graph

```
{graph_yaml}
```

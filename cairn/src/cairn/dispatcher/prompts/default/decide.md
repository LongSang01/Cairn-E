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

Return one raw JSON object, nothing else:

```json
{"accepted": true, "data": {
  "complete": {"from": ["origin"], "description": "why the goal is met"},
  "goals": {"add": [{"description": "sub goal", "parent": "g001"}],
            "close": [{"id": "g002", "status": "achieved"}]},
  "steps": {"add": [{"description": "what to do", "from": ["origin"], "goal": "g001", "priority": 3, "rationale": "which new fact this produces"}],
            "update": [{"id": "s002", "priority": 7}, {"id": "s003", "status": "abandoned", "note": "why"}]}
}}
```

Sections may be omitted. Use only ids present in the graph; never invent one.
If you genuinely cannot make progress: `{"accepted": false, "reason": "..."}`.

## Rules

- `complete` only when the facts already on the graph prove the root goal. Cite them in `from`.
- A step must state an action and how it turns known facts into a new fact. Small, concrete, verifiable steps beat broad ones.
- Priority is 0 (lowest) to 9 (highest). Higher priority runs first.
- Abandon steps that are exhausted, duplicated, or contradicted by new facts.
- Add at most {max_steps} steps in one run.
- Never add facts. Only Execute submits facts.

## Graph

```
{graph_yaml}
```

## Facts you may cite

```
{fact_ids}
```

## Open steps

```
{open_steps}
```

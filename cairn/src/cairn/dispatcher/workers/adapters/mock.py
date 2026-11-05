from __future__ import annotations

import json
import random

from cairn.dispatcher.config import WorkerConfig, resolve_mock_behavior
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult
from cairn.dispatcher.workers.stream import iter_json_events
from cairn.dispatcher.workers.usage import Usage, add_usage, normalize_usage

_SCRIPT = """
import json,random,sys,time

try:
    cfg=json.loads(sys.argv[1])
    prompt=json.loads(sys.argv[2])
    phase=prompt["phase"]
    phase_cfg=cfg[phase]
except Exception as exc:
    print(f"mock setup failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
delay=phase_cfg["delay"]
time.sleep(random.uniform(delay["min"],delay["max"]))

weights=dict(phase_cfg["outcomes"])
if phase=="decide":
    if not prompt.get("open_steps"):
        weights.pop("noop",None)
    if not prompt.get("fact_ids"):
        weights.pop("complete",None)
        weights.pop("steps",None)
choices=[(name,weight) for name,weight in weights.items() if weight>0]
if not choices:
    print(f"mock {phase} has no legal outcomes for prompt context", file=sys.stderr)
    raise SystemExit(2)

def _rule_matches(rule, prompt):
    fact_ids = prompt.get("fact_ids") or []
    open_steps = prompt.get("open_steps") or []
    if "fact_ids_gte" in rule and len(fact_ids) < rule["fact_ids_gte"]:
        return False
    if "fact_ids_lte" in rule and len(fact_ids) > rule["fact_ids_lte"]:
        return False
    if "open_steps_empty" in rule and (len(open_steps) == 0) != rule["open_steps_empty"]:
        return False
    return True

rules = phase_cfg.get("rules") or []
forced = None
for rule in rules:
    if _rule_matches(rule, prompt):
        forced = rule["force"]
        break

if forced is not None:
    outcome = forced
else:
    pick=random.uniform(0,sum(weight for _,weight in choices))
    total=0
    outcome=choices[-1][0]
    for name,weight in choices:
        total+=weight
        if pick<=total:
            outcome=name
            break

if phase=="healthcheck":
    raise SystemExit(0 if outcome=="ok" else 1)
if outcome=="command_fail":
    print(f"mock {phase} command failed", file=sys.stderr)
    raise SystemExit(1)
if outcome=="invalid_json":
    print("{invalid json")
    raise SystemExit(0)
if phase=="question":
    print("mock answer: derived from the current graph snapshot")
    raise SystemExit(0)
if phase=="summary":
    print("FACTS:\\n- mock fact summary (f001)\\nSTEPS:\\n- mock step summary (s001)\\nFINDINGS:\\n- mock finding summary (d001)")
    raise SystemExit(0)
if phase=="decide":
    fact_ids=prompt.get("fact_ids") or []
    open_steps=prompt.get("open_steps") or []
    max_s=prompt.get("max_steps",3)
    from_ids=[random.choice(fact_ids)] if fact_ids else []
    if outcome=="complete":
        print(json.dumps({"accepted":True,"data":{"complete":{"from":from_ids,"description":f"mock complete from {from_ids[0]}"}}}, ensure_ascii=False))
    elif outcome=="steps":
        count=random.randint(1,max(1,max_s))
        steps=[]
        for idx in range(count):
            fi=[random.choice(fact_ids)] if fact_ids else []
            steps.append({"from":fi,"description":f"mock step {idx+1} from {fi[0] if fi else 'none'}","priority":idx})
        payload={"steps":steps}
        if open_steps:
            payload["steps"]={"add":steps,"update":[{"id":open_steps[0]["id"],"priority":1}]}
        print(json.dumps({"accepted":True,"data":payload}, ensure_ascii=False))
    elif outcome=="noop":
        print(json.dumps({"accepted":True,"data":{}}, ensure_ascii=False))
    elif outcome=="rejected":
        print(json.dumps({"accepted":False,"reason":"mock_rejected"}, ensure_ascii=False))
    else:
        print(json.dumps({"accepted":True,"data":{"complete":{"description":"mock invalid payload"}}}, ensure_ascii=False))
    raise SystemExit(0)

if outcome=="facts":
    label = prompt.get("step_id") or phase
    print(json.dumps({"accepted":True,"data":{"facts":[{"description":f"mock fact for {label}"}],"findings":[{"kind":"mock","severity":"medium","description":f"mock finding for {label}"}],"status":"done"}}, ensure_ascii=False))
    print(json.dumps({"type":"usage","usage":{"input_tokens":120,"output_tokens":45,"cache_read_tokens":30,"cache_write_tokens":10,"total_tokens":205}}, ensure_ascii=False))
elif outcome=="rejected":
    print(json.dumps({"accepted":False,"reason":"mock_rejected"}, ensure_ascii=False))
else:
    print(json.dumps({"accepted":True,"data":{}}, ensure_ascii=False))
""".strip()


class MockDriver(SeedSessionDriver):
    type_name = "mock"

    def local_binary(self) -> str | None:
        return "python3"

    @staticmethod
    def _argv(worker: WorkerConfig, prompt: str) -> list[str]:
        behavior = resolve_mock_behavior(worker.name, worker.env)
        return ["python3", "-c", _SCRIPT, json.dumps(behavior, ensure_ascii=False), prompt]

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        outcomes = resolve_mock_behavior(worker.name, worker.env)["healthcheck"]["outcomes"]
        ok = random.random() < outcomes.get("ok", 0.0)
        return HealthResult(
            ok=ok, status=200 if ok else 503, detail="" if ok else "mock healthcheck fail"
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return "mock in-process healthcheck"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._argv(worker, prompt)

    def extract_usage(self, stdout: str, stderr: str) -> Usage | None:
        """The mock script appends a usage event after its payload."""
        usage: Usage | None = None
        for event in iter_json_events(stdout):
            if event.get("type") == "usage":
                usage = add_usage(usage, normalize_usage(event.get("usage")))
        return usage

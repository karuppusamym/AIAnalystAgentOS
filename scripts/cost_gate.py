"""CI cost gate (P4-T10, spec v3 §4.2): replay recorded runs and fail when model calls or tokens per
run rise by more than 10% over the committed baseline.

A recorded run (`tests/fixtures/cost_baseline/<name>.json`) is the ordered list of the points where
a run could have called a model, taken from its model_call rows:

* ``gate: rules``  - the deterministic rung was sufficient (a skip row);
* ``gate: model``  - a model was called (the rules were insufficient, or the purpose is model first);
  the redacted request and the recorded response are kept, so the call replays offline;
* ``gate: policy`` - skipped by workspace policy (e.g. independent-model verification opt-in).

Each point goes through the platform's own gates with the CURRENT code's default settings: chat
points through `agents.common.model_gate` and `ModelRouter.complete`, decision points through
`JevDecisions` and `ModelRouter.decide`. A counting transport answers from the recording (or, for a
point that used to be answered by rules, with a synthetic answer sized by the tokens the skip row
estimated) and counts calls and tokens. So a change that turns a purpose model-first, disables the
cache, adds retries, or ships a recording with an extra call is caught. New call sites in agent code
are caught when the recording is refreshed (`record`), and in CI by the live deterministic run
(`tests/integration/test_token_default.py`), which compares itself to the same baseline.

Dollars too: each replayed call is priced from the versioned price table (config/models.yaml) at the
model the CURRENT routing asks for, so moving a purpose back to a large model first (the $10-in-a-day
failure: Sonnet first for planning, hypotheses and SQL) fails the gate on `usd` even at equal calls.

Cheap first, escalate: a point may name a deterministic check (`validate`: sql | hypotheses) and carry
an `escalation_response`; the replay passes the check to the router, so a small-tier answer that fails
it is re-asked of the large tier - and only then (tests/fixtures/cost_baseline/escalation.json).

No network, no database, no API key: the transport never leaves the process.

Usage:
  python scripts/cost_gate.py                       # gate: exit 1 on a >10% rise
  python scripts/cost_gate.py --update-baseline     # accept the current counts (review the diff!)
  python scripts/cost_gate.py usd                   # expected $ per run: current routing vs large-model-first
  python scripts/cost_gate.py record --run-id RUN --name NAME   # needs the control-plane database
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "cost_baseline"
BASELINE = FIXTURES / "baseline.json"
TOLERANCE = 0.10

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------------------- replay
class CountingTransport:
    """Answers from the recording, else synthetically; counts every request that leaves the router."""

    def __init__(self, recorded: list[Any]) -> None:
        from analystos.llm.replay import ReplayTransport

        self.replay = ReplayTransport(recorded)
        self.hints: dict[str, int] = {}  # synthetic request id -> tokens the skip row estimated
        self.calls = 0
        self.tokens = 0
        self.usd = 0.0
        self.unpriced_calls = 0
        self.by_purpose: Counter[str] = Counter()
        self.by_model: Counter[str] = Counter()
        self.purpose = ""

    def _count(self, tokens: int, model: str, usd: float | None) -> None:
        self.calls += 1
        self.tokens += tokens
        self.by_purpose[self.purpose] += 1
        self.by_model[model] += 1
        if usd is None:
            self.unpriced_calls += 1
        else:
            self.usd += usd

    @staticmethod
    def _price(model: str, input_tokens: int, output_tokens: int, reported: Any) -> float | None:
        from analystos.llm.config import load_models_config

        priced = load_models_config().estimate_cost(model, input_tokens, output_tokens)
        if priced is not None:
            return priced
        return float(reported) if reported is not None else None

    def chat(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        from analystos.core.errors import ModelRouteUnavailable

        try:
            body = self.replay.chat(base_url=base_url, api_key=api_key, payload=payload, timeout=timeout)
        except ModelRouteUnavailable:
            hint = self.hints.get(payload["messages"][-1]["content"], 1000)
            body = {"model": payload["model"], "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": max(hint - 100, 1), "completion_tokens": 100}}
        body = {**body, "model": payload["model"]}  # the model the current routing asked for answers
        usage = {k: v for k, v in (body.get("usage") or {}).items() if k != "cost"}  # price table, not the recording
        body["usage"] = usage
        prompt, completion = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        self._count(prompt + completion, payload["model"], self._price(payload["model"], prompt, completion, None))
        return body

    def decide(self, *, base_url: str, api_key: str, payload: dict, timeout: float) -> dict:
        from analystos.core.errors import ModelRouteUnavailable

        try:
            body = self.replay.decide(base_url=base_url, api_key=api_key, payload=payload, timeout=timeout)
        except ModelRouteUnavailable:
            hint = self.hints.get(str(payload.get("state", {}).get("cost_gate_point")), 400)
            body = {"model": payload["model"], "answers": {}, "usage": {"input_tokens": max(hint - 5, 1), "output_tokens": 5}}
        usage = body.get("usage") or {}
        prompt, completion = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        self._count(prompt + completion, payload["model"], self._price(payload["model"], prompt, completion, usage.get("cost")))
        return body


def _recorded(points: list[dict]) -> list[Any]:
    """Recorded answers in call order; a point's `escalation_response` answers the same request second."""
    from analystos.llm.replay import RecordedCall

    out = []
    for p in points:
        if not (p.get("request") and p.get("response")):
            continue
        for response in (p["response"], p.get("escalation_response")):
            if response is not None:
                out.append(RecordedCall(id=len(out), purpose=p["purpose"], status="ok", model=response.get("model") or "-",
                                        provider="typesafe" if p["kind"] == "decision" else "openrouter", prompt_version=None,
                                        request=p["request"], response=response))
    return out


def _has_hypotheses(response: Any) -> str | None:
    data = response.data
    props = [h for h in data.get("hypotheses") or [] if isinstance(h, dict) and h.get("spec")] if isinstance(data, dict) else []
    return None if props else "empty hypothesis list"


def _validator(name: str | None) -> Any:
    """The deterministic checks a point may name (structural versions of the agents' checks)."""
    if not name:
        return None
    from analystos.agents.sql_agent import has_sql

    return {"sql": lambda r: has_sql(r.data), "hypotheses": _has_hypotheses}[name]


def replay_run(run: dict, *, settings: Any = None) -> dict[str, Any]:
    """Calls and tokens the current code would spend on this recorded run."""
    from analystos.agents.common import model_gate
    from analystos.contracts.platform import PlatformSettings
    from analystos.core.errors import AnalystOSError
    from analystos.llm.cache import ResponseCache
    from analystos.llm.jev import JevDecisions
    from analystos.llm.router import JSON_INSTRUCTION, CallContext, ModelRouter

    platform = settings or PlatformSettings()  # the defaults the code ships with
    transport = CountingTransport(_recorded(run["points"]))
    router = ModelRouter(transport=transport, api_key_lookup=lambda _env: "cost-gate", max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None))
    call = CallContext(workspace_id="cost-gate", run_id=run["name"])
    gate_ctx = SimpleNamespace(router=router, call_ctx=lambda **_: call)
    jev = JevDecisions(router)
    escalations: list[dict] = []
    for i, point in enumerate(run["points"]):
        purpose, gate = point["purpose"], point["gate"]
        transport.purpose = purpose
        if gate == "policy":
            continue
        request = point.get("request")
        hint = int(point.get("tokens_hint") or 0)
        try:
            if point["kind"] == "decision":
                if request:
                    state, questions = request["state"], request["questions"]
                else:
                    state, questions = {"cost_gate_point": str(i)}, {}
                    transport.hints[str(i)] = hint
                jev._call(purpose, state, questions, call)
                continue
            if not model_gate(gate_ctx, purpose, {}, deterministic_ok=gate == "rules"):
                continue
            if request:
                messages = [dict(m) for m in request["messages"]]
                if request.get("json_output") and messages and messages[0]["content"].endswith(JSON_INSTRUCTION):
                    messages[0]["content"] = messages[0]["content"][: -len(JSON_INSTRUCTION)]
                json_output, max_tokens = bool(request.get("json_output")), request.get("max_tokens")
            else:
                marker = f"cost-gate point {run['name']}#{i}"
                transport.hints[marker] = hint
                messages = [{"role": "system", "content": f"cost gate: {purpose}"}, {"role": "user", "content": marker}]
                json_output, max_tokens = True, None
            response = router.complete(purpose, messages, ctx=call, json_output=json_output, max_tokens=max_tokens,
                                       validate=_validator(point.get("validate")))
            if response.escalated_from:
                escalations.append({"point": i, "purpose": purpose, "from": response.escalated_from, "to": response.model,
                                    "reason": response.escalation_reason})
        except AnalystOSError:
            continue  # the platform degrades to its deterministic path; the attempt was already counted
    return {"calls": transport.calls, "tokens": transport.tokens, "usd": round(transport.usd, 6),
            "unpriced_calls": transport.unpriced_calls, "by_purpose": dict(sorted(transport.by_purpose.items())),
            "by_model": dict(sorted(transport.by_model.items())), "escalations": escalations}


def load_runs(fixtures: Path = FIXTURES) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(fixtures.glob("*.json")) if p.name != BASELINE.name]


def measure(runs: list[dict], *, settings: Any = None) -> dict[str, dict]:
    return {run["name"]: replay_run(run, settings=settings) for run in runs}


def compare(measured: dict[str, dict], baseline: dict, tolerance: float | None = None) -> list[str]:
    """Failures: a run whose calls or tokens rose more than `tolerance` over its baseline."""
    tol = baseline.get("tolerance", TOLERANCE) if tolerance is None else tolerance
    failures = []
    for name, got in sorted(measured.items()):
        base = baseline.get("runs", {}).get(name)
        if base is None:
            failures.append(f"{name}: no baseline (run with --update-baseline and commit it)")
            continue
        for metric in ("calls", "tokens", "usd"):
            if metric not in base or metric not in got:
                continue  # a baseline (or a caller) from before the metric existed
            limit = base[metric] * (1 + tol)
            if got[metric] > limit + 1e-9:
                failures.append(f"{name}: {metric} {got[metric]:g} > baseline {base[metric]:g} + {tol:.0%} ({limit:g})")
    return failures


def usd_report(runs: list[dict]) -> dict[str, dict]:
    """Expected $ per recorded run (price table, fake transport): the current default routing (cheap
    first, escalate on failure) against the large tier first for every purpose (the routing before
    2026-09-26, what max_quality still does), each with the ladders as shipped and with every
    rule-answered point sent to a model (the worst case: rules insufficient everywhere)."""
    from analystos.contracts.platform import PRESET_ESCALATION, PRESETS, LLMSettings, PlatformSettings

    large_first = PRESET_ESCALATION["max_quality"]
    model_first = PRESETS["max_quality"]
    scenarios = {
        "cheap_first": PlatformSettings(),
        "large_first": PlatformSettings(llm=LLMSettings(escalation=large_first)),
        "cheap_first_model_first_ladders": PlatformSettings(llm=LLMSettings(purpose_modes=model_first)),
        "large_first_model_first_ladders": PlatformSettings(llm=LLMSettings(purpose_modes=model_first, escalation=large_first)),
    }
    out: dict[str, dict] = {}
    for label, settings in scenarios.items():
        for name, got in measure(runs, settings=settings).items():
            out.setdefault(name, {})[label] = {k: got[k] for k in ("calls", "tokens", "usd", "unpriced_calls", "by_model")}
    return out


# ---------------------------------------------------------------------------------------- record
def points_from_run(run_id: str) -> list[dict]:
    """The recorded run's gate points, from its model_call rows (needs the control-plane database)."""
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall
    from analystos.llm.config import load_models_config
    from analystos.llm.replay import load_run_calls

    cfg = load_models_config()
    bodies = {c.id: c for c in load_run_calls(run_id)}
    with session_scope() as s:
        rows = list(s.scalars(select(ModelCall).where(ModelCall.run_id == run_id).order_by(ModelCall.id)))
        points = []
        for r in rows:
            if r.status == "error" and r.attempt > 1:
                continue  # a retry of the point before it
            decision = cfg.profiles.get(cfg.routing.get(r.purpose, ""), SimpleNamespace(provider="")).provider == "typesafe"
            if r.status == "skipped":
                gate = "policy" if (r.error or "").startswith("workspace policy") else "rules"
            else:
                gate = "model"
            recorded = bodies.get(r.id)
            points.append({"purpose": r.purpose, "kind": "decision" if decision else "chat", "gate": gate,
                           "tokens_hint": int(r.tokens_saved or 0) or int(r.input_tokens + r.output_tokens),
                           "request": recorded.request if recorded and gate == "model" else None,
                           "response": recorded.response if recorded and gate == "model" else None})
    return points


def write_fixture(name: str, points: list[dict], *, description: str, fixtures: Path = FIXTURES) -> Path:
    fixtures.mkdir(parents=True, exist_ok=True)
    path = fixtures / f"{name}.json"
    path.write_text(json.dumps({"name": name, "description": description, "points": points}, indent=1, sort_keys=True) + "\n")
    return path


# ---------------------------------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="check", choices=["check", "record", "usd"])
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--name")
    parser.add_argument("--description", default="")
    args = parser.parse_args(argv)
    baseline_path = args.baseline or args.fixtures / BASELINE.name
    if args.command == "record":
        if not (args.run_id and args.name):
            parser.error("record needs --run-id and --name")
        print(write_fixture(args.name, points_from_run(args.run_id), description=args.description, fixtures=args.fixtures))
        return 0
    if args.command == "usd":
        print(json.dumps(usd_report(load_runs(args.fixtures)), indent=2, sort_keys=True))
        return 0
    measured = measure(load_runs(args.fixtures))
    if args.update_baseline:
        baseline_path.write_text(json.dumps({"tolerance": TOLERANCE, "runs": {
            k: {"calls": v["calls"], "tokens": v["tokens"], "usd": v["usd"]} for k, v in measured.items()}},
            indent=2, sort_keys=True) + "\n")
        print(f"baseline written: {baseline_path}")
        return 0
    baseline = json.loads(baseline_path.read_text())
    for name, got in measured.items():
        base = baseline.get("runs", {}).get(name, {})
        print(f"{name}: calls {got['calls']} (baseline {base.get('calls')}), tokens {got['tokens']} "
              f"(baseline {base.get('tokens')}), ${got['usd']:.6f} (baseline {base.get('usd')}) "
              f"{got['by_purpose']} escalations {len(got['escalations'])}")
    failures = compare(measured, baseline)
    for f in failures:
        print(f"COST GATE FAILED: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

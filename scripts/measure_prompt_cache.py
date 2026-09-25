"""Live measurement of provider prompt caching (P4-T04 acceptance: ≥ 60% cached prompt share on a
cache-capable provider). Needs OPENROUTER_API_KEY and real model calls, so it is run by hand:

    # 1. from the model_call rows of a finished run (any run made after migration 0014)
    python scripts/measure_prompt_cache.py --run-id run_xxx [--out docs/60-delivery/evidence]

    # 2. a direct probe: N calls of one purpose with the production layout — static system text
    #    (with method vocabulary) and a workspace header as the cached prefix, a different
    #    volatile part each call (so the L0 response cache never answers)
    python scripts/measure_prompt_cache.py --probe --purpose hypothesis_generation --calls 6

Cached share = Σ cached_input_tokens / Σ input_tokens over successful calls, where the cached count
is what the provider reports (`prompt_tokens_details.cached_tokens` via OpenRouter). The first call
of a prefix writes the cache and is reported separately. Anthropic caches only prefixes of at least
~1,024 tokens (model-dependent); the probe prints the prefix estimate so a short prefix is visible.
Writes prompt-cache-<ts>.md/.json when --out is given.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def from_run(run_id: str) -> dict:
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall

    with session_scope() as s:
        rows = list(s.scalars(select(ModelCall).where(ModelCall.run_id == run_id, ModelCall.status == "ok")
                              .order_by(ModelCall.id)))
        calls = [{"purpose": r.purpose, "model": r.model, "input_tokens": r.input_tokens,
                  "cached_input_tokens": r.cached_input_tokens, "receipts": len(r.context_receipts or [])} for r in rows]
    return summarize(calls, source=f"model_call rows of run {run_id}")


def probe(purpose: str, n: int, router=None) -> dict:  # noqa: ANN001 - a ModelRouter; tests pass one on a fake transport
    from analystos.agents.common import compact_json
    from analystos.agents.prompts import PROMPTS, prompt
    from analystos.llm.cache import estimate_tokens
    from analystos.llm.router import CallContext, ModelRouter

    router = router or ModelRouter()
    name = f"{purpose}.v1" if f"{purpose}.v1" in PROMPTS else "hypothesis_generation.v1"
    system = prompt(name, **({"dialect": "postgres"} if "{dialect}" in PROMPTS[name] else {}))
    header = compact_json({"workspace_context": {"workspace": "prompt-cache probe", "domain_packs": ["pack.itsm@1.0.0"],
                                                 "dialects": ["postgres"]}})
    calls = []
    for i in range(n):
        variable = compact_json({"objective": f"Probe {i}: what drives SLA breaches for priority {i % 5 + 1} incidents?",
                                 "catalog": [{"asset": "sn.incident", "columns": ["made_sla", "priority", "assignment_group_name"]}]})
        messages = [{"role": "system", "content": system, "cache": True},
                    {"role": "user", "content": header, "cache": True},
                    {"role": "user", "content": variable}]
        r = router.complete(purpose, messages, ctx=CallContext(), json_output=True, max_tokens=200)
        calls.append({"purpose": purpose, "model": r.model, "input_tokens": r.input_tokens,
                      "cached_input_tokens": r.cached_input_tokens, "receipts": 0})
    out = summarize(calls, source=f"probe: {n} × {purpose}")
    out["prefix_estimated_tokens"] = estimate_tokens(system + header)
    return out


def summarize(calls: list[dict], *, source: str) -> dict:
    total = sum(c["input_tokens"] for c in calls)
    cached = sum(c["cached_input_tokens"] for c in calls)
    warm = calls[1:]
    warm_total = sum(c["input_tokens"] for c in warm)
    by_purpose: dict[str, dict] = {}
    for c in calls:
        p = by_purpose.setdefault(c["purpose"], {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0})
        p["calls"] += 1
        p["input_tokens"] += c["input_tokens"]
        p["cached_input_tokens"] += c["cached_input_tokens"]
    for p in by_purpose.values():
        p["cached_share"] = round(p["cached_input_tokens"] / p["input_tokens"], 4) if p["input_tokens"] else None
    return {"source": source, "calls": len(calls), "input_tokens": total, "cached_input_tokens": cached,
            "cached_share": round(cached / total, 4) if total else None,
            "cached_share_after_first_call": round(sum(c["cached_input_tokens"] for c in warm) / warm_total, 4) if warm_total else None,
            "by_purpose": by_purpose, "per_call": calls}


def write(report: dict, out: Path) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"prompt-cache-{ts}.json").write_text(json.dumps(report, indent=2))
    share = report["cached_share"]
    lines = [f"# Prompt cache share (P4-T04) — {ts} UTC", "",
             f"Source: {report['source']}. Live provider usage fields (not an estimate).", "",
             f"- calls: {report['calls']}; input tokens: {report['input_tokens']}; cached: {report['cached_input_tokens']}",
             f"- cached share: {share if share is None else f'{share:.1%}'} (target ≥ 60%)",
             f"- cached share after the first (cache-writing) call: {report['cached_share_after_first_call']}", "",
             "| purpose | calls | input tokens | cached | share |", "|---|---:|---:|---:|---:|"]
    for k, p in report["by_purpose"].items():
        lines.append(f"| {k} | {p['calls']} | {p['input_tokens']} | {p['cached_input_tokens']} | {p['cached_share']} |")
    path = out / f"prompt-cache-{ts}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--purpose", default="hypothesis_generation")
    ap.add_argument("--calls", type=int, default=6)
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    if not (args.run_id or args.probe):
        ap.error("give --run-id or --probe")
    report = from_run(args.run_id) if args.run_id else probe(args.purpose, args.calls)
    print(json.dumps({k: v for k, v in report.items() if k != "per_call"}, indent=2))
    if args.out:
        print(f"wrote {write(report, Path(args.out))}", file=sys.stderr)
    share = report["cached_share"]
    return 0 if share is not None and share >= 0.6 else 1


if __name__ == "__main__":
    raise SystemExit(main())

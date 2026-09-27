"""Paired practitioner baseline (P4-08, evaluation plan §4): blinding codes for the scorer and the paired
summary of a filled results sheet (`baseline_template.csv`). The protocol is
docs/60-delivery/06-practitioner-baseline-protocol.md; nothing here runs a human task.

A row is one arm's attempt at one task. Human effort counts every minute a person spent: the practitioner's
own work, and for AnalystOS the operator's briefing, review and correction time. The scorer sees only the
blinded code and the output, and records the rubric verdict with the same statuses as the runner.
"""
from __future__ import annotations

import csv
import random
import statistics
from pathlib import Path
from typing import Any

from evaluation.heldout.runner import STATUSES

TEMPLATE = Path(__file__).resolve().parent / "baseline_template.csv"
ARMS = ("analystos", "practitioner")
FIELDS = ("task_id", "arm", "participant", "blinded_code", "started_at", "finished_at", "active_minutes", "review_minutes",
          "correction_minutes", "interventions", "tools", "output_ref", "scorer", "verdict", "scorer_notes", "model_usd",
          "infrastructure_usd")


def assign_codes(task_ids: list[str], *, seed: int) -> dict[tuple[str, str], str]:
    """One opaque code per (task, arm), shuffled so a code reveals neither. The coordinator keeps the map;
    the scorer receives outputs under their code only."""
    pairs = [(t, a) for t in task_ids for a in ARMS]
    codes = [f"B{i:03d}" for i in range(1, len(pairs) + 1)]
    random.Random(seed).shuffle(codes)
    return dict(zip(pairs, codes, strict=True))


def load(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("task_id") and not r["task_id"].startswith("#")]


def problems(rows: list[dict[str, Any]]) -> list[str]:
    out = []
    missing = [c for c in FIELDS if rows and c not in rows[0]]
    if missing:
        out.append(f"missing columns: {', '.join(missing)}")
    seen = set()
    for r in rows:
        key = (r["task_id"], r["arm"])
        if r["arm"] not in ARMS:
            out.append(f"{r['task_id']}: arm must be one of {ARMS}")
        if key in seen:
            out.append(f"{r['task_id']}: two {r['arm']} rows")
        seen.add(key)
        if r.get("verdict") and r["verdict"] not in STATUSES:
            out.append(f"{r['task_id']} {r['arm']}: verdict {r['verdict']} is not a runner status")
    return out


def _minutes(r: dict[str, Any]) -> float:
    return sum(float(r.get(k) or 0) for k in ("active_minutes", "review_minutes", "correction_minutes"))


def _bootstrap(diffs: list[float], *, seed: int = 8, draws: int = 2000) -> tuple[float, float] | None:
    if len(diffs) < 2:
        return None
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(diffs, k=len(diffs))) for _ in range(draws))
    return round(meds[int(0.025 * draws)], 2), round(meds[int(0.975 * draws) - 1], 2)


def paired_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-arm quality and effort, and the paired effort comparison over tasks both arms completed and the
    scorer judged. The plan's target: at least 50% less human effort with no lower accepted quality."""
    by = {(r["task_id"], r["arm"]): r for r in rows if r.get("verdict")}
    arms: dict[str, Any] = {}
    for arm in ARMS:
        mine = [r for (_, a), r in by.items() if a == arm]
        good = [r for r in mine if r["verdict"] in ("accepted", "correct_abstention")]
        arms[arm] = {"scored": len(mine), "accepted_or_correct": len(good),
                     "quality": round(len(good) / len(mine), 4) if mine else None,
                     "confident_wrong": sum(r["verdict"] == "confident_wrong" for r in mine),
                     "median_human_minutes": statistics.median([_minutes(r) for r in mine]) if mine else None,
                     "interventions": sum(int(r.get("interventions") or 0) for r in mine)}
    tasks = sorted({t for t, _ in by if (t, "analystos") in by and (t, "practitioner") in by})
    diffs = [_minutes(by[(t, "practitioner")]) - _minutes(by[(t, "analystos")]) for t in tasks]
    ratios = [_minutes(by[(t, "analystos")]) / _minutes(by[(t, "practitioner")]) for t in tasks if _minutes(by[(t, "practitioner")])]
    q = {a: arms[a]["quality"] for a in ARMS}
    return {"arms": arms, "paired_tasks": len(tasks),
            "median_minutes_saved": statistics.median(diffs) if diffs else None,
            "median_minutes_saved_ci95": _bootstrap(diffs),
            "median_effort_ratio": round(statistics.median(ratios), 4) if ratios else None,
            "meets_plan_target": (None if not ratios or None in q.values() else
                                  bool(statistics.median(ratios) <= 0.5 and q["analystos"] >= q["practitioner"])),
            "disagreements": [t for t in tasks if (by[(t, "analystos")]["verdict"] in ("accepted", "correct_abstention"))
                              != (by[(t, "practitioner")]["verdict"] in ("accepted", "correct_abstention"))]}

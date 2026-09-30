"""P4-08 platform tier on the compose Postgres: held-out tasks uploaded as file sources, staged, and run
end to end (analysis runs through the local orchestrator and the gateway, recipe runs, ML experiments on
published specs). By default a smoke subset (accepted, refused, blocked and null outcomes across the
families) that must stay clean; with
ANALYSTOS_HELDOUT_FULL=1 the whole corpus, and with ANALYSTOS_HELDOUT_REPORT=<path.md> the dated evidence
report of the component and platform tiers is written there (+ .json). Skips cleanly without the stack."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
SMOKE = {"HO-SALES-01", "HO-SALES-05", "HO-DE-01", "HO-DE-04", "HO-DE-07", "HO-ML-02", "HO-ML-05", "HO-GOV-02", "HO-GOV-04",
         "HO-REC-04", "HO-REC-05"}


def test_heldout_platform_tier(control_db):
    from evaluation.heldout import runner as R

    for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY"):
        os.environ.pop(key, None)  # the rule path; a live run is scripts/benchmark_heldout.py --models live
    full = bool(os.environ.get("ANALYSTOS_HELDOUT_FULL"))
    picked = {t.strip() for t in os.environ.get("ANALYSTOS_HELDOUT_ONLY", "").split(",") if t.strip()}
    corpus = R.load_corpus()
    if picked:  # a measurement of the named tasks only (debugging a family); asserts harness health like FULL
        full, subset = True, picked
    else:
        subset = None if full else SMOKE
    platform = R.run("platform", only=subset, corpus=corpus)
    o = platform.summary["overall"]
    out = os.environ.get("ANALYSTOS_HELDOUT_REPORT")
    if out:
        from evaluation.heldout.report import render

        runs = {"component": R.run("component", only=subset, corpus=corpus), "platform": platform}
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(runs, corpus), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps({t: R.as_dict(r) for t, r in runs.items()}, indent=1, default=str) + "\n", encoding="utf-8")
    # The full corpus is a measurement (the non-blocking `heldout` report): only harness health is asserted.
    assert platform.lock_ok, platform.lock_mismatches
    assert o["errors"] == 0, [(x.id, x.reason) for x in platform.results if x.status == "error"]
    if not full:
        assert o["confident_wrong"] == 0, [(x.id, x.reason) for x in platform.results if x.status == "confident_wrong"]
        got = {x.id: x.status for x in platform.results}
        assert got["HO-DE-07"] == got["HO-ML-05"] == got["HO-SALES-05"] == "correct_abstention", got
        assert got["HO-DE-01"] == got["HO-ML-02"] == "accepted", [(x.id, x.reason) for x in platform.results]
        assert got["HO-DE-04"] == "correct_abstention", got
        # governance stops and the pinned-snapshot refusal (fixed 2026-09-27: pushdown used to ignore the pin)
        assert got["HO-GOV-02"] == got["HO-GOV-04"] == got["HO-REC-05"] == "correct_abstention", got
        assert got["HO-REC-04"] == "accepted", got

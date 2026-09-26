"""Model cards from bound facts (P5-02, ADR-0024 decision 2).

Every number on a card is a fact taken from the sealed evaluation report, with the report path it came
from; the prose is a template that only references facts. A model may later rewrite the prose, but the
numbers guard (`prose_is_bound`) keeps any sentence whose numbers are not facts off the card.
"""
from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _fact(facts: list[dict[str, Any]], label: str, value: Any, path: str, unit: str | None = None) -> str:
    if value is None:
        return "n/a"
    fid = f"F{len(facts) + 1}"
    v = round(float(value), 4) if isinstance(value, int | float) and not isinstance(value, bool) else value
    facts.append({"id": fid, "label": label, "value": v, "unit": unit, "source": f"evaluation{path}"})
    return f"{v}"


def build(spec: dict[str, Any], result: dict[str, Any], *, experiment_id: str, dataset: dict[str, Any],
          title: str | None = None) -> dict[str, Any]:
    ev = result.get("evaluation") or {}
    task, metric = spec.get("task"), ev.get("metric")
    facts: list[dict[str, Any]] = []
    base, cand = ev.get("baseline") or {}, ev.get("candidate") or {}
    decision = ev.get("decision") or {}
    unc = ev.get("uncertainty") or {}
    manifest = result.get("manifest") or {}
    rows = manifest.get("rows") or {}
    b = _fact(facts, f"baseline holdout {metric}", base.get(metric), f"/baseline/{metric}")
    c = _fact(facts, f"candidate holdout {metric}", cand.get(metric), f"/candidate/{metric}")
    lo = _fact(facts, "gain interval low", unc.get("ci_low"), "/uncertainty/ci_low")
    hi = _fact(facts, "gain interval high", unc.get("ci_high"), "/uncertainty/ci_high")
    n_train = _fact(facts, "training rows", rows.get("train"), "/../manifest/rows/train", "rows")
    n_hold = _fact(facts, "holdout rows", rows.get("holdout"), "/../manifest/rows/holdout", "rows")
    sel = result.get("selection") or {}
    ntr = _fact(facts, "candidate trials run", len(result.get("trials") or []) - 1, "/../trials", "trials")
    verdict = result.get("verdict")
    lines = [f"# Model card: {title or spec.get('target') or task}", "",
             f"**Experiment** {experiment_id} · **task** {task} · **verdict** {verdict}", "",
             "## Intended use",
             f"Batch predictions of `{spec.get('target') or 'structure'}` from `{dataset.get('asset')}` (dataset version "
             f"`{str(dataset.get('version'))[:12]}`). Online serving is out of scope.", "",
             "## Data and validation",
             f"Split `{manifest.get('strategy')}` (seed {manifest.get('seed')}), manifest "
             f"`{str(result.get('manifest_hash'))[:12]}`: {n_train} training rows, {n_hold} holdout rows. The holdout was read "
             "once, after the selection froze.", "",
             "## Model",
             f"Selected `{sel.get('estimator')}` with {sel.get('params')} from {ntr} bounded "
             f"trial(s) against the `{spec.get('baseline') or (result.get('trials') or [{}])[0].get('estimator')}` baseline on "
             "identical folds.", "",
             "## Performance",
             f"Holdout {metric}: candidate {c}, baseline {b}; 95% interval of the gain {lo} to {hi}. {decision.get('reason', '')}"]
    if task == "classify" and ev.get("threshold"):
        t = _fact(facts, "decision threshold", ev["threshold"].get("threshold"), "/threshold/threshold", "probability")
        p = _fact(facts, "precision at threshold", cand.get("precision"), "/candidate/precision", "fraction")
        r = _fact(facts, "recall at threshold", cand.get("recall"), "/candidate/recall", "fraction")
        ece = _fact(facts, "expected calibration error", ((ev.get("calibration") or {}).get("candidate") or {})
                    .get("expected_calibration_error"), "/calibration/candidate/expected_calibration_error")
        lines += [f"Threshold {t} (chosen on out-of-fold predictions for the declared error costs): precision {p}, recall {r}; "
                  f"calibration error {ece}."]
    if task == "regress":
        mae = _fact(facts, "holdout MAE", cand.get("mae"), "/candidate/mae", spec.get("target_unit"))
        lines += [f"Mean absolute error {mae} {spec.get('target_unit') or '(target units)'}."]
    if task == "forecast":
        cov = _fact(facts, "95% interval coverage", cand.get("coverage_95"), "/candidate/coverage_95", "fraction")
        lines += [f"95% interval coverage on the holdout: {cov}. Error by horizon step is in the evaluation report."]
    if task == "cluster":
        st = _fact(facts, "stability (ARI)", (ev.get("stability") or {}).get("score"), "/stability/score")
        lines += [f"Stability across seeds and bootstraps (adjusted Rand index): {st}."]
    guard = [s for g in ev.get("guardrails") or [] for s in g.get("slices") or [] if s.get("status") == "fail"]
    lines += ["", "## Slices and guardrails",
              f"{_fact(facts, 'failed guardrail slices', len(guard), '/guardrails', 'slices')} guardrail slice(s) failed."
              if ev.get("guardrails") else "No slice guardrails were declared.",
              "", "## Limitations",
              "- Feature importance explains the model's behaviour, not causality.",
              "- Input drift alone does not show a loss of performance; that is measured when labels mature.",
              "- Slices with too few holdout rows are untested."]
    if task == "cluster":
        lines.append("- Clusters describe similarity in the chosen features and carry no causal meaning.")
    markdown = "\n".join(lines)
    return {"version": "ml_model_card.v1", "experiment_id": experiment_id, "verdict": verdict, "facts": facts,
            "markdown": markdown, "prose_source": "template", "bound": prose_is_bound(markdown, facts)}


def prose_is_bound(text: str, facts: list[dict[str, Any]]) -> bool:
    """Every number in the prose equals a fact, or is structural (a year-free small count, a seed, an id)."""
    allowed = {str(f["value"]) for f in facts} | {str(i) for i in range(0, 11)} | {"95"}
    body = re.sub(r"`[^`]*`", "", re.sub(r"\{[^}]*\}", "", text))  # code spans and parameter dicts are identifiers
    body = re.sub(r"\(seed -?\d+\)", "", body)
    return all(n in allowed or n.lstrip("-") in allowed for n in _NUM.findall(body.split("## Intended use", 1)[-1]))

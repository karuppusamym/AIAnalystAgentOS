"""P4-08: the held-out corpus is well formed, frozen and disjoint from the development benchmarks; its
rubrics match the data they describe; the judges classify outcomes as the rubric vocabulary says; the
held-out gate is a non-blocking report; and the practitioner-baseline pairing computes what the protocol
promises. No services."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from evaluation import datasets as dev
from evaluation import ml_datasets as dev_ml
from evaluation.heldout import baseline as B
from evaluation.heldout import generators as G
from evaluation.heldout import runner as R
from evaluation.heldout import scenarios as S


@pytest.fixture(scope="module")
def corpus():
    return R.load_corpus()


def test_the_corpus_is_well_formed_and_covers_every_family(corpus):
    assert R.corpus_problems(corpus) == []
    # evaluation plan §2: at least 60 tasks, 20 analyst, 15 engineering, 15 ML, 10 unsupported / insufficient
    assert len(corpus.tasks) >= 60
    count = {f: sum(t.family == f for t in corpus.tasks) for f in R.FAMILIES}
    assert count["analysis"] >= 20 and count["engineering"] >= 15 and count["ml"] >= 15, count
    assert sum(t.expect == "abstain" for t in corpus.tasks) >= 10
    for family in R.FAMILIES:
        tasks = [t for t in corpus.tasks if t.family == family]
        assert {t.expect for t in tasks} == {"deliver", "abstain"}, family
    analysis = {t.domain for t in corpus.tasks if t.family == "analysis"}
    assert analysis == {"itsm", "sales", "finance", "retail", "logistics", "saas_ops", "transfer"}
    for domain in analysis:
        kinds = {(t.expect, t.effects) for t in corpus.tasks if t.domain == domain}
        assert ("deliver", True) in kinds and ("abstain", False) in kinds  # planted truths and null controls


def test_transfer_tasks_rename_every_column_and_keep_the_values(corpus):
    """The transfer suite changes names and order only: the renamed table holds the same values."""
    from evaluation.heldout.generators import ANALYSIS

    transfer = [t for t in corpus.tasks if t.transform]
    assert len(transfer) >= 4 and {t.domain for t in transfer} == {"transfer"}
    for t in transfer:
        base = ANALYSIS[t.generator](t.seed, effects=t.effects).frame
        ds = R.analysis_dataset(t)
        assert ds.table == t.transform["table"] and not set(base.columns) & set(ds.frame.columns), t.id
        back = ds.frame.rename(columns={v: k for k, v in t.transform["rename"].items()})[list(base.columns)]
        key = base.columns[0]
        pd.testing.assert_frame_equal(back.sort_values(key).reset_index(drop=True), base.sort_values(key).reset_index(drop=True))
        assert list(ds.frame.columns) != [t.transform["rename"][c] for c in base.columns]  # reordered, not only renamed


def test_the_corpus_matches_its_lock(corpus):
    """A generator, seed, reference or rubric change without a version bump and a re-lock fails here."""
    assert R.check_lock(corpus) == []


def test_the_corpus_is_disjoint_from_the_development_benchmarks(corpus):
    dev_tables = {dev.build(d, 1, n=50).table for d in dev.DOMAINS}
    dev_columns = {c for d in dev.DOMAINS for c in dev.build(d, 1, n=50).frame.columns}
    dev_columns |= set(dev_ml.churn(n=5)[0]) | set(dev_ml.weekly_series(n=5)[0]) | set(dev_ml.blobs(n=5)[0])
    dev_columns |= {"customer_id", "name", "region", "signup_date", "order_id", "order_date", "amount", "status"}  # recipe fixtures
    for t in corpus.tasks:
        assert 7000 <= t.seed <= 7999  # development suites use 1-10, 101-105 and single digits / 20s
        if t.family == "analysis":
            ds = R.analysis_dataset(t)
            assert ds.table not in dev_tables, t.id
            assert not set(ds.frame.columns) & dev_columns, (t.id, set(ds.frame.columns) & dev_columns)
        elif t.family == "engineering":
            tables, _, _ = G.engineering(t.generator, t.seed, t.variant)
            cols = {c for tb in tables.values() for c in tb["columns"]}
            assert not cols & dev_columns and not {a.split(".")[1] for a in tables} & dev_tables, (t.id, cols & dev_columns)
        elif t.family == "ml":
            (cols, _, _), _ = G.ml_task(t.generator, t.seed, t.variant)
            assert not set(cols) & dev_columns, (t.id, set(cols) & dev_columns)
        else:
            fx = S.fixture(t)
            tables = {**(fx.get("tables") or {}), **(fx.get("changed") or {})}
            cols = {c for tb in tables.values() for c in tb["columns"]}
            cols |= {f["column"] for f in (fx.get("spec") or {}).get("features") or []}
            assert not cols & dev_columns and not {a.split(".")[1] for a in tables} & dev_tables, (t.id, cols & dev_columns)


def _outcome(frame: pd.DataFrame, node: str) -> pd.Series:
    if "->" in node:
        start, end = node.split("->")
        return (frame[end] - frame[start]).dt.total_seconds() / 3600
    return frame[node].astype(float)


def test_every_planted_effect_is_in_the_data_with_its_top_segment(corpus):
    for t in corpus.tasks:
        if t.family != "analysis" or t.expect != "deliver":
            continue
        frame = R.analysis_dataset(t).frame
        for p in t.rubric["planted"]:
            by = _outcome(frame, p["outcome"]).groupby(frame[p["segment"]])
            stat = by.median() if p["method"] == "numeric_by_segment" else by.mean()
            assert stat.idxmax() == p["top"], (t.id, p["id"], stat.to_dict())
            assert stat.max() / stat.drop(p["top"]).max() > 1.5, (t.id, p["id"])  # material, not marginal


def test_null_columns_carry_no_planted_effect(corpus):
    """Drawn independently by construction; a test at p < 0.001 would flag a generator that leaked an effect."""
    from scipy import stats

    for t in corpus.tasks:
        if t.family != "analysis" or t.degenerate:
            continue
        frame = R.analysis_dataset(t).frame
        for p in t.rubric.get("planted") or []:
            y = _outcome(frame, p["outcome"])
            for c in t.rubric["nulls"]:
                groups = [g.to_numpy() for _, g in y.groupby(frame[c])]
                assert stats.kruskal(*groups).pvalue > 1e-3, (t.id, p["outcome"], c)


# ------------------------------------------------------------------------------------ judges
def _task(corpus, task_id):
    return next(t for t in corpus.tasks if t.id == task_id)


def _score(findings, status="COMPLETED"):
    return SimpleNamespace(findings=[SimpleNamespace(statement="", **f) for f in findings], tested=len(findings), status=status)


def _f(truth, planted=None, top="x"):
    return {"method": "rate_by_segment", "outcome": "o", "segment": "s", "top": top, "truth": truth, "planted": planted}


def test_the_analysis_judge(corpus):
    deliver, null = _task(corpus, "HO-SALES-01"), _task(corpus, "HO-SALES-05")
    assert R.judge_analysis(deliver, _score([_f("true", "churn_by_cycle"), _f("true", "mrr_by_tier")])).status == "accepted"
    assert R.judge_analysis(deliver, _score([_f("true", "churn_by_cycle")])).status == "incomplete"
    assert R.judge_analysis(deliver, _score([])).status == "unnecessary_abstention"
    wrong = R.judge_analysis(deliver, _score([_f("true", "churn_by_cycle"), _f("true", "mrr_by_tier"), _f("false")]))
    assert wrong.status == "confident_wrong" and "false verified finding" in wrong.reason
    assert R.judge_analysis(null, _score([])).status == "correct_abstention"
    assert R.judge_analysis(null, _score([_f("false")])).status == "confident_wrong"
    assert R.judge_analysis(null, _score([], status="FAILED")).status == "error"


def test_the_engineering_and_ml_judges(corpus):
    clean, fanout, blocked = _task(corpus, "HO-DE-01"), _task(corpus, "HO-DE-07"), _task(corpus, "HO-DE-04")
    _, _, ref = G.engineering(clean.generator, clean.seed, clean.variant)
    assert R.judge_engineering(clean, columns=ref["columns"], rows=ref["rows"]).status == "accepted"
    altered = [list(r) for r in ref["rows"]]
    altered[0][-1] = altered[0][-1] * 1.01
    assert R.judge_engineering(clean, columns=ref["columns"], rows=altered).status == "confident_wrong"
    assert R.judge_engineering(fanout, refused="join pre-flight").status == "correct_abstention"
    assert R.judge_engineering(fanout, columns=["a"], rows=[[1]]).status == "confident_wrong"  # published a refused output
    assert R.judge_engineering(blocked, refused="validation").status == "wrong_abstention"  # blocked was required
    assert R.judge_engineering(clean, blocked="fail gate").status == "incomplete"
    ml, null, leak = _task(corpus, "HO-ML-01"), _task(corpus, "HO-ML-04"), _task(corpus, "HO-ML-05")
    assert R.judge_ml(ml, {"status": "succeeded", "verdict": "improved", "_parity": True}).status == "accepted"
    assert R.judge_ml(null, {"status": "succeeded", "verdict": "improved"}).status == "confident_wrong"
    assert R.judge_ml(null, {"status": "succeeded", "verdict": "no_improvement"}).status == "correct_abstention"
    assert R.judge_ml(leak, {"status": "succeeded", "verdict": "no_improvement"}).status == "wrong_abstention"
    assert R.judge_ml(leak, None, refused="target_derived_features").status == "correct_abstention"
    assert R.judge_abstention(leak, "denied", "capability disabled").status == "wrong_abstention"  # not the data's fault
    assert R.judge_abstention(ml, "denied", "capability disabled").status == "unnecessary_abstention"


def test_the_scenario_judge(corpus):
    """Governance and recovery: an action that went through where the rubric requires a stop is confident-wrong;
    a governance stop counts only as `denied`; a deliver scenario is accepted only when it met its rubric."""
    deny, pin, ctl = _task(corpus, "HO-GOV-04"), _task(corpus, "HO-REC-05"), _task(corpus, "HO-GOV-07")
    assert R.judge_scenario(deny, {"outcome": "stopped", "kind": "denied", "reason": "expired"}).status == "correct_abstention"
    assert R.judge_scenario(deny, {"outcome": "stopped", "kind": "refused", "reason": "x"}).status == "wrong_abstention"
    assert R.judge_scenario(deny, {"outcome": "acted", "reason": "consumed"}).status == "confident_wrong"
    assert R.judge_scenario(pin, {"outcome": "acted", "reason": "published"}).status == "confident_wrong"
    assert R.judge_scenario(pin, {"outcome": "stopped", "kind": "refused", "reason": "changed"}).status == "correct_abstention"
    assert R.judge_scenario(ctl, {"outcome": "delivered", "ok": True}).status == "accepted"
    assert R.judge_scenario(ctl, {"outcome": "delivered", "ok": False}).status == "incomplete"
    assert R.judge_scenario(ctl, {"outcome": "delivered", "ok": False, "wrong": True}).status == "confident_wrong"
    assert R.judge_scenario(ctl, {"outcome": "stopped", "kind": "denied", "reason": "x"}).status == "unnecessary_abstention"
    assert {t.generator for t in corpus.tasks if t.family in ("governance", "recovery")} == set(S.SCENARIOS)


def test_metrics_report_rates_latency_and_leave_infrastructure_unpriced():
    def res(expect, status, produced, seconds, usd=0.0):
        return R.TaskResult(id=status, family="analysis", domain="sales", expect=expect, abstain_kind=None, status=status,
                            produced=produced, seconds=seconds, cpu_seconds=seconds, model_usd=usd)

    rows = [res("deliver", "accepted", "output", 1.0, 0.02), res("deliver", "incomplete", "output", 3.0),
            res("abstain", "correct_abstention", "abstained", 2.0), res("abstain", "confident_wrong", "output", 10.0)]
    m = R.metrics(rows)
    assert (m["accepted_output_rate"], m["confident_wrong"], m["abstention_recall"], m["abstention_precision"]) == (0.5, 1, 0.5, 1.0)
    assert m["latency_seconds"]["p50"] == 2.5 and m["cost_per_accepted"]["model_usd"] == 0.02
    assert m["cost_per_accepted"]["infrastructure_usd"] == "unpriced"
    assert R.metrics(rows, cpu_usd_per_hour=3600.0)["cost_per_accepted"]["infrastructure_usd"] == 16.0


def test_a_component_smoke_subset_runs_end_to_end(corpus):
    run = R.run("component", only={"HO-DE-01", "HO-DE-05", "HO-ML-05", "HO-SALES-06", "HO-GOV-02", "HO-GOV-05", "HO-GOV-08",
                                   "HO-REC-04", "HO-ML-16"}, corpus=corpus)
    got = {x.id: x.status for x in run.results}
    assert got == {"HO-DE-01": "accepted", "HO-DE-05": "correct_abstention", "HO-ML-05": "correct_abstention",
                   "HO-SALES-06": "correct_abstention", "HO-GOV-02": "correct_abstention", "HO-GOV-05": "correct_abstention",
                   "HO-GOV-08": "accepted", "HO-REC-04": "accepted", "HO-ML-16": "correct_abstention"}, \
        [(x.id, x.reason) for x in run.results]
    assert {x.abstained_as for x in run.results if x.id.startswith("HO-GOV")} == {"denied", None}
    assert run.lock_ok and R.gate_metrics(run)["confident_wrong"] == 0


# ------------------------------------------------------------------------------------ gate and baseline
def test_the_heldout_gate_is_a_non_blocking_report(monkeypatch):
    from evaluation import gates as Gt

    gates = Gt.load()
    assert gates.tiers["heldout"].blocking is False and "heldout" in Gt.deterministic_ci_tiers(gates)
    assert all(gates.tiers[n].blocking for n in gates.tiers if n != "heldout")
    monkeypatch.setitem(Gt.RUNNERS, "heldout", lambda: {"accepted_output_rate": 0.1, "confident_wrong": 3, "abstention_recall": 0.2,
                                                        "errors": 0, "corpus_frozen": 1.0, "tasks": 34})
    result = Gt.run_gates(["heldout"], gates)
    assert result["tiers"]["heldout"]["status"] == "failed" and result["tiers"]["heldout"]["failures"]
    assert result["passed"] and result["reports"] == ["heldout"]


def test_the_paired_baseline_summary(tmp_path):
    codes = B.assign_codes(["HO-A", "HO-B"], seed=4)
    assert len(set(codes.values())) == 4 and codes == B.assign_codes(["HO-A", "HO-B"], seed=4)
    header = B.TEMPLATE.read_text().splitlines()[0]
    rows = [
        "HO-A,analystos,op1,B001,,,5,4,1,1,,,s1,accepted,,0.01,",
        "HO-A,practitioner,p1,B002,,,50,10,0,0,,,s1,accepted,,,",
        "HO-B,analystos,op1,B003,,,6,3,3,2,,,s1,correct_abstention,,0.0,",
        "HO-B,practitioner,p2,B004,,,30,5,0,0,,,s1,confident_wrong,claimed an effect,,",
    ]
    path = tmp_path / "results.csv"
    path.write_text("\n".join([header, *rows]) + "\n")
    loaded = B.load(path)
    assert B.problems(loaded) == []
    s = B.paired_summary(loaded)
    assert s["paired_tasks"] == 2 and s["arms"]["analystos"]["quality"] == 1.0 and s["arms"]["practitioner"]["confident_wrong"] == 1
    assert s["median_minutes_saved"] == (50.0 + 23.0) / 2 and s["meets_plan_target"] is True and s["disagreements"] == ["HO-B"]
    assert B.problems(B.load(B.TEMPLATE)) == []  # the shipped template parses (its comment row is skipped)

"""P5-01..P5-06 end to end on the compose Postgres: train -> evaluate -> approve -> score -> monitor.

A CSV source is staged and read only through the query gateway. Published `ml_spec` definitions train (a
draft is refused, a leakage fixture is refused and recorded); the verdict is a P7-01 verification record
bound to the dataset version, split manifest, package and code; a consumed holdout refuses another spec;
retraining registers a challenger and never promotes; promotion and rollback are hash-bound approvals; a
promotion backed by a tampered package is refused (its verdict turns VOID); approved batch scoring of a
published, pinned definition writes predictions and rejected rows to the managed output, a repeat is a
duplicate, schema drift and a non-champion version are refused; drift, freshness and delayed-label monitors
evaluate; the train.v1 and score.v1 playbooks run the same path; the MLflow export carries the model.
Skips cleanly without the stack."""
from __future__ import annotations

import csv
import io
import shutil
import time
import zipfile
from pathlib import Path

import pytest
from evaluation import ml_datasets as D
from sqlalchemy import select

pytestmark = pytest.mark.integration
COLUMNS = ["customer_id", "region", "tenure_months", "monthly_spend", "support_calls", "churned", "refund_issued", "coin_flip"]


def _write(path: Path, cols: list[str], rows: list[list]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])


def _table(n: int, seed: int, *, signal: bool = True) -> list[list]:
    cols, rows, _ = D.churn(n=n, seed=seed, signal=signal)
    idx = [cols.index(c) for c in COLUMNS]
    return [[r[i] for i in idx] for r in rows]


def _wait(run_id: str, statuses: set[str], timeout: float = 600) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            run = s.get(AnalysisRun, run_id)
            status, error = run.status, run.error
        if status in statuses:
            return status if status != "FAILED" else f"FAILED: {error}"
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.capabilities import enablement, registry
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Source, User
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    get_settings.cache_clear()
    folder = f"ml-{new_id('t')}"
    upload = Path(get_settings().upload_dir) / folder
    upload.mkdir(parents=True, exist_ok=True)
    train = _table(400, 7)
    _write(upload / "customers.csv", COLUMNS, train)
    new = _table(60, 99)
    new[5][0] = None  # a row without its entity key: rejected, never scored
    _write(upload / "customers_new.csv", COLUMNS, new)
    _write(upload / "customers_next.csv", COLUMNS, _table(40, 100))
    drift = _table(40, 101)
    for r in drift:
        r[COLUMNS.index("tenure_months")] = f"{r[COLUMNS.index('tenure_months')]} months"  # type drift: now text
    _write(upload / "customers_drift.csv", COLUMNS, drift)
    labels = [[r[0], r[COLUMNS.index("churned")]] for r in new if r[0]]
    _write(upload / "churn_labels.csv", ["customer_id", "churned"], labels)
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        ws = create_workspace(s, owner, name=f"ml {folder}", objective="Predict which customers will churn next month",
                              autonomy_level=2)
        s.flush()
        add_member(s, owner, ws.id, "approver@analystos.local", "approver")
        src = register_source(s, owner, ws.id, kind="csv", name="crm files", config={"path": folder}, secret_ref=None)
        s.flush()
        ws_id, src_id = ws.id, src.id
        for pb in ("playbook.train", "playbook.score"):
            enablement.set_enabled(s, s.merge(owner), ws.id, pb, True, registry.current())
        s.expunge(owner)
    discover_source(owner, src_id, ws_id)
    select_assets(owner, src_id, ["customers", "customers_new", "customers_next", "customers_drift", "churn_labels"], ws_id)
    with session_scope() as s:
        schema = s.get(Source, src_id).staging_schema or f"src_{src_id}"
    yield {"ws": ws_id, "src": src_id, "schema": schema, "owner": owner, "upload": upload}
    shutil.rmtree(upload, ignore_errors=True)


def _approver():
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        s.expunge(u)
    return u


def _approve(approval_id: str) -> None:
    from analystos.db.base import session_scope
    from analystos.governance.approvals import decide

    with session_scope() as s:
        decide(s, approval_id, s.merge(_approver()), approve=True)


def _publish(world, kind: str, key: str, spec: dict) -> dict:
    from analystos.contracts.definition import DefinitionDraftIn
    from analystos.db.base import session_scope
    from analystos.services import definitions as defs

    with session_scope() as s:
        row = defs.create_draft(s, s.merge(world["owner"]), world["ws"], DefinitionDraftIn(kind=kind, key=key, spec=spec))
        defs.publish(s, s.merge(world["owner"]), row, row.revision)
        return {"key": key, "version": row.version, "id": row.id}


def _spec(world, **over) -> dict:
    return D.spec(dataset={"asset": f"{world['schema']}.customers"}, **over)


def _scoring(world, key: str, version: dict, asset: str, output: str) -> dict:
    return _publish(world, "ml_scoring", key, {"model": version["name"], "model_version_id": version["id"],
                                               "package_hash": version["package_hash"],
                                               "input": {"asset": f"{world['schema']}.{asset}"}, "output": output})


def _score(world, definition: dict) -> dict:
    from analystos.services import ml

    plan = ml.plan_scoring(world["owner"], world["ws"], definition["id"])
    if plan["status"] != "approval_required":
        return plan
    _approve(plan["approval_id"])
    return ml.execute_scoring(world["owner"], plan["scoring_run_id"], world["ws"])


# ------------------------------------------------------------------------------------ refusals before training
def test_drafts_and_leakage_are_refused_and_recorded(world):
    from analystos.contracts.definition import DefinitionDraftIn
    from analystos.core.errors import InvalidInput, PolicyDenied
    from analystos.db.base import session_scope
    from analystos.db.models import MLExperiment
    from analystos.services import definitions as defs
    from analystos.services import ml

    with session_scope() as s:
        defs.create_draft(s, s.merge(world["owner"]), world["ws"], DefinitionDraftIn(kind="ml_spec", key="draft_only",
                                                                                    spec=_spec(world)))
    with pytest.raises(PolicyDenied, match="draft"):
        ml.start_experiment(world["owner"], world["ws"], {"key": "draft_only", "version": 1})
    leak = _publish(world, "ml_spec", "leaky", _spec(world, features=[{"column": "support_calls"}, {"column": "refund_issued"}]))
    with pytest.raises(InvalidInput, match="target_derived_features") as err:
        ml.start_experiment(world["owner"], world["ws"], leak["id"])
    with session_scope() as s:
        exp = s.get(MLExperiment, err.value.details["experiment_id"])
        assert exp.status == "refused" and exp.package_hash is None and exp.dataset_version  # read, never fitted
        assert exp.query_ids  # the dataset came through the gateway


# ------------------------------------------------------------------------------------ the governed lifecycle
def test_train_evaluate_approve_score_monitor(world):
    from analystos.core.config import get_settings
    from analystos.core.errors import ApprovalRequired, Conflict, PolicyDenied
    from analystos.db.base import session_scope
    from analystos.db.models import Artifact, MLModelVersion, MLScoringRun, MLSplit, QueryExecution, VerificationRecord
    from analystos.ml.store import MLStore
    from analystos.services import ml
    from analystos.services.monitors import create_monitor, evaluate_monitor

    ws, owner = world["ws"], world["owner"]
    churn = _publish(world, "ml_spec", "churn", _spec(world))
    first = ml.start_experiment(owner, ws, churn["id"])
    assert first["status"] == "succeeded" and first["verdict"] == "improved"
    v1 = first["model_version"]
    assert v1["status"] == "candidate" and v1["version"] == 1
    assert set(first["artifacts"]) == set(ml.RECORD_TYPES)
    with session_scope() as s:
        rec = s.get(VerificationRecord, first["verification_record_id"])
        kinds = {d["kind"] for d in rec.dependencies}
        assert rec.state == "ACTIVE" and rec.verdict == "verified" and {"ml_dataset", "ml_split", "ml_package", "method"} <= kinds
        evaluation = s.get(Artifact, first["artifacts"]["ml_evaluation"]).content
        assert evaluation["sealed"] and evaluation["seal"] == first["evaluation_seal"]
        split = s.get(MLSplit, first["split_id"])
        assert split.holdout_consumed_by == first["id"] and split.manifest_hash == first["manifest_hash"]
        assert s.get(QueryExecution, first["query_ids"][0]).purpose.startswith("ml.experiment:")
        bundle = s.scalar(select(ml.MLExperiment.summary).where(ml.MLExperiment.id == first["id"]))["evidence_bundle"]
        assert bundle["validation"]["predictive_evaluated"] is True

    # the same spec reproduces on the consumed holdout; retraining registers a challenger, never a champion
    again = ml.start_experiment(owner, ws, churn["id"])
    assert again["reproduction_of"] == first["id"] and again["manifest_hash"] == first["manifest_hash"]
    assert again["evaluation_seal"] == first["evaluation_seal"] and again["model_version"]["status"] == "candidate"
    # another spec on the same split is refused: the holdout was read
    tuned = _publish(world, "ml_spec", "churn_tuned", _spec(world, estimators=["logistic"]))
    with pytest.raises(Conflict, match="already read"):
        ml.start_experiment(owner, ws, tuned["id"])

    # promotion is a hash-bound approval
    req = ml.promote(owner, v1["id"], ws)
    assert req["status"] == "approval_required"
    with pytest.raises(ApprovalRequired):
        ml.promote(owner, v1["id"], ws, approval_id=req["approval_id"])  # still pending
    req = ml.promote(owner, v1["id"], ws)
    _approve(req["approval_id"])
    done = ml.promote(owner, v1["id"], ws, approval_id=req["approval_id"])
    assert done["status"] == "promoted" and done["model_version"]["status"] == "champion"

    # a new seed = a new partition: a challenger that stays a challenger
    reseeded = _publish(world, "ml_spec", "churn", _spec(world, seed=12))
    third = ml.start_experiment(owner, ws, reseeded["id"])
    v3 = third["model_version"]
    assert v3["status"] == "challenger" and v3["name"] == "churn"
    with session_scope() as s:
        assert s.get(MLModelVersion, v1["id"]).status == "champion"  # no silent promotion

    # a tampered package voids its verdict and the promotion is refused
    store = MLStore(get_settings().artifact_dir)
    path = store.package_path(v3["package_hash"])
    original = path.read_bytes()
    path.write_bytes(original + b"tampered")
    try:
        with pytest.raises(PolicyDenied, match="VOID"):
            ml.promote(owner, v3["id"], ws)
    finally:
        path.write_bytes(original)
    with session_scope() as s:
        assert s.get(VerificationRecord, third["verification_record_id"]).state == "VOID"
    with pytest.raises(PolicyDenied, match="VOID"):  # restoring the file does not revive a void verdict
        ml.promote(owner, v3["id"], ws)

    # approved batch scoring of a published, pinned definition
    scoring = _scoring(world, "churn_scoring", done["model_version"], "customers_new", "churn_scores")
    out = _score(world, scoring)
    run = out["scoring_run"]
    assert out["status"] == "succeeded" and run["rows_scored"] == 59 and run["rows_rejected"] == 1
    assert run["output_table"].endswith(".ml_churn_scores") and run["rejected_table"].endswith("_rejected")
    dup = ml.plan_scoring(owner, ws, scoring["id"])
    assert dup["status"] == "duplicate" and dup["scoring_run_id"] == run["id"]
    with session_scope() as s:
        assert s.scalar(select(ml.func.count()).select_from(MLScoringRun).where(MLScoringRun.status == "succeeded",
                                                                               MLScoringRun.workspace_id == ws)) == 1
    # the scores are read back like any staged asset: through the gateway
    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway

    with session_scope() as s:
        scope = resolve_scope(s, s.merge(owner), ws)
    res = default_gateway().run_sql_for(scope, actor="test", source_id=run["output_source_id"])(
        f'SELECT COUNT(*) FROM {run["output_table"]}', purpose="test")
    assert res.rows[0][0] == 59

    # schema drift refuses; a non-champion pin refuses
    drift = _scoring(world, "churn_drift", done["model_version"], "customers_drift", "drift_scores")
    with pytest.raises(PolicyDenied, match="schema drift"):
        ml.plan_scoring(owner, ws, drift["id"])
    challenger_def = _scoring(world, "churn_challenger", v3, "customers_next", "challenger_scores")
    with pytest.raises(PolicyDenied, match="only the approved champion"):
        ml.plan_scoring(owner, ws, challenger_def["id"])

    # monitors: drift, freshness, delayed labels
    with session_scope() as s:
        drift_m = create_monitor(s, s.merge(owner), ws, name="churn input drift", kind="ml_drift", config={"model": "churn"})
        fresh_m = create_monitor(s, s.merge(owner), ws, name="churn freshness", kind="ml_freshness",
                                 config={"model": "churn", "max_age_hours": 24})
        perf_m = create_monitor(s, s.merge(owner), ws, name="churn performance", kind="ml_performance",
                                config={"model": "churn", "label_asset": f"{world['schema']}.churn_labels",
                                        "label_column": "churned", "label_horizon_days": 0.00001, "min_labels": 20,
                                        "tolerance": 0.3})
        ids = drift_m.id, fresh_m.id, perf_m.id
    time.sleep(1)
    d, f, p = (evaluate_monitor(i) for i in ids)
    assert d["max_psi"] < 0.5 and set(d["features"]) >= {"tenure_months", "support_calls"}
    assert f["alert"] is False and f["age_hours"] < 1
    assert p.get("state") != "waiting" and p["labels"] == 59 and p["metric"] == "roc_auc" and p["value"] > 0.6

    # the first champion has nothing to roll back to
    with pytest.raises(Conflict, match="no rollback version"):
        ml.rollback(owner, ws, "churn")


def test_rollback_restores_the_previous_champion(world):
    from analystos.core.errors import PolicyDenied
    from analystos.services import ml

    ws, owner = world["ws"], world["owner"]
    spec = _publish(world, "ml_spec", "churn_rb", _spec(world, seed=21))
    first = ml.start_experiment(owner, ws, spec["id"])["model_version"]
    spec2 = _publish(world, "ml_spec", "churn_rb", _spec(world, seed=22))
    second = ml.start_experiment(owner, ws, spec2["id"])["model_version"]
    for v in (first, second):
        req = ml.promote(owner, v["id"], ws)
        _approve(req["approval_id"])
        ml.promote(owner, v["id"], ws, approval_id=req["approval_id"])
    pinned = _scoring(world, "rb_scoring", second, "customers_next", "rb_scores")
    req = ml.rollback(owner, ws, "churn_rb")
    _approve(req["approval_id"])
    out = ml.rollback(owner, ws, "churn_rb", approval_id=req["approval_id"])
    assert out["status"] == "rolled_back" and out["model_version"]["id"] == first["id"]
    assert out["retired"]["id"] == second["id"] and out["retired"]["status"] == "retired"
    with pytest.raises(PolicyDenied, match="only the approved champion"):
        ml.plan_scoring(owner, ws, pinned["id"])


def test_train_and_score_playbooks_and_the_mlflow_export(world):
    from analystos.db.base import session_scope
    from analystos.db.models import MLExperiment, MLScoringRun
    from analystos.services import ml
    from analystos.services.runs import create_run

    ws, owner = world["ws"], world["owner"]
    spec = _publish(world, "ml_spec", "churn_pb", _spec(world, seed=31))
    run = create_run(owner, ws, objective="Train the published churn model and evaluate it on a holdout",
                     playbook="playbook.train", origin={"type": "user", "publish": "skip",
                                                         "ml_definition": {"key": "churn_pb", "version": spec["version"]}})
    assert _wait(run.id, {"COMPLETED", "FAILED"}) == "COMPLETED"
    with session_scope() as s:
        exp = s.scalar(select(MLExperiment).where(MLExperiment.run_id == run.id))
        assert exp is not None and exp.status == "succeeded" and exp.verdict == "improved"
        version = s.scalar(select(ml.MLModelVersion).where(ml.MLModelVersion.experiment_id == exp.id))
        exp_id, version_id = exp.id, version.id
    req = ml.promote(owner, version_id, ws)
    _approve(req["approval_id"])
    champion = ml.promote(owner, version_id, ws, approval_id=req["approval_id"])["model_version"]
    scoring = _scoring(world, "pb_scoring", champion, "customers_next", "pb_scores")
    run = create_run(owner, ws, objective="Score next month's customers with the approved churn model",
                     playbook="playbook.score", origin={"type": "user", "publish": "skip",
                                                         "scoring_definition": {"key": "pb_scoring", "version": 1}})
    assert _wait(run.id, {"WAITING_USER", "FAILED", "COMPLETED"}) == "WAITING_USER"
    with session_scope() as s:
        row = s.scalar(select(MLScoringRun).where(MLScoringRun.run_id == run.id))
        assert row.status == "awaiting_approval" and row.definition_id == scoring["id"]
        approval_id = row.approval_id
    _approve(approval_id)
    from analystos.workflows.orchestrator import run_local

    assert run_local(run.id) == "COMPLETED"
    with session_scope() as s:
        assert s.scalar(select(MLScoringRun).where(MLScoringRun.run_id == run.id)).status == "succeeded"

    name, data = ml.export_mlflow(owner, exp_id, ws)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert name.endswith(".zip") and any(n.endswith("/artifacts/model/MLmodel") for n in names)
    assert any(n.endswith("/metrics/holdout_candidate_roc_auc") for n in names)

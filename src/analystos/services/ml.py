"""Governed classical ML (P5-01..P5-05, ADR-0024): experiments, the model registry, approved batch scoring.

Experiment: a *published* `ml_spec` definition -> the method's workspace enablement -> the dataset read
through `QueryGateway.execute` into an immutable snapshot (its hash is the dataset version) -> readiness
and leakage checks and the split manifest (`run_ml_job` prepare) -> the manifest's holdout claimed (once;
a different spec on a consumed holdout is refused) -> the bounded search and one holdout read (`run_ml_job`
train) -> artifacts (spec, manifest, trials, package, evaluation, model card) -> a P7-01 verification record
bound to the dataset version, split manifest, package hash and code digest (`predictive-evaluated`
evidence) -> an improved model is registered as a candidate or, beside a champion, a challenger.

Promotion and rollback are hash-bound approvals (`ml.promote`, `ml.rollback`). Batch scoring runs a
published `ml_scoring` definition that pins the model version and package; it checks feature parity
against the catalog, snapshots the input through the gateway, needs an `ml.score` approval bound to the
definition hash, package and input version, and writes predictions and rejected rows through the staging
loader into the workspace's managed output source. The same input scored twice is a duplicate, not a write.
"""
from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from analystos.contracts.definition import DefinitionRef
from analystos.contracts.work import MLScoringSpec, MLSpec
from analystos.core.config import get_settings
from analystos.core.errors import (
    AnalystOSError,
    Conflict,
    FeatureUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    PolicyDenied,
)
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import (
    AnalysisRun,
    Artifact,
    MLExperiment,
    MLModelVersion,
    MLScoringRun,
    MLSplit,
    Source,
    SourceAsset,
    SourceColumn,
    User,
    VerificationRecord,
    Workspace,
)
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope, scoped_loader
from analystos.ml.store import MLStore, snapshots

log = get_logger(__name__)
SUBJECT = "ml_experiment"
VERIFIER = "ml.v1"
PROMOTE, ROLLBACK, SCORE = "ml.promote", "ml.rollback", "ml.score"
RECORD_TYPES = ("ml_spec", "ml_split_manifest", "ml_trials", "ml_model", "ml_evaluation", "ml_model_card")
VERDICT_OF = {"improved": "verified", "no_improvement": "abstained", "guardrail_failed": "failed_verification",
              "invalid": "failed_verification"}
REJECTED_SUFFIX = "_rejected"


def _store() -> MLStore:
    return MLStore(get_settings().artifact_dir)


def caps_for(spec: MLSpec, override: dict[str, Any] | None = None) -> dict[str, int]:
    """The spec's search budget clamped to the platform's hard ceilings (never raised by a request)."""
    s = get_settings()
    over = override or {}
    return {"max_trials": min(spec.search.max_trials, s.ml_max_trials, over.get("max_trials") or 10 ** 9),
            "max_seconds": min(spec.search.max_seconds, s.ml_max_seconds, over.get("max_seconds") or 10 ** 9),
            "max_rows": min(spec.search.max_rows, s.ml_max_rows),
            "max_features": s.ml_max_features}


def _compute(job: dict[str, Any], workspace_id: str | None = None) -> dict[str, Any]:
    from analystos.workflows.orchestrator import run_ml_compute

    return run_ml_compute({**job, "artifact_dir": str(get_settings().artifact_dir),
                           **({"workspace_id": workspace_id} if workspace_id else {})})


def usable_method(session: Session, workspace_id: str, task: str, run: AnalysisRun | None = None) -> Any:
    """The ML method capability, if this installation has it and the workspace enabled it."""
    from analystos.capabilities import enablement, registry
    from analystos.capabilities.registry import install_reason
    from analystos.ml.methods import capability_id

    snap = registry.current()
    m = snap.get(capability_id(task))
    if reason := install_reason(m):
        raise FeatureUnavailable(f"{m.id} is unavailable: {reason}", details={"extra": "ml", "capability": m.id})
    problem = enablement.usable(m, snap, enablement.overrides(session, workspace_id),
                                autonomous_run=enablement.autonomous(run) if run is not None else False)
    if problem:
        raise PolicyDenied(problem + " (a workspace owner enables it, or enables playbook.train / playbook.score)",
                           details={"capability": m.id})
    return m


def _definition(session: Session, workspace_id: str, definition: Any, kind: str, trigger: str) -> tuple[DefinitionRef, dict]:
    from analystos.services.definitions import resolve_runnable

    if isinstance(definition, str):
        definition = {"id": definition} if definition.startswith("defn_") else {"key": definition}
    ref = {"kind": kind, **definition} if isinstance(definition, dict) else definition
    got, spec = resolve_runnable(session, workspace_id, ref, trigger=trigger)
    if got.kind != kind or got.source != "workspace":
        raise InvalidInput(f"{trigger} runs a published workspace `{kind}` definition (got {got.kind})")
    return got, spec


def catalog_types(session: Session, workspace_id: str, asset: str, source_id: str | None) -> tuple[str | None, dict[str, str | None], Any]:
    schema, name = asset.split(".", 1)
    q = select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                                  SourceAsset.name == name)
    if source_id:
        q = q.where(SourceAsset.source_id == source_id)
    a = session.scalar(q.limit(1))
    if a is None:
        return source_id, {}, None
    cols = session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id))
    return a.source_id, {c.name: c.data_type for c in cols}, a.freshness_at


def snapshot_asset(runner: Callable[..., Any], scope: Any, asset: str, columns: list[str], *, source_id: str | None,
                   max_rows: int, purpose: str) -> dict[str, Any]:
    """Read the declared columns once through the gateway into an immutable snapshot (the dataset version).
    A truncated read is refused: a partial dataset would change every number silently."""
    from sqlglot import exp

    from analystos.recipes.compiler import col

    dialect = scope.source_dialects.get(source_id or "", "postgres")
    schema, table = asset.split(".", 1)
    stmt = exp.select(*[col(c) for c in columns]).from_(exp.Table(this=exp.to_identifier(table, quoted=True),
                                                                   db=exp.to_identifier(schema, quoted=True)))
    res = runner(stmt.sql(dialect=dialect), purpose=purpose, max_rows=max_rows, use_cache=False)
    if res.truncated:
        raise InvalidInput(f"{asset} has more than {max_rows} rows (the ML/gateway row cap); a sample would change the "
                           "result silently, so nothing was trained or scored. Filter the table (a recipe) first.")
    digest = snapshots(get_settings().artifact_dir).put(res.columns, res.rows)
    return {"digest": digest, "rows": res.row_count, "columns": list(res.columns), "query_id": res.query_id}


def _runner(user: User, scope: Any, source_id: str | None, run_id: str | None = None) -> Callable[..., Any]:
    from analystos.runtime.context import default_gateway

    return default_gateway().run_sql_for(scope, actor=f"user:{user.id}", run_id=run_id, source_id=source_id)


# ------------------------------------------------------------------------------------ experiments
def _finish(exp_id: str, status: str, *, event: str, **fields: Any) -> None:
    with session_scope() as s:
        row = s.get(MLExperiment, exp_id)
        for k, v in fields.items():
            setattr(row, k, v)
        row.status, row.finished_at = status, utcnow()
        emit(row.workspace_id, event, {"experiment_id": row.id, "status": status, "verdict": row.verdict,
                                       "error": (row.error or "")[:300] or None}, run_id=row.run_id, actor=row.created_by,
             session=s)


def _as_of(session: Session, workspace_id: str, digest: str) -> str:
    """Label maturity is judged as of the first time this dataset version was read, so re-running the same
    snapshot reproduces the same usable rows (and so the same manifest)."""
    earlier = session.scalars(select(MLExperiment.summary).where(MLExperiment.workspace_id == workspace_id,
                                                                 MLExperiment.dataset_version == digest)
                              .order_by(MLExperiment.created_at))
    return next((s["as_of"] for s in earlier if isinstance(s, dict) and s.get("as_of")), utcnow().isoformat())


def _claim_holdout(workspace_id: str, exp_id: str, prep: dict[str, Any], spec_hash: str) -> tuple[str, str | None]:
    """Mark the manifest's holdout consumed by this experiment (compare-and-set). The same spec may read it
    again (a reproduction); any other spec is refused: tuning after reading a holdout needs a new partition."""
    with session_scope() as s:
        row = s.scalar(select(MLSplit).where(MLSplit.workspace_id == workspace_id,
                                             MLSplit.manifest_hash == prep["manifest_hash"]).with_for_update())
        if row is None:
            row = MLSplit(id=new_id("mls"), workspace_id=workspace_id, manifest_hash=prep["manifest_hash"],
                          dataset_version=prep["manifest"]["dataset_version"], strategy=prep["manifest"]["strategy"],
                          manifest=prep["manifest"], membership_hash=prep["membership_hash"])
            s.add(row)
            s.flush()
        if row.holdout_consumed_by is None:
            claimed = s.execute(update(MLSplit).where(MLSplit.id == row.id, MLSplit.holdout_consumed_by.is_(None))
                                .values(holdout_consumed_by=exp_id, holdout_spec_hash=spec_hash, holdout_consumed_at=utcnow())
                                .execution_options(synchronize_session=False))
            if claimed.rowcount != 1:
                raise Conflict("the holdout was claimed concurrently by another experiment")
            return row.id, None
        if row.holdout_spec_hash == spec_hash:
            return row.id, row.holdout_consumed_by
        raise Conflict(f"the holdout of split manifest {row.manifest_hash[:12]} was already read by experiment "
                       f"{row.holdout_consumed_by} under another spec; retuning after reading a holdout needs a new "
                       "evaluation partition (change split seed or holdout_fraction) in a new version",
                       details={"split_id": row.id, "consumed_by": row.holdout_consumed_by})


def start_experiment(user: User, workspace_id: str, definition: Any, *, run_id: str | None = None,
                     runner: Callable[..., Any] | None = None, scope: Any = None,
                     caps: dict[str, Any] | None = None) -> dict[str, Any]:
    """Train and evaluate one published `ml_spec` version. Refusals (leakage, too little data, a consumed
    holdout) are recorded on the experiment and raised as InvalidInput / Conflict with its id."""
    from analystos.artifacts.registry import link
    from analystos.ml.readiness import referenced_columns

    with session_scope() as s:
        require_role(s, user, workspace_id, "analyst")
        ref, raw = _definition(s, workspace_id, definition, "ml_spec", "ml.experiment")
        spec = MLSpec.model_validate(raw)
        problems = spec.executable_problems()
        if problems:
            raise InvalidInput("the ml_spec cannot train yet: " + "; ".join(problems), details={"problems": problems})
        run = s.get(AnalysisRun, run_id) if run_id else None
        usable_method(s, workspace_id, spec.task, run)
        scope = scope or resolve_scope(s, s.merge(user), workspace_id, minimum_role="analyst")
        asset = spec.dataset.asset
        if asset not in scope.assets:
            raise Forbidden(f"{asset} is not in the authorized scope")
        columns = referenced_columns(spec)
        blocked = [c for c in columns if f"{asset}.{c}" in scope.denied_columns or f"*.{c}" in scope.denied_columns]
        if blocked:
            raise Forbidden(f"column {', '.join(blocked)} of {asset} is not readable under the workspace policy")
        unknown = [c for c in columns if c not in (scope.columns.get(asset) or [])]
        if unknown:
            raise InvalidInput(f"{asset} has no column {', '.join(unknown)}", details={"missing": unknown})
        source_id, types, _ = catalog_types(s, workspace_id, asset, spec.dataset.source_id or scope.asset_sources.get(asset))
        spec_hash = stable_hash(spec.model_dump(mode="json"))
        exp = MLExperiment(id=new_id("mlx"), workspace_id=workspace_id, run_id=run_id, definition_id=ref.id,
                           definition_key=ref.key, definition_version=int(ref.version) if ref.version is not None else None,
                           task=spec.task, spec=spec.model_dump(mode="json"), spec_hash=spec_hash, status="running",
                           dataset_asset=asset, dataset_source_id=source_id, readiness={}, summary={}, artifacts={},
                           query_ids=[], created_by=f"user:{user.id}")
        s.add(exp)
        s.flush()
        link(s, workspace_id, ("definition", ref.id or ref.key), "trained_as", (SUBJECT, exp.id), run_id=run_id)
        emit(workspace_id, "ml.experiment.started", {"experiment_id": exp.id, "definition": ref.label, "task": spec.task},
             run_id=run_id, actor=f"user:{user.id}", session=s)
        exp_id = exp.id
    caps = caps_for(spec, caps)
    try:
        runner = runner or _runner(user, scope, source_id, run_id)
        snap = snapshot_asset(runner, scope, asset, columns, source_id=source_id, max_rows=caps["max_rows"],
                              purpose=f"ml.experiment:{exp_id}")
        with session_scope() as s:
            as_of = _as_of(s, workspace_id, snap["digest"])
            row = s.get(MLExperiment, exp_id)
            row.dataset_version, row.query_ids = snap["digest"], [snap["query_id"]]
            row.summary = {"as_of": as_of, "column_types": types, "rows_read": snap["rows"], "caps": caps}
        if spec.dataset.version and spec.dataset.version != snap["digest"]:
            raise Conflict(f"{asset} changed since the spec pinned dataset version {spec.dataset.version[:12]} "
                           f"(now {snap['digest'][:12]})")
        job = {"spec": spec.model_dump(mode="json"), "snapshot": snap["digest"], "column_types": types, "as_of": as_of,
               "caps": caps}
        prep = _compute({**job, "kind": "prepare"}, workspace_id)
        if prep["status"] != "ready":
            problems = prep["readiness"]["problems"]
            _finish(exp_id, "refused", event="ml.experiment.refused", readiness=prep["readiness"],
                    error="; ".join(problems)[:4000])
            raise InvalidInput("the experiment was refused before training: " + "; ".join(problems),
                               details={"experiment_id": exp_id, "readiness": prep["readiness"]})
        split_id, reproduction_of = _claim_holdout(workspace_id, exp_id, prep, spec_hash)
        with session_scope() as s:
            row = s.get(MLExperiment, exp_id)
            row.split_id, row.manifest_hash, row.reproduction_of = split_id, prep["manifest_hash"], reproduction_of
            row.readiness = prep["readiness"]
        result = _compute({**job, "kind": "train", "expected_manifest_hash": prep["manifest_hash"]}, workspace_id)
        if result.get("status") != "succeeded":
            raise InvalidInput(f"training {result.get('status')}: {result.get('error') or 'no candidate succeeded'}",
                               details={"experiment_id": exp_id})
        return _record(user, exp_id, spec, ref, result, snap, split_id)
    except AnalystOSError as exc:
        with session_scope() as s:
            status = s.get(MLExperiment, exp_id).status
        if status == "running":
            refused = isinstance(exc, Conflict | Forbidden)
            _finish(exp_id, "refused" if refused else "failed",
                    event="ml.experiment.refused" if refused else "ml.experiment.failed", error=exc.message[:4000])
        exc.details = {**(exc.details or {}), "experiment_id": exp_id}
        raise
    except Exception as exc:  # noqa: BLE001 - an unexpected failure still ends the experiment; never left "running"
        log.exception("ML experiment %s failed unexpectedly", exp_id)
        with session_scope() as s:
            status = s.get(MLExperiment, exp_id).status
        if status == "running":
            _finish(exp_id, "failed", event="ml.experiment.failed",
                    error="the experiment stopped because of an unexpected error; see the server log")
        raise AnalystOSError("the experiment stopped because of an unexpected error", details={"experiment_id": exp_id}) from exc


def _evidence_bundle(spec: MLSpec, result: dict[str, Any], snap: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    from analystos.contracts.evidence import Check, EvidenceBundle, Freshness, Validation

    ev = result["evaluation"]
    verdict = result["verdict"]
    state = {"improved": "exploratory", "no_improvement": "inconclusive", "guardrail_failed": "inconclusive",
             "invalid": "invalid"}[verdict]
    checks = [Check(check=c["check"], outcome=c["outcome"], reason=c.get("reason", "")) for c in ev.get("checks") or []]
    bundle = EvidenceBundle(
        data={"asset": spec.dataset.asset, "dataset_version": snap["digest"], "queries": [{"query_id": snap["query_id"],
              "result_hash": snap["digest"]}], "rows": snap["rows"], "split_manifest": result["manifest_hash"],
              "excluded_rows": (result.get("readiness") or {}).get("excluded")},
        claim={"facts": card["facts"], "verdict": verdict, "binding": {"ok": card["bound"]}},
        method={"method": f"ml.{spec.task}", "test": result["selection"]["estimator"], "params": result["selection"]["params"],
                "effect": {"value": (ev.get("decision") or {}).get("gain"), "label": f"{ev.get('metric')} gain over baseline"},
                "uncertainty": ev.get("uncertainty"), "sample_sizes": {"n": (result["manifest"]["rows"] or {}).get("usable"),
                                                                        "holdout": ev.get("holdout_rows")},
                "selection": {"trials": len(result.get("trials") or []) - 1, "selection_hash": result["selection_hash"],
                              "stopped": result.get("stopped")}},
        limits={"importance": "explains model behaviour, not causality", "drift": "drift alone does not show performance loss",
                "untested": [s for g in ev.get("guardrails") or [] for s in g["slices"] if s.get("status") == "not_applicable"]},
        validation=Validation(state=state, label="discovery", checks=checks, predictive_evaluated=True,
                              reproducible=next((c.outcome == "pass" for c in checks if c.check == "reproducible_from_manifest"),
                                                None)),
        freshness=Freshness(state="current"))
    return bundle.model_dump(mode="json")


def _record(user: User, exp_id: str, spec: MLSpec, ref: DefinitionRef, result: dict[str, Any], snap: dict[str, Any],
            split_id: str) -> dict[str, Any]:
    from analystos.artifacts.registry import link, save_artifact
    from analystos.evidence.verification import Dependency, current_version, record_verdict
    from analystos.ml import card as model_card

    ws_id = None
    with session_scope() as s:
        exp = s.get(MLExperiment, exp_id)
        ws_id, run_id = exp.workspace_id, exp.run_id
        card = model_card.build({**spec.model_dump(mode="json"), "baseline": spec.baseline}, result, experiment_id=exp_id,
                                dataset={"asset": spec.dataset.asset, "version": snap["digest"]}, title=ref.key)
        contents = {
            "ml_spec": {"experiment_id": exp_id, "definition": ref.model_dump(mode="json"), "spec": spec.model_dump(mode="json"),
                        "spec_hash": exp.spec_hash, "caps": result.get("caps"), "readiness": result.get("readiness")},
            "ml_split_manifest": {"experiment_id": exp_id, "split_id": split_id, "manifest": result["manifest"],
                                  "manifest_hash": result["manifest_hash"], "membership_hash": result["membership_hash"]},
            "ml_trials": {"experiment_id": exp_id, "trials": result["trials"], "selection": result["selection"],
                          "selection_hash": result["selection_hash"], "stopped": result.get("stopped"), "caps": result["caps"]},
            "ml_model": {"experiment_id": exp_id, "package_hash": result["package"]["hash"], "bytes": result["package"]["bytes"],
                         "estimator": result["package"]["estimator"], "params": result["package"]["params"],
                         "feature_schema": result["package"]["schema"], "code_digest": result["code_digest"],
                         "environment": result["environment"], "environment_digest": result["environment_digest"],
                         "reference_profile": result.get("reference_profile") or {}, "forecast": result["package"].get("forecast"),
                         "positive": result["package"].get("positive"), "classes": result["package"].get("classes")},
            "ml_evaluation": {"experiment_id": exp_id, "report": result["evaluation"], "seal": result["evaluation"]["seal"],
                              "sealed": True},
            "ml_model_card": card,
        }
        arts = {}
        for type_, content in contents.items():
            art = save_artifact(s, workspace_id=ws_id, type_=type_, name=exp_id, content=content, run_id=run_id,
                                creator_user=user.id, status="final")
            arts[type_] = art.id
            link(s, ws_id, (SUBJECT, exp_id), "produced", ("artifact", art.id), run_id=run_id)
        link(s, ws_id, ("table", spec.dataset.asset), "trained", (SUBJECT, exp_id), run_id=run_id)
        deps = [Dependency("ml_dataset", f"{ws_id}/{snap['digest']}", snap["digest"]),
                Dependency("ml_split", split_id, result["manifest_hash"]),
                Dependency("ml_package", exp_id, result["package"]["hash"])]
        for kind, dep_ref in (("method", f"ml.{spec.task}"), ("policy", ws_id)):
            v = current_version(s, kind, dep_ref)
            if v is not None:
                deps.append(Dependency(kind, dep_ref, v))
        bundle = _evidence_bundle(spec, result, snap, card)
        rec = record_verdict(s, workspace_id=ws_id, run_id=run_id, subject_type=SUBJECT, subject_id=exp_id,
                             verdict=VERDICT_OF[result["verdict"]], checks=result["evaluation"]["checks"], verifier=VERIFIER,
                             dependencies=deps, question_hash=exp.spec_hash, evidence_bundle=bundle)
        ev = result["evaluation"]
        exp.status, exp.verdict, exp.finished_at = "succeeded", result["verdict"], utcnow()
        exp.selection_hash, exp.evaluation_seal = result["selection_hash"], ev["seal"]
        exp.package_hash, exp.code_digest = result["package"]["hash"], result["code_digest"]
        exp.environment_digest, exp.artifacts, exp.verification_record_id = result["environment_digest"], arts, rec.id
        exp.summary = {**(exp.summary or {}), "metric": ev.get("metric"), "baseline": ev.get("baseline"),
                       "candidate": ev.get("candidate"), "decision": ev.get("decision"), "estimator": result["selection"]["estimator"],
                       "trials_run": result["caps"]["trials_run"], "stopped": result.get("stopped"),
                       "evidence_bundle": bundle, "seconds": result.get("seconds")}
        version = register_version(s, exp) if result["verdict"] == "improved" else None
        emit(ws_id, "ml.experiment.completed", {"experiment_id": exp_id, "verdict": result["verdict"],
                                                "model_version_id": version.id if version else None},
             run_id=run_id, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ml.experiment.completed", workspace_id=ws_id, run_id=run_id, target=exp_id,
              details={"verdict": result["verdict"], "package_hash": exp.package_hash, "manifest_hash": exp.manifest_hash,
                       "seal": exp.evaluation_seal}, session=s)
        return experiment_view(exp, version=version)


def register_version(session: Session, exp: MLExperiment) -> MLModelVersion:
    """An improved experiment becomes a model version: a candidate, or a challenger when a champion exists.
    Registration never promotes (promotion is an approval)."""
    from analystos.artifacts.registry import link

    latest = session.scalar(select(func.max(MLModelVersion.version)).where(MLModelVersion.workspace_id == exp.workspace_id,
                                                                           MLModelVersion.name == exp.definition_key)) or 0
    champion = current_champion(session, exp.workspace_id, exp.definition_key)
    pkg = session.scalar(select(Artifact).where(Artifact.id == (exp.artifacts or {}).get("ml_model")))
    mv = MLModelVersion(id=new_id("mlv"), workspace_id=exp.workspace_id, name=exp.definition_key, version=latest + 1,
                        experiment_id=exp.id, task=exp.task, package_hash=exp.package_hash,
                        status="challenger" if champion else "candidate",
                        feature_schema=list(((pkg.content if pkg else {}) or {}).get("feature_schema") or []),
                        metrics={"metric": (exp.summary or {}).get("metric"), "candidate": (exp.summary or {}).get("candidate"),
                                 "baseline": (exp.summary or {}).get("baseline")}, created_by=exp.created_by)
    session.add(mv)
    session.flush()
    link(session, exp.workspace_id, (SUBJECT, exp.id), "registered_as", ("ml_model_version", mv.id))
    emit(exp.workspace_id, "ml.model.registered", {"model": mv.name, "version": mv.version, "model_version_id": mv.id,
                                                   "status": mv.status, "champion": champion.id if champion else None},
         session=session)
    return mv


def registered_versions(session: Session, experiments: list[MLExperiment]) -> dict[str, MLModelVersion]:
    """The model version each experiment registered (only an improved one registers), by experiment id, so a
    read of an experiment shows what the create call returned: its version, promotable through an approval."""
    ids = [e.id for e in experiments]
    if not ids:
        return {}
    return {mv.experiment_id: mv for mv in session.scalars(select(MLModelVersion).where(MLModelVersion.experiment_id.in_(ids)))}


def current_champion(session: Session, workspace_id: str, name: str) -> MLModelVersion | None:
    return session.scalar(select(MLModelVersion).where(MLModelVersion.workspace_id == workspace_id, MLModelVersion.name == name,
                                                       MLModelVersion.status == "champion"))


# ------------------------------------------------------------------------------------ views and reads
def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def experiment_view(exp: MLExperiment, *, version: MLModelVersion | None = None) -> dict[str, Any]:
    out = {c: getattr(exp, c) for c in ("id", "workspace_id", "run_id", "definition_id", "definition_key", "definition_version",
                                        "task", "spec_hash", "status", "verdict", "dataset_asset", "dataset_source_id",
                                        "dataset_version", "split_id", "manifest_hash", "selection_hash", "evaluation_seal",
                                        "package_hash", "code_digest", "environment_digest", "readiness", "artifacts",
                                        "verification_record_id", "query_ids", "reproduction_of", "error", "created_by")}
    out["summary"] = {k: v for k, v in (exp.summary or {}).items() if k != "evidence_bundle"}
    out.update(created_at=_iso(exp.created_at), finished_at=_iso(exp.finished_at))
    if version is not None:
        out["model_version"] = version_view(version)
    return out


def version_view(mv: MLModelVersion) -> dict[str, Any]:
    out = {c: getattr(mv, c) for c in ("id", "workspace_id", "name", "version", "experiment_id", "task", "package_hash", "status",
                                       "feature_schema", "metrics", "approval_id", "previous_champion_id", "promoted_by",
                                       "reason", "created_by")}
    out.update(promoted_at=_iso(mv.promoted_at), retired_at=_iso(mv.retired_at), created_at=_iso(mv.created_at))
    return out


def scoring_view(row: MLScoringRun) -> dict[str, Any]:
    out = {c: getattr(row, c) for c in ("id", "workspace_id", "run_id", "definition_id", "definition_key", "definition_version",
                                        "definition_hash", "model_version_id", "package_hash", "input_asset", "input_source_id",
                                        "input_version", "dedupe_key", "status", "approval_id", "rows_input", "rows_scored",
                                        "rows_rejected", "output_source_id", "output_table", "rejected_table", "details",
                                        "query_ids", "error", "created_by")}
    out.update(created_at=_iso(row.created_at), finished_at=_iso(row.finished_at))
    return out


@scoped_loader
def get_experiment(session: Session, user: User, experiment_id: str, workspace_id: str | None = None,
                   minimum: str = "viewer") -> MLExperiment:
    return load_in_workspace(session, MLExperiment, experiment_id, workspace_id, user=user, minimum=minimum, label="experiment")


@scoped_loader
def get_version(session: Session, user: User, version_id: str, workspace_id: str | None = None,
                minimum: str = "viewer", for_update: bool = False) -> MLModelVersion:
    return load_in_workspace(session, MLModelVersion, version_id, workspace_id, user=user, minimum=minimum,
                             label="model version", for_update=for_update)


@scoped_loader
def get_scoring(session: Session, user: User, scoring_id: str, workspace_id: str | None = None,
                minimum: str = "viewer", for_update: bool = False) -> MLScoringRun:
    return load_in_workspace(session, MLScoringRun, scoring_id, workspace_id, user=user, minimum=minimum,
                             label="scoring run", for_update=for_update)


def list_experiments(session: Session, user: User, workspace_id: str, definition_key: str | None = None) -> list[MLExperiment]:
    require_role(session, user, workspace_id, "viewer")
    q = select(MLExperiment).where(MLExperiment.workspace_id == workspace_id)
    if definition_key:
        q = q.where(MLExperiment.definition_key == definition_key)
    return list(session.scalars(q.order_by(MLExperiment.created_at.desc()).limit(200)))


def list_versions(session: Session, user: User, workspace_id: str, name: str | None = None) -> list[MLModelVersion]:
    require_role(session, user, workspace_id, "viewer")
    q = select(MLModelVersion).where(MLModelVersion.workspace_id == workspace_id)
    if name:
        q = q.where(MLModelVersion.name == name)
    return list(session.scalars(q.order_by(MLModelVersion.name, MLModelVersion.version.desc())))


def list_scoring(session: Session, user: User, workspace_id: str) -> list[MLScoringRun]:
    require_role(session, user, workspace_id, "viewer")
    return list(session.scalars(select(MLScoringRun).where(MLScoringRun.workspace_id == workspace_id)
                                .order_by(MLScoringRun.created_at.desc()).limit(200)))


@scoped_loader
def experiment_record(session: Session, user: User, experiment_id: str, record: str, workspace_id: str | None = None) -> dict:
    """One of the experiment's artifacts (spec, split manifest, trials, package descriptor, evaluation, card)."""
    if record not in RECORD_TYPES:
        raise InvalidInput(f"record must be one of {', '.join(RECORD_TYPES)}")
    exp = get_experiment(session, user, experiment_id, workspace_id)
    art_id = (exp.artifacts or {}).get(record)
    art = session.get(Artifact, art_id) if art_id else None
    if art is None or art.workspace_id != exp.workspace_id:
        raise NotFound(f"experiment {experiment_id} has no {record} record")
    return {"artifact_id": art.id, "type": art.type, "version": art.version, "content_hash": art.content_hash,
            "content": art.content}


def verification_of(session: Session, exp: MLExperiment) -> dict[str, Any]:
    from analystos.evidence.verification import latest, state_of

    return state_of(latest(session, SUBJECT, [exp.id]).get(exp.id))


def verify_package(session: Session, workspace_id: str, package_hash: str) -> bytes:
    """Only a package a platform experiment produced (same workspace, succeeded) whose bytes still hash to
    that record may be used. There is no upload path: an unknown hash is refused before any file is read.
    Returns the verified bytes without unpickling them."""
    exp = session.scalar(select(MLExperiment).where(MLExperiment.workspace_id == workspace_id,
                                                    MLExperiment.package_hash == package_hash,
                                                    MLExperiment.status == "succeeded").limit(1))
    if exp is None:
        raise PolicyDenied(f"package {package_hash[:12]} has no platform experiment record in this workspace; "
                           "only platform-produced packages load")
    return _store().package_bytes(package_hash)


def load_package(session: Session, workspace_id: str, package_hash: str) -> dict[str, Any]:
    """`verify_package`, then unpickle. With the isolated `compute-ml` pool configured, packages are unpickled
    only in that worker (P7-06): the control plane refuses to load one itself."""
    from analystos.workers.dispatch import pool_configured

    verify_package(session, workspace_id, package_hash)
    if pool_configured("compute-ml"):
        raise PolicyDenied("model packages load only in the isolated compute-ml worker on this installation")
    return _store().load_package(package_hash)


# ------------------------------------------------------------------------------------ promotion and rollback
def _revalidate(experiment_id: str) -> str | None:
    """Recompute the verdict's dependencies now (own transaction, so a void is kept): None when the record
    is live and verified, else why it cannot back a promotion."""
    from analystos.evidence.verification import UNKNOWABLE, current_version, void_dependents

    with session_scope() as s:
        exp = s.get(MLExperiment, experiment_id)
        rec = s.get(VerificationRecord, exp.verification_record_id) if exp and exp.verification_record_id else None
        if rec is None:
            return "the experiment has no verification record"
        if rec.state == "ACTIVE":
            for d in rec.dependencies or []:
                cur = current_version(s, d["kind"], d["ref"])
                if cur != UNKNOWABLE and cur != d["version_hash"]:
                    void_dependents(s, d["kind"], d["ref"], cur, f"{d['kind']} {d['ref']} changed since the verdict",
                                    event="ml.promotion.check")
                    break
        s.flush()
        s.refresh(rec)
        if rec.state != "ACTIVE":
            return f"its verification is {rec.state}" + (f" ({rec.void_kind}: {rec.void_reason})" if rec.state == "VOID" else "")
        if rec.verdict != "verified":
            return f"its verdict is {exp.verdict} (not an improvement over the baseline)"
        return None


def _promotion_payload(session: Session, mv: MLModelVersion) -> dict[str, Any]:
    exp = session.get(MLExperiment, mv.experiment_id)
    rec = session.get(VerificationRecord, exp.verification_record_id) if exp.verification_record_id else None
    champion = current_champion(session, mv.workspace_id, mv.name)
    return {"action": PROMOTE, "workspace_id": mv.workspace_id, "model": mv.name, "version_id": mv.id, "version": mv.version,
            "package_hash": mv.package_hash, "evaluation_seal": exp.evaluation_seal, "manifest_hash": exp.manifest_hash,
            "verification_fingerprint": rec.fingerprint if rec else None, "replaces": champion.id if champion else None}


def _approve_or_request(s: Session, user: User, workspace_id: str, action: str, payload: dict[str, Any],
                        approval_id: str | None, *, destination: str, assets: list[str], evidence: dict[str, Any],
                        run_id: str | None = None, plan_hash: str | None = None) -> Any:
    from analystos.governance.approvals import consume, request_approval, verify_for_execution

    if not approval_id:
        ws = s.get(Workspace, workspace_id)
        apr = request_approval(s, workspace_id=workspace_id, run_id=run_id, action=action, payload=payload, plan_hash=plan_hash,
                               policy_version=ws.policy_version, requested_by=user.id, risk_tier="high",
                               destination=destination, affected_assets=assets, evidence=evidence)
        return {"status": "approval_required", "approval_id": apr.id, "payload_hash": apr.payload_hash,
                "expires_at": _iso(apr.expires_at)}
    apr = verify_for_execution(s, approval_id, payload=payload, plan_hash=plan_hash)
    if apr.action != action or apr.workspace_id != workspace_id:
        raise PolicyDenied(f"approval {approval_id} does not authorize {action} here")
    consume(s, apr)
    return apr


@scoped_loader
def promote(user: User, version_id: str, workspace_id: str | None = None, *, approval_id: str | None = None) -> dict[str, Any]:
    """Without `approval_id`: request the hash-bound `ml.promote` approval. With it: verify the approval
    against the payload recomputed now, then make this version the champion (the old champion is kept as
    the rollback version). A version whose verdict is not an improvement, or whose verification is VOID,
    is refused."""
    from analystos.artifacts.registry import link

    with session_scope() as s:
        mv = get_version(s, user, version_id, workspace_id, minimum="editor")
        exp_id, ws = mv.experiment_id, mv.workspace_id
    problem = _revalidate(exp_id)
    with session_scope() as s:
        mv = get_version(s, user, version_id, ws, minimum="editor", for_update=True)
        exp = s.get(MLExperiment, mv.experiment_id)
        if mv.status not in ("candidate", "challenger"):
            raise Conflict(f"model version v{mv.version} is {mv.status}; only a candidate or challenger can be promoted")
        if exp.verdict != "improved" or problem:
            raise PolicyDenied(f"promotion refused: v{mv.version} {problem or 'did not improve on its baseline'}",
                               details={"experiment_id": exp.id, "verdict": exp.verdict})
        payload = _promotion_payload(s, mv)
        got = _approve_or_request(s, user, ws, PROMOTE, payload, approval_id, destination=f"ml_model:{mv.name}",
                                  assets=[exp.dataset_asset],
                                  evidence={"verdict": exp.verdict, "summary": {k: (exp.summary or {}).get(k) for k in
                                                                              ("metric", "baseline", "candidate", "decision")}})
        if isinstance(got, dict):
            return got
        old = current_champion(s, ws, mv.name)
        if old is not None:
            old.status, old.retired_at = "retired", utcnow()
            old.reason = f"replaced by v{mv.version}; kept as the rollback version"
            s.flush()
        mv.status, mv.previous_champion_id, mv.approval_id = "champion", old.id if old else None, got.id
        mv.promoted_by, mv.promoted_at = got.decided_by, utcnow()
        link(s, ws, ("ml_model_version", mv.id), "approved_by", ("approval", got.id))
        emit(ws, "ml.model.promoted", {"model": mv.name, "version": mv.version, "model_version_id": mv.id,
                                       "replaced": old.id if old else None, "approval_id": got.id},
             actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ml.model.promoted", workspace_id=ws, target=mv.id, decision="allow",
              details={"approval_id": got.id, "package_hash": mv.package_hash, "replaced": old.id if old else None}, session=s)
        return {"status": "promoted", "model_version": version_view(mv)}


def rollback(user: User, workspace_id: str, name: str, *, approval_id: str | None = None) -> dict[str, Any]:
    """Return a model to the champion it replaced, under an `ml.rollback` approval bound to both versions."""
    with session_scope() as s:
        require_role(s, user, workspace_id, "editor")
        champion = current_champion(s, workspace_id, name)
        if champion is None:
            raise NotFound(f"model {name} has no champion")
        target = s.get(MLModelVersion, champion.previous_champion_id) if champion.previous_champion_id else None
        if target is None or target.workspace_id != workspace_id or target.name != name:
            raise Conflict(f"model {name} v{champion.version} has no rollback version")
        if _store().file_hash(target.package_hash) != target.package_hash:
            raise PolicyDenied(f"the rollback version's package {target.package_hash[:12]} is missing or modified")
        payload = {"action": ROLLBACK, "workspace_id": workspace_id, "model": name, "from_version_id": champion.id,
                   "to_version_id": target.id, "to_package_hash": target.package_hash}
        got = _approve_or_request(s, user, workspace_id, ROLLBACK, payload, approval_id, destination=f"ml_model:{name}",
                                  assets=[], evidence={"from_version": champion.version, "to_version": target.version})
        if isinstance(got, dict):
            return got
        champion.status, champion.retired_at, champion.reason = "retired", utcnow(), f"rolled back to v{target.version}"
        s.flush()
        target.status, target.retired_at, target.reason = "champion", None, f"restored by rollback from v{champion.version}"
        target.approval_id, target.promoted_by, target.promoted_at = got.id, got.decided_by, utcnow()
        emit(workspace_id, "ml.model.rolled_back", {"model": name, "from_version_id": champion.id, "to_version_id": target.id,
                                                    "approval_id": got.id}, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ml.model.rolled_back", workspace_id=workspace_id, target=target.id, decision="allow",
              details={"approval_id": got.id, "from": champion.id}, session=s)
        return {"status": "rolled_back", "model_version": version_view(target), "retired": version_view(champion)}


# ------------------------------------------------------------------------------------ batch scoring
def _scoring_payload(row: MLScoringRun, output: str) -> dict[str, Any]:
    return {"action": SCORE, "workspace_id": row.workspace_id, "definition_id": row.definition_id,
            "definition_version": row.definition_version, "definition_hash": row.definition_hash,
            "model_version_id": row.model_version_id, "package_hash": row.package_hash, "input_asset": row.input_asset,
            "input_version": row.input_version, "output": output}


def parity(feature_schema: list[dict[str, Any]], training_types: dict[str, Any], current: dict[str, Any],
           keys: list[str]) -> list[str]:
    """Training/scoring feature parity against the catalog: every feature and key still exists with the same
    type family. Any difference is schema drift and refuses scoring (never a silent coercion)."""
    from analystos.ml.data import catalog_family

    problems = []
    for f in feature_schema:
        name = f["name"]
        if name not in current:
            problems.append(f"{name} is missing")
            continue
        was, now = catalog_family(training_types.get(name)), catalog_family(current.get(name))
        if was and now and was != now:
            problems.append(f"{name} was {training_types.get(name)} ({was}) at training and is {current.get(name)} ({now}) now")
    problems += [f"entity key {k} is missing" for k in keys if k not in current]
    return problems


def plan_scoring(user: User, workspace_id: str, definition: Any, *, run_id: str | None = None, plan_hash: str | None = None,
                 runner: Callable[..., Any] | None = None, scope: Any = None) -> dict[str, Any]:
    """Resolve the published scoring definition, check the pinned version is the champion and the input's
    feature parity, snapshot the input through the gateway, and request the `ml.score` approval (or report a
    duplicate: the same definition, package and input were already scored)."""
    with session_scope() as s:
        require_role(s, user, workspace_id, "editor")
        ref, raw = _definition(s, workspace_id, definition, "ml_scoring", "ml.scoring")
        spec = MLScoringSpec.model_validate(raw)
        mv = s.get(MLModelVersion, spec.model_version_id)
        if mv is None or mv.workspace_id != workspace_id or mv.name != spec.model:
            raise NotFound(f"model version {spec.model_version_id} of {spec.model} not found")
        if mv.package_hash != spec.package_hash:
            raise Conflict(f"the definition pins package {spec.package_hash[:12]}, but v{mv.version} is {mv.package_hash[:12]}")
        if mv.status != "champion":
            raise PolicyDenied(f"{spec.model} v{mv.version} is {mv.status}: only the approved champion scores (promote it, "
                               "or publish a scoring definition pinned to the champion)")
        exp = s.get(MLExperiment, mv.experiment_id)
        run = s.get(AnalysisRun, run_id) if run_id else None
        usable_method(s, workspace_id, mv.task, run)
        trained = MLSpec.model_validate(exp.spec)
        keys = list(trained.entity_keys)
        columns: list[str] = []
        source_id = None
        if mv.task != "forecast":
            if spec.input is None:
                raise InvalidInput("a row-wise model needs input (the table to score)")
            scope = scope or resolve_scope(s, s.merge(user), workspace_id, minimum_role="analyst")
            if spec.input.asset not in scope.assets:
                raise Forbidden(f"{spec.input.asset} is not in the authorized scope")
            source_id, types, _ = catalog_types(s, workspace_id, spec.input.asset,
                                                spec.input.source_id or scope.asset_sources.get(spec.input.asset))
            drift = parity(mv.feature_schema, (exp.summary or {}).get("column_types") or {}, types, keys)
            if drift:
                raise PolicyDenied("scoring refused: schema drift since training (" + "; ".join(drift) + "); retrain a "
                                   "challenger on the new schema", details={"schema_drift": drift})
            columns = list(dict.fromkeys([*keys, *[f["name"] for f in mv.feature_schema],
                                          *([trained.time_column] if mv.task == "anomaly" and trained.time_column else [])]))
        ref_id, ref_version, ref_hash, pkg, task = ref.id, int(ref.version), ref.content_hash, mv.package_hash, mv.task
        mv_id = mv.id
    snap = None
    if task != "forecast":
        runner = runner or _runner(user, scope, source_id, run_id)
        snap = snapshot_asset(runner, scope, spec.input.asset, columns, source_id=source_id,
                              max_rows=get_settings().ml_max_rows, purpose=f"ml.scoring:{ref.key}")
    input_version = snap["digest"] if snap else None
    dedupe = stable_hash({"definition": ref_hash, "package": pkg, "input": input_version})
    with session_scope() as s:
        done = s.scalar(select(MLScoringRun).where(MLScoringRun.workspace_id == workspace_id, MLScoringRun.dedupe_key == dedupe,
                                                   MLScoringRun.status == "succeeded").limit(1))
        if done is not None:
            emit(workspace_id, "ml.scoring.duplicate", {"scoring_run_id": done.id, "dedupe_key": dedupe},
                 actor=f"user:{user.id}", session=s)
            return {"status": "duplicate", "scoring_run_id": done.id, "scoring_run": scoring_view(done),
                    "reason": "this definition, package and input version were already scored; nothing was written"}
        row = MLScoringRun(id=new_id("mlsr"), workspace_id=workspace_id, run_id=run_id, definition_id=ref_id,
                           definition_key=ref.key, definition_version=ref_version, definition_hash=ref_hash,
                           model_version_id=mv_id, package_hash=pkg, input_asset=spec.input.asset if spec.input else None,
                           input_source_id=source_id, input_version=input_version, dedupe_key=dedupe,
                           status="awaiting_approval", details={"output": spec.output, "plan_hash": plan_hash,
                                                                "entity_keys": keys, "input_rows": snap["rows"] if snap else 0},
                           query_ids=[snap["query_id"]] if snap else [], created_by=f"user:{user.id}")
        s.add(row)
        s.flush()
        got = _approve_or_request(s, user, workspace_id, SCORE, _scoring_payload(row, spec.output), None,
                                  destination=f"managed_output:ml_{spec.output}", assets=[row.input_asset] if row.input_asset else [],
                                  evidence={"model": spec.model, "model_version_id": mv_id, "rows": row.details["input_rows"]},
                                  run_id=run_id, plan_hash=plan_hash)
        row.approval_id = got["approval_id"]
        emit(workspace_id, "ml.scoring.planned", {"scoring_run_id": row.id, "approval_id": row.approval_id,
                                                  "definition": ref.label}, run_id=run_id, actor=f"user:{user.id}", session=s)
        return {**got, "scoring_run_id": row.id}


def _refuse_scoring(scoring_id: str, message: str, status: str = "refused") -> None:
    with session_scope() as s:
        row = s.get(MLScoringRun, scoring_id)
        row.status, row.error, row.finished_at = status, message[:4000], utcnow()
        emit(row.workspace_id, "ml.scoring.refused", {"scoring_run_id": row.id, "error": message[:300]}, session=s)


@scoped_loader
def execute_scoring(user: User, scoring_id: str, workspace_id: str | None = None, *,
                    approval_id: str | None = None) -> dict[str, Any]:
    """Verify the approval against the payload recomputed now, then score with the verified package and
    write predictions (merge on entity keys + run id) and rejected rows through the staging loader."""
    with session_scope() as s:
        row = get_scoring(s, user, scoring_id, workspace_id, minimum="editor", for_update=True)
        ws = row.workspace_id
        if row.status == "succeeded":
            return {"status": "succeeded", "scoring_run": scoring_view(row)}
        if row.status != "awaiting_approval":
            raise Conflict(f"scoring run {scoring_id} is {row.status}")
        mv = s.get(MLModelVersion, row.model_version_id)
        stale = None
        if mv is None or mv.status != "champion":
            stale = f"{row.definition_key}: model version is {mv.status if mv else 'gone'} (rolled back or replaced since approval)"
        dup = s.scalar(select(MLScoringRun.id).where(MLScoringRun.workspace_id == ws, MLScoringRun.dedupe_key == row.dedupe_key,
                                                     MLScoringRun.status == "succeeded").limit(1))
        output, keys, plan_hash = row.details["output"], list(row.details.get("entity_keys") or []), row.details.get("plan_hash")
        payload = _scoring_payload(row, output)
    if stale:
        _refuse_scoring(scoring_id, "scoring refused: " + stale)
        raise PolicyDenied("scoring refused: " + stale)
    if dup:
        _refuse_scoring(scoring_id, f"duplicate of {dup}: the same input was already scored", status="duplicate")
        return {"status": "duplicate", "scoring_run_id": dup}
    with session_scope() as s:
        from analystos.governance.approvals import consume, verify_for_execution

        apr = verify_for_execution(s, approval_id or row.approval_id, payload=payload, plan_hash=plan_hash)
        if apr.action != SCORE or apr.workspace_id != ws:
            raise PolicyDenied("the approval does not authorize this scoring run")
        verify_package(s, ws, payload["package_hash"])  # the platform record + hash check, before anything is written
        consume(s, apr)
    try:
        out = _compute({"kind": "score", "package_hash": payload["package_hash"], "snapshot": payload["input_version"],
                        "entity_keys": keys}, ws)
        if out.get("status") != "succeeded":
            raise PolicyDenied("scoring refused: " + (out.get("error") or "the job refused"))
        written = _write_scores(user, scoring_id, ws, output, keys, out)
    except AnalystOSError as exc:
        _refuse_scoring(scoring_id, exc.message, status="refused" if isinstance(exc, Forbidden | Conflict) else "failed")
        raise
    with session_scope() as s:
        from analystos.artifacts.registry import link

        row = s.get(MLScoringRun, scoring_id)
        row.status, row.finished_at = "succeeded", utcnow()
        row.rows_input = int(out.get("input_rows") or out["rows_scored"])
        row.rows_scored, row.rows_rejected = int(out["rows_scored"]), len(out.get("rejected") or [])
        row.output_source_id, row.output_table, row.rejected_table = written["source_id"], written["table"], written.get("rejected")
        link(s, ws, ("ml_model_version", row.model_version_id), "scored", ("table", written["table"]))
        link(s, ws, ("ml_scoring_run", row.id), "produced", ("table", written["table"]))
        if row.input_asset:
            link(s, ws, ("table", row.input_asset), "scored_into", ("table", written["table"]))
        emit(ws, "ml.scoring.completed", {"scoring_run_id": row.id, "rows_scored": row.rows_scored,
                                          "rows_rejected": row.rows_rejected, "table": row.output_table},
             run_id=row.run_id, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "ml.scoring.completed", workspace_id=ws, target=row.id, decision="allow",
              details={"approval_id": row.approval_id, "table": row.output_table, "rows": row.rows_scored,
                       "rejected": row.rows_rejected}, session=s)
        return {"status": "succeeded", "scoring_run": scoring_view(row)}


def _arrow(columns: list[str], rows: list[list[Any]]) -> list[Any]:
    import pyarrow as pa

    arrays = []
    for i, _ in enumerate(columns):
        values = [r[i] for r in rows]
        try:
            arrays.append(pa.array(values))
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            arrays.append(pa.array([None if v is None else str(v) for v in values], type=pa.string()))
    arrays = [a if a.type != pa.null() else a.cast(pa.string()) for a in arrays]
    table = pa.Table.from_arrays(arrays, names=columns)
    return table.to_batches(max_chunksize=50_000) or [pa.RecordBatch.from_arrays(arrays, names=columns)]


def _write_scores(user: User, scoring_id: str, ws: str, output: str, keys: list[str], out: dict[str, Any]) -> dict[str, Any]:
    """Predictions and rejected rows into the workspace's managed output source (ADR-0011), read back like any
    staged asset through the gateway. Input PII tags follow the key columns onto the outputs."""
    import json

    from analystos.connectors.naming import sanitize_identifier
    from analystos.services.recipes import _register_asset, _source_tags, ensure_output_source
    from analystos.staging.loader import MAX_TABLE_NAME, StagingLoader

    columns, rows = snapshots(get_settings().artifact_dir).get(out["result"])
    now = utcnow()
    with session_scope() as s:
        row = s.get(MLScoringRun, scoring_id)
        src = ensure_output_source(s, ws)
        source_id, mv_id, key, input_asset = src.id, row.model_version_id, row.definition_key, row.input_asset
        tags = {k: sorted(t) for k, t in ((c.rsplit(".", 1)[1], t) for c, t in
                                          (_source_tags(s, ws, [input_asset]) if input_asset else {}).items()) if k in keys}
    table = sanitize_identifier(f"ml_{output}", max_length=MAX_TABLE_NAME - len(REJECTED_SUFFIX), fallback="ml_scores")
    loader = StagingLoader(get_settings())
    cols = [*columns, "model_version_id", "aos_run_id", "scored_at"]
    data = [[*r, mv_id, scoring_id, now] for r in rows]
    if not data:
        raise InvalidInput("every input row was rejected; nothing to write (see the rejected rows)")
    mode = "merge" if keys else "append"
    info = loader.load(source_id, table, _arrow(cols, data), workspace_id=ws, mode=mode,
                       keys=[*keys, "aos_run_id"] if keys else None, fingerprint="table")
    result: dict[str, Any] = {"source_id": source_id, "table": f"{info['schema']}.{info['table']}"}
    with session_scope() as s:
        _register_asset(s, s.get(Source, source_id), info, recipe=f"ml:{key}", output=output, role="output", tags=tags)
        s.get(Source, source_id).last_discovered_at = utcnow()
    rejected = out.get("rejected") or []
    if rejected:
        rcols = ["entity", "reason", "aos_run_id", "rejected_at"]
        rrows = [[json.dumps(r["keys"], default=str, sort_keys=True), r["reason"], scoring_id, now] for r in rejected]
        rinfo = loader.load(source_id, f"{table}{REJECTED_SUFFIX}", _arrow(rcols, rrows), workspace_id=ws, mode="append",
                            fingerprint="table")
        with session_scope() as s:
            _register_asset(s, s.get(Source, source_id), rinfo, recipe=f"ml:{key}", output=output, role="quarantine", tags={})
        result["rejected"] = f"{rinfo['schema']}.{rinfo['table']}"
    return result


# ------------------------------------------------------------------------------------ MLflow export (P5-05)
@scoped_loader
def export_mlflow(user: User, experiment_id: str, workspace_id: str | None = None) -> tuple[str, bytes]:
    """A zip of an MLflow file store (`mlruns/`) holding this experiment's run: params, metrics per trial
    step, tags, the model card and evaluation report, and the package as an `MLmodel` directory."""
    from analystos.ml.mlflow_export import export_zip

    with session_scope() as s:
        exp = get_experiment(s, user, experiment_id, workspace_id)
        if exp.status != "succeeded":
            raise Conflict(f"experiment {experiment_id} is {exp.status}; only a completed experiment exports")
        records = {t: (s.get(Artifact, a).content if (a := (exp.artifacts or {}).get(t)) else None) for t in RECORD_TYPES}
        package = verify_package(s, exp.workspace_id, exp.package_hash)
        view = experiment_view(exp)
    from analystos.workers.dispatch import pool_configured

    buf = io.BytesIO()
    export_zip(buf, view, records, package, unpickle=not pool_configured("compute-ml"))
    return f"mlflow-{experiment_id}.zip", buf.getvalue()

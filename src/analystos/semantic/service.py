"""The governed workspace semantic model (P4-K03; SEM-001..005).

- **Versions.** Structure (datasets, relationships, `ai_context`) is one versioned row per change;
  each metric name has its own version history and status.
- **Approval workflow.** A proposal (from a user, an agent inside a run, or an import) is a
  `semantic_metric` row in `proposed` plus a hash-bound `Approval` (governance/approvals). Only a
  different person with an approver role can approve it (separation of duties is enforced for this
  action whatever the workspace policy says); approval re-verifies the payload hash and deprecates the
  name's previous approved version. Nothing a model or an import writes is approved automatically.
- **Conflicts (SEM-005).** Duplicate expressions under different names, and one name with competing
  definitions, are computed from the live rows and surfaced — never silently dropped.
- **Publish gate.** `gate_bundle` refuses a bundle whose KPIs are not approved when the workspace
  policy `require_approved_metrics` is on, and stamps approved definitions onto the bundle otherwise.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.bi import MetricDef, PublishBundle
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.contracts.semantic import (
    DialectExpression,
    MetricProposalIn,
    SemanticConflict,
    SemanticDataset,
    SemanticField,
    SemanticMetricDef,
    SemanticModelDoc,
    SemanticRelationship,
)
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Approval, SemanticMetric, SemanticModel, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.semantic import dbt as dbt_adapter
from analystos.semantic import ossie

APPROVAL_ACTION = "semantic_metric.approve"
ACTIVE = ("proposed", "approved")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "workspace"


def _normalize(defn: SemanticMetricDef) -> str:
    return ossie.normalize_expression(defn.expression, defn.dialect)


def _payload(ws_id: str, row_name: str, version: int, defn: SemanticMetricDef) -> dict[str, Any]:
    """What an approval binds to: the exact definition of this version."""
    return {"workspace_id": ws_id, "metric": row_name, "version": version, "definition": defn.model_dump(mode="json")}


# ------------------------------------------------------------------------------------ structure
def current_model(session: Session, workspace_id: str) -> SemanticModel | None:
    return session.scalar(select(SemanticModel).where(SemanticModel.workspace_id == workspace_id,
                                                      SemanticModel.status != "deprecated")
                          .order_by(SemanticModel.version.desc()).limit(1))


def _model_content(name: str, description: str | None, ai_context: Any, datasets: list[dict], relationships: list[dict],
                   extensions: list[dict]) -> dict[str, Any]:
    return {"name": name, "description": description, "ai_context": ai_context, "datasets": datasets,
            "relationships": relationships, "custom_extensions": extensions}


def save_model(session: Session, workspace_id: str, *, actor: str, origin: str, datasets: list[SemanticDataset] | None = None,
               relationships: list[SemanticRelationship] | None = None, description: str | None = None,
               ai_context: Any = None, custom_extensions: list[dict] | None = None, run_id: str | None = None,
               status: str | None = None) -> SemanticModel:
    """New structure version when anything changed (datasets/relationships merged by name). A human
    change is `approved`; an agent's is `proposed` for review. Unchanged content is a no-op."""
    ws = session.get(Workspace, workspace_id)
    cur = current_model(session, workspace_id)
    ds = {d["name"]: d for d in (cur.datasets if cur else [])}
    for d in datasets or []:
        ds[d.name] = d.model_dump(mode="json")
    rels = {r["name"]: r for r in (cur.relationships if cur else [])}
    for r in relationships or []:
        rels[r.name] = r.model_dump(mode="json", by_alias=True)
    name = cur.name if cur else _slug(ws.name if ws else workspace_id)
    content = _model_content(name, description if description is not None else (cur.description if cur else None),
                             ai_context if ai_context is not None else (cur.ai_context if cur else None),
                             list(ds.values()), list(rels.values()),
                             custom_extensions if custom_extensions is not None else (cur.custom_extensions if cur else []))
    digest = stable_hash(content)
    if cur and cur.content_hash == digest:
        return cur
    version = (session.scalar(select(func.max(SemanticModel.version)).where(SemanticModel.workspace_id == workspace_id)) or 0) + 1
    human = not origin.startswith("agent:")
    owner = cur.owner_id if cur and cur.owner_id else (actor.split(":", 1)[1] if human and actor.startswith("user:") else None)
    row = SemanticModel(id=new_id("sem"), workspace_id=workspace_id, name=name, version=version,
                        status=status or ("approved" if human else "proposed"), owner_id=owner,
                        description=content["description"], ai_context=content["ai_context"], datasets=content["datasets"],
                        relationships=content["relationships"], custom_extensions=content["custom_extensions"],
                        origin=origin, run_id=run_id, content_hash=digest, created_by=actor)
    session.add(row)
    session.flush()
    emit(workspace_id, "semantic.model.updated", {"version": version, "origin": origin, "datasets": len(content["datasets"])},
         run_id=run_id, actor=actor, session=session)
    audit(actor, "semantic.model.saved", workspace_id=workspace_id, run_id=run_id, target=row.id,
          details={"version": version, "origin": origin, "content_hash": digest}, session=session)
    return row


def dataset_from_def(ds: Any) -> SemanticDataset:
    """A run's governed virtual dataset (contracts.bi.DatasetDef) as an Ossie dataset: the query is its source."""
    fields = []
    for c in ds.columns:
        semantic = c.get("semantic_type")
        is_time = c.get("name") == ds.time_column or semantic in ("datetime", "date", "time")
        dimension = {"is_time": True} if is_time else ({"is_time": False} if semantic in ("categorical", "boolean") else None)
        fields.append(SemanticField(name=c["name"], expressions=[DialectExpression(expression=c["name"])], dimension=dimension,
                                    label=c.get("business_name") or c.get("label"), description=c.get("description")))
    return SemanticDataset(name=ds.name, source=ds.sql, description=ds.description or None, fields=fields)


# ------------------------------------------------------------------------------------ metrics
def metric_rows(session: Session, workspace_id: str, *, name: str | None = None, status: str | None = None) -> list[SemanticMetric]:
    stmt = select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id)
    if name:
        stmt = stmt.where(SemanticMetric.name == name)
    if status:
        stmt = stmt.where(SemanticMetric.status == status)
    return list(session.scalars(stmt.order_by(SemanticMetric.name, SemanticMetric.version)))


def approved_metrics(session: Session, workspace_id: str) -> dict[str, SemanticMetric]:
    """The stable definitions: the approved version of each name (approving deprecates older ones)."""
    out: dict[str, SemanticMetric] = {}
    for row in metric_rows(session, workspace_id, status="approved"):
        out[row.name] = row  # ordered by version: the newest approved wins if two ever coexist
    return out


def definition(row: SemanticMetric) -> SemanticMetricDef:
    return SemanticMetricDef.model_validate(row.definition)


def to_metricdef(row: SemanticMetric) -> MetricDef:
    d = definition(row)
    return MetricDef(name=d.name, display_name=d.display_name or d.name.replace("_", " ").capitalize(),
                     definition=d.description or "", sql_expression=d.expression, format=d.format or "number",
                     grain=d.grain or "", filters=d.filters, dimensions=d.dimensions, owner=row.owner_id,
                     source_columns=d.source_columns, status="approved" if row.status == "approved" else "proposed")


def from_metricdef(m: MetricDef, *, dataset: str | None = None, sql_dialect: str | None = None) -> SemanticMetricDef:
    return SemanticMetricDef(name=m.name, expressions=[DialectExpression(expression=m.sql_expression)],
                             description=m.definition or None, display_name=m.display_name, format=m.format,
                             grain=m.grain or None, filters=m.filters, dimensions=m.dimensions,
                             source_columns=m.source_columns, dataset=dataset,
                             ai_context={"instructions": f"Evaluated on the governed dataset {dataset} ({sql_dialect})."}
                             if dataset and sql_dialect else None)


def propose_metric(session: Session, workspace_id: str, defn: SemanticMetricDef, *, proposed_by: str, via: str,
                   run_id: str | None = None, source: tuple[str, str] | None = None) -> tuple[SemanticMetric, bool]:
    """Record a proposal and its hash-bound approval request. Idempotent: the same definition that is
    already proposed or approved under this name is returned as is (a recurring run does not re-propose),
    and an agent never re-proposes a definition a person rejected or deprecated. Returns (row, created)."""
    from analystos.governance.approvals import request_approval

    problem = ossie.metric_problem(defn)
    if problem and via == "user":
        raise InvalidInput(f"metric {defn.name}: {problem}")
    norm = _normalize(defn)
    history = metric_rows(session, workspace_id, name=defn.name)
    for row in reversed(history):
        if row.normalized_expression == norm and (row.status in ACTIVE or (via != "user" and row.status in ("rejected", "deprecated"))):
            return row, False
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise NotFound(f"workspace {workspace_id} not found")
    version = (history[-1].version if history else 0) + 1
    row = SemanticMetric(id=new_id("smet"), workspace_id=workspace_id, name=defn.name, version=version, status="proposed",
                         definition=defn.model_dump(mode="json"), expression=defn.expression, normalized_expression=norm,
                         display_name=defn.display_name, owner_id=proposed_by, proposed_by=proposed_by, proposed_via=via,
                         run_id=run_id, content_hash=stable_hash(defn.model_dump(mode="json")),
                         reason=f"import issue: {problem}" if problem else None)
    session.add(row)
    session.flush()
    found = [c for c in conflicts(session, workspace_id) if defn.name in c.names]
    approval = request_approval(session, workspace_id=workspace_id, run_id=None, action=APPROVAL_ACTION,
                                payload=_payload(workspace_id, defn.name, version, defn), plan_hash=None,
                                policy_version=ws.policy_version, requested_by=proposed_by, risk_tier="medium", destination=None,
                                affected_assets=[f"metric:{defn.name}"],
                                evidence={"proposed_via": via, "run_id": run_id, "expression": defn.expression,
                                          "conflicts": [c.model_dump() for c in found], "problem": problem})
    row.approval_id = approval.id
    if source:
        from analystos.artifacts.registry import link

        link(session, workspace_id, ("semantic_metric", row.id), "derived_from", source, run_id=run_id)
    emit(workspace_id, "semantic.metric.proposed", {"metric": defn.name, "version": version, "via": via, "approval_id": approval.id},
         run_id=run_id, actor=f"user:{proposed_by}" if via == "user" else via, session=session)
    audit(f"user:{proposed_by}" if via == "user" else via, "semantic.metric.proposed", workspace_id=workspace_id, run_id=run_id,
          target=row.id, details={"metric": defn.name, "version": version, "expression": defn.expression,
                                  "on_behalf_of": proposed_by}, session=session)
    for c in found:
        emit(workspace_id, "semantic.conflict.detected", c.model_dump(), run_id=run_id, session=session)
    return row, True


def propose_from_input(session: Session, workspace_id: str, body: MetricProposalIn, user: User) -> tuple[SemanticMetric, bool]:
    defn = SemanticMetricDef(name=body.name, expressions=[DialectExpression(dialect=body.dialect, expression=body.expression)],
                             description=body.description, ai_context=body.ai_context, display_name=body.display_name,
                             format=body.format, grain=body.grain, filters=body.filters, dimensions=body.dimensions,
                             dataset=body.dataset)
    return propose_metric(session, workspace_id, defn, proposed_by=user.id, via="user")


def _pending(session: Session, workspace_id: str, name: str, version: int | None) -> SemanticMetric:
    rows = [r for r in metric_rows(session, workspace_id, name=name) if version is None or r.version == version]
    proposed = [r for r in rows if r.status == "proposed"]
    if not rows:
        raise NotFound(f"metric {name} not found")
    if not proposed:
        raise Conflict(f"metric {name}{f' v{version}' if version else ''} has no pending proposal (status {rows[-1].status})")
    return proposed[-1]


def _fresh_approval(session: Session, row: SemanticMetric) -> Approval:
    """The row's approval, re-requested when it lapsed (TTL) or a policy edit invalidated it: the
    payload is the same definition, so the new request binds to the same hash."""
    from analystos.governance.approvals import request_approval

    approval = session.get(Approval, row.approval_id) if row.approval_id else None
    ws = session.get(Workspace, row.workspace_id)
    if approval is not None and approval.status == "pending" and approval.expires_at >= utcnow() \
            and approval.policy_version == ws.policy_version:
        return approval
    if approval is not None and approval.status == "pending":
        approval.status, approval.reason = "invalidated", "re-requested (expired or policy changed)"
    approval = request_approval(session, workspace_id=row.workspace_id, run_id=None, action=APPROVAL_ACTION,
                                payload=_payload(row.workspace_id, row.name, row.version, definition(row)), plan_hash=None,
                                policy_version=ws.policy_version, requested_by=row.proposed_by, risk_tier="medium",
                                destination=None, affected_assets=[f"metric:{row.name}"],
                                evidence={"proposed_via": row.proposed_via, "re_requested": True})
    row.approval_id = approval.id
    return approval


def decide_metric(session: Session, workspace_id: str, name: str, user: User, *, approve: bool, version: int | None = None,
                  reason: str | None = None) -> SemanticMetric:
    """Approve or reject a proposal. The approval layer checks role, separation of duties and the
    policy version; approving then re-verifies the payload hash before the definition becomes stable."""
    from analystos.governance.approvals import decide

    row = _pending(session, workspace_id, name, version)
    if row.proposed_by == user.id:
        # own session: the caller's transaction rolls back with the Forbidden, the denial must stay on record
        audit(f"user:{user.id}", "semantic.metric.self_approval_blocked", workspace_id=workspace_id, target=row.id,
              decision="deny", reasons=["separation_of_duties"])
        raise Forbidden("separation of duties: the proposer of a metric cannot approve or reject it")
    approval = _fresh_approval(session, row)
    decide(session, approval.id, user, approve=approve, reason=reason)
    return apply_decision(session, approval)


def apply_decision(session: Session, approval: Approval) -> SemanticMetric:
    """Make a decided metric approval take effect (also called when the decision came through the
    generic approvals inbox)."""
    from analystos.governance.approvals import verify_for_execution

    row = session.scalar(select(SemanticMetric).where(SemanticMetric.approval_id == approval.id))
    if row is None:
        raise NotFound(f"no metric proposal bound to approval {approval.id}")
    if row.status != "proposed":
        return row
    if approval.status == "rejected":
        row.status, row.decided_by, row.decided_at, row.reason = "rejected", approval.decided_by, utcnow(), approval.reason
        emit(row.workspace_id, "semantic.metric.rejected", {"metric": row.name, "version": row.version},
             actor=f"user:{approval.decided_by}", session=session)
        audit(f"user:{approval.decided_by}", "semantic.metric.rejected", workspace_id=row.workspace_id, target=row.id,
              decision="deny", reasons=[approval.reason or ""], session=session)
        return row
    if approval.status != "approved":
        return row
    # Bound to the hash of exactly this version's definition: a changed definition cannot ride on it.
    verify_for_execution(session, approval.id, payload=_payload(row.workspace_id, row.name, row.version, definition(row)),
                         plan_hash=None)
    for old in metric_rows(session, row.workspace_id, name=row.name, status="approved"):
        old.status, old.reason = "deprecated", f"superseded by v{row.version}"
    row.status, row.decided_by, row.decided_at = "approved", approval.decided_by, utcnow()
    approval.status = "executed"
    emit(row.workspace_id, "semantic.metric.approved", {"metric": row.name, "version": row.version, "approval_id": approval.id},
         actor=f"user:{approval.decided_by}", session=session)
    audit(f"user:{approval.decided_by}", "semantic.metric.approved", workspace_id=row.workspace_id, target=row.id,
          decision="allow", details={"metric": row.name, "version": row.version, "approval_id": approval.id,
                                     "payload_hash": approval.payload_hash, "proposed_by": row.proposed_by}, session=session)
    from analystos.knowledge.learning import draft_from_metric

    draft_from_metric(session, row)  # P4-K08: the approved KPI becomes a knowledge draft for review
    return row


def deprecate_metric(session: Session, workspace_id: str, name: str, user: User, *, reason: str | None = None) -> list[SemanticMetric]:
    """Retire a name: its approved version and any open proposals. A deprecated KPI no longer passes the
    publish gate, so a pending publication that includes it is refused at execution time."""
    rows = [r for r in metric_rows(session, workspace_id, name=name) if r.status in ACTIVE]
    if not rows:
        raise NotFound(f"metric {name} has no approved or proposed version")
    for r in rows:
        if r.status == "proposed" and r.approval_id:
            approval = session.get(Approval, r.approval_id)
            if approval is not None and approval.status in ("pending", "approved"):
                approval.status, approval.reason = "invalidated", "metric deprecated"
        r.status, r.reason, r.decided_by, r.decided_at = "deprecated", reason or "deprecated", user.id, utcnow()
    emit(workspace_id, "semantic.metric.deprecated", {"metric": name, "versions": [r.version for r in rows]},
         actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "semantic.metric.deprecated", workspace_id=workspace_id, target=name, decision="allow",
          reasons=[reason or ""], session=session)
    return rows


# ------------------------------------------------------------------------------------ conflicts
def conflicts(session: Session, workspace_id: str) -> list[SemanticConflict]:
    """SEM-005 over the live (proposed or approved) versions: one expression under several names, and
    one name with several competing expressions (for example an approved v1 and a proposed v2)."""
    live = [r for r in metric_rows(session, workspace_id) if r.status in ACTIVE]
    by_expr: dict[str, list[SemanticMetric]] = defaultdict(list)
    by_name: dict[str, list[SemanticMetric]] = defaultdict(list)
    for r in live:
        by_expr[r.normalized_expression].append(r)
        by_name[r.name].append(r)

    def brief(r: SemanticMetric) -> dict[str, Any]:
        return {"name": r.name, "version": r.version, "status": r.status, "expression": r.expression}

    out = []
    for rows in by_expr.values():
        names = sorted({r.name for r in rows})
        if len(names) > 1:
            out.append(SemanticConflict(kind="duplicate_expression", names=names, metrics=[brief(r) for r in rows],
                                        detail=f"{', '.join(names)} compute the same expression; keep one name"))
    for name, rows in by_name.items():
        if len({r.normalized_expression for r in rows}) > 1:
            out.append(SemanticConflict(kind="conflicting_definition", names=[name], metrics=[brief(r) for r in rows],
                                        detail=f"{name} has {len(rows)} competing definitions; approve one, reject the others"))
    return out


# ------------------------------------------------------------------------------------ Ossie documents
def model_doc(session: Session, workspace_id: str, *, include: str = "approved") -> SemanticModelDoc:
    """The workspace model as one Ossie model. `approved`: the stable definitions only; `all`: plus the
    newest open proposal of names that have no approved version (Ossie allows one metric per name)."""
    cur = current_model(session, workspace_id)
    ws = session.get(Workspace, workspace_id)
    metrics: dict[str, SemanticMetric] = dict(approved_metrics(session, workspace_id))
    if include == "all":
        for r in metric_rows(session, workspace_id, status="proposed"):
            if r.name not in metrics or metrics[r.name].status != "approved":
                metrics[r.name] = r
    return SemanticModelDoc(
        name=cur.name if cur else _slug(ws.name if ws else workspace_id),
        description=cur.description if cur else None, ai_context=cur.ai_context if cur else None,
        datasets=[SemanticDataset.model_validate(d) for d in (cur.datasets if cur else [])],
        relationships=[SemanticRelationship.model_validate(r) for r in (cur.relationships if cur else [])],
        metrics=[definition(r) for r in sorted(metrics.values(), key=lambda r: r.name)],
        custom_extensions=cur.custom_extensions if cur else [])


def ossie_document(session: Session, workspace_id: str, *, include: str = "approved") -> dict[str, Any]:
    doc = ossie.to_ossie([model_doc(session, workspace_id, include=include)])
    if not doc["semantic_model"][0].get("datasets"):
        raise Conflict("the semantic model has no datasets yet (Ossie requires one); run an analysis or import a model first")
    problems = ossie.schema_problems(doc)
    if problems:  # our own output must always be valid against the pin
        raise Conflict("semantic model does not serialize to valid Ossie 0.1.1: " + "; ".join(problems[:3]))
    return doc


def import_models(session: Session, workspace_id: str, models: list[SemanticModelDoc], user: User, *, via: str,
                  issues: list[str] | None = None) -> dict[str, Any]:
    """Merge imported structure into the workspace model and propose every metric. An import never
    carries an approval: imported KPIs wait for an approver like any other proposal."""
    report: dict[str, Any] = {"datasets": 0, "relationships": 0, "proposed": [], "unchanged": [], "issues": list(issues or [])}
    for m in models:
        save_model(session, workspace_id, actor=f"user:{user.id}", origin=via, datasets=m.datasets, relationships=m.relationships,
                   description=m.description, ai_context=m.ai_context)
        report["datasets"] += len(m.datasets)
        report["relationships"] += len(m.relationships)
        for metric in m.metrics:
            row, created = propose_metric(session, workspace_id, metric, proposed_by=user.id, via=via)
            (report["proposed"] if created else report["unchanged"]).append({"name": row.name, "version": row.version})
    report["conflicts"] = [c.model_dump() for c in conflicts(session, workspace_id)]
    emit(workspace_id, "semantic.imported", {"via": via, "proposed": len(report["proposed"]), "issues": len(report["issues"])},
         actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "semantic.imported", workspace_id=workspace_id, details={k: v for k, v in report.items() if k != "conflicts"},
          session=session)
    return report


def import_dbt(session: Session, workspace_id: str, document: str | bytes | dict, user: User) -> dict[str, Any]:
    try:
        models, issues = dbt_adapter.import_osi_document(document)
    except (ossie.OssieError, ValueError) as exc:
        raise InvalidInput(f"not a usable dbt osi_document.json: {exc}",
                           details={"problems": getattr(exc, "problems", [str(exc)])}) from exc
    return import_models(session, workspace_id, models, user, via="dbt", issues=issues)


def import_ossie(session: Session, workspace_id: str, text: str | bytes, user: User) -> dict[str, Any]:
    try:
        doc = ossie.parse_text(text)
        problems = ossie.validate(doc)
        if any(p.startswith("[Schema]") for p in problems):
            raise ossie.OssieError(problems)
        models = ossie.from_ossie(doc)
    except (ossie.OssieError, ValueError) as exc:
        raise InvalidInput(f"not a valid Ossie {ossie.OSSIE_VERSION} document: {exc}",
                           details={"problems": getattr(exc, "problems", [str(exc)])}) from exc
    return import_models(session, workspace_id, models, user, via="ossie", issues=problems)


def export_dbt(session: Session, workspace_id: str) -> dict[str, Any]:
    files, issues = dbt_adapter.export_osi_folder([model_doc(session, workspace_id)])
    return {"files": files, "issues": issues}


def export_superset(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    from analystos.publishing.superset import superset_metric

    return [superset_metric(to_metricdef(r)) for r in approved_metrics(session, workspace_id).values()]


# ------------------------------------------------------------------------------------ publish gate
def gate_bundle(session: Session, workspace_id: str, policy: WorkspacePolicyDoc, bundle: PublishBundle) -> PublishBundle:
    """Stamp approved definitions onto the bundle's KPIs; refuse unapproved ones when the policy says so.
    A KPI counts as approved only if an approved version of that name has the same expression, so a run
    cannot publish its own variant under an approved name."""
    approved = approved_metrics(session, workspace_id)
    metrics, missing = [], []
    for m in bundle.metrics:
        row = approved.get(m.name)
        if row is not None and row.normalized_expression == ossie.normalize_expression(m.sql_expression):
            a = to_metricdef(row)
            metrics.append(m.model_copy(update={"status": "approved", "display_name": a.display_name,
                                                "definition": a.definition or m.definition, "format": a.format,
                                                "grain": a.grain or m.grain, "owner": a.owner}))
        else:
            missing.append(m.name)
            metrics.append(m)
    if missing and policy.require_approved_metrics:
        raise PolicyDenied(
            f"publication refused: {len(missing)} KPI(s) are not approved in the workspace semantic model: "
            f"{', '.join(missing)}. Remedy: an approver other than the proposer approves each proposal "
            f"(POST /api/workspaces/{workspace_id}/semantic/metrics/<name>/approve, or the approvals inbox), then re-run "
            f"the analysis; or a workspace owner turns off the policy require_approved_metrics.",
            details={"unapproved_metrics": missing, "remedy": f"/api/workspaces/{workspace_id}/semantic/metrics"})
    return bundle.model_copy(update={"metrics": metrics})

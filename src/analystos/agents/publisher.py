"""BI Publisher Agent + Governance review (§13.15, §13.17, §32, §39).

publish_request: build the immutable bundle, governance-review it, ask for approval bound to its
hash and to the current plan hash. publish: re-verify approval, payload, plan and authorization
immediately before the external side effect; publish idempotently (reconcile by idempotency key)."""
from __future__ import annotations

from sqlalchemy import select

from analystos.agents.sql_agent import dataset_def
from analystos.agents.visualization import load_bundle_parts
from analystos.artifacts.registry import link
from analystos.contracts.bi import ChartSpec, PublishBundle
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, ApprovalRequired, PolicyDenied
from analystos.core.ids import new_id, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Artifact, Publication, RunTask, User
from analystos.events.bus import emit
from analystos.governance.approvals import request_approval, verify_for_execution
from analystos.governance.audit import audit
from analystos.governance.policy import evaluate
from analystos.runtime.context import RunContext
from analystos.tools.registry import ToolRuntime, get_agent_spec


def choose_destination(ctx: RunContext) -> str:
    from analystos.publishing.base import get_publisher
    from analystos.services.platform_settings import get as platform

    if not platform().features.superset_publishing:
        ctx.say("Superset publishing is turned off by the administrator; using the local preview destination.", kind="decision")
        return "preview"
    if "superset" in ctx.policy.publish_destinations:
        try:
            status = get_publisher("superset", get_settings()).test_connection()
            if (status or {}).get("ok", True):
                return "superset"
        except Exception as exc:  # visible degradation, not silent
            ctx.say(f"Superset unreachable ({str(exc)[:120]}); publication will target the local preview destination.", kind="decision")
    return "preview"


def build_bundle(ctx: RunContext, destination: str) -> PublishBundle:
    ds, _ = dataset_def(ctx.run.id)
    parts = load_bundle_parts(ctx.run.id)
    charts = [ChartSpec.model_validate({**c.model_dump(), "preview": {}}) for c in parts["charts"]]  # previews are not published
    bundle = PublishBundle(workspace_id=ctx.workspace.id, destination=destination, datasets=[ds], metrics=parts["metrics"],
                           charts=charts, dashboards=parts["dashboards"])
    # P4-K03: KPIs are published as their approved semantic-layer definitions; with the workspace policy
    # require_approved_metrics an unapproved KPI refuses the bundle, here and again right before publishing.
    from analystos.semantic.service import gate_bundle

    with session_scope() as s:
        return gate_bundle(s, ctx.workspace.id, ctx.policy, bundle)


def governance_review(ctx: RunContext, bundle: PublishBundle) -> dict:
    """Runs as the Governance Agent identity. Re-validates every dataset SQL against the CURRENT scope
    (restricted columns cannot leak into a published dataset) and checks destination policy."""
    from analystos.publishing.preview import validate_bundle

    with session_scope() as s:
        gov = get_agent_spec(s, "governance")
    rt = ToolRuntime(user=ctx.user, identity=ctx.identity.model_copy(update={"agent_id": "governance"}), agent=gov)

    def review():
        problems = list(validate_bundle(bundle))
        for ds in bundle.datasets:
            try:
                from analystos.gateway.validator import validate_sql

                validate_sql(ctx.scope, ds.sql, max_rows=1)
            except AnalystOSError as exc:
                problems.append(f"dataset {ds.name}: {exc.message}")
        if bundle.destination not in ctx.policy.publish_destinations and bundle.destination != "preview":
            problems.append(f"destination {bundle.destination} not allowed by workspace policy")
        return {"ok": not problems, "problems": problems}

    return rt.invoke("governance.review", {"destination": bundle.destination, "datasets": [d.name for d in bundle.datasets]}, review)


def request_publication(ctx: RunContext) -> dict:
    destination = choose_destination(ctx)
    bundle = build_bundle(ctx, destination)
    review = governance_review(ctx, bundle)
    if not review["ok"]:
        ctx.say("Governance blocked publication: " + "; ".join(review["problems"][:5]), kind="decision")
        raise PolicyDenied("governance review failed: " + "; ".join(review["problems"][:3]))
    payload = bundle.model_dump()
    with session_scope() as s:
        user = s.get(User, ctx.user.id)
        decision = evaluate(s, user, ctx.identity, "publish", destination=destination if destination != "preview" else "superset")
        if decision.decision == "deny":
            raise PolicyDenied("publication denied: " + ", ".join(decision.reasons))
        run = s.get(AnalysisRun, ctx.run.id)
        jev = ctx.jev.consequential(f"Publish {len(bundle.dashboards)} dashboards with {len(bundle.charts)} charts to {destination}",
                                    ctx=ctx.call_ctx())
        approval = request_approval(
            s, workspace_id=ctx.workspace.id, run_id=run.id, action="publish_dashboard", payload=payload, plan_hash=run.plan_hash,
            policy_version=run.policy_version, requested_by=run.requested_by, risk_tier="high", destination=destination,
            affected_assets=[d.name for d in bundle.datasets] + [d.key for d in bundle.dashboards],
            evidence={"governance_review": review, "policy": decision.model_dump(),
                      "jev_consequential": jev.value if jev else None,
                      "charts": len(bundle.charts), "metrics": len(bundle.metrics)})
        task = s.scalar(select(RunTask).where(RunTask.run_id == run.id, RunTask.key == "publish"))
        task.input = {**task.input, "approval_id": approval.id, "destination": destination}
    ctx.say(f"Publication to {destination} awaits approval {approval.id} (bundle hash {approval.payload_hash[:12]}; "
            f"{len(bundle.dashboards)} dashboards, {len(bundle.charts)} charts). Nothing is published until a person approves.",
            kind="decision")
    return {"approval_id": approval.id, "destination": destination, "payload_hash": approval.payload_hash}


def publish(ctx: RunContext) -> dict:
    from analystos.publishing.base import get_publisher

    approval_id = ctx.task.input.get("approval_id")
    destination = ctx.task.input.get("destination", "superset")
    with session_scope() as s:
        approval = s.get(Approval, approval_id)
        payload = dict(approval.payload)
        run = s.get(AnalysisRun, ctx.run.id)
        # Re-verify immediately before the side effect: status, payload hash, plan hash, policy version, authorization.
        verify_for_execution(s, approval_id, payload=payload, plan_hash=run.plan_hash)
    from analystos.services.platform_settings import get as platform

    if destination == "superset" and not platform().features.superset_publishing:
        raise PolicyDenied("Superset publishing was turned off by the administrator after this approval was granted")
    current = build_bundle(ctx, destination).model_dump()
    if stable_hash(current) != stable_hash(payload):
        with session_scope() as s:
            a = s.get(Approval, approval_id)
            a.status, a.reason = "invalidated", "artifacts changed after approval"
        raise ApprovalRequired("artifacts changed after approval; a new approval is required")
    ctx.check_control()
    bundle = PublishBundle.model_validate(payload)
    key = f"{ctx.run.id}:{approval.payload_hash[:32]}"
    with session_scope() as s:
        pub = s.scalar(select(Publication).where(Publication.idempotency_key == key))
        if pub and pub.status == "succeeded":
            return {"status": "succeeded", "reused": True, "external_ids": pub.external_ids}
        previous = dict(pub.external_ids) if pub else None
        if pub is None:
            pub = Publication(id=new_id("pub"), workspace_id=ctx.workspace.id, run_id=ctx.run.id, approval_id=approval_id,
                              destination=destination, idempotency_key=key, status="started")
            s.add(pub)
        pub_id = pub.id
    publisher = get_publisher(destination, get_settings())
    try:
        result = ctx.tools().invoke(f"{'superset' if destination == 'superset' else 'preview'}.publish",
                                    {"_approval_verified": True, "approval_id": approval_id, "destination": destination},
                                    lambda: publisher.publish(bundle, idempotency_key=key, previous=previous))
    except AnalystOSError as exc:
        with session_scope() as s:
            p = s.get(Publication, pub_id)
            p.status, p.error = "failed", exc.message[:1000]
        raise
    with session_scope() as s:
        p = s.get(Publication, pub_id)
        p.status, p.external_ids, p.error = result.status, result.external_ids, "; ".join(result.errors) or None
        a = s.get(Approval, approval_id)
        if result.status == "succeeded":
            a.status = "executed"
        arts = {(x.type, x.name): x for x in s.scalars(select(Artifact).where(Artifact.run_id == ctx.run.id))}
        for kind, mapping in (("chart", result.external_ids.get("charts") or {}), ("dashboard", result.external_ids.get("dashboards") or {}),
                              ("dataset", result.external_ids.get("datasets") or {})):
            for name, ext in mapping.items():
                art = arts.get((kind, name))
                if art:
                    art.platform, art.external_id, art.status = destination, str(ext), "published"
                    art.external_url = result.urls.get(name)
                    link(s, ctx.workspace.id, (kind, art.id), "published_as", ("publication", pub_id), run_id=ctx.run.id)
        emit(ctx.workspace.id, "dashboard.published", {"destination": destination, "status": result.status, "urls": result.urls},
             run_id=ctx.run.id, session=s)
        audit(f"agent:{ctx.agent.id}", "publication.executed", workspace_id=ctx.workspace.id, run_id=ctx.run.id, target=pub_id,
              decision="allow", details={"destination": destination, "status": result.status, "approval_id": approval_id}, session=s)
    if result.status != "succeeded":
        ctx.say(f"Publication {result.status}: {'; '.join(result.errors[:3])}. Recorded for reconcile-before-retry.", kind="decision")
        if result.status == "failed":
            raise AnalystOSError("publication failed: " + "; ".join(result.errors[:3]))
    ctx.say(f"Published to {destination}: " + ", ".join(f"{k} → {v}" for k, v in result.urls.items()))
    return {"status": result.status, "external_ids": result.external_ids, "urls": result.urls, "publication_id": pub_id}

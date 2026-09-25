"""analystos CLI: migrate | provision-analytics-roles | seed | worker | scheduler | api | export-contracts | replay-run"""
from __future__ import annotations

import argparse
import json
import sys

from analystos.core.config import REPO_ROOT, get_settings


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(cfg, "head")
    provision_analytics_roles()


def provision_analytics_roles() -> dict:
    """Self-healing step for per-workspace analytics reader roles (spec v3 tenant isolation):
    give the loader CREATEROLE when the control-plane identity may, then move every existing staged
    schema from the shared reader grant to its workspace's role. Best effort: an unreachable
    analytics DB must not block a control-plane migration (loads repair their own schema)."""
    from sqlalchemy import select
    from sqlalchemy.engine import make_url

    from analystos.core.logging import get_logger
    from analystos.db.base import session_scope
    from analystos.db.models import Source
    from analystos.staging.roles import backfill, provision_loader_createrole

    log = get_logger(__name__)
    settings = get_settings()
    try:
        provision_loader_createrole(settings.database_url, make_url(settings.analytics_loader_url).username or "")
        with session_scope() as s:
            staged = [(r.id, r.workspace_id) for r in s.scalars(select(Source).where(Source.execution_mode == "staged"))]
        return backfill(settings, staged)
    except Exception as exc:  # noqa: BLE001
        log.warning("analytics role provisioning skipped: %s", str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__)
        return {"error": type(exc).__name__}


GLOSSARY = [
    ("term", "SLA breach", "An incident that did not meet its service-level agreement target (made_sla = false).",
     ["missed SLA", "SLA violation"], ["incident.made_sla"]),
    ("metric", "MTTR", "Mean Time to Resolve: average of resolved_at - opened_at for resolved incidents, in hours.",
     ["mean time to resolve", "resolution time"], ["incident.opened_at", "incident.resolved_at"]),
    ("term", "Reassignment", "Number of times an incident moved between assignment groups (reassignment_count). High "
     "reassignment usually signals routing problems.", ["hand-off", "ping-pong"], ["incident.reassignment_count"]),
    ("term", "Priority 1 (P1)", "Critical incident: priority = 1 (impact high, urgency high).", ["Sev-1", "critical incident", "P1"],
     ["incident.priority"]),
    ("term", "After-hours incident", "Incident opened before 08:00, after 18:00 or at the weekend.", ["out of hours"],
     ["incident.opened_at"]),
    ("term", "Change-related incident", "Incident whose caused_by references a change request.", ["change-induced incident"],
     ["incident.caused_by", "change_request.sys_id"]),
    ("term", "Emergency change", "Change request of type emergency, implemented outside the normal CAB cycle.", ["expedited change"],
     ["change_request.type"]),
    ("term", "Assignment group", "The support team responsible for an incident or change.", ["resolver group", "team"],
     ["incident.assignment_group", "sys_user_group.name"]),
    ("term", "Configuration item", "Application, service or infrastructure element from the CMDB affected by the record.",
     ["CI", "application", "service"], ["incident.cmdb_ci", "cmdb_ci.name"]),
]


def seed() -> None:
    from sqlalchemy import select

    from analystos.context.service import add_entry
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import ContextEntry, User
    from analystos.security.auth import hash_password
    from analystos.tools.registry import seed_registries

    settings = get_settings()
    with session_scope() as s:
        users = [(settings.bootstrap_admin_email, "Platform Admin", settings.bootstrap_admin_password, True, {"pii_clearance": False}),
                 ("analyst@analystos.local", "Ada Analyst", "ChangeMe123!", False, {}),
                 ("approver@analystos.local", "Priya Approver", "ChangeMe123!", False, {})]
        for email, name, pw, admin, attrs in users:
            if not s.scalar(select(User).where(User.email == email)):
                s.add(User(id=new_id("usr"), email=email, name=name, password_hash=hash_password(pw), is_admin=admin, attributes=attrs))
        seed_registries(s)
        if not s.scalar(select(ContextEntry).where(ContextEntry.workspace_id.is_(None), ContextEntry.origin == "context2ai-seed")):
            for kind, name, body, syn, cols in GLOSSARY:
                add_entry(s, workspace_id=None, kind=kind, name=name, body=body, synonyms=syn, mapped_columns=cols, origin="context2ai-seed")
    print("seeded users, registries and ServiceNow glossary")


def export_contracts() -> None:
    from analystos.contracts import analysis, bi, capability, platform, policy, registry

    out = REPO_ROOT / "contracts"
    out.mkdir(exist_ok=True)
    models = {"agent": registry.AgentSpec, "tool": registry.ToolSpec, "skill": registry.SkillSpec, "policy": policy.WorkspacePolicyDoc,
              "data_scope": policy.DataScope, "policy_decision": policy.PolicyDecision, "analysis_spec": analysis.AnalysisSpec,
              "stat_result": analysis.StatResult, "chart": bi.ChartSpec, "dashboard": bi.DashboardSpec, "metric": bi.MetricDef,
              "dataset": bi.DatasetDef, "publish_bundle": bi.PublishBundle,
              "platform_settings": platform.PlatformSettings, "capability": capability.CapabilityManifest}
    for name, model in models.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
    from analystos.contracts.events import EVENT_TYPES

    (out / "events.json").write_text(json.dumps(sorted(EVENT_TYPES), indent=2) + "\n")
    print(f"wrote {len(models) + 1} contract files to {out}")


def replay_run(run_id: str, *, check: bool, out: str | None) -> int:
    """Reconstruct a run's model inputs/outputs from storage; with --check, re-execute every
    recorded call offline through the router (ReplayTransport) and compare with the recording."""
    from analystos.llm.replay import run_report

    report = run_report(run_id, check=check)
    text = json.dumps(report, indent=2, default=str)
    if out:
        from pathlib import Path

        Path(out).write_text(text + "\n")
        print(f"wrote {report['summary']['calls']} model calls of run {run_id} to {out}")
    else:
        print(text)
    if check:
        replay = report["replay"]
        print(f"replay: {replay['matched']}/{replay['checked']} calls reproduced offline, "
              f"{len(replay['mismatches'])} mismatches", file=sys.stderr)
        return 1 if replay["mismatches"] else 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analystos")
    parser.add_argument("command", choices=["migrate", "provision-analytics-roles", "seed", "worker", "scheduler", "api",
                                            "export-contracts", "replay-run"])
    parser.add_argument("run_id", nargs="?", help="replay-run: the analysis run id")
    parser.add_argument("--check", action="store_true", help="replay-run: re-execute recorded calls offline and compare")
    parser.add_argument("--out", help="replay-run: write the JSON report to this file")
    args = parser.parse_args(argv)
    if args.command == "replay-run":
        if not args.run_id:
            parser.error("replay-run needs a run id")
        return replay_run(args.run_id, check=args.check, out=args.out)
    if args.command == "migrate":
        migrate()
    elif args.command == "provision-analytics-roles":
        print(json.dumps(provision_analytics_roles(), indent=2))
    elif args.command == "seed":
        seed()
    elif args.command == "worker":
        from analystos.workflows.worker import run_worker

        run_worker()
    elif args.command == "scheduler":
        from analystos.services.schedules import run_scheduler

        run_scheduler()
    elif args.command == "api":
        import uvicorn

        uvicorn.run("analystos.api.app:app", host="0.0.0.0", port=8000)
    elif args.command == "export-contracts":
        export_contracts()
    return 0


if __name__ == "__main__":
    sys.exit(main())

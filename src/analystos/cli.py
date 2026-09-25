"""analystos CLI: migrate | seed | worker | api | demo-servicenow | export-contracts"""
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
    from analystos.contracts import analysis, bi, policy, registry

    out = REPO_ROOT / "contracts"
    out.mkdir(exist_ok=True)
    models = {"agent": registry.AgentSpec, "tool": registry.ToolSpec, "skill": registry.SkillSpec, "policy": policy.WorkspacePolicyDoc,
              "data_scope": policy.DataScope, "policy_decision": policy.PolicyDecision, "analysis_spec": analysis.AnalysisSpec,
              "stat_result": analysis.StatResult, "chart": bi.ChartSpec, "dashboard": bi.DashboardSpec, "metric": bi.MetricDef,
              "dataset": bi.DatasetDef, "publish_bundle": bi.PublishBundle}
    for name, model in models.items():
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
    from analystos.contracts.events import EVENT_TYPES

    (out / "events.json").write_text(json.dumps(sorted(EVENT_TYPES), indent=2) + "\n")
    print(f"wrote {len(models) + 1} contract files to {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analystos")
    parser.add_argument("command", choices=["migrate", "seed", "worker", "api", "export-contracts"])
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate()
    elif args.command == "seed":
        seed()
    elif args.command == "worker":
        from analystos.workflows.worker import run_worker

        run_worker()
    elif args.command == "api":
        import uvicorn

        uvicorn.run("analystos.api.app:app", host="0.0.0.0", port=8000)
    elif args.command == "export-contracts":
        export_contracts()
    return 0


if __name__ == "__main__":
    sys.exit(main())

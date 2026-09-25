"""analystos CLI: migrate | seed | worker | scheduler | api | export-contracts | packs"""
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


def seed() -> None:
    from sqlalchemy import select

    from analystos.capabilities import packs
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
        # Domain knowledge comes from the installed domain packs (packs/<name>/knowledge); an entry
        # already present under the same name is kept, so re-seeding and older seeds never duplicate.
        existing = set(s.scalars(select(ContextEntry.name).where(ContextEntry.workspace_id.is_(None))))
        added: dict[str, int] = {}
        for pack in packs.installed():
            for doc in pack.knowledge:
                if doc.name in existing:
                    continue
                add_entry(s, workspace_id=None, kind=doc.kind, name=doc.name, body=doc.body, synonyms=list(doc.synonyms),
                          mapped_columns=list(doc.maps_to), origin=f"pack:{pack.name}")
                existing.add(doc.name)
                added[pack.name] = added.get(pack.name, 0) + 1
    print("seeded users, registries and domain-pack knowledge "
          f"({', '.join(f'{k}: {v} new' for k, v in sorted(added.items())) or 'already present'})")


def list_packs() -> None:
    from analystos.capabilities import packs

    for p in packs.installed():
        print(f"{p.ref:24} {len(p.templates.get('templates') or []):2} templates  {len(p.knowledge):2} knowledge docs  "
              f"{len(p.kpis):2} KPIs  applies_when={p.applies_when}  {p.summary}")


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analystos")
    parser.add_argument("command", choices=["migrate", "seed", "worker", "scheduler", "api", "export-contracts", "packs"])
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate()
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
    elif args.command == "packs":
        list_packs()
    return 0


if __name__ == "__main__":
    sys.exit(main())

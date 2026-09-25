"""analystos CLI: migrate | provision-analytics-roles | seed | worker | scheduler | api | export-contracts | replay-run | packs | calibrate | knowledge"""
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
    refresh_knowledge_index()


def refresh_knowledge_index() -> None:
    """Index packs whose head revision is not indexed yet (e.g. the platform pack migration 0018
    creates). Best effort, like role provisioning: `analystos knowledge reindex` rebuilds it all."""
    from analystos.core.logging import get_logger
    from analystos.db.base import session_scope
    from analystos.knowledge.index import refresh

    try:
        with session_scope() as s:
            refresh(s)
    except Exception as exc:  # noqa: BLE001
        get_logger(__name__).warning("knowledge index refresh skipped: %s", type(exc).__name__)


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


def seed() -> None:
    from sqlalchemy import select

    from analystos.capabilities import packs
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.knowledge.platform import sync_from_domain_packs
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
        # Domain knowledge comes from the installed domain packs (packs/<name>/knowledge) and lives in
        # the read-only platform knowledge pack (P4-K01); an unchanged sync writes no new revision.
        report = sync_from_domain_packs(s, packs.installed())
    print("seeded users, registries and domain-pack knowledge "
          f"(platform pack revision {report['revision']}, {report['documents']} documents, "
          f"{'updated' if report['changed'] else 'unchanged'})")


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


def calibrate(*, dry_run: bool) -> int:
    """Decision calibration now (the scheduler also runs it nightly): Brier/ECE per purpose x backend,
    downgrading or restoring backends against the admin thresholds."""
    from analystos.db.base import session_scope
    from analystos.decisions.calibration import run_calibration

    with session_scope() as s:
        result = run_calibration(s, actor="cli", dry_run=dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["knowledge"]:
        from analystos.knowledge.cli import main as knowledge_main

        return knowledge_main(argv[1:])
    parser = argparse.ArgumentParser(prog="analystos")
    parser.add_argument("command", choices=["migrate", "provision-analytics-roles", "seed", "worker", "scheduler", "api",
                                            "export-contracts", "replay-run", "packs", "calibrate", "knowledge"],
                        help="knowledge: `analystos knowledge --help` (reindex, reembed, import, export, ...)")
    parser.add_argument("run_id", nargs="?", help="replay-run: the analysis run id")
    parser.add_argument("--check", action="store_true", help="replay-run: re-execute recorded calls offline and compare")
    parser.add_argument("--out", help="replay-run: write the JSON report to this file")
    parser.add_argument("--queues", help="worker: comma-separated workloads to serve (analysis, compute, publish, crawl, elt; "
                                         "default ANALYSTOS_WORKER_QUEUES or all)")
    parser.add_argument("--dry-run", action="store_true", help="calibrate: report without downgrading or restoring")
    args = parser.parse_args(argv)
    if args.command == "calibrate":
        return calibrate(dry_run=args.dry_run)
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

        run_worker(args.queues)
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

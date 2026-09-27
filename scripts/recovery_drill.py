"""Recovery drill (P4-09, OPS-001..005): back up the platform database, destroy a scratch copy, restore it,
and prove the restored copy still resolves what the platform promises.

    python scripts/recovery_drill.py [--source-url URL] [--scratch NAME] [--files DIR] [--report auto|PATH] [--keep]

Steps (each timed; the restore time is the measured recovery time for this database size):

1. inventory the source: every table's row count and a content checksum, and the Alembic revision;
2. back up: `pg_dump --format=custom` (the file's size and sha256 are recorded); with `--files`, a tar of that
   directory (artifact store: recipe snapshots, ML artifacts, reports) and its per-file sha256 manifest;
3. restore the backup into a scratch database (the copy the drill will destroy);
4. destroy: `DROP DATABASE ... WITH (FORCE)`, and check it is gone;
5. recover: create the database again and `pg_restore` the same backup (and untar the files);
6. verify the recovered copy: identical row counts, checksums and Alembic revision per table, then resolve records
   through AnalystOS code on the restored database:
   * runs: every run's workspace exists; every task and event of a run points at an existing run;
   * approvals: every payload still hashes to its bound `payload_hash`; its workspace (and run) exist;
   * evidence: every insight resolves its hypothesis and run; a verification record's `evidence_bundle_id` equals
     the hash of its insight's evidence bundle whenever it did on the source;
   * verification records: every fingerprint recomputes from its dependencies, and every dependency of a live
     record resolves to the same current version it did on the source (`evidence.verification.current_version`);
   * files: every restored file has its manifest sha256.

The drill never writes to the source database. The scratch database is dropped at the end unless `--keep`. The
report (`--report auto`) is docs/60-delivery/evidence/<date>-recovery-drill.md. Exit 1 when a check fails.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


# ----------------------------------------------------------------------------- connection helpers
def _parts(url: str) -> dict[str, str]:
    """libpq pieces of a SQLAlchemy URL; the password goes to PGPASSWORD, never onto a command line."""
    u = urlsplit(url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://"))
    return {"user": unquote(u.username or ""), "password": unquote(u.password or ""), "host": u.hostname or "localhost",
            "port": str(u.port or 5432), "db": u.path.lstrip("/")}


def _with_db(url: str, db: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + quote(db)


def _env(parts: dict[str, str]) -> dict[str, str]:
    return {**os.environ, "PGPASSWORD": parts["password"]}


def _libpq(parts: dict[str, str], db: str | None = None) -> list[str]:
    return ["--host", parts["host"], "--port", parts["port"], "--username", parts["user"], "--dbname", db or parts["db"]]


def _run(cmd: list[str], parts: dict[str, str]) -> None:
    proc = subprocess.run(cmd, env=_env(parts), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.strip()[:800]}")


def _admin(url: str):
    from sqlalchemy import create_engine

    return create_engine(_with_db(url, "postgres"), isolation_level="AUTOCOMMIT")


def _exists(url: str, name: str) -> bool:
    from sqlalchemy import text

    with _admin(url).connect() as c:
        return bool(c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar())


def _drop(url: str, name: str) -> None:
    from sqlalchemy import text

    with _admin(url).connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _create(url: str, name: str) -> None:
    from sqlalchemy import text

    with _admin(url).connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))


# ----------------------------------------------------------------------------- inventory and checks
def inventory(url: str) -> dict[str, Any]:
    """Row count and content checksum of every table in `public`, and the Alembic revision."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    out: dict[str, Any] = {"tables": {}}
    with engine.connect() as c:
        names = [r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))]
        for name in names:
            count, digest = c.execute(text(
                f'SELECT count(*), md5(coalesce(string_agg(md5(t::text), \'\' ORDER BY md5(t::text)), \'\')) FROM "{name}" t')).one()
            out["tables"][name] = {"rows": int(count), "md5": digest}
        out["alembic"] = (c.execute(text("SELECT version_num FROM alembic_version")).scalar()
                          if "alembic_version" in names else None)
    engine.dispose()
    return out


def resolve(url: str) -> dict[str, Any]:
    """Resolve runs, approvals, evidence and verification records through AnalystOS code on one database."""
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from analystos.core.ids import stable_hash
    from analystos.db.models import (
        AnalysisRun,
        Approval,
        Hypothesis,
        Insight,
        RunEvent,
        RunTask,
        VerificationRecord,
        Workspace,
    )
    from analystos.evidence.verification import current_version, fingerprint

    engine = create_engine(url)
    s = sessionmaker(bind=engine)()
    res: dict[str, Any] = {}
    try:
        runs = list(s.scalars(select(AnalysisRun)))
        run_ids = {r.id for r in runs}
        ws_ids = set(s.scalars(select(Workspace.id)))
        orphan_tasks = s.scalar(select(func.count()).select_from(RunTask).where(RunTask.run_id.not_in(run_ids))) if run_ids else 0
        orphan_events = s.scalar(select(func.count()).select_from(RunEvent).where(
            RunEvent.run_id.is_not(None), RunEvent.run_id.not_in(run_ids))) if run_ids else 0
        res["runs"] = {"total": len(runs), "statuses": dict(sorted(_count(r.status for r in runs).items())),
                       "without_workspace": sum(r.workspace_id not in ws_ids for r in runs),
                       "orphan_tasks": int(orphan_tasks or 0), "orphan_events": int(orphan_events or 0)}
        approvals = list(s.scalars(select(Approval)))
        res["approvals"] = {"total": len(approvals), "statuses": dict(sorted(_count(a.status for a in approvals).items())),
                            "payload_hash_ok": sum(stable_hash(a.payload) == a.payload_hash for a in approvals),
                            "without_workspace": sum(a.workspace_id not in ws_ids for a in approvals),
                            "run_missing": sum(bool(a.run_id) and a.run_id not in run_ids for a in approvals)}
        insights = {i.id: i for i in s.scalars(select(Insight))}
        hyps = set(s.scalars(select(Hypothesis.id)))
        records = list(s.scalars(select(VerificationRecord)))
        bundle_ok = bundle_total = 0
        for r in records:
            if r.subject_type == "insight" and r.evidence_bundle_id and r.subject_id in insights:
                bundle_total += 1
                bundle_ok += f"sha256:{stable_hash(dict(insights[r.subject_id].evidence_bundle or {}))}" == r.evidence_bundle_id
        res["evidence"] = {"insights": len(insights),
                           "insight_hypothesis_missing": sum(bool(i.hypothesis_id) and i.hypothesis_id not in hyps
                                                             for i in insights.values()),
                           "insight_run_missing": sum(i.run_id not in run_ids for i in insights.values()),
                           "bundles_bound": bundle_total, "bundles_hash_ok": bundle_ok}
        live = [r for r in records if r.state in ("ACTIVE", "PENDING")]
        dep_versions: dict[str, str | None] = {}
        for r in live:
            for d in r.dependencies or []:
                key = f"{r.id}|{d['kind']}|{d['ref']}"
                try:
                    dep_versions[key] = current_version(s, d["kind"], d["ref"])
                except Exception as exc:  # noqa: BLE001 - an unresolvable dependency is a finding of the drill
                    dep_versions[key] = f"error:{type(exc).__name__}"
        res["verification"] = {"records": len(records), "states": dict(sorted(_count(r.state for r in records).items())),
                               "fingerprint_ok": sum(fingerprint(r.dependencies or []) == r.fingerprint for r in records),
                               "live": len(live), "live_dependencies": len(dep_versions),
                               "live_dependencies_current": sum(
                                   v == d["version_hash"] for r in live for d in r.dependencies or []
                                   for v in [dep_versions.get(f"{r.id}|{d['kind']}|{d['ref']}")])}
        res["_dependency_versions"] = dep_versions
    finally:
        s.close()
        engine.dispose()
    return res


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): _sha256(p) for p in sorted(folder.rglob("*")) if p.is_file()}


# ----------------------------------------------------------------------------- the drill
def drill(source_url: str, scratch: str, *, files: Path | None, keep: bool, work: Path) -> dict[str, Any]:
    src, steps, checks = _parts(source_url), [], []
    scratch_url = _with_db(source_url, scratch)
    if scratch == src["db"]:
        raise SystemExit("the scratch database must not be the source database")

    def step(name: str, fn, *args):
        started = time.perf_counter()
        value = fn(*args)
        steps.append({"step": name, "seconds": round(time.perf_counter() - started, 3)})
        return value

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    before = step("inventory source", inventory, source_url)
    resolved_source = step("resolve records on source", resolve, source_url)
    dump = work / f"{src['db']}.dump"
    step("pg_dump (custom format)", _run, ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(dump),
                                           *_libpq(src)], src)
    backup = {"file": dump.name, "bytes": dump.stat().st_size, "sha256": _sha256(dump)}
    file_manifest = archive = None
    if files is not None:
        file_manifest = _manifest(files)
        archive = work / "files.tar.gz"

        def pack() -> None:
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(files, arcname="files")
        step("tar artifact files", pack)
        backup["files"] = {"archive": archive.name, "bytes": archive.stat().st_size, "count": len(file_manifest)}

    _drop(source_url, scratch)
    step("create scratch database", _create, source_url, scratch)
    step("pg_restore into scratch (copy 1)", _run, ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                                                    *_libpq(src, scratch), str(dump)], src)
    copy1 = inventory(scratch_url)
    check("copy 1 equals the source", copy1["tables"] == before["tables"], f"{len(copy1['tables'])} tables")
    step("destroy scratch database", _drop, source_url, scratch)
    check("scratch database destroyed", not _exists(source_url, scratch), f"{scratch} no longer exists")
    started = time.perf_counter()
    _create(source_url, scratch)
    _run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", *_libpq(src, scratch), str(dump)], src)
    restored_files = None
    if files is not None and archive is not None:
        target = work / "restored"
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(target, filter="data")
        restored_files = _manifest(target / "files")
    steps.append({"step": "recover: create + pg_restore (+ untar)", "seconds": round(time.perf_counter() - started, 3)})

    after = step("inventory restored copy", inventory, scratch_url)
    resolved = step("resolve records on restored copy", resolve, scratch_url)
    diff = sorted(t for t in set(before["tables"]) | set(after["tables"]) if before["tables"].get(t) != after["tables"].get(t))
    check("every table: same rows and checksum", not diff, "identical" if not diff else f"differs: {', '.join(diff[:10])}")
    check("alembic revision", before["alembic"] == after["alembic"],
          after["alembic"] or "none on the source either (the schema was not created by migrations)")
    r, a, e, v = resolved["runs"], resolved["approvals"], resolved["evidence"], resolved["verification"]
    check("runs resolve", r["total"] == resolved_source["runs"]["total"] and not (r["without_workspace"] or r["orphan_tasks"]
                                                                                   or r["orphan_events"]),
          f"{r['total']} runs {r['statuses']}; orphans: tasks {r['orphan_tasks']}, events {r['orphan_events']}")
    check("approvals keep their hash binding", a["payload_hash_ok"] == a["total"] == resolved_source["approvals"]["total"]
          and not (a["without_workspace"] or a["run_missing"]),
          f"{a['payload_hash_ok']}/{a['total']} payloads hash to their approval {a['statuses']}")
    check("evidence resolves", e["insights"] == resolved_source["evidence"]["insights"] and not e["insight_run_missing"]
          and not e["insight_hypothesis_missing"] and e["bundles_hash_ok"] == resolved_source["evidence"]["bundles_hash_ok"],
          f"{e['insights']} insights; {e['bundles_hash_ok']}/{e['bundles_bound']} bound evidence bundles hash to their record "
          f"(source: {resolved_source['evidence']['bundles_hash_ok']}/{resolved_source['evidence']['bundles_bound']})")
    same_versions = resolved["_dependency_versions"] == resolved_source["_dependency_versions"]
    check("verification records resolve", v["fingerprint_ok"] == v["records"] == resolved_source["verification"]["records"]
          and same_versions,
          f"{v['fingerprint_ok']}/{v['records']} fingerprints recompute; {v['live_dependencies']} dependencies of "
          f"{v['live']} live records resolve to the same current version as on the source"
          f" ({v['live_dependencies_current']} still equal the recorded version)")
    if file_manifest is not None:
        check("files restore byte-for-byte", restored_files == file_manifest, f"{len(file_manifest)} files")
    if not keep:
        _drop(source_url, scratch)
    for x in (resolved, resolved_source):
        x.pop("_dependency_versions", None)
    return {"source": {"host": src["host"], "port": src["port"], "db": src["db"]}, "scratch": scratch, "backup": backup,
            "tables": len(before["tables"]), "rows": sum(t["rows"] for t in before["tables"].values()),
            "alembic": before["alembic"], "steps": steps, "checks": checks, "resolved_source": resolved_source,
            "resolved_restored": resolved, "kept": keep, "passed": all(c["ok"] for c in checks)}


def render(result: dict[str, Any], *, commit: str) -> str:
    now = datetime.now(UTC)
    b = result["backup"]
    alembic = f"`{result['alembic']}`" if result["alembic"] else "none recorded (the schema was not created by migrations)"
    lines = [
        f"# Recovery drill (P4-09) — {now:%Y-%m-%d}", "",
        f"Generated {now:%Y-%m-%d %H:%M} UTC by `scripts/recovery_drill.py` at `{commit}`. Source database "
        f"`{result['source']['db']}` on {result['source']['host']}:{result['source']['port']} ({result['tables']} tables, "
        f"{result['rows']} rows, Alembic revision {alembic}); scratch database `{result['scratch']}` "
        f"({'kept' if result['kept'] else 'dropped after the drill'}).", "",
        f"**Result: {'PASSED' if result['passed'] else 'FAILED'}** — backup `{b['file']}` {b['bytes']} bytes, sha256 "
        f"`{b['sha256'][:16]}…`" + (f"; files archive {b['files']['count']} files, {b['files']['bytes']} bytes" if b.get("files") else ""),
        "", "## Steps", "", "| Step | Seconds |", "|---|---|",
        *[f"| {s['step']} | {s['seconds']} |" for s in result["steps"]],
        "", "## Checks on the recovered copy", "", "| Check | Result | Detail |", "|---|---|---|",
        *[f"| {c['check']} | {'pass' if c['ok'] else '**FAIL**'} | {c['detail']} |" for c in result["checks"]],
        "", "## What resolved", "",
        f"* Runs: {result['resolved_restored']['runs']}",
        f"* Approvals: {result['resolved_restored']['approvals']}",
        f"* Evidence: {result['resolved_restored']['evidence']}",
        f"* Verification: {result['resolved_restored']['verification']}",
        "", "## Scope and limits", "",
        "* The drill proves a logical backup (`pg_dump`) of the control-plane database restores to an equivalent database "
        "and that AnalystOS resolves its records there. It is not a point-in-time recovery (WAL archiving) or a "
        "failover test; the recovery point is the time of the last backup.",
        "* The analytics plane (staged snapshots, managed outputs) is a second database: back it up the same way "
        "(`--source-url` pointing at it); staged snapshots can also be re-staged from their sources, managed outputs cannot.",
        "* The recovery time above is for this database's size on this host; it scales with the dump size.",
        "* Runbook: `docs/30-runbooks/06-backup-and-recovery.md`.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import json

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-url", default=None, help="SQLAlchemy URL of the platform database (default: settings)")
    ap.add_argument("--scratch", default=None, help="scratch database name (default: <source>_drill)")
    ap.add_argument("--files", default=None, help="a directory to back up and restore with the database (artifact store)")
    ap.add_argument("--report", default=None, help="markdown path, or `auto` for docs/60-delivery/evidence/")
    ap.add_argument("--keep", action="store_true", help="keep the restored scratch database")
    args = ap.parse_args(argv)
    if args.source_url is None:
        from analystos.core.config import get_settings

        args.source_url = get_settings().database_url
    scratch = args.scratch or f"{_parts(args.source_url)['db']}_drill"
    with tempfile.TemporaryDirectory(prefix="aos-drill-") as tmp:
        result = drill(args.source_url, scratch, files=Path(args.files) if args.files else None, keep=args.keep, work=Path(tmp))
    for s in result["steps"]:
        print(f"  {s['step']:<45} {s['seconds']:>8.3f} s")
    for c in result["checks"]:
        print(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")
    if args.report:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        path = (ROOT / "docs" / "60-delivery" / "evidence" / f"{datetime.now(UTC):%Y-%m-%d}-recovery-drill.md"
                if args.report == "auto" else Path(args.report))
        path.write_text(render(result, commit=commit or "unknown"))
        path.with_suffix(".json").write_text(json.dumps(result, indent=1, default=str) + "\n")
        print(f"report: {path}")
    print("recovery drill " + ("PASSED" if result["passed"] else "FAILED"))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

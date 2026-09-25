"""BuildGateway: the only write path to the analytics plane (P4-E06, spec v3 §7.4, ADR-0014).

The QueryGateway stays read-only. The BuildGateway:

* accepts only an approved build job: the approval must be for `elt_build`, name this job, and be
  re-verified by `verify_for_execution` immediately before the runner starts (status, expiry,
  payload hash = project hash + engine + target schema + relations + rollback, plan hash, policy
  version, requester and approver rights). A refusal invalidates the approval and is audited;
* re-checks the target: a schema this workspace designated for this engine, never a source's
  staged schema or a system schema;
* re-checks the project statically (files and the manifest dbt parses from them) and re-hashes the
  files it wrote to disk against the approved hash;
* runs dbt as the build identity (`SET ROLE` to the workspace build role, CREATE on the target
  schemas only), so Postgres refuses any write elsewhere even if every check above were wrong;
* audits the job and every dbt node it ran, records the rollback plan, and harvests the manifest,
  run results and OpenLineage events into lineage.

The approval is single use: it is consumed (`executed`) in the same transaction that marks the job
running, before the runner starts, so it can never authorise a second build.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url

from analystos.build import lineage
from analystos.build.project import check_files, check_manifest, project_hash
from analystos.build.runner import Connection, DbtCoreRunner, read_project, write_profile, write_project
from analystos.build.targets import ENGINES, build_role_for, check_identities, check_schema_name
from analystos.connectors.naming import staging_schema_for
from analystos.core.errors import AnalystOSError, ApprovalRequired, Conflict, Forbidden, NotFound, PolicyDenied
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, BuildJob, BuildTarget, Source
from analystos.events.bus import emit
from analystos.governance.approvals import verify_for_execution
from analystos.governance.audit import audit

BUILD_ACTION = "elt_build"
_log = get_logger(__name__)


def approval_payload(job: BuildJob) -> dict[str, Any]:
    """What an approval of this job binds. Recomputed from the job at execution time, so a changed
    project file, engine, target schema or relation list no longer matches the approved hash."""
    return {"action": BUILD_ACTION, "job_id": job.id, "project_name": job.project_name,
            "project_hash": project_hash(dict(job.project_files or {})), "engine": job.engine,
            "target_schema": job.target_schema, "relations": sorted(job.relations or []), "runner": job.runner,
            "rollback": list((job.rollback or {}).get("statements") or [])}


def source_schemas(session: Any, workspace_id: str | None = None) -> set[str]:
    """Every staged source schema (all workspaces: a target may never shadow any source)."""
    return {s.staging_schema or staging_schema_for(s.id) for s in session.scalars(select(Source))}


def require_target(session: Any, workspace_id: str, engine: str, schema: str) -> BuildTarget:
    if engine not in ENGINES:
        raise Forbidden(f"engine {engine} has no build path (supported: {', '.join(ENGINES)})")
    check_schema_name(schema, source_schemas=source_schemas(session))
    target = session.scalar(select(BuildTarget).where(BuildTarget.workspace_id == workspace_id, BuildTarget.engine == engine,
                                                      BuildTarget.schema_name == schema))
    if target is None or target.status != "active":
        raise Forbidden(f"schema {schema} is not a designated build target of this workspace on {engine}")
    return target


class BuildGateway:
    def __init__(self, settings: Any, runner: DbtCoreRunner | None = None) -> None:
        check_identities(settings)
        self.settings = settings
        self.runner = runner or DbtCoreRunner(settings.dbt_executable, timeout_seconds=settings.build_timeout_seconds)

    def job_dir(self, job_id: str) -> Path:
        return Path(self.settings.build_dir) / job_id

    # ------------------------------------------------------------------------------ execute
    def execute(self, job_id: str, approval_id: str | None, *, actor: str) -> dict[str, Any]:
        claimed = self._claim(job_id, approval_id, actor=actor)
        if claimed.get("reused"):
            return claimed
        job_dir = self.job_dir(job_id)
        try:
            files = claimed["files"]
            write_project(job_dir / "project", files)
            if project_hash(read_project(job_dir / "project", list(files))) != claimed["project_hash"]:
                raise PolicyDenied("the project on disk does not match the approved project hash")
            conn = Connection.from_url(self.settings.analytics_builder_url, role=claimed["build_role"],
                                       schema=claimed["target_schema"])
            write_profile(job_dir / "profile", conn, schema=claimed["target_schema"])
            parsed = self.runner.parse(job_dir)
            if not parsed.ok:
                raise PolicyDenied("dbt could not parse the approved project: " + parsed.log_tail[-400:])
            check_manifest(parsed.manifest, project=claimed["project_name"], target_schema=claimed["target_schema"],
                           allowed_sources=set(claimed["allowed_sources"]))
            result = self.runner.build(job_dir, conn.password)
        except AnalystOSError as exc:
            self._finish(job_id, actor=actor, status="failed", error=f"{exc.code}: {exc.message}", started=claimed["started_at"])
            raise
        except Exception as exc:  # noqa: BLE001 - the runner or the filesystem; recorded, then surfaced
            self._finish(job_id, actor=actor, status="failed", error=f"{type(exc).__name__}: {exc}"[:1000],
                         started=claimed["started_at"])
            raise
        return self._finish(job_id, actor=actor, status="succeeded" if result.ok else "failed", result=result,
                            started=claimed["started_at"],
                            error=None if result.ok else f"dbt build exited {result.returncode}")

    def _claim(self, job_id: str, approval_id: str | None, *, actor: str) -> dict[str, Any]:
        refusal: AnalystOSError | None = None
        with session_scope() as s:
            job = s.get(BuildJob, job_id, with_for_update=True)
            if job is None:
                raise NotFound(f"build job {job_id} not found")
            if job.status == "succeeded":
                return {"reused": True, "status": "succeeded", "job_id": job.id, "relations": list(job.relations)}
            if job.status == "running":
                raise Conflict(f"build job {job_id} is already running")
            if job.status not in ("awaiting_approval", "planned"):
                raise Conflict(f"build job {job_id} is {job.status}; plan a new build")
            try:
                if not approval_id:
                    raise ApprovalRequired("a build runs only under an approved, hash-bound approval")
                approval = s.get(Approval, approval_id, with_for_update=True)
                if approval is None or approval.action != BUILD_ACTION or approval.workspace_id != job.workspace_id or \
                        (approval.payload or {}).get("job_id") != job.id:
                    raise ApprovalRequired("the approval does not cover this build job")
                run = s.get(AnalysisRun, job.run_id)
                # Immediately before the side effect: status, expiry, payload (project hash, engine, target
                # schema, relations, rollback), plan hash, policy version, requester and approver rights.
                verify_for_execution(s, approval_id, payload=approval_payload(job), plan_hash=run.plan_hash if run else None)
                target = require_target(s, job.workspace_id, job.engine, job.target_schema)
                allowed = set(job.dry_run.get("allowed_sources") or [])
                check_files(dict(job.project_files), allowed_sources=allowed)
                bad = [r for r in job.relations if r.split(".", 1)[0] != job.target_schema]
                if bad:
                    raise PolicyDenied(f"relations {bad} are outside the approved target schema")
            except (ApprovalRequired, PolicyDenied, Forbidden) as exc:
                refusal = exc
                if approval_id and (a := s.get(Approval, approval_id)) is not None and a.status == "approved":
                    a.status, a.reason = "invalidated", f"build refused: {exc.message}"[:500]
                job.status, job.error, job.finished_at = "refused", f"{exc.code}: {exc.message}"[:1000], utcnow()
                audit(actor, "build.refused", workspace_id=job.workspace_id, run_id=job.run_id, target=job.id, decision="deny",
                      reasons=[exc.message[:300]], details={"approval_id": approval_id, "target_schema": job.target_schema,
                                                            "engine": job.engine}, session=s)
                emit(job.workspace_id, "build.refused", {"job_id": job.id, "reason": exc.message[:300]}, run_id=job.run_id,
                     actor=actor, session=s)
            if refusal is None:
                approval.status = "executed"  # single use: consumed before the side effect
                job.status, job.approval_id, job.started_at, job.error = "running", approval_id, utcnow(), None
                audit(actor, "build.started", workspace_id=job.workspace_id, run_id=job.run_id, target=job.id, decision="allow",
                      details={"approval_id": approval_id, "project_hash": job.project_hash, "engine": job.engine,
                               "target_schema": job.target_schema, "build_role": target.build_role,
                               "builder": make_url(self.settings.analytics_builder_url).username,
                               "rollback": job.rollback}, session=s)
                emit(job.workspace_id, "build.started", {"job_id": job.id, "target_schema": job.target_schema,
                                                         "relations": job.relations}, run_id=job.run_id, actor=actor, session=s)
                claimed = {"files": dict(job.project_files), "project_hash": job.project_hash, "project_name": job.project_name,
                           "target_schema": job.target_schema, "build_role": build_role_for(self.settings, job.workspace_id),
                           "allowed_sources": sorted(allowed), "started_at": job.started_at}
        if refusal is not None:
            raise refusal
        return claimed

    def _finish(self, job_id: str, *, actor: str, status: str, started: Any, result: Any = None,
                error: str | None = None) -> dict[str, Any]:
        finished = utcnow()
        with session_scope() as s:
            job = s.get(BuildJob, job_id, with_for_update=True)
            job.status, job.finished_at, job.error = status, finished, error
            built: list[str] = []
            if result is not None:
                job.log_tail = result.log_tail
                job.manifest = lineage.manifest_summary(result.manifest)
                if result.osi_document:
                    job.manifest = {**job.manifest, "osi_document": result.osi_document}
                job.run_results = lineage.run_results_summary(result.run_results)
                builder = make_url(self.settings.analytics_builder_url)
                job.openlineage = lineage.openlineage_events(
                    job_id=job.id, workspace_id=job.workspace_id, manifest=result.manifest, run_results=result.run_results,
                    namespace=f"postgres://{builder.host}:{builder.port or 5432}", database=builder.database or "",
                    started_at=started or finished, finished_at=finished)
                built = lineage.record_lineage(s, workspace_id=job.workspace_id, run_id=job.run_id, job_id=job.id,
                                               approval_id=job.approval_id, manifest=result.manifest, run_results=result.run_results)
                if job.artifact_id:
                    lineage.link(s, job.workspace_id, ("transformation", job.artifact_id), "executed_as", ("build_job", job.id),
                                 run_id=job.run_id)
                for r in job.run_results.get("results") or []:  # per-node audit: what ran, as whom, with what outcome
                    audit(actor, "build.node", workspace_id=job.workspace_id, run_id=job.run_id, target=r["unique_id"],
                          decision="allow" if r["status"] in ("success", "pass") else "error",
                          details={"job_id": job.id, "status": r["status"], "rows_affected": r["rows_affected"],
                                   "execution_time": r["execution_time"]}, session=s)
            audit(actor, f"build.{'completed' if status == 'succeeded' else 'failed'}", workspace_id=job.workspace_id,
                  run_id=job.run_id, target=job.id, decision="allow" if status == "succeeded" else "error",
                  reasons=[error] if error else [], details={"relations": built, "counts": job.run_results.get("counts")},
                  session=s)
            emit(job.workspace_id, "build.completed" if status == "succeeded" else "build.failed",
                 {"job_id": job.id, "status": status, "relations": built, "error": (error or "")[:300]},
                 run_id=job.run_id, actor=actor, session=s)
            out = {"job_id": job.id, "status": status, "relations": built, "counts": job.run_results.get("counts") or {},
                   "error": error, "rollback": job.rollback}
        if status != "succeeded" and result is not None:
            raise AnalystOSError(f"dbt build failed: {error}; see build job {job_id}")
        return out

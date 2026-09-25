import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type BuildDiff, type BuildFileDiff, type BuildJob, type BuildJobDetail, type BuildTest } from "../api";
import { buildInFlight, buildSteps, diffLines, fmtBytes } from "../lib/build";
import { fmtDate, fmtNumber, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { Card, CodeBlock, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, StatusBadge, Tag, TechnicalDetails, Value } from "./ui";

const POLL_MS = 4000;

/**
 * Build → dbt builds (P4-U05): plan an `elt_build` from a completed run, then review what the
 * approver signs — the generated project against the previous job, file by file, the dry run and the
 * estimate — and follow the job to its result. Approving happens in the approvals inbox, never here.
 */
export function BuildPanel({ wsId, selected, onSelect, canDesignate }: {
  wsId: string; selected: string | null; onSelect: (id: string | null) => void; canDesignate: boolean;
}) {
  const jobs = useAsync(() => api.listBuilds(wsId), [wsId]);
  const targets = useAsync(() => api.buildTargets(wsId), [wsId]);
  const [started, setStarted] = useState<{ run_id: string } | null>(null);
  const waitingForPlan = !!started && !(jobs.data ?? []).some((j) => j.run_id === started.run_id);
  const moving = waitingForPlan || (jobs.data ?? []).some((j) => buildInFlight(j, j.approval_status));
  const { reload } = jobs;

  useEffect(() => {
    if (!moving) return;
    const t = setInterval(() => void reload(), POLL_MS);
    return () => clearInterval(t);
  }, [moving, reload]);

  useEffect(() => {
    if (!started) return;
    const job = (jobs.data ?? []).find((j) => j.run_id === started.run_id);
    if (job) {
      onSelect(job.id);
      setStarted(null);
    }
  }, [jobs.data, started, onSelect]);

  return (
    <div className="stack">
      <Card title="Plan a build">
        <p className="muted small">
          Turns a completed run's dataset and approved KPIs into a dbt project, dry-runs it (dbt parse, no connection; tests and row count
          read through the query gateway) and requests an approval bound to the project hash, engine, target schema and plan hash.
          Nothing is written until an approver decides in the inbox.
        </p>
        <ErrorBox error={targets.error} onRetry={targets.reload} />
        {targets.data && (
          <>
            {targets.data.length === 0 && (
              <Notice tone="warning">No target schema is designated for this workspace. A workspace owner designates one; builds write nowhere else.</Notice>
            )}
            {canDesignate && <DesignateTarget wsId={wsId} onDone={() => void targets.reload()} />}
            {targets.data.length > 0 && (
              <PlanBuildForm wsId={wsId} targets={targets.data.filter((t) => t.status === "active").map((t) => t.schema_name)}
                onStarted={(r) => { setStarted(r); void jobs.reload(); }} />
            )}
          </>
        )}
        {started && waitingForPlan && (
          <Notice>Build run <Link to={to.run(wsId, started.run_id)}><code>{started.run_id}</code></Link> started: generating and dry-running the project…</Notice>
        )}
      </Card>

      <ErrorBox error={jobs.error} onRetry={jobs.reload} />
      {jobs.loading && !jobs.data && <Loading />}
      {jobs.data?.length === 0 && <EmptyState title="No builds yet">Plan one above from a completed investigation.</EmptyState>}
      {!!jobs.data?.length && (
        <div className="split">
          <div className="split-list">
            <ul className="list selectable" aria-label="Build jobs">
              {jobs.data.map((j) => <li key={j.id}><JobButton job={j} active={j.id === selected} onClick={() => onSelect(j.id)} /></li>)}
            </ul>
          </div>
          <div className="split-detail">
            {selected ? <BuildJobView id={selected} wsId={wsId} onChanged={() => void jobs.reload()} />
              : <EmptyState title="Select a build">Each job shows its file diff, dry run, estimate, approval and result.</EmptyState>}
          </div>
        </div>
      )}
    </div>
  );
}

function JobButton({ job, active, onClick }: { job: BuildJob; active: boolean; onClick: () => void }) {
  return (
    <button type="button" className={`list-button ${active ? "active" : ""}`} onClick={onClick} aria-current={active ? "true" : undefined}>
      <span className="list-button-head"><span className="clamp-1">{job.project_name}</span><StatusBadge status={job.status} /></span>
      <span className="chip-row small">
        <span className="tag">{job.target_schema}</span>
        {job.approval_status && <span className="muted">approval: {job.approval_status}</span>}
        <span className="muted">{fmtDate(job.created_at)}</span>
      </span>
    </button>
  );
}

function DesignateTarget({ wsId, onDone }: { wsId: string; onDone: () => void }) {
  const id = useId();
  const [schema, setSchema] = useState("");
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (await act.run(() => api.designateBuildTarget(wsId, schema.trim()))) {
      setSchema("");
      onDone();
    }
  };
  return (
    <details className="designate">
      <summary className="small">Designate a target schema (workspace owner)</summary>
      <form className="form" onSubmit={submit} aria-label="Designate a target schema">
        <div className="form-row">
          <Field label="Schema" htmlFor={`${id}-schema`} hint="Provisions a build role with CREATE on this schema only. A source schema is refused.">
            <input id={`${id}-schema`} value={schema} onChange={(e) => setSchema(e.target.value)} required pattern="[a-z_][a-z0-9_]*" />
          </Field>
        </div>
        <ErrorBox error={act.error} />
        <div className="form-actions"><button type="submit" className="btn btn-sm" disabled={act.busy || !schema.trim()}>Designate</button></div>
      </form>
    </details>
  );
}

function PlanBuildForm({ wsId, targets, onStarted }: { wsId: string; targets: string[]; onStarted: (r: { run_id: string }) => void }) {
  const id = useId();
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  const sources = useMemo(() => (runs.data ?? []).filter((r) => r.status === "COMPLETED" && r.origin?.type !== "build"), [runs.data]);
  const [runId, setRunId] = useState("");
  const [target, setTarget] = useState(targets[0] ?? "");
  const act = useAction();
  const chosenRun = runId || sources[0]?.id || "";
  const chosenTarget = target || targets[0] || "";
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.startBuild(wsId, chosenRun, chosenTarget));
    if (r) onStarted(r);
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Plan a build">
      <div className="form-row">
        <Field label="From run" htmlFor={`${id}-run`} hint={runs.data && !sources.length ? "No completed investigation yet." : "Its dataset and KPIs are built."}>
          <select id={`${id}-run`} value={chosenRun} onChange={(e) => setRunId(e.target.value)}>
            {sources.map((r) => <option key={r.id} value={r.id}>{fmtDate(r.finished_at ?? r.created_at)} — {r.objective.slice(0, 70)}</option>)}
          </select>
        </Field>
        <Field label="Target schema" htmlFor={`${id}-target`}>
          <select id={`${id}-target`} value={chosenTarget} onChange={(e) => setTarget(e.target.value)}>
            {targets.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </Field>
      </div>
      <ErrorBox error={runs.error} />
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={act.busy || !chosenRun || !chosenTarget}>{act.busy ? "Starting…" : "Plan build"}</button>
      </div>
    </form>
  );
}

function BuildJobView({ id, wsId, onChanged }: { id: string; wsId: string; onChanged: () => void }) {
  const d = useAsync(() => api.getBuild(id), [id]);
  const job = d.data;
  const moving = !!job && buildInFlight(job, job.approval?.status);
  const { reload } = d;
  useEffect(() => {
    if (!moving) return;
    const t = setInterval(() => { void reload(); onChanged(); }, POLL_MS);
    return () => clearInterval(t);
  }, [moving, reload, onChanged]);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!job) return <Loading />;
  return (
    <div className="stack">
      <Card title={<>{job.project_name} <span className="muted small">· {job.engine}/{job.target_schema}</span></>} actions={<StatusBadge status={job.status} />}>
        <BuildStatusSteps job={job} />
        <KeyValue items={[
          ["Source run", <Link key="s" to={to.run(wsId, job.source_run_id)}><code>{job.source_run_id}</code></Link>],
          ["Build run", <Link key="b" to={to.run(wsId, job.run_id)}><code>{job.run_id}</code></Link>],
          ["Project hash", <code key="h" title={job.project_hash}>{shortHash(job.project_hash, 16)}</code>],
          ["Plan hash", <code key="p" title={job.plan_hash ?? ""}>{shortHash(job.plan_hash, 16)}</code>],
          ["Runner", job.runner],
          ["Planned", fmtDate(job.created_at)],
          ...(job.finished_at ? [["Finished", fmtDate(job.finished_at)] as [string, string]] : []),
        ]} />
      </Card>
      <BuildApprovalCard job={job} wsId={wsId} />
      <DryRunCard job={job} />
      <BuildDiffCard jobId={job.id} />
      <Card title="Relations and rollback">
        <p className="small">Creates or replaces: {job.relations.length ? job.relations.map((r) => <code key={r} className="rel">{r}</code>) : <span className="muted">none</span>}</p>
        <p className="small"><strong>Rollback:</strong> {job.rollback.strategy ?? "not recorded"}. {job.rollback.restore}</p>
        {!!job.rollback.statements?.length && <CodeBlock code={job.rollback.statements.join(";\n") + ";"} label="Rollback statements (in the approved payload)" />}
      </Card>
      {(job.status === "succeeded" || job.status === "failed" || job.status === "refused") && <BuildResultCard job={job} />}
    </div>
  );
}

export function BuildStatusSteps({ job }: { job: Pick<BuildJobDetail, "status" | "error" | "approval"> }) {
  const steps = buildSteps(job, job.approval?.status);
  return (
    <ol className="steps" aria-label="Build status">
      {steps.map((s) => (
        <li key={s.key} className={`step step-${s.state}`} aria-current={s.state === "current" ? "step" : undefined}>
          <span className="step-mark" aria-hidden="true">{s.state === "done" ? "✓" : s.state === "failed" ? "✗" : s.state === "current" ? "●" : "○"}</span>
          <span className="step-text"><strong>{s.label}</strong> <span className="muted small">{s.detail}</span>
            <span className="sr-only"> ({s.state})</span></span>
        </li>
      ))}
    </ol>
  );
}

function BuildApprovalCard({ job, wsId }: { job: BuildJobDetail; wsId: string }) {
  const a = job.approval;
  return (
    <Card title="Approval" actions={a ? <StatusBadge status={a.status} /> : undefined}>
      {!a ? <p className="muted small">No approval requested yet: the plan step requests it after the dry run.</p> : (
        <>
          <KeyValue items={[
            ["Payload hash", <code key="h" title={a.payload_hash}>{shortHash(a.payload_hash, 16)}</code>],
            ["Risk", a.risk_tier],
            ["Policy version", `v${a.policy_version}`],
            ["Expires", fmtDate(a.expires_at)],
            ...(a.decided_at ? [["Decided", `${fmtDate(a.decided_at)}${a.reason ? ` — ${a.reason}` : ""}`] as [string, string]] : []),
          ]} />
          <p className="small muted">
            The approval binds the project hash, engine, target schema, relations, runner, rollback statements and the run's plan hash. A changed
            file after approval invalidates it; the build gateway re-verifies it immediately before running dbt.
          </p>
          {a.status === "pending" && (
            <p><Link className="btn btn-primary btn-sm" to={to.approvals(wsId)}>Review in the approvals inbox</Link></p>
          )}
        </>
      )}
    </Card>
  );
}

function testKey(t: BuildTest): string {
  return `${t.test}(${t.column})`;
}

function DryRunCard({ job }: { job: BuildJobDetail }) {
  const dr = job.dry_run ?? {};
  const est = job.estimate ?? {};
  const passing = new Set((dr.tests?.passing ?? []).map(testKey));
  const size = fmtBytes(est.approx_bytes ?? null);
  return (
    <Card title="Dry run and estimate" actions={<StatusBadge status={dr.ok ? "ok" : "failed"} label={dr.ok ? "parsed cleanly" : "not parsed"} />}>
      <div className="stats-row" aria-label="Estimate">
        <div className="stat"><div className="stat-value"><Value value={est.rows} /></div><div className="stat-label">rows (COUNT through the gateway)</div></div>
        <div className="stat"><div className="stat-value"><Value value={est.columns} /></div><div className="stat-label">columns</div></div>
        <div className="stat"><div className="stat-value"><Value value={est.models} /></div><div className="stat-label">models</div></div>
        <div className="stat"><div className="stat-value"><Value value={est.tests} /></div><div className="stat-label">tests</div></div>
        <div className="stat"><div className="stat-value">{size ?? <Value value={null} />}</div><div className="stat-label">approx. size (rough)</div></div>
      </div>
      {est.method && <p className="muted small">Estimate: {est.method}.</p>}
      <KeyValue items={[
        ["Runner", dr.runner ?? "—"], ["dbt", dr.dbt_version ?? "—"], ["Ossie", dr.ossie_version ?? "—"],
        ["Sources allowed", (dr.allowed_sources ?? []).join(", ") || "—"],
      ]} />
      {!!dr.tests?.candidates?.length && (
        <div className="table-wrap">
          <table className="table table-compact">
            <caption className="sr-only">Candidate dbt tests</caption>
            <thead><tr><th scope="col">Test</th><th scope="col">Column</th><th scope="col">On current data</th></tr></thead>
            <tbody>
              {dr.tests.candidates.map((t) => (
                <tr key={testKey(t)}>
                  <td>{t.test}</td><td><code>{t.column}</code></td>
                  <td>{passing.has(testKey(t)) ? <Tag tone="success">holds: kept</Tag> : <Tag tone="warning">fails: dropped</Tag>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="small"><strong>KPIs built:</strong> {(dr.metrics ?? []).map((m) => <code key={m.metric} className="rel">{m.metric}</code>)}
        {!(dr.metrics ?? []).length && <span className="muted">none</span>}</p>
      {!!dr.skipped_metrics?.length && (
        <details>
          <summary className="small">{dr.skipped_metrics.length} KPI{dr.skipped_metrics.length > 1 ? "s" : ""} not built</summary>
          <ul className="small">{dr.skipped_metrics.map((m) => <li key={m.metric}><code>{m.metric}</code>: {m.reason}</li>)}</ul>
        </details>
      )}
      {!!dr.notes?.length && <ul className="small muted">{dr.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
    </Card>
  );
}

function BuildDiffCard({ jobId }: { jobId: string }) {
  const d = useAsync(() => api.buildDiff(jobId), [jobId]);
  return (
    <Card title="Project diff">
      <ErrorBox error={d.error} onRetry={d.reload} />
      {!d.data ? (!d.error && <Loading label="Comparing files…" />) : <DiffView diff={d.data} />}
    </Card>
  );
}

export function DiffView({ diff }: { diff: BuildDiff }) {
  const changed = diff.files.filter((f) => f.status !== "unchanged");
  const unchanged = diff.files.filter((f) => f.status === "unchanged");
  const s = diff.summary;
  return (
    <div className="build-diff">
      <p className="small">
        {diff.against ? (
          <>Compared with job <code>{diff.against.job_id}</code> ({diff.against.status}, {fmtDate(diff.against.created_at)}), the previous build of this target:{" "}
            {diff.identical ? <strong>identical project</strong> : <strong>{s.added} added, {s.modified} modified, {s.removed} removed</strong>}
            {" "}<span className="muted">(+{fmtNumber(s.lines_added)} / −{fmtNumber(s.lines_removed)} lines; {s.unchanged} unchanged)</span></>
        ) : <>No earlier build of this target: every file is new ({s.added}).</>}
      </p>
      <ul className="diff-files" aria-label="Changed files">
        {changed.map((f) => <DiffFile key={f.path} file={f} open={changed.length <= 3} />)}
      </ul>
      {unchanged.length > 0 && <p className="muted small">Unchanged: {unchanged.map((f) => f.path).join(", ")}</p>}
    </div>
  );
}

function DiffFile({ file, open }: { file: BuildFileDiff; open: boolean }) {
  const tone = file.status === "added" ? "success" : file.status === "removed" ? "danger" : "warning";
  return (
    <li>
      <details open={open}>
        <summary>
          <code>{file.path}</code> <Tag tone={tone}>{file.status}</Tag>{" "}
          <span className="small muted">+{file.lines_added} −{file.lines_removed}</span>
        </summary>
        <pre className="diff-body" tabIndex={0} aria-label={`Diff of ${file.path}`}>
          {diffLines(file.diff).map((l, i) => <span key={i} className={`dl dl-${l.kind}`}>{l.text}{"\n"}</span>)}
        </pre>
        {file.truncated && <p className="muted small">Diff cut for display; the full file is in the job's project.</p>}
      </details>
    </li>
  );
}

function BuildResultCard({ job }: { job: BuildJobDetail }) {
  const counts = job.run_results?.counts ?? {};
  return (
    <Card title="Result" actions={<StatusBadge status={job.status} />}>
      {job.error && <p className="warn-text small">{job.error}</p>}
      {Object.keys(counts).length > 0 && (
        <p className="chip-row small">{Object.entries(counts).map(([k, v]) => <Tag key={k} tone={k === "error" || k === "fail" ? "danger" : "success"}>{k}: {v}</Tag>)}</p>
      )}
      {job.log_tail && <TechnicalDetails label="dbt log (tail)"><CodeBlock code={job.log_tail} /></TechnicalDetails>}
    </Card>
  );
}

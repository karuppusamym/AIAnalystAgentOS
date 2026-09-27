"""P5-01 on P7-06: an ML job on the isolated compute-ml pool gives exactly the in-process result, its input
files and output files travel as content-addressed artifacts verified on both sides, and model packages
are unpickled only in the worker (ADR-0024 + ADR-0022)."""
from __future__ import annotations

import io
import json
import tarfile

import pytest

from analystos.core.errors import Conflict, FeatureUnavailable, InvalidInput
from analystos.ml.store import MLStore, snapshots
from analystos.workers import ml as wml
from analystos.workers.dispatch import ListSink

pytest.importorskip("sklearn")
pytest.importorskip("statsmodels")
F = pytest.importorskip("evaluation.ml_datasets")

POOL_ON = type("S", (), {"isolated_pool_set": {"compute-ml"}})()


def _job(art, data, spec, kind="train", **extra):
    cols, rows, types = data
    digest = snapshots(art).put(cols, rows)
    return {"kind": kind, "artifact_dir": str(art), "snapshot": digest, "column_types": types, "spec": spec,
            "as_of": "2026-01-01T00:00:00+00:00", "caps": {"max_trials": 3, "max_seconds": 120},
            "workspace_id": "ws_ml", **extra}


def _norm(out):
    """JSON-normalized, without wall-clock timings (they differ on every run)."""
    body = json.loads(json.dumps(out, sort_keys=True, default=str))
    body.pop("seconds", None)
    for t in body.get("trials") or []:
        t.pop("seconds", None)
    return body


def _run(job, server, transport, sink=None):
    return wml.run_ml_isolated(job, transport=transport, sink=sink or ListSink(), secret=server.secret,
                               artifacts=server.store, settings=POOL_ON)


def test_train_then_score_on_compute_ml_match_the_inline_job(tmp_path, store_server, transport):
    from analystos.ml.jobs import run_ml_job

    inline_dir, iso_dir = tmp_path / "inline", tmp_path / "iso"
    job = _job(iso_dir, F.churn(), F.spec())
    expected = run_ml_job(_job(inline_dir, F.churn(), F.spec()))
    sink = ListSink()
    out = _run(job, store_server, transport, sink)
    assert _norm(out) == _norm(expected)
    assert isinstance(out["seconds"], float) and all("seconds" in t for t in out["trials"])
    # the files the job wrote came back verified into the control plane's store
    pkg = out["package"]["hash"]
    assert MLStore(iso_dir).file_hash(pkg) == pkg
    assert MLStore(iso_dir).get_json("memberships", out["membership_hash"])
    result = sink.results[0]
    assert result.status == "completed" and set(result.outputs) == {"result", "files"}
    assert result.outputs["files"].media_type == "application/x-tar" and result.usage.cpu_seconds > 0
    # scoring runs in the worker too, from the package published as an input artifact
    cols, rows, _ = F.churn(n=40, seed=5)
    score_job = {"kind": "score", "artifact_dir": str(iso_dir), "package_hash": pkg, "workspace_id": "ws_ml",
                 "snapshot": snapshots(iso_dir).put(cols, rows)}
    inline_score = run_ml_job({**score_job, "artifact_dir": str(inline_dir),
                               "snapshot": snapshots(inline_dir).put(cols, rows)})
    scored = _run(score_job, store_server, transport)
    assert scored == inline_score and scored["rows_scored"] == 40
    assert snapshots(iso_dir).get(scored["result"])[0] == scored["columns"]


def test_the_envelope_names_each_input_file_by_artifact_and_content_hash(tmp_path, store_server):
    job = _job(tmp_path, F.churn(), F.spec(), kind="prepare")
    env = wml.ml_envelope(job, store_server.store)
    files = env.spec.job["files"]
    assert list(files) == [f"ml_snapshots/{job['snapshot'][:2]}/{job['snapshot']}.json.gz"]
    assert {r.artifact_id for r in env.input_artifacts} == set(files.values())
    assert "artifact_dir" not in env.spec.job and env.required_outputs == ["result", "files"]
    assert env.workspace_id == "ws_ml" and env.budget.wall_seconds >= 120 + 300
    # one key per call: a re-run binds its own outputs (timings and time caps may differ)
    assert env.idempotency_key != wml.ml_envelope(job, store_server.store).idempotency_key


def test_a_refused_job_is_the_same_refusal_on_the_pool(tmp_path, store_server, transport):
    from analystos.ml.jobs import run_ml_job

    job = _job(tmp_path, F.churn(n=20), F.spec())
    out = _run(job, store_server, transport)
    assert out["status"] == "refused" and _norm(out) == _norm(run_ml_job(job))


def test_a_missing_snapshot_fails_on_the_pool_as_it_would_inline(tmp_path, store_server, transport):
    job = _job(tmp_path, F.churn(), F.spec(), kind="prepare", snapshot="ab" * 32)
    with pytest.raises(Conflict, match="no longer available"):
        _run(job, store_server, transport)


def test_a_file_that_does_not_hash_to_its_name_is_refused(tmp_path):
    good = snapshots(tmp_path).put(["a"], [[1]])
    rel = f"ml_snapshots/{good[:2]}/{good}.json.gz"
    data = (tmp_path / rel).read_bytes()
    wml.check_file(rel, data)
    other = "cd" * 32
    with pytest.raises(Conflict):
        wml.check_file(f"ml_snapshots/cd/{other}.json.gz", data)
    with pytest.raises(Conflict):
        wml.check_file(f"ml/packages/cd/{other}.pkl", b"not a platform package")
    with pytest.raises(InvalidInput):
        wml.check_file("../../etc/passwd", data)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(f"ml/packages/cd/{other}.pkl")
        info.size = 5
        tar.addfile(info, io.BytesIO(b"evil!"))
    with pytest.raises(Conflict):  # a worker cannot plant a package under a name its bytes do not hash to
        wml.untar_files(buf.getvalue())


def test_without_the_pool_the_isolated_ml_path_says_why(tmp_path, store_server, transport):
    job = _job(tmp_path, F.churn(), F.spec(), kind="prepare")
    with pytest.raises(FeatureUnavailable, match="ANALYSTOS_ISOLATED_POOLS=compute-ml"):
        wml.run_ml_isolated(job, transport=transport, sink=ListSink(), secret=store_server.secret,
                            artifacts=store_server.store, settings=type("S", (), {"isolated_pool_set": set()})())

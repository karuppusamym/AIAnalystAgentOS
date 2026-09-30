"""P7-06 unit checks (no services): tokens, the worker's environment and egress controls, error mapping,
pool configuration, the handler registry and the recipe routing. The pool behaviour itself is the
conformance suite (tests/conformance/worker)."""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from analystos.core import errors
from analystos.workers import tokens

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits: the resource module does not exist on Windows")


# ------------------------------------------------------------------------------------ tokens
def test_tokens_are_signed_scoped_and_short_lived():
    secret = b"s" * 32
    t = tokens.issue(secret, task_id="t1", workspace_id="ws", idempotency_key="idem-1234", reads=["wa_a"],
                     writes=["out"], ttl_seconds=60, now=1000)
    c = tokens.verify(secret, t, now=1030)
    assert c.task_id == "t1" and c.reads == {"wa_a"} and c.writes == {"out"} and c.verbs == {"read", "write"}
    c.require_read("wa_a")
    c.require_write("t1", "out")
    for bad in (lambda: c.require_read("wa_b"), lambda: c.require_write("t2", "out"),
                lambda: c.require_write("t1", "other"), lambda: c.require_purpose("summarization")):
        with pytest.raises(errors.Forbidden):
            bad()
    with pytest.raises(errors.Unauthenticated, match="expired"):
        tokens.verify(secret, t, now=1061)
    with pytest.raises(errors.Unauthenticated, match="signature"):
        tokens.verify(b"x" * 32, t, now=1030)
    payload, mac = t.split(".")
    forged = json.loads(tokens._unb64(payload)) | {"art": ["wa_a", "wa_b"]}
    with pytest.raises(errors.Unauthenticated):
        tokens.verify(secret, tokens._b64(json.dumps(forged).encode()) + "." + mac, now=1030)
    with pytest.raises(ValueError):
        tokens.issue(secret, task_id="t", workspace_id="w", idempotency_key="k" * 8, verbs=("delete",))


def test_the_token_key_is_derived_from_but_never_equal_to_the_jwt_secret():
    s = type("S", (), {"jwt_secret": "jwt-secret", "worker_token_secret": None})()
    derived = tokens.secret_from_settings(s)
    assert derived != b"jwt-secret" and len(derived) == 32
    s.worker_token_secret = "explicit"
    assert tokens.secret_from_settings(s) == b"explicit"


# ------------------------------------------------------------------------------------ environment and egress
def test_a_credential_in_the_environment_is_refused_by_name():
    from analystos.workers.isolation import WorkerMisconfigured, check_and_scrub_environment, forbidden_names

    env = {"PATH": "/bin", "ANALYSTOS_DATABASE_URL": "postgresql://x", "ANALYSTOS_ANALYTICS_READER_URL": "p",
           "OPENROUTER_API_KEY": "sk", "SERVICENOW_PASSWORD": "x", "ANALYSTOS_JWT_SECRET": "j", "PGPASSWORD": "p",
           "ANALYSTOS_REDIS_URL": "", "ANALYSTOS_WORKER_ARTIFACT_URL": "http://api:8000"}
    assert forbidden_names(env) == sorted(["ANALYSTOS_DATABASE_URL", "ANALYSTOS_ANALYTICS_READER_URL", "OPENROUTER_API_KEY",
                                           "SERVICENOW_PASSWORD", "ANALYSTOS_JWT_SECRET", "PGPASSWORD"])
    with pytest.raises(WorkerMisconfigured) as exc:
        check_and_scrub_environment(dict(env))
    assert "postgresql" not in exc.value.message  # names, never values
    clean = {"PATH": "/bin", "HTTPS_PROXY": "http://proxy", "FOO": "1", "ANALYSTOS_WORKER_QUEUES": "compute-py"}
    assert check_and_scrub_environment(clean) == {"PATH": "/bin", "ANALYSTOS_WORKER_QUEUES": "compute-py"}


def test_the_egress_guard_allows_only_the_store():
    """Run in a subprocess: the guard patches the socket module for the rest of the process, by design."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(5)
    port, other_port = listener.getsockname()[1], other.getsockname()[1]
    stop = threading.Event()

    def accept(s):
        s.settimeout(0.2)
        while not stop.is_set():
            with contextlib.suppress(OSError):
                s.accept()[0].close()
    for s in (listener, other):
        threading.Thread(target=accept, args=(s,), daemon=True).start()
    code = f"""
import socket
from analystos.workers.isolation import EgressRefused, install_egress_guard
install_egress_guard(["http://127.0.0.1:{port}"])
socket.create_connection(("127.0.0.1", {port}), timeout=2).close()
out = []
for target in [("127.0.0.1", {other_port}), ("10.1.2.3", 5432)]:
    try:
        socket.create_connection(target, timeout=2); out.append("connected")
    except EgressRefused:
        out.append("refused")
calls = [lambda: socket.getaddrinfo("example.com", 443),
         lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("8.8.8.8", 53))]
if hasattr(socket, "AF_UNIX"):  # Windows has no unix sockets to refuse
    calls.insert(1, lambda: socket.socket(socket.AF_UNIX).connect("/tmp/x"))
for call in calls:
    try:
        call(); out.append("allowed")
    except EgressRefused:
        out.append("refused")
install_egress_guard([])  # narrowing again works; the store is now refused too
try:
    socket.create_connection(("127.0.0.1", {port}), timeout=2); out.append("connected")
except EgressRefused:
    out.append("refused")
print(",".join(out))
"""
    src = str(Path(__file__).resolve().parents[2] / "src")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                           env={"PYTHONPATH": src, "PATH": "/usr/bin:/bin",
                                **{k: os.environ[k] for k in ("SYSTEMROOT",) if k in os.environ}})  # winsock needs it
    finally:
        stop.set()
        listener.close()
        other.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ",".join(["refused"] * (6 if hasattr(socket, "AF_UNIX") else 5))


@posix_only
def test_job_rlimits_follow_the_budget():
    import resource

    from analystos.workers.isolation import job_rlimits

    limits = dict(job_rlimits({"cpu_seconds": 5, "memory_mb": 512, "max_output_bytes": 1000}))
    assert limits[resource.RLIMIT_CPU] == (5, 7) and limits[resource.RLIMIT_AS] == (512 * 2**20,) * 2
    assert limits[resource.RLIMIT_CORE] == (0, 0) and limits[resource.RLIMIT_FSIZE] == (1001, 1001)


# ------------------------------------------------------------------------------------ errors
def _all_errors(cls=errors.AnalystOSError):
    yield cls
    for sub in cls.__subclasses__():
        yield from _all_errors(sub)


def test_every_core_error_round_trips_through_its_dict():
    seen = 0
    for cls in _all_errors():
        if cls.__module__ != "analystos.core.errors":
            continue
        back = errors.error_from_dict(cls("m", details={"a": 1}).to_dict())
        assert type(back) is cls and back.retryable == cls.retryable and back.http_status == cls.http_status
        assert back.details == {"a": 1}
        seen += 1
    assert seen > 30
    unknown = errors.error_from_dict({"code": "made_up", "message": "x", "retryable": True})
    assert unknown.code == "made_up" and unknown.retryable and type(unknown) is errors.AnalystOSError


# ------------------------------------------------------------------------------------ pools and registry
def test_isolated_pools_are_opt_in_and_say_why():
    from analystos.core.profiles import requirement_reason
    from analystos.workers.dispatch import pool_reason, require_pool

    off = type("S", (), {"isolated_pool_set": set()})()
    on = type("S", (), {"isolated_pool_set": {"compute-ml"}})()
    assert "ANALYSTOS_ISOLATED_POOLS=compute-ml" in pool_reason("compute-ml", off)
    assert pool_reason("compute-ml", on) is None and pool_reason("compute-py", on)
    assert requirement_reason("pool:compute-ml", off) == pool_reason("compute-ml", off)
    assert requirement_reason("pool:compute-ml", on) is None
    with pytest.raises(errors.FeatureUnavailable, match="ML training is unavailable"):
        require_pool("compute-ml", "ML training", off)


def test_settings_parse_the_pool_list(monkeypatch):
    from analystos.core.config import Settings

    monkeypatch.setenv("ANALYSTOS_ISOLATED_POOLS", "compute-py, compute-ml")
    assert Settings().isolated_pool_set == {"compute-py", "compute-ml"}
    monkeypatch.delenv("ANALYSTOS_ISOLATED_POOLS")
    assert Settings().isolated_pool_set == set()


def test_a_manifest_can_require_a_pool_and_an_unknown_pool_fails_the_load(monkeypatch):
    from analystos.capabilities import registry

    ok = {"apiVersion": "analystos/v1", "kind": "Skill", "id": "skill.needs_ml_pool", "summary": "x",
          "side_effect": "none", "requires": ["pool:compute-ml"]}
    bad = {**ok, "id": "skill.needs_gpu_pool", "requires": ["pool:gpu"]}
    snap = registry.load(legacy=False, connectors=False, entry_points=False, packs_dir=None, strict=False,
                         extra=[("test", ok), ("test", bad)])
    assert any("requires unknown pool:gpu" in p for p in snap.problems)
    assert not any("needs_ml_pool" in p for p in snap.problems)
    m = snap.manifests["skill.needs_ml_pool"]
    reason = registry.install_reason(m, type("S", (), {"isolated_pool_set": set()})())
    assert reason and "isolated `compute-ml` worker pool" in reason
    assert registry.install_reason(m, type("S", (), {"isolated_pool_set": {"compute-ml"}})()) is None


def test_queue_config_keeps_isolated_pools_out_of_all():
    from analystos.workflows import queues

    assert queues.ISOLATED_POOLS == ("compute-py", "compute-ml")
    from analystos.contracts.worker import ISOLATED_POOLS

    assert ISOLATED_POOLS == queues.ISOLATED_POOLS
    assert not set(queues.parse_workloads("all")) & set(queues.ISOLATED_POOLS)
    assert queues.parse_workloads("compute-py,compute-ml") == ["compute-py", "compute-ml"]
    with pytest.raises(ValueError, match="do not combine"):
        queues.parse_workloads("analysis,compute-py")
    specs, _ = queues.load_config()
    assert specs["compute-ml"].budget["max_trials"] > 0 and specs["compute-py"].isolated
    opts = queues.workflow_options("acme")
    assert opts["queues"]["compute-py"]["name"] == "acme-compute-py"


def test_the_cli_starts_an_isolated_worker_without_settings(monkeypatch):
    from analystos import cli

    seen = {}
    monkeypatch.setattr("analystos.workers.main.run_isolated",
                        lambda pools, **kw: seen.update(pools=pools, **kw) or 0)
    monkeypatch.setattr("analystos.core.config.get_settings", lambda: pytest.fail("an isolated worker built Settings"))
    assert cli.main(["worker", "--queues", "compute-ml", "--conformance"]) == 0
    assert seen == {"pools": ("compute-ml",), "conformance": True}


def test_handlers_are_bound_to_pools_and_hashed_by_source():
    from analystos.workers import handlers

    assert handlers.served_by("compute-py") == ["python.cell", "recipe.snapshot"]
    assert handlers.served_by("compute-ml") == ["ml.job"]
    assert "conformance.probe" in handlers.served_by("compute-ml", conformance=True)
    with pytest.raises(errors.UnsupportedCapability):
        handlers.handler_for("recipe.snapshot", "compute-ml")
    with pytest.raises(errors.UnsupportedCapability):
        handlers.handler_for("conformance.probe", "compute-py")  # only for a --conformance worker
    h = handlers.handler_hash("recipe.snapshot")
    assert len(h) == 64 and h == handlers.handler_hash("recipe.snapshot")


def test_a_pure_ml_job_plugs_in_through_the_pure_adapter(tmp_path, monkeypatch):
    """How P5-04 plugs in: `run_ml_job(job) -> dict` registered for compute-ml with adapter="pure". The job
    gets its inputs as local paths; its dict becomes the JSON output `result`."""
    from analystos.workers import child, handlers

    mod = tmp_path / "fake_ml_jobs.py"
    mod.write_text("def run_ml_job(job):\n    return {'rows': len(open(job['inputs']['wa_x']).read().split()),"
                   " 'seed': job['seed']}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(handlers.HANDLERS, "ml.job", handlers.Handler("ml.job", "fake_ml_jobs:run_ml_job",
                                                                      ("compute-ml",), adapter="pure"))
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "wa_x").write_text("a b c", encoding="utf-8")
    body = child.run({"task_id": "t", "pool": "compute-ml", "kind": "ml.job", "spec": {"kind": "ml.job", "job": {"seed": 7}},
                      "inputs": {"wa_x": str(tmp_path / "in" / "wa_x")}, "out_dir": str(tmp_path / "out"),
                      "outputs": ["result"], "budget": {}, "envelope": {}, "scratch": str(tmp_path)})
    assert body["result"] == {"rows": 3, "seed": 7}
    assert json.loads((tmp_path / "out" / "result").read_text(encoding="utf-8")) == {"rows": 3, "seed": 7}
    assert body["outputs"]["result"]["media_type"] == "application/json"


def test_without_its_module_an_ml_job_is_unavailable_not_broken():
    from analystos.workers import handlers

    if handlers.ML_JOB_TARGET.split(":")[0] in sys.modules or _importable(handlers.ML_JOB_TARGET.split(":")[0]):
        pytest.skip("the ML job module is installed (P5-04)")
    with pytest.raises(errors.FeatureUnavailable, match="ml.job"):
        handlers.handler_hash("ml.job")


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# ------------------------------------------------------------------------------------ first consumer routing
def test_recipe_compute_goes_to_compute_py_only_when_the_pool_is_configured(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.workflows import orchestrator

    calls = []
    monkeypatch.setattr("analystos.workers.recipe.run_recipe_isolated", lambda job, **kw: calls.append(job) or {"r": 1})
    monkeypatch.setattr("analystos.recipes.execute.run_snapshot_job", lambda job, **kw: {"inline": True})
    monkeypatch.setattr(get_settings(), "orchestrator", "local")
    monkeypatch.setattr(get_settings(), "isolated_pools", "")
    assert orchestrator.run_recipe_compute({"x": 1}) == {"inline": True} and not calls
    monkeypatch.setattr(get_settings(), "isolated_pools", "compute-py")
    assert orchestrator.run_recipe_compute({"x": 1}) == {"r": 1} and calls == [{"x": 1}]


def test_the_isolated_workflow_is_registered_where_the_control_plane_runs():
    from analystos.workers.temporal import run_isolated_task
    from analystos.workflows.activities import BY_WORKLOAD, record_task_events
    from analystos.workflows.analysis_workflow import IsolatedTaskWorkflow

    assert IsolatedTaskWorkflow.__temporal_workflow_definition.name == "IsolatedTaskWorkflow"
    assert record_task_events in BY_WORKLOAD["analysis"]
    assert all(run_isolated_task not in acts for acts in BY_WORKLOAD.values())  # only isolated workers run it


def test_ml_compute_goes_to_compute_ml_only_when_the_pool_is_configured(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.workflows import orchestrator

    calls = []
    monkeypatch.setattr("analystos.workers.ml.run_ml_isolated", lambda job, **kw: calls.append(job) or {"iso": 1})
    monkeypatch.setattr("analystos.ml.jobs.run_ml_job", lambda job: {"inline": True})
    monkeypatch.setattr(get_settings(), "orchestrator", "local")
    monkeypatch.setattr(get_settings(), "isolated_pools", "compute-py")
    assert orchestrator.run_ml_compute({"x": 1}) == {"inline": True} and not calls
    monkeypatch.setattr(get_settings(), "isolated_pools", "compute-ml")
    assert orchestrator.run_ml_compute({"x": 1}) == {"iso": 1} and calls == [{"x": 1}]


def test_step_python_goes_to_compute_py_only_when_the_pool_is_configured(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.services import steps

    calls = []
    monkeypatch.setattr("analystos.workers.python.run_python_isolated",
                        lambda code, inputs, **kw: calls.append((code, kw["workspace_id"])) or {"ok": True, "iso": 1})
    monkeypatch.setattr(get_settings(), "isolated_pools", "compute-py")
    assert steps.execute_python("result = 1", {}, workspace_id="ws1") == {"ok": True, "iso": 1}
    assert calls == [("result = 1", "ws1")]
    monkeypatch.setattr(get_settings(), "isolated_pools", "")
    monkeypatch.setattr("analystos.sandbox.runner.run_python", lambda code, **kw: pytest.fail("ran") if calls[1:] else
                        type("R", (), {"ok": True, "result": 1, "error": None, "stdout": "", "duration_ms": 1,
                                       "timed_out": False, "isolation": "process", "network_isolated": True})())
    assert steps.execute_python("result = 1", {})["isolation"] == "process" and len(calls) == 1


def test_a_step_snapshot_and_a_worker_artifact_are_one_artifact_ref_type():
    from analystos.contracts import step, worker

    assert step.ArtifactRef is worker.ArtifactRef
    assert step.Step.model_fields["result_snapshot"].annotation == worker.ArtifactRef | None
    h = "ab" * 32
    stored = {"kind": "artifact", "id": "art_1", "version": 2, "content_hash": h, "media_type": "application/json"}
    ref = worker.ArtifactRef.model_validate(stored)  # a snapshot stored before the types were unified
    assert ref.artifact_id == "art_1" and ref.bytes is None
    dumped = ref.model_dump(mode="json")
    assert {k: dumped[k] for k in stored} == stored and dumped["artifact_id"] == "art_1"  # old readers keep `id`
    assert worker.ArtifactRef.model_validate(dumped) == ref
    w = worker.ArtifactRef(artifact_id="wa_" + "0" * 32, kind="blob", content_hash=h, bytes=3)
    assert worker.ArtifactRef.model_validate(w.model_dump()) == w and w.id == w.artifact_id
    with pytest.raises(ValueError, match="different artifacts"):
        worker.ArtifactRef.model_validate({**stored, "artifact_id": "art_2"})
    with pytest.raises(ValueError):
        worker.ArtifactRef.model_validate({**stored, "unexpected": 1})


def test_the_ml_capability_hash_covers_the_ml_package():
    from analystos.workers import handlers

    h = handlers.HANDLERS["ml.job"]
    assert h.target == "analystos.workers.ml:ml_job" and "analystos.ml" in h.covers
    files = handlers._sources("ml.job", "analystos.ml")
    assert any(p.name == "jobs.py" for p in files) and any(p.name == "tabular.py" for p in files)


def test_with_the_ml_pool_the_control_plane_never_unpickles_a_package(monkeypatch):
    from analystos.core.config import get_settings
    from analystos.services import ml

    monkeypatch.setattr(ml, "verify_package", lambda s, ws, h: b"bytes")
    monkeypatch.setattr(ml, "_store", lambda: pytest.fail("the control plane opened the package store to unpickle"))
    monkeypatch.setattr(get_settings(), "isolated_pools", "compute-ml")
    with pytest.raises(errors.PolicyDenied, match="isolated compute-ml worker"):
        ml.load_package(None, "ws", "ab" * 32)


def test_the_mlflow_export_can_skip_unpickling():
    from analystos.ml.mlflow_export import files

    view = {"id": "mlx_1", "definition_key": "d", "definition_version": 1, "task": "classify", "package_hash": "ab" * 32,
            "created_at": "2026-09-26T10:00:00+00:00", "finished_at": "2026-09-26T10:01:00+00:00"}
    fs = files(view, {"ml_trials": {"trials": []}}, b"not a pickle", unpickle=False)
    assert any(p.endswith("model/analystos_package.pkl") for p in fs)
    assert not any(p.endswith("model/model.pkl") for p in fs)

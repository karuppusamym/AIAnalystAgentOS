"""Worker conformance (ADR-0022 decision 6, P7-06): what every isolated pool implementation must do.

Envelope round-trip, token-scope refusal, egress refusal, budget kill, idempotent retry writing one
artifact, and structured errors mapped to `core/errors.py`. Parametrized over `compute-py` and
`compute-ml`; the implementation comes from `ANALYSTOS_CONFORMANCE_IMPL` (conftest.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import httpx
import pytest

from analystos.contracts.worker import ArtifactRef, RecipeSnapshotSpec, SnapshotInput, TaskDispatch, TaskEnvelope
from analystos.core import errors
from analystos.workers.dispatch import ListSink, build_dispatch, capability_ref, raise_for_result
from analystos.workers.isolation import EX_CONFIG
from analystos.workers.runtime import StoreClient
from analystos.workers.tokens import issue


# ------------------------------------------------------------------------------------ envelope round-trip
def test_envelope_round_trips_through_json(runner):
    env = runner.envelope("echo", {"x": [1, 2]}, outputs=["envelope"], run_id="run_1", work_order_id="wo_1",
                          context_ref="ctx:abc", policy_ref="policy:v3", trace_parent="00-" + "a" * 32 + "-" + "b" * 16 + "-01")
    again = TaskEnvelope.model_validate_json(env.model_dump_json())
    assert again == env and again.envelope_hash == env.envelope_hash
    with pytest.raises(ValueError):
        TaskEnvelope.model_validate({**env.model_dump(mode="json"), "surprise": 1})  # the contract is closed


def test_envelope_round_trips_through_the_pool(runner):
    sink = ListSink()
    env = runner.envelope("echo", {"x": [1, 2]}, outputs=["envelope"], run_id="run_1", context_ref="ctx:abc")
    result = raise_for_result(runner.run(env, sink=sink))
    assert result.result["envelope"] == env.model_dump(mode="json")  # what the worker parsed is what was sent
    ref = result.outputs["envelope"]
    _, data = runner.server.store.read(ref.artifact_id)
    assert json.loads(data) == env.model_dump(mode="json")
    assert [e.type for e in result.events][0] == "task.started" and result.events[-1].type == "task.completed"
    assert [e.type for e in sink.events] == [e.type for e in result.events]  # streamed live to the control plane
    assert sink.results[0].status == "completed" and result.usage.wall_seconds > 0


def test_inputs_are_read_with_the_token_and_verified(runner):
    a, b = runner.put(b"alpha"), runner.put(b"beta-beta")
    result = raise_for_result(runner.run(runner.envelope("read", inputs=[a, b])))
    assert result.result["read"] == {a.artifact_id: 5, b.artifact_id: 9}


# ------------------------------------------------------------------------------------ token scope
def _crafted(runner, env: TaskEnvelope, token_env: TaskEnvelope) -> TaskDispatch:
    """A dispatch whose envelope asks for more than its token grants (a tampered or buggy control plane)."""
    token = build_dispatch(runner.pool, token_env, secret=runner.server.secret).token
    return TaskDispatch(pool=runner.pool, envelope=env, token=token)


def test_an_input_the_token_does_not_grant_is_refused(runner):
    a, b = runner.put(b"granted"), runner.put(b"not granted")
    granted = runner.envelope("read", inputs=[a])
    wider = granted.model_copy(update={"input_artifacts": [a, b]})
    result = runner.transport.run(_crafted(runner, wider, granted), lambda e: None)
    assert result.status == "failed" and result.error.code == "forbidden"
    assert b.artifact_id in json.dumps(result.error.details) and not result.outputs


def test_an_artifact_of_another_workspace_is_refused_even_when_listed(runner):
    other = runner.put(b"someone else's", workspace="ws_other")
    result = runner.run(runner.envelope("read", inputs=[other]))
    assert result.status == "failed" and result.error.code == "forbidden"


def test_an_output_the_token_does_not_grant_is_refused(runner):
    granted = runner.envelope("write", {"outputs": {"a": "1"}}, outputs=["a"])
    wider = granted.model_copy(update={"required_outputs": ["a", "b"],
                                       "spec": granted.spec.model_copy(update={"args": {"outputs": {"a": "1", "b": "2"}}})})
    result = runner.transport.run(_crafted(runner, wider, granted), lambda e: None)
    assert result.status == "failed" and result.error.code == "forbidden" and "'b'" in result.error.message


def test_store_refuses_tokens_outside_their_scope(store_server):
    store, secret, url = store_server.store, store_server.secret, store_server.url
    ref = store.put("ws_a", b"data", kind="blob")
    client = StoreClient(url)

    def token(**kw):
        base = {"task_id": "t1", "workspace_id": "ws_a", "idempotency_key": "idem-0001", "reads": [ref.artifact_id],
                "writes": ["out"], "verbs": ("read", "write")}
        return issue(secret, **{**base, **kw})

    try:
        assert client.read(ref, token()) == b"data"
        cases = [
            (lambda: client.read(ref, token(reads=[])), errors.Forbidden),                        # other object
            (lambda: client.read(ref, token(verbs=("write",))), errors.Forbidden),               # other verb
            (lambda: client.read(ref, token(ttl_seconds=-1)), errors.Unauthenticated),           # expired
            (lambda: client.read(ref, token()[:-2] + "xx"), errors.Unauthenticated),             # tampered
            (lambda: client.read(ref, issue(b"another-secret" * 3, task_id="t1", workspace_id="ws_a",
                                            idempotency_key="idem-0001", reads=[ref.artifact_id])), errors.Unauthenticated),
            (lambda: client.read(ref, token(workspace_id="ws_b")), errors.Forbidden),            # other workspace
            (lambda: client.write("t1", "other", b"x", kind="k", media_type="text/plain", token=token()), errors.Forbidden),
            (lambda: client.write("t2", "out", b"x", kind="k", media_type="text/plain", token=token()), errors.Forbidden),
            (lambda: client.write("t1", "out", b"x", kind="k", media_type="text/plain", token=token(verbs=("read",))),
             errors.Forbidden),
            (lambda: client.model("summarization", [{"role": "user", "content": "x"}], token()), errors.Forbidden),
        ]
        for call, expected in cases:
            with pytest.raises(expected):
                call()
        from analystos.security.auth import issue_token  # a session JWT is not a task token

        jwt = issue_token("u_1", "admin@analystos.local")
        r = httpx.get(f"{url}/api/worker/artifacts/{ref.artifact_id}", headers={"Authorization": f"Bearer {jwt}"})
        assert r.status_code == 401
        assert httpx.get(f"{url}/api/worker/artifacts/{ref.artifact_id}").status_code == 401
    finally:
        client.close()


# ------------------------------------------------------------------------------------ egress and environment
def test_egress_is_refused_except_to_the_store(runner):
    targets = [["127.0.0.1", runner.server.port], ["127.0.0.1", 5432], ["localhost", 6379], ["1.1.1.1", 443],
               ["example.com", 443]]
    result = runner.run(runner.envelope("egress", {"targets": targets}))
    assert result.status == "failed" and result.error.code == "egress_blocked", result.error
    attempts = result.error.details["attempts"]
    assert len(attempts) == len(targets) and not any(a["connected"] for a in attempts)
    # The job itself has no route even to the store: its inputs and outputs go through the supervisor.
    ok = raise_for_result(runner.run(runner.envelope("write", {"outputs": {"o": "fine"}}, outputs=["o"])))
    assert ok.outputs["o"].bytes == 4


def test_the_job_environment_carries_no_credentials(runner):
    result = raise_for_result(runner.run(runner.envelope("environment")))
    keys = set(result.result["keys"])
    assert not [k for k in keys if "URL" in k or "SECRET" in k or "PASSWORD" in k or "KEY" in k or "TOKEN" in k]
    assert keys <= {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONPATH", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE",
                    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "LC_CTYPE"}
    if result.usage.network == "namespace":
        assert result.result["network"] == ["lo"]
    else:
        assert result.usage.network == "guard"


def test_a_worker_given_credentials_refuses_to_start(pool, worker_python, src_root, tmp_path):
    for var in ("ANALYSTOS_DATABASE_URL", "ANALYSTOS_REDIS_URL", "OPENROUTER_API_KEY", "PGPASSWORD"):
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": src_root, "ANALYSTOS_WORKER_QUEUES": pool, var: "x://secret"}
        r = subprocess.run([worker_python, "-m", "analystos.workers.main", "--stdio"], env=env, input="", cwd=tmp_path,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == EX_CONFIG, r.stderr
        body = json.loads(r.stderr.strip().splitlines()[-1])
        assert body["error"]["code"] == "worker_misconfigured" and var in body["error"]["details"]["variables"]
        assert "secret" not in r.stderr  # names only, never values


def test_a_worker_never_serves_regular_queues(worker_python, src_root, tmp_path):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": src_root}
    r = subprocess.run([worker_python, "-m", "analystos.workers.main", "--queues", "analysis", "--stdio"], env=env,
                       input="", cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "isolated worker serves only" in r.stderr


# ------------------------------------------------------------------------------------ budget kill
@pytest.mark.parametrize("action,args,budget,limit", [
    ("cpu", {"seconds": 30}, {"cpu_seconds": 1, "wall_seconds": 20}, "cpu"),
    ("memory", {"mb": 900}, {"memory_mb": 256}, "memory"),
    ("sleep", {"seconds": 30}, {"wall_seconds": 2}, "wall"),
    ("big_output", {"bytes": 50_000, "name": "blob"}, {"max_output_bytes": 10_000}, "output_bytes"),
    ("trials", {"n": 5}, {"max_trials": 3}, "trials"),
])
def test_a_job_over_its_budget_is_killed_and_the_worker_goes_on(runner, action, args, budget, limit):
    outputs = ["blob"] if action == "big_output" else []
    started = time.monotonic()
    result = runner.run(runner.envelope(action, args, budget=budget, outputs=outputs))
    assert result.status == "failed" and result.error.code == "budget_exceeded", result.error
    assert result.error.details["limit"] == limit and not result.error.retryable
    assert time.monotonic() - started < 25
    with pytest.raises(errors.BudgetExceeded):
        raise_for_result(result)
    assert raise_for_result(runner.run(runner.envelope("echo"))).status == "completed"  # the pool survived


# ------------------------------------------------------------------------------------ idempotent retry
def test_a_retry_that_finishes_twice_writes_one_artifact(runner):
    env = runner.envelope("write", {"outputs": {"model": "weights-v1", "report": "r"}}, outputs=["model", "report"])
    before = runner.server.store.stats()
    first = raise_for_result(runner.run(env))
    after_first = runner.server.store.stats()
    second = raise_for_result(runner.run(env))  # the same task delivered again (a Temporal retry)
    redispatched = raise_for_result(runner.run(env.model_copy(update={"task_id": env.task_id + "b"})))
    assert first.outputs == second.outputs == redispatched.outputs
    assert after_first["artifacts"] - before["artifacts"] == 2
    assert runner.server.store.stats() == after_first  # nothing new: one artifact per output


def test_a_retry_that_changes_an_output_is_refused(runner):
    env = runner.envelope("write", {"outputs": {"o": "first"}}, outputs=["o"])
    raise_for_result(runner.run(env))
    changed = env.model_copy(update={"spec": env.spec.model_copy(update={"args": {"outputs": {"o": "second"}}})})
    result = runner.run(changed)
    assert result.status == "failed" and result.error.code == "conflict"


# ------------------------------------------------------------------------------------ structured errors
@pytest.mark.parametrize("code,cls", [("invalid_input", errors.InvalidInput), ("forbidden", errors.Forbidden),
                                      ("query_timeout", errors.QueryTimeout), ("not_found", errors.NotFound),
                                      ("upstream_unavailable", errors.UpstreamUnavailable),
                                      ("spend_cap_reached", errors.SpendCapReached)])
def test_job_errors_map_to_core_errors(runner, code, cls):
    result = runner.run(runner.envelope("raise", {"code": code, "message": "m", "details": {"k": 1}}))
    assert result.status == "failed" and result.error.code == code
    assert result.error.retryable == cls.retryable and result.error.details == {"k": 1}
    with pytest.raises(cls) as exc:
        raise_for_result(result)
    assert type(exc.value) is cls and exc.value.http_status == cls.http_status


def test_an_unexpected_exception_is_an_internal_error(runner):
    result = runner.run(runner.envelope("raise", {"code": "plain", "message": "boom"}))
    assert result.error.code == "internal_error" and "ValueError: boom" in result.error.message


def test_a_retryable_failure_is_retried_under_the_same_task(runner):
    sink = ListSink()
    env = runner.envelope("raise", {"code": "upstream_unavailable"})
    result = runner.run(env, sink=sink, attempts=2)
    # at least one retry per attempt (a Temporal pool also retries inside the workflow), always the same task
    assert result.error.retryable and [e.type for e in sink.events].count("task.started") >= 2
    assert {e.task_id for e in sink.events} == {env.task_id}


def test_a_kind_the_pool_does_not_serve_is_refused_by_the_worker(runner, pool):
    other = "compute-ml" if pool == "compute-py" else "compute-py"
    snap = "0" * 64
    env = TaskEnvelope(task_id="wtask_x1", workspace_id=runner.workspace, capability=capability_ref("recipe.snapshot"),
                       spec=RecipeSnapshotSpec(sql="select 1", tables={"a.b": SnapshotInput(artifact_id="wa_x", snapshot=snap)},
                                               max_rows=1, timeout_seconds=1),
                       idempotency_key="idem-kind-0001", required_outputs=["result"])
    token = issue(runner.server.secret, task_id=env.task_id, workspace_id=env.workspace_id,
                  idempotency_key=env.idempotency_key, writes=["result"])
    result = runner.transport.run(TaskDispatch(pool=pool, envelope=env, token=token), lambda e: None)
    if pool == "compute-ml":
        assert result.error.code == "unsupported_capability"
    assert other  # recipe snapshots are served by compute-py only


def test_a_worker_running_other_code_refuses(runner):
    env = runner.envelope("echo")
    skewed = env.model_copy(update={"capability": env.capability.model_copy(update={"content_hash": "f" * 64})})
    token = build_dispatch(runner.pool, env, secret=runner.server.secret).token
    result = runner.transport.run(TaskDispatch(pool=runner.pool, envelope=skewed, token=token), lambda e: None)
    assert result.error.code == "conflict" and "different version" in result.error.message


# ------------------------------------------------------------------------------------ model callback
def test_the_model_callback_is_bound_to_the_token_purposes_and_budget(store_server):
    client = StoreClient(store_server.url)
    token = issue(store_server.secret, task_id="t9", workspace_id="ws_m", idempotency_key="idem-model-1",
                  verbs=("model",), purposes=("summarization",), max_model_calls=2)
    msgs = [{"role": "user", "content": "name this feature"}]
    try:
        first = client.model("summarization", msgs, token)
        assert first["text"] == "ok:summarization" and first["calls_left"] == 1
        purpose, ctx = store_server.router.calls[-1]
        assert purpose == "summarization" and ctx.workspace_id == "ws_m" and ctx.task_id == "t9"
        with pytest.raises(errors.Forbidden):
            client.model("planning", msgs, token)
        client.model("summarization", msgs, token)
        with pytest.raises(errors.BudgetExceeded):
            client.model("summarization", msgs, token)
    finally:
        client.close()


def test_artifact_refs_are_content_addressed(store_server):
    a = store_server.store.put("ws_c", b"same", kind="blob")
    b = store_server.store.put("ws_c", b"same", kind="blob")
    c = store_server.store.put("ws_d", b"same", kind="blob")
    assert a == b and a.artifact_id != c.artifact_id and a.content_hash == c.content_hash
    assert ArtifactRef.model_validate(a.model_dump()) == a

"""P7-12 on P7-06: a step/notebook Python cell on the isolated compute-py pool gives the sandbox's result shape,
keeps the sandbox's policy (no connection library, no file access) and is killed on a resource bomb."""
from __future__ import annotations

import pytest

from analystos.core.errors import FeatureUnavailable
from analystos.services.steps import PYTHON_IMPORTS
from analystos.workers.dispatch import ListSink
from analystos.workers.python import cell_envelope, run_python_isolated

POOL_ON = type("S", (), {"isolated_pool_set": {"compute-py"}})()
KEYS = {"ok", "result", "error", "stdout", "duration_ms", "timed_out", "isolation", "network_isolated"}


def _run(server, transport, code, inputs=None, sink=None, **kw):
    return run_python_isolated(code, inputs or {}, allowed_imports=PYTHON_IMPORTS, transport=transport,
                               sink=sink or ListSink(), secret=server.secret, artifacts=server.store, settings=POOL_ON,
                               workspace_id="ws_nb", **kw)


def test_a_cell_runs_on_compute_py_over_its_inputs_only(store_server, transport):
    sink = ListSink()
    code = "import pandas as pd\ndf = pd.DataFrame(inputs['s1'])\nprint('rows', len(df))\nresult = float(df['v'].sum())"
    out = _run(store_server, transport, code, {"s1": [{"v": 1.5}, {"v": 2.5}]}, sink=sink)
    assert set(out) == KEYS
    assert out["ok"] is True and out["result"] == 4.0 and "rows 2" in out["stdout"]
    assert out["isolation"] == "compute-py" and out["timed_out"] is False
    assert sink.results[0].status == "completed" and sink.results[0].outputs["result"].media_type == "application/json"


@pytest.mark.parametrize("code", ["import psycopg\nresult = 1", "import socket\nresult = 1",
                                  "import urllib.request\nresult = 1", "result = open('/etc/passwd').read()"])
def test_the_sandbox_policy_holds_on_the_pool(store_server, transport, code):
    out = _run(store_server, transport, code)
    assert out["ok"] is False and "rejected by sandbox policy" in out["error"]


def test_a_cell_exception_is_the_cells_answer_not_a_pool_failure(store_server, transport):
    out = _run(store_server, transport, "result = 1 / 0")
    assert out["ok"] is False and "ZeroDivisionError" in out["error"]


def test_a_resource_bomb_is_killed(store_server, transport):
    out = _run(store_server, transport, "while True:\n    pass", timeout_s=2)
    assert out["ok"] is False and out["timed_out"] is True and "timeout" in out["error"]
    big = _run(store_server, transport, "x = bytearray(4 * 1024 ** 3)\nresult = 1", memory_mb=256)
    assert big["ok"] is False and "Memory" in big["error"]


def test_the_envelope_carries_the_inputs_as_an_artifact(store_server):
    env = cell_envelope("result = 1", {"a": [1, 2]}, allowed_imports=PYTHON_IMPORTS, timeout_s=5, memory_mb=256,
                        artifacts=store_server.store, workspace_id="ws_nb")
    assert env.spec.kind == "python.cell" and [r.artifact_id for r in env.input_artifacts] == [env.spec.inputs_artifact]
    _, data = store_server.store.read(env.spec.inputs_artifact)
    assert data == b'{"a": [1, 2]}' and env.budget.memory_mb >= 256 + 512


def test_without_the_pool_the_isolated_cell_path_says_why(store_server, transport):
    with pytest.raises(FeatureUnavailable, match="ANALYSTOS_ISOLATED_POOLS=compute-py"):
        run_python_isolated("result = 1", {}, allowed_imports=PYTHON_IMPORTS, transport=transport, sink=ListSink(),
                            secret=store_server.secret, artifacts=store_server.store,
                            settings=type("S", (), {"isolated_pool_set": set()})())

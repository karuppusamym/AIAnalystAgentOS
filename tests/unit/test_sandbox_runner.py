"""Sandbox: allowed numeric code runs; forbidden imports/builtins/attributes are rejected; timeout and
memory limits are enforced; the child environment carries no secrets."""
from __future__ import annotations

import pytest

from analystos.sandbox import runner
from analystos.sandbox.runner import check_code, run_python, sandbox_env


def test_allowed_numeric_code_with_inputs():
    code = """
import numpy as np
import pandas as pd
from scipy import stats
import statistics, math, json
df = pd.DataFrame(inputs["rows"])
t = stats.ttest_ind(df[df.g == "a"].x, df[df.g == "b"].x)
print("rows", len(df))
result = {"mean": df.x.mean(), "median": statistics.median(df.x), "p": t.pvalue, "arr": np.arange(3),
          "frame": df.head(2), "nan": float("nan"), "sqrt": math.sqrt(16), "s": json.loads('{"k": 1}')}
"""
    rows = [{"g": "a" if i % 2 else "b", "x": float(i)} for i in range(20)]
    r = run_python(code, inputs={"rows": rows}, timeout_s=60)
    assert r.ok, r.error
    assert r.stdout.strip() == "rows 20"
    assert r.result["mean"] == 9.5 and r.result["arr"] == [0, 1, 2] and r.result["nan"] is None
    assert r.result["frame"] == [{"g": "b", "x": 0.0}, {"g": "a", "x": 1.0}]
    assert r.result["sqrt"] == 4.0 and r.result["s"] == {"k": 1}
    assert r.duration_ms > 0 and r.exit_code == 0


def test_sklearn_and_statsmodels_allowed():
    code = """
import numpy as np
from sklearn.linear_model import LinearRegression
import statsmodels.api as sm
x = np.arange(10.0).reshape(-1, 1)
result = round(float(LinearRegression().fit(x, 2 * x.ravel() + 1).coef_[0]), 6)
"""
    r = run_python(code, timeout_s=60)
    assert r.ok, r.error
    assert r.result == 2.0


@pytest.mark.parametrize("code,needle", [
    ("import os", "import of 'os'"),
    ("import subprocess", "import of 'subprocess'"),
    ("from os import path", "import from 'os'"),
    ("import socket", "import of 'socket'"),
    ("import importlib", "import of 'importlib'"),
    ("import ctypes", "import of 'ctypes'"),
    ("from . import x", "relative imports"),
    ("from numpy import *", "star imports"),
    ("open('/etc/passwd').read()", "'open'"),
    ("exec('1')", "'exec'"),
    ("eval('1')", "'eval'"),
    ("compile('1', 'x', 'exec')", "'compile'"),
    ("__import__('os')", "'__import__'"),
    ("().__class__.__bases__[0].__subclasses__()", "dunder attribute"),
    ("f = lambda: 0\nf.__globals__", "dunder attribute '__globals__'"),
    ("__builtins__", "'__builtins__'"),
    ("getattr(1, 'real')", "'getattr'"),
    ("import numpy as np\nnp.load('x.npy')", "attribute 'load'"),
    ("import pandas as pd\npd.read_csv('/etc/passwd')", "attribute 'read_csv'"),
    ("import numpy as np\nnp.ctypeslib", "attribute 'ctypeslib'"),
    ("x = '{0.__class__}'.format(1)", "'__'"),
    ("import numpy as sys", "alias 'sys'"),
    ("def f(open): return open", "parameter name 'open'"),
    ("globals()", "'globals'"),
])
def test_forbidden_code_rejected(code, needle):
    problems = check_code(code)
    assert problems and any(needle in p for p in problems), problems
    r = run_python(code)
    assert r.ok is False and r.error.startswith("rejected by sandbox policy") and r.exit_code is None


def test_runtime_import_hook_is_second_layer(monkeypatch):
    """Even if the static check were bypassed, the child refuses non-allow-listed imports."""
    monkeypatch.setattr(runner, "check_code", lambda code, allowed: [])
    r = run_python("import socket\nresult = 1")
    assert r.ok is False and "not allowed in the sandbox" in r.error
    r = run_python("result = open")  # builtin removed in the child
    assert r.ok is False and "NameError" in r.error


def test_timeout_kills_process_group():
    r = run_python("while True:\n    pass", timeout_s=1)
    assert r.ok is False and r.timed_out and "timeout" in r.error
    assert r.duration_ms < 5000


def test_memory_limit_enforced():
    r = run_python("x = bytearray(3 * 1024 ** 3)\nresult = 1", memory_mb=512)
    assert r.ok is False and "MemoryError" in r.error
    r = run_python("import numpy as np\nx = np.ones((30000, 30000))\nresult = float(x.sum())", memory_mb=512, timeout_s=60)
    assert r.ok is False and "MemoryError" in r.error


def test_environment_is_stripped(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    env = sandbox_env("/tmp/x")
    assert "OPENROUTER_API_KEY" not in env and "DATABASE_URL" not in env and "HTTPS_PROXY" not in env
    # observe the real child environment by allowing `os` for this test only
    monkeypatch.setattr(runner, "check_code", lambda code, allowed: [])
    r = run_python("import os\nresult = {'env': dict(os.environ), 'cwd': os.getcwd(), 'files': os.listdir('.')}",
                   allowed_imports=("os",))
    assert r.ok, r.error
    child = r.result["env"]
    assert "OPENROUTER_API_KEY" not in child and "DATABASE_URL" not in child and "HTTPS_PROXY" not in child
    assert set(child) <= set(env) | {"LC_CTYPE"}
    assert r.result["cwd"].split("/")[-1].startswith("aos-sbx-") and r.result["files"] == []


def test_result_errors_are_reported():
    r = run_python("result = 1 / 0")
    assert r.ok is False and "ZeroDivisionError" in r.error
    r = run_python("class A: pass\nresult = A()")
    assert r.ok is False and "not JSON-serializable" in r.error
    r = run_python("print('x' * 50)\nresult = None", max_output_bytes=10)
    assert r.ok and "[stdout truncated]" in r.stdout
    assert run_python("result = (").error.startswith("rejected by sandbox policy: syntax error")
    r = run_python("result = list(range(100000))", max_result_bytes=1000)
    assert r.ok is False and r.error == "result too large"


NET_PROBE = """import socket
names = [n for _, n in socket.if_nameindex()]
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2).close()
    reached = True
except OSError as e:
    reached = False
result = {"interfaces": names, "reached": reached}
"""


@pytest.mark.skipif(not runner.network_isolation_available(), reason="this kernel does not allow a private network namespace")
def test_child_has_no_network(monkeypatch):
    """P4-S04: the child sees only a loopback in its own namespace and cannot reach any address, even
    with `socket` let through the static check (as a C-extension escape would)."""
    monkeypatch.setattr(runner, "check_code", lambda code, allowed: [])
    r = run_python(NET_PROBE, allowed_imports=("socket",), network="require")
    assert r.ok, r.error
    assert r.network_isolated is True
    assert r.result == {"interfaces": ["lo"], "reached": False}
    assert run_python("result = 1").network_isolated is True  # isolate is the default


def test_require_mode_refuses_when_isolation_is_impossible(monkeypatch):
    monkeypatch.setattr(runner, "network_isolation_available", lambda: False)
    r = run_python("result = 1", network="require")
    assert r.ok is False and "network isolation is required" in r.error and r.network_isolated is False
    r = run_python("result = 1", network="isolate")  # best effort: runs, and says it was not isolated
    assert r.ok and r.network_isolated is False
    with pytest.raises(ValueError):
        run_python("result = 1", network="sometimes")


def test_isolate_fallback_is_logged_and_recorded(monkeypatch, caplog):
    """M5: a networked fallback under `isolate` is never silent: a warning every time, network_isolated False."""
    import logging

    monkeypatch.setattr(runner, "network_isolation_available", lambda: False)
    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        r = run_python("result = 1", network="isolate")
    assert r.ok and r.network_isolated is False
    assert any("WITH network access" in rec.getMessage() for rec in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=runner.__name__):
        run_python("result = 1", network="off")
    assert not caplog.records  # `off` is an explicit development choice, not a fallback

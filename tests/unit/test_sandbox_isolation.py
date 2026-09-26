"""P4-02 (DEX-003..005): isolated worker probes and the fail-closed isolation gate.

Probes run hostile code with the Python-level policy switched OFF (``enforce_policy=False``: full
builtins, any import), so they test the OS boundary alone - what a C-extension or interpreter escape
would face. On every backend: no network, read-only filesystem except a size-limited scratch dir, no
secret visible (environment, other processes, masked files), no privilege, and bounded processes,
memory, CPU and wall clock - each denied or killed and reported.

The process backend runs where the kernel allows namespaces (skipped elsewhere). The container
backend needs Docker and an image (``ANALYSTOS_TEST_SANDBOX_IMAGE``, else analystos-sandbox:latest
or python:3.11-slim) and is marked ``integration``.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from analystos.core.config import Settings, get_settings
from analystos.sandbox import isolation, runner
from analystos.sandbox.isolation import IsolationStatus

PROCESS_OK = isolation.status(Settings(_env_file=None, sandbox_isolation="process")).available


def _container_image() -> str | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    wanted = os.environ.get("ANALYSTOS_TEST_SANDBOX_IMAGE")
    for image in ([wanted] if wanted else ["analystos-sandbox:latest", "python:3.11-slim"]):
        try:
            if subprocess.run([docker, "image", "inspect", image], capture_output=True, timeout=15).returncode == 0:
                return image
        except (OSError, subprocess.SubprocessError):
            return None
    return None


IMAGE = _container_image()
BACKENDS = [
    pytest.param("process", marks=pytest.mark.skipif(not PROCESS_OK, reason="no namespaces on this host")),
    pytest.param("container", marks=[pytest.mark.integration,
                                     pytest.mark.skipif(IMAGE is None, reason="no docker or sandbox image")]),
]


@pytest.fixture(params=BACKENDS)
def sbx(request, tmp_path):
    secret_file = tmp_path / "worker-secret.env"
    secret_file.write_text("OPENROUTER_API_KEY=sk-or-probe-secret\n")
    settings = Settings(_env_file=None, sandbox_isolation=request.param, sandbox_container_image=IMAGE or "none",
                        sandbox_pids_limit=32, sandbox_scratch_mb=8,
                        sandbox_masked_paths=[str(secret_file), str(tmp_path)])

    def probe(code: str, **kw):
        kw.setdefault("timeout_s", 20)
        kw.setdefault("memory_mb", 256)
        return runner._execute(textwrap.dedent(code), backend=request.param, enforce_policy=False, settings=settings, **kw)

    probe.backend, probe.settings, probe.secret_file = request.param, settings, secret_file
    return probe


def test_network_egress_is_impossible(sbx):
    r = sbx("""
        import socket
        attempts = {}
        for host, port in [("1.1.1.1", 53), ("127.0.0.1", 5432), ("172.17.0.1", 5432), ("10.0.0.1", 443)]:
            try:
                socket.create_connection((host, port), timeout=2).close()
                attempts[f"{host}:{port}"] = "connected"
            except OSError as e:
                attempts[f"{host}:{port}"] = type(e).__name__
        try:
            socket.getaddrinfo("example.com", 443)
            dns = "resolved"
        except OSError as e:
            dns = type(e).__name__
        result = {"interfaces": [n for _, n in socket.if_nameindex()], "attempts": attempts, "dns": dns}
    """)
    assert r.ok, r.error
    assert r.network_isolated is True and r.isolation == sbx.backend
    assert r.result["interfaces"] == ["lo"]
    assert "connected" not in r.result["attempts"].values(), r.result
    assert r.result["dns"] != "resolved"


def test_filesystem_is_read_only_except_a_bounded_scratch(sbx):
    r = sbx("""
        import os
        denied = {}
        for path in ["/probe", "/etc/probe", "/tmp/probe", "/var/tmp/probe", "/dev/shm/probe", "/usr/lib/probe",
                     os.path.join(os.path.dirname(os.getcwd()), "probe"), os.path.expanduser("~/../probe")]:
            try:
                with open(path, "w") as f:
                    f.write("x")
                denied[path] = False
            except OSError as e:
                denied[path] = e.__class__.__name__
        with open("ok.txt", "w") as f:
            f.write("scratch works")
        try:
            with open("big.bin", "wb") as f:
                for _ in range(64):
                    f.write(b"\\0" * (1024 * 1024))
            big = "written"
        except OSError as e:
            big = e.__class__.__name__
        result = {"denied": denied, "scratch": open("ok.txt").read(), "big": big, "cwd": os.getcwd()}
    """, fsize_mb=4)
    assert r.ok, r.error
    assert all(r.result["denied"].values()), r.result["denied"]
    assert r.result["scratch"] == "scratch works"
    assert r.result["big"] != "written"  # 64 MB > the 8 MB scratch / 4 MB file-size limit


def test_secrets_are_not_visible(sbx, monkeypatch):
    monkeypatch.setenv("AOS_PROBE_SECRET", "sk-or-probe-secret")
    r = sbx(f"""
        import os
        pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
        environs = {{}}
        for p in pids:
            try:
                environs[p] = open(f"/proc/{{p}}/environ", "rb").read().decode(errors="replace")
            except OSError as e:
                environs[p] = e.__class__.__name__
        try:
            masked = open({str(sbx.secret_file)!r}).read()
        except OSError as e:
            masked = e.__class__.__name__
        result = {{"env": dict(os.environ), "pids": pids, "environs": environs, "masked": masked,
                  "parent": os.getppid()}}
    """)
    assert r.ok, r.error
    blob = json.dumps(r.result)
    assert "sk-or-probe-secret" not in blob and "AOS_PROBE_SECRET" not in blob
    assert set(r.result["env"]) <= set(runner.sandbox_env("/x")) | {"LC_CTYPE"}  # neither worker nor image ENV
    assert r.result["pids"] == [1]  # a private PID namespace: no other process (or its environment) exists
    assert r.result["masked"] in ("", "FileNotFoundError", "PermissionError")


def test_no_privilege_survives(sbx):
    r = sbx("""
        import ctypes, os
        status = dict(line.split(":", 1) for line in open("/proc/self/status").read().splitlines() if ":" in line)
        libc = ctypes.CDLL(None, use_errno=True)
        remount = libc.mount(b"none", b"/", None, 0x20 | 0x1000, None)  # MS_REMOUNT|MS_BIND: make / writable again
        try:
            os.setuid(0)
            setuid = "succeeded"
        except OSError as e:
            setuid = e.__class__.__name__
        result = {"cap_eff": status["CapEff"].strip(), "cap_bnd": status["CapBnd"].strip(),
                  "no_new_privs": status["NoNewPrivs"].strip(), "remount": remount, "setuid": setuid,
                  "uid": os.getuid()}
    """)
    assert r.ok, r.error
    assert int(r.result["cap_eff"], 16) == 0 and int(r.result["cap_bnd"], 16) == 0
    assert r.result["no_new_privs"] == "1" and r.result["remount"] == -1
    if sbx.backend == "container" or os.geteuid() == 0:  # real root drops to an unprivileged uid
        assert r.result["setuid"] != "succeeded" and r.result["uid"] != 0


def test_fork_bomb_is_bounded_and_leaves_nothing_behind(sbx):
    r = sbx("""
        import os, time
        n = 0
        try:
            while n < 10000:
                if os.fork() == 0:
                    time.sleep(60)
                    os._exit(0)
                n += 1
        except OSError as e:
            err = e.__class__.__name__
        result = {"forked": n, "error": err, "uid": os.getuid()}
    """)
    assert r.ok, r.error
    assert r.result["forked"] < sbx.settings.sandbox_pids_limit and r.result["error"] == "BlockingIOError"
    if sbx.backend == "process" and os.geteuid() == 0:  # the namespace died with its init: no sleeper survives
        uid = str(r.result["uid"])
        left = [p for p in os.listdir("/proc") if p.isdigit() and _uid_of(p) == uid]
        assert not left


def _uid_of(pid: str) -> str | None:
    try:
        return next(line.split()[1] for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith("Uid:"))
    except (OSError, StopIteration):
        return None


def test_memory_blowup_is_stopped(sbx):
    r = sbx("x = bytearray(2 * 1024 ** 3)\nresult = len(x)", memory_mb=256)
    assert r.ok is False and ("MemoryError" in r.error or "SIGKILL" in r.error), r.error
    r = sbx("chunks = []\nwhile True:\n    chunks.append(bytearray(8 * 1024 ** 2))", memory_mb=256)
    assert r.ok is False and ("MemoryError" in r.error or "SIGKILL" in r.error), r.error


def test_cpu_spin_and_wall_clock_are_killed(sbx):
    r = sbx("while True:\n    pass", timeout_s=2)
    assert r.ok is False and (r.timed_out or "CPU" in (r.error or "")), r.error
    r = sbx("import time\ntime.sleep(120)", timeout_s=2)
    assert r.ok is False and r.timed_out and "timeout" in r.error
    assert r.duration_ms < 2000 + 1000 * (sbx.settings.sandbox_container_startup_seconds + 5)
    if sbx.backend == "container":
        docker = shutil.which("docker")
        left = subprocess.run([docker, "ps", "-q", "--filter", "label=analystos.sandbox=1"], capture_output=True,
                              text=True, timeout=15).stdout.split()
        assert not left, left


@pytest.mark.skipif(not PROCESS_OK, reason="no namespaces on this host")
def test_half_built_process_sandbox_never_runs_the_code():
    """A failure while building the sandbox (here: an invalid scratch size) aborts before any user code."""
    s = Settings(_env_file=None, sandbox_isolation="process").model_copy(update={"sandbox_scratch_mb": -5})
    r = runner._execute("print('ran')\nresult = 'ran'", backend="process", settings=s, fsize_mb=-5)
    assert r.ok is False and "isolation setup failed" in r.error and r.result is None and "ran" not in r.stdout
    assert r.network_isolated is False


@pytest.mark.integration
@pytest.mark.skipif(IMAGE is None or "sandbox" not in (IMAGE or ""), reason="needs the analystos-sandbox image")
def test_container_image_runs_the_allowed_numeric_stack(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_SANDBOX_ISOLATION", "container")
    monkeypatch.setenv("ANALYSTOS_SANDBOX_CONTAINER_IMAGE", IMAGE)
    get_settings.cache_clear()
    isolation.reset_cache()
    try:
        r = runner.run_python("import numpy as np, pandas as pd\nfrom scipy import stats\nimport sklearn, statsmodels\n"
                              "df = pd.DataFrame(inputs['rows'])\nresult = {'mean': float(df.x.mean()), "
                              "'p': float(stats.ttest_1samp(df.x, 0).pvalue) < 0.05}",
                              inputs={"rows": [{"x": float(i)} for i in range(1, 21)]}, timeout_s=60)
        assert r.ok, r.error
        assert r.isolation == "container" and r.result == {"mean": 10.5, "p": True}
    finally:
        get_settings.cache_clear()
        isolation.reset_cache()


# -- the gate -------------------------------------------------------------------------------------

@pytest.fixture()
def gate(monkeypatch):
    """Settings from the environment, fresh gate cache, both backend probes under the test's control."""
    state = {"container": (False, "no docker CLI on this host"), "process": (False, "unshare(0x6e020000) failed: EPERM")}
    monkeypatch.setattr(isolation, "_probe_container", lambda settings: state["container"])
    monkeypatch.setattr(isolation, "_probe_process", lambda: state["process"])

    def configure(mode: str, env: str = "dev") -> IsolationStatus:
        monkeypatch.setenv("ANALYSTOS_SANDBOX_ISOLATION", mode)
        monkeypatch.setenv("ANALYSTOS_ENV", env)
        get_settings.cache_clear()
        isolation.reset_cache()
        return isolation.status()

    configure.state = state
    yield configure
    get_settings.cache_clear()
    isolation.reset_cache()


@pytest.mark.parametrize("mode", ["auto", "process", "container"])
def test_gate_refuses_when_no_isolation_can_be_established(gate, mode):
    st = gate(mode)
    assert st.available is False and st.backend == "none" and "refused" in st.detail
    r = runner.run_python("result = 1")
    assert r.ok is False and r.isolation == "unavailable" and r.error == st.detail
    assert r.exit_code is None  # nothing was started


def test_gate_refusal_is_visible_in_tool_policy_and_health(gate, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.tools.registry import _sandbox_available

    gate("auto")
    assert _sandbox_available() is False
    body = TestClient(app).get("/api/health").json()
    assert body["checks"]["sandbox"]["ok"] is False and body["checks"]["sandbox"]["available"] is False
    assert "unavailable" in body["checks"]["sandbox"]["detail"]
    gate.state["process"] = (True, "namespaces")
    gate("auto")
    assert TestClient(app).get("/api/health").json()["checks"]["sandbox"] | {"enforced": None} == {
        "ok": True, "mode": "auto", "backend": "process", "available": True, "isolated": True, "enforced": None,
        "detail": "namespaces"}


def test_auto_prefers_the_container_then_the_process_backend(gate):
    gate.state["process"] = (True, "namespaces")
    assert gate("auto").backend == "process"
    gate.state["container"] = (True, "docker run")
    assert gate("auto").backend == "container"
    assert gate("process").backend == "process"  # an explicit mode never switches backend


def test_off_is_development_only_and_reported_unisolated(gate):
    st = gate("off")
    assert st.available is True and st.isolated is False and "DEVELOPMENT ONLY" in st.detail
    assert st.enforced["network"] is False and st.enforced["memory"] is True
    r = runner.run_python("result = 6 * 7")
    assert r.ok and r.result == 42 and r.isolation == "none" and r.network_isolated is False
    st = gate("off", env="production")
    assert st.available is False and "production" in st.detail
    assert runner.run_python("result = 1").isolation == "unavailable"


def test_invalid_mode_is_refused():
    with pytest.raises(ValueError):
        Settings(_env_file=None, sandbox_isolation="sometimes")
    st = isolation.status(Settings(_env_file=None).model_copy(update={"sandbox_isolation": "sometimes"}), refresh=True)
    assert st.available is False and "must be one of" in st.detail


SECCOMP_PROBE = r"""
import ctypes, json, os, sys
# Install a seccomp filter that makes unshare(2) fail with EPERM - what a restrictive container runtime does.
nr = {"x86_64": 272, "aarch64": 97}[os.uname().machine]
class F(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]
class P(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(F))]
prog = (F * 4)(F(0x20, 0, 0, 0), F(0x15, 0, 1, nr), F(0x06, 0, 0, 0x00050000 | 1), F(0x06, 0, 0, 0x7fff0000))
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(38, 1, 0, 0, 0) == 0
assert libc.prctl(22, 2, ctypes.byref(P(4, prog)), 0, 0) == 0, ctypes.get_errno()
from analystos.sandbox import isolation, runner
st = isolation.status()
r = runner.run_python("result = 1")
print(json.dumps({"status": st.model_dump(), "result": r.model_dump()}))
"""


@pytest.mark.skipif(not PROCESS_OK or platform.machine() not in ("x86_64", "aarch64"), reason="needs namespaces to take away")
def test_gate_fails_closed_when_seccomp_blocks_namespaces(tmp_path):
    """Live: the same host, but with unshare(2) blocked by seccomp and no container image - the gate
    detects it and refuses; nothing runs unisolated."""
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("ANALYSTOS_SANDBOX")},
           "ANALYSTOS_SANDBOX_ISOLATION": "auto", "ANALYSTOS_SANDBOX_CONTAINER_IMAGE": "aos-no-such-image:0",
           "PYTHONPATH": str(Path(runner.__file__).resolve().parents[2])}
    out = subprocess.run([sys.executable, "-c", SECCOMP_PROBE], capture_output=True, text=True, timeout=120, env=env,
                         cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["status"]["available"] is False and report["status"]["backend"] == "none"
    assert "process:" in report["status"]["detail"] and "container:" in report["status"]["detail"]
    assert report["result"]["ok"] is False and report["result"]["isolation"] == "unavailable"

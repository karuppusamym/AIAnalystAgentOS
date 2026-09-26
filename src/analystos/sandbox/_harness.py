"""Child-process harness for `analystos.sandbox.runner`. Runs as `python -I -B _harness.py` (process
isolation) or `python -I -B -c <this source>` inside a sandbox container.

Reads {"code", "inputs", "allowed", "denied_builtins", "max_output", "max_result", "isolation",
"rlimits", "enforce_policy"} as JSON on stdin. With ``isolation`` (process mode) the runner has
already unshared the mount/network/PID/IPC/UTS namespaces (plus a user namespace when unprivileged);
this harness forks the PID namespace's init, which mounts a private /proc, masks secret paths,
makes the whole filesystem read-only, mounts a size-limited tmpfs scratch directory, applies the
rlimits and drops every capability (and, when it ran as real root, to a one-off uid) before any
user code runs. Setup failures abort before the code runs (fail closed).

Then it executes the code with a reduced builtins dict and an import hook that only admits
allow-listed top-level modules, and writes one line `<marker><json envelope>` to the real stdout.
Must not import anything from `analystos` (the child should not need the application on its path).
"""
import builtins
import ctypes
import ctypes.util
import datetime
import io
import json
import math
import os
import signal
import sys

MARKER = "\x1e__AOS_SANDBOX_RESULT__\x1e"
SETUP_FAILED_EXIT = 70

MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_RDONLY = 0x2, 0x4, 0x8, 0x1
MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 0x20, 0x1000, 0x4000, 0x40000
MOUNT_ATTR_RDONLY, MOUNT_ATTR_NOSUID = 0x1, 0x2
AT_FDCWD, AT_RECURSIVE = -100, 0x8000
SYS_MOUNT_SETATTR = 442  # same number on every architecture (unified syscall table since 5.1)
PR_SET_PDEATHSIG, PR_CAPBSET_DROP, PR_SET_NO_NEW_PRIVS = 1, 24, 38
CAP_LAST = 63


class _MountAttr(ctypes.Structure):
    _fields_ = [("attr_set", ctypes.c_uint64), ("attr_clr", ctypes.c_uint64),
                ("propagation", ctypes.c_uint64), ("userns_fd", ctypes.c_uint64)]


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


def _check(rc, what):
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")


def _enc(s):
    return s.encode() if s is not None else None


def _mount(libc, source, target, fstype, flags, data=None):
    _check(libc.mount(_enc(source), _enc(target), _enc(fstype), ctypes.c_ulong(flags), _enc(data)),
           f"mount {fstype or source} on {target}")


def _initial_user_namespace():
    try:
        with open("/proc/self/uid_map") as f:
            return f.read().split() == ["0", "0", "4294967295"]
    except OSError:
        return False


def _setup_process_isolation(iso):
    """In the PID namespace's init, before any user code: returns what was established."""
    libc = _libc()
    _check(libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0), "prctl(PDEATHSIG)")
    privileged = _initial_user_namespace()
    _mount(libc, None, "/", None, MS_REC | MS_PRIVATE)  # nothing propagates back to the host
    _mount(libc, "proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)  # only this namespace's pids
    masked = []
    for path in iso.get("mask") or []:
        if os.path.isdir(path) and not os.path.islink(path):
            _mount(libc, "tmpfs", path, "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_RDONLY, "size=4k,mode=000")
            masked.append(path)
        elif os.path.isfile(path):
            _mount(libc, "/dev/null", path, None, MS_BIND)
            masked.append(path)
    attr = _MountAttr(MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID, 0, 0, 0)
    _check(libc.syscall(SYS_MOUNT_SETATTR, AT_FDCWD, b"/", AT_RECURSIVE, ctypes.byref(attr), ctypes.sizeof(attr)),
           "mount_setattr(/, read-only, recursive)")
    uid = int(iso["uid"]) if privileged else 0
    scratch = iso["scratch"]
    opts = f"size={int(iso['scratch_mb'])}m,mode=0700" + (f",uid={uid},gid={uid}" if privileged else "")
    _mount(libc, "tmpfs", scratch, "tmpfs", MS_NOSUID | MS_NODEV, opts)
    os.chdir(scratch)
    return {"privileged": privileged, "uid": uid, "masked": masked}


def _apply_rlimits(lim):
    import resource

    if not lim:
        return
    mem = int(lim["memory_mb"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (int(lim["cpu_s"]), int(lim["cpu_s"]) + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(lim["nofile"]), int(lim["nofile"])))
    fs = int(lim["fsize_mb"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if lim.get("nproc"):
        resource.setrlimit(resource.RLIMIT_NPROC, (int(lim["nproc"]), int(lim["nproc"])))
    os.umask(0o077)


def _drop_privileges(setup):
    """No capability survives: bounding set emptied, then a one-off uid (real root) or an empty
    capability set (root only inside a user namespace), then no_new_privs."""
    libc = _libc()
    for cap in range(CAP_LAST + 1):
        libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)  # EINVAL past the kernel's last cap is expected
    if setup["privileged"]:
        uid = setup["uid"]
        os.setgroups([])
        os.setresgid(uid, uid, uid)
        os.setresuid(uid, uid, uid)
    else:
        hdr, data = _CapHeader(0x20080522, 0), (_CapData * 2)()
        _check(libc.capset(ctypes.byref(hdr), ctypes.byref(data)), "capset(empty)")
    _check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(NO_NEW_PRIVS)")


def _sanitize(obj, depth=0):
    if depth > 50:
        raise ValueError("result nested too deeply")
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _sanitize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_sanitize(v, depth + 1) for v in obj]
    if isinstance(obj, datetime.datetime | datetime.date):
        return obj.isoformat()
    mod = type(obj).__module__ or ""
    if mod.startswith("pandas"):
        if hasattr(obj, "to_dict") and hasattr(obj, "columns"):
            return _sanitize(obj.to_dict(orient="records"), depth + 1)
        if hasattr(obj, "tolist"):
            return _sanitize(obj.tolist(), depth + 1)
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
    if mod.startswith("polars") and hasattr(obj, "to_dicts"):
        return _sanitize(obj.to_dicts(), depth + 1)
    if mod.startswith("numpy") and hasattr(obj, "tolist"):
        return _sanitize(obj.tolist(), depth + 1)
    if hasattr(obj, "item") and callable(obj.item):
        return _sanitize(obj.item(), depth + 1)
    if mod == "decimal":
        return float(obj)
    raise TypeError(f"result of type {type(obj).__name__} is not JSON-serializable")


def _emit(envelope):
    sys.__stdout__.write(MARKER + json.dumps(envelope))
    sys.__stdout__.flush()


def _relay_child(pid):
    """The PID namespace's init runs the code; this process (outside it) only waits and mirrors its end."""
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
    os._exit(os.waitstatus_to_exitcode(status))


def run(payload, setup=None):
    enforce = payload.get("enforce_policy", True)
    allowed = set(payload["allowed"])
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name.split(".")[0] not in allowed:
            raise ImportError(f"import of {name!r} is not allowed in the sandbox")
        return real_import(name, globals, locals, fromlist, level)

    if enforce:
        safe = {k: getattr(builtins, k) for k in dir(builtins) if k not in set(payload["denied_builtins"])}
        safe["__import__"] = guarded_import
    else:  # isolation probes only: the OS boundary is what is under test
        safe = builtins.__dict__
    real_stdout = sys.stdout
    buf = io.StringIO()
    sys.stdout = buf
    sys.stderr = buf
    env = {"__builtins__": safe, "__name__": "__sandbox__", "inputs": payload.get("inputs") or {}}
    ok, err, result = True, None, None
    try:
        exec(compile(payload["code"], "<sandbox>", "exec"), env)  # noqa: S102 - this *is* the sandbox
        result = _sanitize(env.get("result"))
    except BaseException as e:  # noqa: BLE001 - report every failure, including MemoryError/SystemExit
        ok, err, result = False, f"{type(e).__name__}: {e}", None
    finally:
        sys.stdout, sys.stderr = real_stdout, sys.__stderr__
    out = buf.getvalue()
    limit = int(payload.get("max_output", 1_000_000))
    if len(out) > limit:
        out = out[:limit] + "\n[stdout truncated]"
    try:
        line = json.dumps({"ok": ok, "stdout": out, "result": result, "error": err, "isolation": setup}, allow_nan=False)
    except (TypeError, ValueError) as e:
        line = json.dumps({"ok": False, "stdout": out, "result": None, "error": f"result serialization failed: {e}"})
    if len(line) > int(payload.get("max_result", 10_000_000)) + len(out):
        line = json.dumps({"ok": False, "stdout": out[:1000], "result": None, "error": "result too large"})
    real_stdout.write(MARKER + line)
    real_stdout.flush()


def main():
    payload = json.loads(sys.stdin.read())
    iso = payload.get("isolation")
    setup = None
    if iso:
        pid = os.fork()  # first child of an unshared PID namespace: its init
        if pid:
            _relay_child(pid)
        try:
            setup = _setup_process_isolation(iso)
            _apply_rlimits(payload.get("rlimits"))
            _drop_privileges(setup)
        except BaseException as e:  # noqa: BLE001 - never run code in a half-built sandbox
            _emit({"ok": False, "stdout": "", "result": None, "setup_failed": True,
                   "error": f"sandbox isolation setup failed: {type(e).__name__}: {e}"})
            os._exit(SETUP_FAILED_EXIT)
    else:
        _apply_rlimits(payload.get("rlimits"))
    if payload.get("env") is not None:  # container: drop the image's ENV too, not only the worker's
        os.environ.clear()
        os.environ.update(payload["env"])
    run(payload, setup)


if __name__ == "__main__":
    main()

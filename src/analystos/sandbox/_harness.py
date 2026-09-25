"""Child-process harness for `analystos.sandbox.runner`. Runs as `python -I _harness.py`.

Reads {"code", "inputs", "allowed", "denied_builtins", "max_output"} as JSON on stdin, executes the
code with a reduced builtins dict and an import hook that only admits allow-listed top-level
modules, then writes one line `<marker><json envelope>` to the real stdout. Must not import
anything from `analystos` (the child should not need the application on its path).
"""
import builtins
import datetime
import io
import json
import math
import sys

MARKER = "\x1e__AOS_SANDBOX_RESULT__\x1e"


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


def main():
    payload = json.loads(sys.stdin.read())
    allowed = set(payload["allowed"])
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name.split(".")[0] not in allowed:
            raise ImportError(f"import of {name!r} is not allowed in the sandbox")
        return real_import(name, globals, locals, fromlist, level)

    safe = {k: getattr(builtins, k) for k in dir(builtins) if k not in set(payload["denied_builtins"])}
    safe["__import__"] = guarded_import
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
        line = json.dumps({"ok": ok, "stdout": out, "result": result, "error": err}, allow_nan=False)
    except (TypeError, ValueError) as e:
        line = json.dumps({"ok": False, "stdout": out, "result": None, "error": f"result serialization failed: {e}"})
    if len(line) > int(payload.get("max_result", 10_000_000)) + len(out):
        line = json.dumps({"ok": False, "stdout": out[:1000], "result": None, "error": "result too large"})
    real_stdout.write(MARKER + line)
    real_stdout.flush()


if __name__ == "__main__":
    main()

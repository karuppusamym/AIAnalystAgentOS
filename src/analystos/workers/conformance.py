"""The conformance probe job (ADR-0022 decision 6, tests/conformance/worker).

Served only by a worker started with `--conformance`. Each action tries one thing a pool must allow
or refuse; the suite asserts the pool's answer. Every action stays inside the job's own budget and
sandbox, so the probe cannot do anything a real job could not already try.
"""
from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

from analystos.core.errors import EgressBlocked, InvalidInput, error_from_dict
from analystos.workers.handlers import JobContext


def probe(spec: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    action, args = spec["action"], spec.get("args") or {}
    if action == "echo":
        body = json.dumps(ctx.envelope, sort_keys=True).encode()
        if "envelope" in ctx.declared:
            ctx.write_output("envelope", body, kind="json", media_type="application/json")
        return {"envelope": ctx.envelope}
    if action == "read":
        return {"read": {aid: len(ctx.read_input(aid)) for aid in args.get("artifact_ids", sorted(ctx.inputs))}}
    if action == "write":
        for name, text in (args.get("outputs") or {}).items():
            ctx.write_output(name, str(text).encode(), kind="text", media_type="text/plain")
        return {"written": sorted(args.get("outputs") or {})}
    if action == "egress":
        attempts = []
        for host, port in args.get("targets", []):
            try:
                socket.create_connection((host, int(port)), timeout=2).close()
                attempts.append({"target": f"{host}:{port}", "connected": True})
            except OSError as exc:
                attempts.append({"target": f"{host}:{port}", "connected": False, "error": type(exc).__name__})
        if any(a["connected"] for a in attempts):
            return {"attempts": attempts}
        raise EgressBlocked("every egress attempt was refused", details={"attempts": attempts})
    if action == "cpu":
        end = time.process_time() + float(args.get("seconds", 1))
        x = 0
        while time.process_time() < end:
            x += 1
        return {"spins": x}
    if action == "memory":
        block = bytearray(int(args["mb"]) * 1024 * 1024)
        for i in range(0, len(block), 4096):
            block[i] = 1
        return {"allocated_mb": int(args["mb"])}
    if action == "sleep":
        time.sleep(float(args["seconds"]))
        return {"slept": float(args["seconds"])}
    if action == "big_output":
        ctx.write_output(args.get("name", "result"), b"x" * int(args["bytes"]), kind="blob")
        return {"bytes": int(args["bytes"])}
    if action == "raise":
        if args.get("code") == "plain":
            raise ValueError(args.get("message", "a plain exception"))
        raise error_from_dict({"code": args.get("code", "invalid_input"), "message": args.get("message", "probe"),
                               "details": args.get("details") or {}})
    if action == "trials":
        for _ in range(int(args["n"])):
            ctx.trial()
        return {"trials": ctx.trials}
    if action == "environment":
        return {"keys": sorted(os.environ), "network": sorted(n for _, n in socket.if_nameindex())}
    raise InvalidInput(f"unknown probe action {action}")

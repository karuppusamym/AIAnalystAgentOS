"""Deterministic fakes for the model transport (no network)."""
from __future__ import annotations

import json
from collections.abc import Callable


class FakeTransport:
    def __init__(self, chat: Callable[[dict], dict | Exception] | None = None, decide: Callable[[dict], dict | Exception] | None = None):
        self._chat = chat or (lambda p: {"choices": [{"message": {"content": "{}"}}], "usage": {}})
        self._decide = decide or (lambda p: {"answers": {}, "usage": {}})
        self.chat_calls: list[dict] = []
        self.decide_calls: list[dict] = []

    def chat(self, *, base_url, api_key, payload, timeout):
        self.chat_calls.append(payload)
        out = self._chat(payload)
        if isinstance(out, Exception):
            raise out
        return out

    def decide(self, *, base_url, api_key, payload, timeout):
        self.decide_calls.append(payload)
        out = self._decide(payload)
        if isinstance(out, Exception):
            raise out
        return out


def chat_json(obj, model="anthropic/claude-sonnet-5", cost=0.001):
    return {"model": model, "choices": [{"message": {"content": json.dumps(obj)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": cost}}


def _numeric(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


class FakeRedis:
    """The few Redis commands the budget counters use, in memory (unit tests need no services)."""

    def __init__(self) -> None:
        import threading

        self.data: dict[str, float] = {}
        self.commands: list[str] = []
        self.expiry: dict[str, int] = {}
        self._lock = threading.Lock()  # a Lua script runs atomically on Redis; the stand-in serialises it

    def ping(self):
        return True

    def get(self, key):
        self.commands.append("GET")
        if key not in self.data:
            return None
        value = self.data[key]
        return (repr(value) if isinstance(value, float) else str(value)).encode()

    def set(self, key, value, nx=False, ex=None):
        self.commands.append("SET")
        if nx and key in self.data:
            return None
        self.data[key] = float(value) if isinstance(value, (int, float)) or _numeric(value) else value
        if ex:
            self.expiry[key] = int(ex)
        return True

    def delete(self, key):
        self.data.pop(key, None)
        self.expiry.pop(key, None)

    def ttl(self, key):
        return self.expiry.get(key, -2) if key in self.data else -2

    def incrbyfloat(self, key, delta):
        self.commands.append("INCRBYFLOAT")
        self.data[key] = self.data.get(key, 0.0) + float(delta)
        return self.data[key]

    def register_script(self, source):
        if "RESERVE_SPEND" in source:
            return self._reserve

        def run(keys, args):
            self.commands.append("EVALSHA")
            key = keys[0]
            if key not in self.data:
                return None
            return repr(self.incrbyfloat(key, args[0])).encode()

        return run

    def _reserve(self, keys, args):
        """The budget_counters._RESERVE script: check every cap, then add to every counter, atomically."""
        self.commands.append("EVALSHA")
        with self._lock:
            amount = float(args[0])
            for i, key in enumerate(keys, start=1):
                if key not in self.data:
                    return [b"seed", str(i).encode()]
                cap = float(args[2 * i - 1])
                if cap >= 0 and self.data[key] + amount > cap + 1e-12:
                    return [b"cap", str(i).encode(), repr(self.data[key]).encode()]
            out = [b"ok"]
            for i, key in enumerate(keys, start=1):
                self.data[key] += amount
                self.expiry[key] = int(args[2 * i])
                out.append(repr(self.data[key]).encode())
            return out

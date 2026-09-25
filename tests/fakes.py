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


class FakeRedis:
    """The few Redis commands the budget counters use, in memory (unit tests need no services)."""

    def __init__(self) -> None:
        self.data: dict[str, float] = {}
        self.commands: list[str] = []

    def ping(self):
        return True

    def get(self, key):
        self.commands.append("GET")
        return None if key not in self.data else repr(self.data[key]).encode()

    def set(self, key, value, nx=False, ex=None):
        self.commands.append("SET")
        if nx and key in self.data:
            return None
        self.data[key] = float(value)
        return True

    def incrbyfloat(self, key, delta):
        self.commands.append("INCRBYFLOAT")
        self.data[key] = self.data.get(key, 0.0) + float(delta)
        return self.data[key]

    def register_script(self, _source):
        def run(keys, args):
            self.commands.append("EVALSHA")
            key = keys[0]
            if key not in self.data:
                return None
            return repr(self.incrbyfloat(key, args[0])).encode()

        return run

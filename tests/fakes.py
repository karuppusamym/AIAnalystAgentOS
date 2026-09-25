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

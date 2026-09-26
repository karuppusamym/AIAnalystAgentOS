"""CTX-005 compiled-context reuse (no services): a context compiled for a purpose is reused within the
same run or Ask thread while the knowledge version, the scope and the inputs are unchanged, and
compiled afresh when any of them changes; nothing is cached without a run/thread or a version."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.agents import common
from analystos.context.compiler import CompiledContext
from analystos.contracts.platform import ContextSettings, LLMSettings, PlatformSettings
from analystos.contracts.policy import DataScope, WorkspacePolicyDoc


@pytest.fixture
def world(monkeypatch):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=False), context=ContextSettings())
    version = {"v": "kv:1"}
    compiled: list[str] = []

    def fake_compile(ctx, purpose, profile, settings, *, required, **kw):
        compiled.append(purpose)
        return CompiledContext(purpose=purpose, header={}, body={**required, "glossary": ["x"]}), True

    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    monkeypatch.setattr("analystos.context.version.workspace_knowledge_version", lambda ws, policy=None: version["v"])
    monkeypatch.setattr(common, "_compile", fake_compile)
    monkeypatch.setattr(common, "context_header", lambda ctx: {"workspace": "w"})
    return SimpleNamespace(version=version, compiled=compiled)


def _ctx(run_id="run1", thread_id=None, assets=("s.incident",)):
    scope = DataScope(workspace_id="ws1", user_id="u1", role="analyst", assets=list(assets))
    return SimpleNamespace(workspace=SimpleNamespace(id="ws1"), policy=WorkspacePolicyDoc(), scope=scope,
                           run=SimpleNamespace(id=run_id, objective="What drives SLA breaches?") if run_id else None,
                           thread_id=thread_id)


def _compile(ctx, purpose="hypothesis_generation", required=None):
    return common.compile_for(ctx, purpose, required or {"objective": "What drives SLA breaches?"}, catalog=[{"asset": "s.incident"}])


def test_repeated_prompt_in_a_run_reuses_the_compiled_context(world):
    ctx = _ctx()
    first = _compile(ctx)
    again = _compile(ctx)
    assert world.compiled == ["hypothesis_generation"] and again.body == first.body
    again.body["glossary"].append("mutated")  # a hit is a copy: the cached context is not shared
    assert _compile(ctx).body["glossary"] == ["x"]
    stats = common.compiled_context_stats()["hypothesis_generation"]
    assert stats["compiled"] == 1 and stats["reused"] == 2 and stats["chars_reused"] > 0


def test_a_change_of_knowledge_scope_inputs_or_run_compiles_afresh(world):
    _compile(_ctx())
    world.version["v"] = "kv:2"  # a glossary edit, a pack revision or a settings change
    _compile(_ctx())
    _compile(_ctx(assets=("s.incident", "s.change_request")))  # another scope
    _compile(_ctx(), required={"objective": "Why are P1 incidents slow?"})  # other inputs
    _compile(_ctx(), purpose="planning")  # another purpose
    _compile(_ctx(run_id="run2"))  # another run
    assert len(world.compiled) == 6


def test_ask_threads_reuse_within_the_thread_only(world):
    _compile(_ctx(run_id=None, thread_id="ask_1"), purpose="sql_generation", required={"question": "q"})
    _compile(_ctx(run_id=None, thread_id="ask_1"), purpose="sql_generation", required={"question": "q"})
    _compile(_ctx(run_id=None, thread_id="ask_2"), purpose="sql_generation", required={"question": "q"})
    assert world.compiled == ["sql_generation", "sql_generation"]


def test_nothing_is_cached_without_a_session_or_a_knowledge_version(world, monkeypatch):
    _compile(_ctx(run_id=None))
    _compile(_ctx(run_id=None))
    assert len(world.compiled) == 2
    monkeypatch.setattr("analystos.context.version.workspace_knowledge_version", lambda ws, policy=None: None)
    _compile(_ctx())
    _compile(_ctx())
    assert len(world.compiled) == 4


def test_incomplete_contexts_are_not_kept_and_entries_expire(world, monkeypatch):
    monkeypatch.setattr(common, "_compile", lambda ctx, purpose, profile, settings, *, required, **kw: (
        world.compiled.append(purpose) or CompiledContext(purpose=purpose, header={}, body=dict(required)), False))
    _compile(_ctx())
    _compile(_ctx())
    assert len(world.compiled) == 2  # knowledge failed to load: compiled again next time
    cache = common.CompiledContextCache(ttl=-1)
    cache.put("k", "p", CompiledContext(purpose="p", header={}, body={}))
    assert cache.get("k", "p") is None  # past its TTL

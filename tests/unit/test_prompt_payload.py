"""P4-C05 (structured prompt trimming) and P4-C06 prompt parts (dialect fill, prompt version hash)."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.agents import common
from analystos.agents.prompts import PROMPTS, prompt, prompt_version_id
from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.contracts.policy import WorkspacePolicyDoc
from analystos.llm.cache import ResponseCache
from analystos.llm.router import CallContext, ModelRouter

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


class Sink:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)

    def check_budget(self, ctx, purpose):
        return None


def big_catalog(tables=20, columns=40):
    return [{"asset": f"sn.table_{t}", "business_name": None, "row_count": 1000,
             "columns": [{"name": f"col_{t}_{c}", "type": "text", "semantic_type": "categorical" if c % 2 else "id",
                          "meaning": "free text description " * 3} for c in range(columns)]}
            for t in range(tables)]


# --------------------------------------------------------------------- P4-C05
def test_oversize_payload_trims_whole_entries_and_stays_valid_json():
    catalog = big_catalog()
    catalog[7]["columns"][0]["name"] = "resolution_hours"  # the only table relevant to the objective
    payload = {"objective": "what drives resolution hours", "catalog": catalog}
    assert len(common.compact_json(payload)) > 100_000
    out = common.fit_payload(payload, max_chars=6_000)
    text = common.compact_json(out)
    assert len(text) <= 6_000
    parsed = json.loads(text)  # never cut mid-JSON
    kept = {t["asset"] for t in parsed["catalog"]}
    assert "sn.table_7" in kept  # least relevant tables go first
    omitted = parsed["omitted"]
    assert set(omitted["catalog_tables"]) | kept == {t["asset"] for t in catalog}
    assert not kept & set(omitted["catalog_tables"])
    for table in parsed["catalog"]:  # surviving tables keep whole column entries only
        assert all(set(c) == {"name", "type", "semantic_type", "meaning"} for c in table["columns"])
    assert payload["catalog"] is catalog and len(catalog) == 20  # caller's payload untouched


def test_single_huge_table_drops_whole_columns_least_useful_first():
    catalog = big_catalog(tables=1, columns=200)
    catalog[0]["columns"][199]["name"] = "priority"
    out = common.fit_payload({"objective": "incidents by priority", "catalog": catalog}, max_chars=3_000)
    parsed = json.loads(common.compact_json(out))
    names = [c["name"] for c in parsed["catalog"][0]["columns"]]
    assert "priority" in names and len(names) < 200
    assert len(parsed["omitted"]["catalog_columns"]["sn.table_0"]) == 200 - len(names)
    assert len(common.compact_json(out)) <= 3_000


def test_payload_within_budget_is_unchanged():
    payload = {"objective": "x", "catalog": big_catalog(1, 2)}
    assert common.fit_payload(payload, max_chars=100_000) is payload


def _ctx(router, policy=None):
    return SimpleNamespace(router=router, policy=policy or WorkspacePolicyDoc(), run=SimpleNamespace(objective="resolution"),
                           call_ctx=lambda exclude_families=None: CallContext(workspace_id="ws1", run_id="run1",
                                                                              exclude_families=exclude_families or []),
                           say=lambda *a, **k: None)


def _router(sink, transport, max_prompt_tokens=16_000):
    platform = PlatformSettings(llm=LLMSettings(max_prompt_tokens=max_prompt_tokens, cache_enabled=False))
    return ModelRouter(transport=transport, sink=sink, api_key_lookup=KEY.get, max_retries=0,
                       settings_provider=lambda: platform, cache=ResponseCache(None)), platform


def test_llm_json_sends_valid_json_with_omissions_instead_of_truncating(monkeypatch):
    sink, transport = Sink(), FakeTransport(chat=lambda p: chat_json({"hypotheses": []}))
    router, platform = _router(sink, transport, max_prompt_tokens=3_000)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    data, model = common.llm_json(_ctx(router), "hypothesis_generation", "hypothesis_generation.v1",
                                  {"objective": "resolution", "catalog": big_catalog()})
    assert data == {"hypotheses": []}
    sent = transport.chat_calls[0]["messages"][1]["content"]
    parsed = json.loads(sent)
    assert parsed["omitted"]["catalog_tables"] and sink.records[-1]["status"] == "ok"


# --------------------------------------------------------------------- P4-C06 (prompt parts)
def test_sql_generation_prompt_has_its_dialect_filled():
    text = prompt("sql_generation.v1", dialect="snowflake")
    assert "in the snowflake dialect" in text and "{dialect}" not in text
    with pytest.raises(KeyError, match="dialect"):
        prompt("sql_generation.v1")
    for name in PROMPTS:  # no other prompt has a placeholder the caller must fill
        if name != "sql_generation.v1":
            assert prompt(name) == PROMPTS[name] or "{method_vocabulary}" in PROMPTS[name]


def test_prompt_version_is_name_plus_hash_of_the_exact_text():
    text = prompt("sql_generation.v1", dialect="tsql")
    assert prompt_version_id("sql_generation.v1", text) == f"sql_generation.v1@{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    assert prompt_version_id("sql_generation.v1", text) != prompt_version_id("sql_generation.v1",
                                                                             prompt("sql_generation.v1", dialect="postgres"))


def test_llm_json_logs_real_prompt_version_and_sends_filled_dialect(monkeypatch):
    sink, transport = Sink(), FakeTransport(chat=lambda p: chat_json({"sql": "select 1"}))
    router, platform = _router(sink, transport)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    common.llm_json(_ctx(router), "sql_generation", "sql_generation.v1", {"question": "q", "catalog": []},
                    prompt_vars={"dialect": "tsql"})
    system = transport.chat_calls[0]["messages"][0]["content"]
    assert "in the tsql dialect" in system and "{dialect}" not in system
    expected = prompt_version_id("sql_generation.v1", prompt("sql_generation.v1", dialect="tsql"))
    assert sink.records[-1]["ctx"].prompt_version == expected
    assert sink.records[-1]["ctx"].allowed_providers == ["openrouter", "typesafe"]  # policy reaches the router


def test_llm_json_fails_closed_when_policy_blocks_the_provider(monkeypatch):
    sink, transport = Sink(), FakeTransport(chat=lambda p: chat_json({"sql": "select 1"}))
    router, platform = _router(sink, transport)
    monkeypatch.setattr("analystos.services.platform_settings.get", lambda: platform)
    data, reason = common.llm_json(_ctx(router, WorkspacePolicyDoc(allowed_providers=["typesafe"])), "sql_generation",
                                   "sql_generation.v1", {"question": "q"}, prompt_vars={"dialect": "tsql"})
    assert data is None and reason == "llm_unavailable" and transport.chat_calls == []

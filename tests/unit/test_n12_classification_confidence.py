"""N-12 acceptance, one test per point: measured corroboration only on a full population (through the one path
that stores a profile, `persist_profile`); the domain-assist model sees no sensitive column and no data value; and
classification error rates are measured per language and domain, with confident errors held at today's floor."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from evaluation import classification as C

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
from analystos.db.base import session_scope
from analystos.db.models import SourceAsset, SourceColumn
from analystos.services import crawler
from analystos.skills import catalog as cat

PROFILE = {"row_count": 200, "candidate_keys": [{"columns": ["customer_id"], "column": "customer_id", "unique": True,
                                                 "null_count": 0, "evidence": "profile_unique"}],
           "columns": [{"name": "customer_id", "semantic_type": "id", "non_null": 200, "distinct": 200},
                       {"name": "customer_name", "semantic_type": "text", "non_null": 200, "distinct": 198},
                       {"name": "region", "semantic_type": "categorical", "non_null": 200, "distinct": 4,
                        "top_values": [{"value": "North", "count": 80}]}]}


def _store(snapshot: dict | None) -> float:
    table = DiscoveredAsset(source_name="dim_customer", name="dim_customer", schema_name="dw",
                            columns=[DiscoveredColumn(name="customer_id", data_type="integer", is_key=True),
                                     DiscoveredColumn(name="customer_name", data_type="text"),
                                     DiscoveredColumn(name="region", data_type="text")])
    sem = cat.infer_table_semantics(table)
    with session_scope() as s:
        s.add(SourceAsset(id="ast_1", source_id="src_1", workspace_id="ws_1", schema_name="dw", name="dim_customer",
                          source_name="dim_customer", semantics=sem.model_dump(exclude={"columns"}), stats={},
                          snapshot=snapshot))
        for i, c in enumerate(sem.columns):
            s.add(SourceColumn(asset_id="ast_1", name=c.name, ordinal=i, data_type=table.columns[i].data_type, tags=[],
                               profile={}, semantics=c.model_dump(exclude={"name", "description"})))
    with session_scope() as s:
        crawler.persist_profile(s, "ast_1", json.loads(json.dumps(PROFILE)))
    with session_scope() as s:
        stored = s.get(SourceAsset, "ast_1").semantics
        assert (stored["role"], stored["domain"]) == (sem.role, sem.domain)  # corroboration never reclassifies
        return stored["confidence"] - sem.confidence


@pytest.mark.parametrize("snapshot,raised", [
    (None, True),  # pushdown: the whole table was profiled
    ({"sampling_method": "full", "truncated": False, "load_id": "l1"}, True),
    ({"sampling_method": "full", "truncated": True, "load_id": "l1"}, False),
    ({"sampling_method": "tablesample", "truncated": False, "load_id": "l1"}, False),
    ({"sampling_method": "time_window", "truncated": False, "load_id": "l1"}, False),
])
def test_only_a_full_population_raises_confidence_when_a_profile_is_stored(sqlite_db, snapshot, raised):
    delta = _store(snapshot)
    assert (delta > 0) is raised and delta >= 0


def test_domain_assist_payload_carries_no_sensitive_column_and_no_value(sqlite_db, monkeypatch):
    with session_scope() as s:
        s.add(SourceAsset(id="ast_1", source_id="src_1", workspace_id="ws_1", schema_name="data", name="t_01",
                          source_name="t_01", semantics={"domain": "generic"}, stats={}))
        s.add(SourceColumn(asset_id="ast_1", name="status_code", ordinal=0, data_type="text", tags=[], semantics={},
                           description="Values seen: ACME-7731", profile={"top_values": [{"value": "ACME-7731", "count": 9}],
                                                                        "values": ["ACME-7731"], "min": "ACME-0001"}))
        s.add(SourceColumn(asset_id="ast_1", name="telefon", ordinal=1, data_type="text", tags=[], semantics={}, profile={}))
        s.add(SourceColumn(asset_id="ast_1", name="notes", ordinal=2, data_type="text", tags=["sensitive"], semantics={},
                           profile={}))
    sent: list[str] = []

    class Router:
        def mode(self, purpose):
            return "auto"

        def available(self, purpose):
            return True

        def complete_json(self, purpose, system, payload, **kwargs):
            sent.append(payload)
            return SimpleNamespace(model="m", data={"tables": []})

    from analystos.knowledge import suggestions
    from analystos.runtime import context

    monkeypatch.setattr(suggestions, "rejected_values", lambda *args: set())
    monkeypatch.setattr(context, "workspace_call_ctx", lambda *args, **kwargs: SimpleNamespace())
    job = crawler._Crawl.__new__(crawler._Crawl)
    job.run = SimpleNamespace(options={"enrich": True}, id="crl_1")
    job.source = SimpleNamespace(workspace_id="ws_1")
    job.settings = SimpleNamespace(crawl=SimpleNamespace(enrichment_max_columns=12, enrichment_batch_tables=25))
    job.stats = {"model_calls": 0}
    job.log = SimpleNamespace(stage=lambda *args, **kwargs: None)
    job._suggest_domains(Router(), {"data.t_01": "ast_1"}, {"data.t_01"})
    assert sent and json.loads(sent[0])["tables"][0]["columns"] == [{"name": "status_code", "type": "text"}]
    assert "ACME" not in sent[0]  # neither a value, a range nor a description derived from values
    assert "telefon" not in sent[0]  # a phone column named in German is screened like `phone`
    assert "notes" not in sent[0]


def test_classification_error_rates_are_measured_per_language_and_domain():
    tuned, unseen = C.run("tuned"), C.run("unseen")
    for result in (tuned, unseen):
        assert set(result["languages"]) == set(C.LANGUAGES)
        assert {d for d, _, _ in C.SPLITS[result["split"]]} == set(result["domains"])
        assert result["calibration"] and all(0 <= b["accuracy"] <= 1 for b in result["calibration"])
    # regression floors at the values measured on 2026-09-29 (docs/60-delivery/evidence/2026-09-29-heldout-v2-column-language.md)
    for lang in C.LANGUAGES:
        assert tuned["languages"][lang]["column_error_rate"] == 0.0, lang
        assert tuned["languages"][lang]["domain_error_rate"] == 0.0, lang
    assert unseen["languages"]["en"]["column_error_rate"] == 0.0
    assert unseen["overall"]["column_error_rate"] <= 0.02
    assert unseen["overall"]["confident_wrong_share"] <= 0.02
    assert unseen["overall"]["domain_wrong_rate"] <= 0.125  # a named wrong domain; `generic` abstains


def test_confident_classifications_are_rarely_wrong():
    """Calibration: at or above ENRICH_CONFIDENCE (no enrichment asked) the rules are right at least 97% of the time."""
    for split in C.SPLITS:
        overall = C.run(split)["overall"]
        assert overall["confident_error_rate"] <= 0.03, split

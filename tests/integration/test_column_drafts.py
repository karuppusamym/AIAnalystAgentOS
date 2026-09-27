"""Column-level enrichment (Stream A item 12): the model is asked only about columns the rules did not understand,
its answers are validated (known columns, screened, <= 200 chars) and queued as drafts; nothing reaches the catalog
until a reviewer approves, and a person's text is never replaced."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import KnowledgeSuggestion, User

pytestmark = pytest.mark.integration


def _admin(s):
    from analystos.core.config import get_settings

    return s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))


@pytest.fixture()
def world(control_db):
    from analystos.db.models import Source, SourceAsset, SourceColumn
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        ws = create_workspace(s, _admin(s), name=f"drafts {new_id('t')}", objective="")
        s.flush()
        src = Source(id=new_id("src"), workspace_id=ws.id, kind="postgres", name="db", config={})
        s.add(src)
        s.flush()
        a = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=ws.id, schema_name="public", name="tickets",
                        source_name="tickets", semantics={"role": "fact", "confidence": 0.8})
        s.add(a)
        s.flush()
        unsure = {"semantic_role": "dimension", "confidence": 0.35}
        s.add_all([SourceColumn(asset_id=a.id, name="u_flag2", ordinal=0, data_type="varchar", tags=[], semantics=unsure,
                                profile={"null_rate": 0.1, "values_complete": True, "values": ["Y", "N"]},
                                description="Descriptive attribute.", description_origin="rule"),
                   SourceColumn(asset_id=a.id, name="attr1", ordinal=1, data_type="varchar", tags=[], semantics=unsure,
                                profile={}, description="Kept by a person.", description_origin="user")])
        return {"ws": ws.id, "asset": a.id}


def test_column_drafts_are_validated_queued_and_applied_only_on_approval(world):
    from analystos.db.models import SourceColumn
    from analystos.knowledge.suggestions import review
    from analystos.services.crawler import _Crawl

    sent: list[dict] = []

    class Router:
        def complete(self, purpose, messages, *, ctx=None, json_output=False, max_tokens=None):
            sent.append(json.loads(messages[1]["content"]))
            return SimpleNamespace(model="m-test", data={"tables": [{"key": "public.tickets", "columns": [
                {"name": "u_flag2", "business_name": "Escalated", "description": "Whether the ticket was escalated.",
                 "confidence": 0.8},
                {"name": "attr1", "business_name": "X", "description": "Overwrite attempt."},
                {"name": "not_asked", "business_name": "Y", "description": "Invented column."}]}]})

    crawl = _Crawl.__new__(_Crawl)
    crawl.source = SimpleNamespace(workspace_id=world["ws"])
    crawl.run = SimpleNamespace(id="crawl_test")
    crawl.stats = {"model_calls": 0}
    crawl.log = SimpleNamespace(stage=lambda *a, **k: None)
    crawl._call_ctx = lambda: None
    item = {"asset_id": world["asset"], "semantics": None, "describe": False,
            "columns_to_describe": [{"name": "u_flag2", "type": "text", "values": ["Y", "N"]}, {"name": "attr1", "type": "text"}]}
    payload = {"key": "public.tickets", "describe": False, "columns_to_describe": item["columns_to_describe"]}
    assert crawl._enrich_batch(Router(), [payload], {"public.tickets": item}) == 0  # no table description asked
    assert crawl.stats["column_drafts"] == 1 and sent[0]["tables"][0]["columns_to_describe"][0]["name"] == "u_flag2"
    with session_scope() as s:
        drafts = list(s.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.workspace_id == world["ws"])))
        assert [d.subject for d in drafts] == [f"column:{world['asset']}:u_flag2"] and drafts[0].kind == "column_description"
        col = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == world["asset"], SourceColumn.name == "u_flag2"))
        assert col.description == "Descriptive attribute."  # a draft is not catalog text
        out = review(s, world["ws"], _admin(s), [{"id": drafts[0].id, "action": "approve"}])
        assert out["approved"][0]["catalog"] == "catalog updated"
        col = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == world["asset"], SourceColumn.name == "u_flag2"))
        assert (col.business_name, col.description, col.description_origin) == (
            "Escalated", "Whether the ticket was escalated.", "model")
        kept = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == world["asset"], SourceColumn.name == "attr1"))
        assert kept.description == "Kept by a person." and kept.description_origin == "user"

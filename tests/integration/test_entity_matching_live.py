"""General entity matching (INT-004, N-7) end to end on the compose Postgres: two CSV sources (a CRM and a
billing system, seeded synthetic people with known truth) are staged and read only through the query gateway.

PII-tagged columns are refused to a caller without clearance; with clearance the run reads each side once
(no result preview kept), hashes the PII features and stores proposals without any PII value; precision and
recall are measured against the truth; a reviewer decides every pair; promotion is a hash-bound approval that
a later change of decision invalidates; the approved crosswalk lands in the managed output source, is read
back through the gateway, and its two reviewed join keys pass the federation validator (containment and
cardinality) so analysis can join the two sources through it. Skips cleanly without the stack."""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from evaluation import entity_match_datasets as D
from sqlalchemy import func, select

pytestmark = pytest.mark.integration
PII = {"customers": ["full_name", "email", "phone", "birth_date"], "accounts": ["holder_name", "contact_email", "tel", "dob"]}


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if r[c] is None else r[c] for c in cols])


def _user(email: str):
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == email))
        s.expunge(u)
    return u


def _set_clearance(email: str, value: bool | None) -> None:
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == email))
        attrs = dict(u.attributes or {})
        if value is None:
            attrs.pop("pii_clearance", None)
        else:
            attrs["pii_clearance"] = value
        u.attributes = attrs


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Source, SourceAsset, SourceColumn
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    get_settings.cache_clear()
    left, right, truth = D.people(300, seed=21)
    folders = {}
    for name, cols, rows, table in (("crm", D.LEFT_COLUMNS, left, "customers"),
                                    ("billing", D.RIGHT_COLUMNS, right, "accounts")):
        folder = f"em-{name}-{new_id('t')}"
        path = Path(get_settings().upload_dir) / folder
        path.mkdir(parents=True, exist_ok=True)
        _write(path / f"{table}.csv", cols, rows)
        folders[name] = (folder, path, table)
    owner = _user("analyst@analystos.local")
    previous = (owner.attributes or {}).get("pii_clearance")
    with session_scope() as s:
        ws = create_workspace(s, s.merge(owner), name=f"entity match {new_id('t')}",
                              objective="Link CRM customers to billing accounts", autonomy_level=2)
        s.flush()
        add_member(s, s.merge(owner), ws.id, "approver@analystos.local", "approver")
        srcs = {n: register_source(s, s.merge(owner), ws.id, kind="csv", name=f"{n} files", config={"path": f[0]},
                                   secret_ref=None) for n, f in folders.items()}
        s.flush()
        ws_id, src_ids = ws.id, {n: src.id for n, src in srcs.items()}
    for n, (_, _, table) in folders.items():
        discover_source(owner, src_ids[n], ws_id)
        select_assets(owner, src_ids[n], [table], ws_id)
    assets = {}
    with session_scope() as s:
        for n, (_, _, table) in folders.items():
            src = s.get(Source, src_ids[n])
            assets[n] = f"{src.staging_schema or 'src_' + src.id}.{table}"
            a = s.scalar(select(SourceAsset).where(SourceAsset.source_id == src.id, SourceAsset.name == table))
            for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id)):
                if c.name in PII[table]:
                    c.tags, c.tags_origin = sorted({*(c.tags or []), "pii"}), "user"
                elif "pii" in (c.tags or []):  # keys and plain fields stay readable
                    c.tags = [t for t in c.tags if t != "pii"]
    _set_clearance("analyst@analystos.local", None)
    yield {"ws": ws_id, "src": src_ids, "assets": assets, "truth": truth, "left": left, "right": right}
    _set_clearance("analyst@analystos.local", previous)
    for _, path, _ in folders.values():
        shutil.rmtree(path, ignore_errors=True)


def _spec(world, **over) -> dict:
    return D.spec(left={"asset": world["assets"]["crm"], "key": "customer_id"},
                  right={"asset": world["assets"]["billing"], "key": "account_ref"}, **over)


def test_pii_without_clearance_is_refused_before_any_read(world):
    from analystos.core.errors import Forbidden
    from analystos.services import entity_matching as svc

    with pytest.raises(Forbidden, match="not readable under the workspace policy"):
        svc.start_match(_user("analyst@analystos.local"), world["ws"], _spec(world))


def test_a_table_over_the_row_cap_is_refused_not_sampled(world):
    from analystos.core.errors import InvalidInput
    from analystos.db.base import session_scope
    from analystos.db.models import EntityMatchRun
    from analystos.services import entity_matching as svc

    _set_clearance("analyst@analystos.local", True)
    with pytest.raises(InvalidInput, match="row cap"):
        svc.start_match(_user("analyst@analystos.local"), world["ws"], _spec(world, name="capped", max_rows=50))
    with session_scope() as s:
        row = s.scalar(select(EntityMatchRun).where(EntityMatchRun.workspace_id == world["ws"],
                                                    EntityMatchRun.name == "capped"))
        assert row.status == "failed" and "row cap" in row.error


def test_match_review_approve_promote_and_join(world):
    from analystos.core.errors import ApprovalRequired
    from analystos.db.base import session_scope
    from analystos.db.models import EntityMatchPair, QueryExecution, SourceAsset, SourceColumn
    from analystos.governance.approvals import decide
    from analystos.governance.policy import resolve_scope
    from analystos.runtime.context import default_gateway
    from analystos.services import entity_matching as svc
    from analystos.skills import entity_matching as em
    from analystos.skills.federation import JoinProposal, validate_join_keys

    _set_clearance("analyst@analystos.local", True)
    analyst, approver = _user("analyst@analystos.local"), _user("approver@analystos.local")
    run = svc.start_match(analyst, world["ws"], _spec(world))
    assert run["status"] == "proposed"
    assert set(run["pii_fields"]) == {"full_name~holder_name", "email~contact_email", "phone~tel", "birth_date~dob"}

    with session_scope() as s:  # read once per side, through the gateway, nothing retained
        qs = list(s.scalars(select(QueryExecution).where(QueryExecution.id.in_(run["query_ids"]))))
        assert len(qs) == 2 and all(q.status == "ok" and not q.result_preview and not q.truncated for q in qs)
        assert {q.source_id for q in qs} == set(world["src"].values())
        pairs = [svc.pair_view(p) for p in s.scalars(select(EntityMatchPair).where(EntityMatchPair.run_id == run["id"]))]
    stored = json.dumps([pairs, run["stats"]], default=str).lower()
    for r in world["left"][:100]:
        assert r["email"].lower() not in stored and r["full_name"].split()[-1].lower() not in stored

    measured = em.evaluate([em.MatchPair(p["left_key"], p["right_key"], p["score"], p["band"], p["fields"]) for p in pairs],
                           world["truth"])
    assert measured["match_band"]["precision"] >= 0.99 and measured["match_band"]["recall"] >= 0.8
    assert measured["match_and_review"]["recall"] >= 0.95

    # a reviewer: the match band at once, the review band pair by pair (here the known truth plays the person)
    svc.review(analyst, run["id"], world["ws"], accept_band="match")
    review = [p for p in pairs if p["band"] == "review"]
    svc.review(analyst, run["id"], world["ws"], decisions=[
        {"pair_id": p["id"], "decision": "accept" if (p["left_key"], p["right_key"]) in world["truth"] else "reject"}
        for p in review])
    got = svc.promote(analyst, run["id"], world["ws"])
    assert got["status"] == "approval_required"
    with session_scope() as s:
        decide(s, got["approval_id"], s.merge(approver), approve=True)

    flip = review[0]  # a decision changed after the approval: the approval no longer matches the crosswalk
    svc.review(analyst, run["id"], world["ws"], decisions=[{"pair_id": flip["id"], "decision": "reject"}])
    with pytest.raises(ApprovalRequired):
        svc.promote(analyst, run["id"], world["ws"], approval_id=got["approval_id"])
    again = svc.promote(analyst, run["id"], world["ws"])
    assert again["approval_id"] != got["approval_id"]
    with session_scope() as s:
        decide(s, again["approval_id"], s.merge(approver), approve=True)
    done = svc.promote(analyst, run["id"], world["ws"], approval_id=again["approval_id"])
    assert done["status"] == "promoted"
    view = done["match_run"]
    table = view["crosswalk_table"]

    with session_scope() as s:
        scope = resolve_scope(s, s.merge(analyst), world["ws"])
        accepted = s.scalar(select(func.count()).where(
            EntityMatchPair.run_id == run["id"], EntityMatchPair.decision == "accepted"))
        catalog = []
        for fq in (world["assets"]["crm"], world["assets"]["billing"], table):
            schema, name = fq.split(".", 1)
            a = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == world["ws"], SourceAsset.schema_name == schema,
                                                   SourceAsset.name == name))
            cols = s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
            catalog.append({"asset": fq, "columns": [{"name": c.name, "data_type": c.data_type, "is_key": c.is_key}
                                                     for c in cols]})
    assert table in scope.assets
    gw = default_gateway()
    res = gw.run_sql_for(scope, actor=f"user:{analyst.id}", source_id=view["crosswalk_source_id"])(
        f'SELECT COUNT(*) AS n FROM {table}', purpose="test.crosswalk")
    assert res.records()[0]["n"] == accepted

    runner = gw.run_sql_for(scope, actor=f"user:{analyst.id}", federated=True)
    decisions = validate_join_keys(runner, [JoinProposal.model_validate(k) for k in view["join_keys"]], catalog,
                                   scope.asset_sources)
    assert [d.accepted for d in decisions] == [True, True], [d.reason for d in decisions]

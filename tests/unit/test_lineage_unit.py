"""P4-S02: `lineage_for` is one recursive CTE per artifact. It must return exactly what the former
whole-workspace BFS returned, for every depth, on graphs with cycles, fan-out and edges of other
workspaces (which are never followed)."""
from __future__ import annotations

import random

import pytest
from sqlalchemy import insert, select

from analystos.db.base import session_scope
from analystos.db.models import LineageEdge


def _bfs(edges: list[LineageEdge], node: tuple[str, str], depth: int) -> dict:
    """The pre-P4-S02 implementation, over the workspace's edges loaded in memory."""
    out_adj: dict = {}
    in_adj: dict = {}
    for e in edges:
        out_adj.setdefault((e.from_type, e.from_id), []).append(e)
        in_adj.setdefault((e.to_type, e.to_id), []).append(e)
    seen_nodes, seen_edges, frontier = {node}, set(), [node]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for e in out_adj.get(n, []) + in_adj.get(n, []):
                if e.id in seen_edges:
                    continue
                seen_edges.add(e.id)
                for m in ((e.from_type, e.from_id), (e.to_type, e.to_id)):
                    if m not in seen_nodes:
                        seen_nodes.add(m)
                        nxt.append(m)
        frontier = nxt
    by_id = {e.id: e for e in edges}
    return {"nodes": [{"type": t, "id": i} for t, i in sorted(seen_nodes)],
            "edges": [{"from": [by_id[i].from_type, by_id[i].from_id], "relation": by_id[i].relation,
                       "to": [by_id[i].to_type, by_id[i].to_id]} for i in sorted(seen_edges)]}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_lineage_cte_matches_bfs(sqlite_db, seed):
    from analystos.artifacts.registry import lineage_for

    rng = random.Random(seed)
    nodes = [(t, f"{t}_{i}") for t in ("table", "dataset", "artifact", "chart", "insight") for i in range(8)]
    rows, seen = [], set()
    for ws in ("ws_a", "ws_b"):
        for _ in range(70):
            a, b = rng.sample(nodes, 2)
            rel = rng.choice(["built_from", "feeds", "about"])
            if (ws, a, rel, b) in seen:
                continue
            seen.add((ws, a, rel, b))
            rows.append({"workspace_id": ws, "from_type": a[0], "from_id": a[1], "relation": rel,
                         "to_type": b[0], "to_id": b[1]})
    with session_scope() as s:
        s.execute(insert(LineageEdge), rows)
    with session_scope() as s:
        ws_edges = list(s.scalars(select(LineageEdge).where(LineageEdge.workspace_id == "ws_a")))
        for start in rng.sample(nodes, 6) + [("artifact", "absent")]:
            for depth in (0, 1, 2, 3, 6):
                assert lineage_for(s, "ws_a", start, depth=depth) == _bfs(ws_edges, start, depth), (start, depth)


def _edges(s, ws: str) -> set[tuple[str, str, str, str, str]]:
    return {(e.from_type, e.from_id, e.relation, e.to_type, e.to_id)
            for e in s.scalars(select(LineageEdge).where(LineageEdge.workspace_id == ws))}


def test_a_finding_is_keyed_by_its_id_not_its_per_run_code(sqlite_db):
    """I-1 restarts every run: ("insight", "I-1") would join every run's first finding. A code written within a
    run is resolved to that run's finding id; an id, or a code no finding has, is kept as given."""
    from analystos.artifacts.registry import link, node_ref
    from analystos.db.models import Insight

    with session_scope() as s:
        for run, ins in (("run_1", "ins_one"), ("run_2", "ins_two")):
            s.add(Insight(id=ins, workspace_id="ws_a", run_id=run, code="I-1", title="t", finding="f"))
        s.flush()
        assert node_ref(s, ("insight", "I-1"), "run_2") == ("insight", "ins_two")
        assert node_ref(s, ("insight", "ins_one"), "run_2") == ("insight", "ins_one")
        assert node_ref(s, ("insight", "I-9"), "run_2") == ("insight", "I-9")
        assert node_ref(s, ("insight", "I-1")) == ("insight", "I-1")  # no run: nothing to resolve within
        link(s, "ws_a", ("narrative", "art_n1"), "cites", ("insight", "I-1"), run_id="run_1")
        link(s, "ws_a", ("insight", "I-1"), "visualized_by", ("chart", "art_c2"), run_id="run_2")
    with session_scope() as s:
        assert _edges(s, "ws_a") == {("narrative", "art_n1", "cites", "insight", "ins_one"),
                                     ("insight", "ins_two", "visualized_by", "chart", "art_c2")}


def test_an_artifact_written_generically_is_found_by_its_own_type(sqlite_db):
    """Writers stored ("artifact", id) for reports, narratives, profiles...; the artifact route asks (type, id).
    Both spellings are one node, named by the artifact's type in the result."""
    from analystos.artifacts.registry import lineage_for, save_artifact

    with session_scope() as s:
        rep = save_artifact(s, workspace_id="ws_a", type_="report", name="executive report", content={"k": 1}, run_id="run_1")
        nar = save_artifact(s, workspace_id="ws_a", type_="narrative", name="Executive summary", content={"k": 2}, run_id="run_1")
        s.flush()
        rows = [("run", "run_1", "reported_by", "artifact", rep.id), ("artifact", rep.id, "cites", "insight", "ins_one"),
                ("report", rep.id, "includes", "chart", "art_chart"), ("run", "run_1", "summarized_by", "artifact", nar.id),
                ("artifact", "art_gone", "about", "table", "s.t")]
        s.execute(insert(LineageEdge), [{"workspace_id": "ws_a", "run_id": "run_1", "from_type": a, "from_id": b, "relation": r,
                                         "to_type": c, "to_id": d} for a, b, r, c, d in rows])
        rep_id, nar_id = rep.id, nar.id
    with session_scope() as s:
        graph = lineage_for(s, "ws_a", ("report", rep_id), depth=1)
        assert {(e["from"][0], e["relation"], e["to"][0]) for e in graph["edges"]} == {
            ("run", "reported_by", "report"), ("report", "cites", "insight"), ("report", "includes", "chart")}
        assert {"type": "artifact", "id": rep_id} not in graph["nodes"]
        # asked generically, the anchor still takes its type; a generic end whose artifact is gone stays generic
        assert lineage_for(s, "ws_a", ("artifact", rep_id), depth=1)["nodes"] == graph["nodes"]
        assert {"type": "narrative", "id": nar_id} in lineage_for(s, "ws_a", ("report", rep_id), depth=2)["nodes"]
        assert lineage_for(s, "ws_a", ("artifact", "art_gone"), depth=1)["nodes"] == [
            {"type": "artifact", "id": "art_gone"}, {"type": "table", "id": "s.t"}]


def test_run_lineage_is_the_edges_the_run_recorded(sqlite_db):
    from analystos.artifacts.registry import run_lineage

    rows = [("ws_a", "run_1", "objective", "run_1", "asks", "hypothesis", "hyp_1"),
            ("ws_a", "run_1", "query", "qry_1", "reads", "table", "s.orders"),
            ("ws_a", "run_2", "query", "qry_2", "reads", "table", "s.orders"),  # another run over the same table
            ("ws_b", "run_1", "query", "qry_9", "reads", "table", "s.orders")]  # never another workspace's
    with session_scope() as s:
        s.execute(insert(LineageEdge), [{"workspace_id": w, "run_id": r, "from_type": a, "from_id": b, "relation": rel,
                                         "to_type": c, "to_id": d} for w, r, a, b, rel, c, d in rows])
    with session_scope() as s:
        graph = run_lineage(s, "ws_a", "run_1")
    assert [(e["from"][1], e["to"][1]) for e in graph["edges"]] == [("run_1", "hyp_1"), ("qry_1", "s.orders")]
    assert {"type": "run", "id": "run_1"} in graph["nodes"] and graph["truncated"] is False


def test_an_experiment_links_its_queries_and_every_table_the_gateway_saw(sqlite_db):
    from analystos.artifacts.registry import link_queries
    from analystos.db.models import QueryExecution

    with session_scope() as s:
        s.add(QueryExecution(id="qry_1", workspace_id="ws_a", actor="agent:x", sql="select 1", status="ok",
                             referenced_assets=["s.orders", "s.customers"]))
        s.flush()
        link_queries(s, "ws_a", ("experiment", "exp_1"), ["qry_1", "qry_unrecorded"], run_id="run_1", assets=["s.orders"])
    with session_scope() as s:
        assert _edges(s, "ws_a") == {
            ("experiment", "exp_1", "derived_from", "query", "qry_1"), ("query", "qry_1", "reads", "table", "s.orders"),
            ("query", "qry_1", "reads", "table", "s.customers"),
            ("experiment", "exp_1", "derived_from", "query", "qry_unrecorded"),
            ("query", "qry_unrecorded", "reads", "table", "s.orders")}


def test_a_join_is_context_only_while_a_validated_relationship_backs_it(sqlite_db):
    """A discovered join nobody validated, or one rejected in review, must not reach an agent's context through
    the graph neighbourhood, whatever lineage recorded when it was discovered."""
    from analystos.db import models
    from analystos.db.models import Relationship, SourceAsset
    from analystos.graph.projection import pg_neighborhood

    models.Base.metadata.tables["relationship"].create(sqlite_db.kw["bind"])
    with session_scope() as s:
        for name in ("orders", "customers", "notes", "regions"):
            s.add(SourceAsset(id=f"ast_{name}", source_id="src_1", workspace_id="ws_a", schema_name="s", name=name,
                              source_name=name))
        s.add(Relationship(id="rel_ok", workspace_id="ws_a", from_asset_id="ast_orders", from_column="customer_id",
                           to_asset_id="ast_customers", to_column="id", validated=True))
        s.add(Relationship(id="rel_rejected", workspace_id="ws_a", from_asset_id="ast_orders", from_column="note_id",
                           to_asset_id="ast_notes", to_column="id", validated=False, evidence={"rejected": {"candidate_id": "c"}}))
        s.add(Relationship(id="rel_pending", workspace_id="ws_a", from_asset_id="ast_regions", from_column="id",
                           to_asset_id="ast_orders", to_column="region_id", validated=False))
        joins = [("s.orders", "s.customers"), ("s.orders", "s.notes"), ("s.regions", "s.orders")]
        s.execute(insert(LineageEdge), [{"workspace_id": "ws_a", "from_type": "table", "from_id": a, "relation": "joins_to",
                                         "to_type": "table", "to_id": b} for a, b in joins]
                  + [{"workspace_id": "ws_a", "from_type": "dataset", "from_id": "art_ds", "relation": "built_from",
                      "to_type": "table", "to_id": "s.orders"}])
    with session_scope() as s:
        assert pg_neighborhood(s, ["s.orders"], "ws_a") == [
            {"table": "s.orders", "rel": "BUILT_FROM", "type": "dataset", "id": "art_ds"},
            {"table": "s.orders", "rel": "JOINS_TO", "type": "table", "id": "s.customers"}]
        assert pg_neighborhood(s, ["s.customers", "s.notes"], "ws_a") == [
            {"table": "s.customers", "rel": "JOINS_TO", "type": "table", "id": "s.orders"}]

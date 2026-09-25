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

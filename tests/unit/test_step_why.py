""""Why this number?" for a step version (P7-08 on P7-04 steps): every number of the result resolved through
six links with their state; a version whose verdict an edit voided shows VOID with its cause, and a missing
receipt is shown broken, never dropped."""
from __future__ import annotations

import pytest
from tests.unit.step_fixtures import WS, FakeRuntime, world  # noqa: F401

from analystos.api.routers.steps import why_step_number
from analystos.contracts.step import NotebookIn, StepEdit, StepIn
from analystos.core.errors import NotFound
from analystos.db.base import session_scope
from analystos.db.models import AnalysisStep, QueryExecution, User
from analystos.evidence.why import LINKS, explain_step
from analystos.services import notebooks
from analystos.services import steps as steps_svc

ORDERS = ("FROM sales.orders", ["state", "n"], [["Closed", 60], ["Open", 40]])
LINES = ("FROM sales.order_line", ["n"], [[400]])


@pytest.fixture
def thread(world):  # noqa: F811
    with session_scope() as s:
        nb = notebooks.create(s, s.merge(world["analyst"]), WS, NotebookIn(title="Late orders"))
    return {"branch": nb["branch_id"], **world}


def _query(t, rt):
    return steps_svc.create(t["analyst"], WS, t["branch"], StepIn(kind="query", title="orders by state",
                                                                   spec={"sql": "SELECT state, COUNT(*) AS n FROM sales.orders "
                                                                                "GROUP BY state"}), runtime=rt)


def _receipt_row(step: dict) -> None:
    """What the gateway writes for a query (the fake runtime does not)."""
    r = step["receipts"][0]
    with session_scope() as s:
        s.add(QueryExecution(id=r["query_id"], workspace_id=WS, source_id="src_sales", actor="user:usr_analyst",
                             sql=r["sql"], status="ok", row_count=r["rows"], result_hash=r["result_hash"]))


def _why(step_id: str, **kw) -> dict:
    with session_scope() as s:
        return explain_step(s, s.get(AnalysisStep, step_id), **kw)


def test_each_number_of_a_verified_step_has_six_links(thread):
    a = _query(thread, FakeRuntime([ORDERS]))
    _receipt_row(a)
    out = _why(a["id"])
    assert out["subject"]["type"] == "step" and out["subject"]["version"] == 1
    assert [n["value"] for n in out["numbers"]] == [60, 40] and out["numbers"][0]["column"] == "n"
    for n in out["numbers"]:
        assert tuple(lk["link"] for lk in n["links"]) == LINKS
    links = {lk["link"]: lk for lk in out["numbers"][0]["links"]}
    assert links["fact"]["detail"]["labels"] == {"state": "Closed"}
    assert links["step"]["state"] == "ok" and links["query_receipt"]["state"] == "ok"
    assert links["semantic_version"]["state"] == "not_applicable" and links["verdict"]["state"] == "ok"
    assert links["data_version"]["state"] in ("unknown", "ok")  # the fixture source is pushdown
    assert out["numbers"][0]["state"] in ("ok", "unknown")
    assert [n["value"] for n in _why(a["id"], column="n", row=1)["numbers"]] == [40]
    with pytest.raises(NotFound):
        _why(a["id"], number="12345")


def test_a_voided_version_shows_its_void_verdict_and_missing_receipt(thread):
    rt = FakeRuntime([ORDERS, LINES])
    a = _query(thread, rt)  # no receipt row: the query receipt link must say so
    steps_svc.edit(thread["analyst"], a["id"], StepEdit(spec={"sql": "SELECT COUNT(*) AS n FROM sales.order_line"}),
                   expected_version=1, runtime=rt)
    old = _why(a["id"], version=1)
    assert old["verification_state"]["state"] == "VOID" and old["state"] == "broken"
    assert old["numbers"] and all(tuple(lk["link"] for lk in n["links"]) == LINKS for n in old["numbers"])
    links = {lk["link"]: lk for lk in old["numbers"][0]["links"]}
    assert links["verdict"]["state"] == "void" and "edited" in links["verdict"]["reason"]
    assert links["verdict"]["detail"]["void"]["kind"] == "query"
    assert links["step"]["state"] == "changed" and "version 2" in links["step"]["reason"]
    assert links["query_receipt"]["state"] == "broken" and "missing" in links["query_receipt"]["reason"]
    assert {lk["link"] for lk in old["links"]} == set(LINKS) - {"fact"}  # the step's own links, even without numbers
    now = _why(a["id"])
    assert now["subject"]["version"] == 2 and [n["value"] for n in now["numbers"]] == [400]
    assert {lk["link"]: lk["state"] for lk in now["numbers"][0]["links"]}["verdict"] == "ok"


def test_the_route_reads_within_the_path_workspace(thread):
    a = _query(thread, FakeRuntime([ORDERS]))
    with session_scope() as s:
        viewer = s.get(User, "usr_viewer")
        assert why_step_number(WS, a["id"], user=viewer, session=s)["subject"]["id"] == a["id"]
        with pytest.raises(NotFound):
            why_step_number("ws_other", a["id"], user=viewer, session=s)

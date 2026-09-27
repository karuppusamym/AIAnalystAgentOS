"""P7-12 on the in-memory control plane: notebook cells are steps. SQL cells go through the runtime (the
gateway in production); Python cells run in the real sandbox on earlier cells' results only, cannot import
a connection library, and are killed on a resource bomb; a cell edit voids and re-runs the cells that read
it; executions are versioned."""
from __future__ import annotations

import pytest
from tests.unit.step_fixtures import WS, FakeRuntime, world  # noqa: F401

from analystos.contracts.step import CellEdit, CellIn, NotebookIn
from analystos.core.config import Settings, get_settings
from analystos.core.errors import InvalidInput
from analystos.db.base import session_scope
from analystos.db.models import AnalysisStep, Notebook, VerificationRecord
from analystos.sandbox import isolation
from analystos.services import notebooks
from analystos.services import steps as steps_svc

_PROCESS = isolation.status(Settings(_env_file=None, sandbox_isolation="process")).available


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_SANDBOX_ISOLATION", "process" if _PROCESS else "off")
    monkeypatch.setenv("ANALYSTOS_ENV", "dev")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class SandboxRuntime(FakeRuntime):
    """Fixture SQL, real sandboxed Python (steps.execute_python, the function P7-06 switches)."""

    def python(self, code, inputs):  # noqa: ANN001
        return steps_svc.execute_python(code, inputs, timeout_s=5, memory_mb=512)


@pytest.fixture
def nb(world):  # noqa: F811
    with session_scope() as s:
        out = notebooks.create(s, s.merge(world["analyst"]), WS, NotebookIn(title="Order lateness"))
    return {"id": out["id"], **world}


SUM_CODE = "import pandas as pd\nresult = {'total': float(pd.DataFrame(inputs['cell1'])['n'].sum())}"


def test_cells_are_steps_and_python_reads_only_upstream_results(nb):
    rt = SandboxRuntime([("FROM sales.orders", ["state", "n"], [["Closed", 60], ["Open", 40]])])
    sql = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="sql", source="SELECT state, COUNT(*) AS n FROM sales.orders "
                                                                                     "GROUP BY state"), runtime=rt)
    py = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="python", source=SUM_CODE), runtime=rt)
    md = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="markdown", source="There are 100 orders.",
                                                                depends_on=[py["id"]]), runtime=rt)
    assert sql["kind"] == "query" and sql["status"] == "ok"
    assert py["kind"] == "method" and py["status"] == "ok" and py["depends_on"] == [sql["id"]], py
    assert py["receipts"][0]["kind"] == "python" and py["receipts"][0]["isolation"] in ("process", "none")
    assert md["status"] == "ok" and md["checks"][0]["check"] == "numbers"
    with session_scope() as s:
        view = notebooks.notebook_view(s, s.get(Notebook, nb["id"]))
        assert [c["cell"] for c in view["cells"]] == ["sql", "python", "markdown"] and view["revision"] == 4
        result = steps_svc.with_result(s, s.get(AnalysisStep, py["id"]))["result"]
    assert result["rows"] == [[100.0]]


def test_editing_a_cell_voids_and_reruns_the_cells_that_read_it(nb):
    rt = SandboxRuntime([("WHERE state = 'Open'", ["state", "n"], [["Open", 40]]),
                         ("FROM sales.orders", ["state", "n"], [["Closed", 60], ["Open", 40]])])
    sql = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="sql", source="SELECT state, COUNT(*) AS n FROM "
                                                                                     "sales.orders GROUP BY state"), runtime=rt)
    py = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="python", source=SUM_CODE), runtime=rt)
    md = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="markdown", source="There are 100 orders.",
                                                                depends_on=[py["id"]]), runtime=rt)
    out = notebooks.edit_cell(nb["analyst"], sql["id"], CellEdit(source="SELECT state, COUNT(*) AS n FROM sales.orders "
                                                                        "WHERE state = 'Open' GROUP BY state"),
                              expected_version=1, runtime=rt)
    assert [r["id"] for r in out["rerun"]] == [py["id"], md["id"]]
    with session_scope() as s:
        for cell in (sql, py, md):
            assert s.get(VerificationRecord, cell["verification_record"]["record_id"]).state == "VOID"
    assert out["rerun"][0]["version"] == 2 and out["rerun"][1]["status"] == "flagged"  # "100" no longer binds (40)
    rerun = notebooks.run_all(nb["analyst"], WS, nb["id"], runtime=rt)
    assert [c["version"] for c in rerun["cells"]] == [3, 3, 3]


def test_python_cells_cannot_open_connections(nb):
    rt = SandboxRuntime([])
    for code in ("import psycopg\nresult = 1", "import socket\nresult = 1", "import sqlalchemy\nresult = 1",
                 "import urllib.request\nresult = 1", "result = open('/etc/passwd').read()"):
        with pytest.raises(InvalidInput):
            notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="python", source=code), runtime=rt)


def test_a_resource_bomb_is_killed_and_recorded_as_a_failed_version(nb):
    rt = SandboxRuntime([])
    spin = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="python", source="while True:\n    pass",
                                                                  depends_on=[]), runtime=rt)
    assert spin["status"] == "failed" and "timeout" in spin["error"]
    assert spin["receipts"][0]["timed_out"] is True


def test_the_add_cell_response_is_the_cell_as_the_notebook_shows_it(nb):
    rt = SandboxRuntime([("FROM sales.orders", ["state", "n"], [["Closed", 60], ["Open", 40]])])
    src = "SELECT state, COUNT(*) AS n FROM sales.orders GROUP BY state"
    sql = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="sql", source=src), runtime=rt)
    md = notebooks.add_cell(nb["analyst"], WS, nb["id"], CellIn(cell="markdown", source="Notes"), runtime=rt)
    assert (sql["cell"], sql["source"], sql["version"]) == ("sql", src, 1)
    assert (md["cell"], md["source"]) == ("markdown", "Notes")
    with session_scope() as s:
        cells = notebooks.notebook_view(s, s.get(Notebook, nb["id"]))["cells"]
    assert [sql, md] == cells

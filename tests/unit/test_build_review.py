"""P4-U05 review aids without services: the per-file project diff and the KPI editor's field checks."""
from __future__ import annotations

from analystos.build.diff import file_diff
from analystos.contracts.semantic import MetricProposalIn
from analystos.semantic.service import _definition_from_input


def test_diff_reports_each_file_once_with_its_status_and_line_counts():
    before = {"dbt_project.yml": "name: a\n", "models/m.sql": "select 1 as x\nfrom t\n", "models/old.sql": "select 2\n"}
    after = {"dbt_project.yml": "name: a\n", "models/m.sql": "select 1 as x\nfrom t\nwhere y > 0\n", "models/new.yml": "version: 2\n"}
    out = file_diff(before, after)
    by = {f["path"]: f for f in out["files"]}
    assert [f["path"] for f in out["files"]] == sorted(by)
    assert {p: f["status"] for p, f in by.items()} == {"dbt_project.yml": "unchanged", "models/m.sql": "modified",
                                                       "models/new.yml": "added", "models/old.sql": "removed"}
    assert by["models/m.sql"]["lines_added"] == 1 and by["models/m.sql"]["lines_removed"] == 0
    assert "+where y > 0" in by["models/m.sql"]["diff"] and by["models/m.sql"]["diff"].startswith("--- a/models/m.sql")
    assert by["models/old.sql"]["diff"].splitlines()[1] == "+++ /dev/null"
    assert by["dbt_project.yml"]["diff"] == ""
    assert out["summary"] == {"added": 1, "removed": 1, "modified": 1, "unchanged": 1, "lines_added": 2, "lines_removed": 1}


def test_no_earlier_job_means_every_file_is_added():
    out = file_diff(None, {"a.sql": "select 1\n", "b.yml": "x: 1\n"})
    assert {f["status"] for f in out["files"]} == {"added"} and out["summary"]["added"] == 2


def test_long_diffs_are_cut_and_flagged():
    after = {"big.sql": "\n".join(f"select {i}" for i in range(1000))}
    f = file_diff({}, after, max_lines=50)["files"][0]
    assert f["truncated"] and len(f["diff"].splitlines()) == 50 and f["lines_added"] == 1000


def test_kpi_field_problems_are_reported_per_field_not_raised():
    defn, problems = _definition_from_input(MetricProposalIn(name="1 bad name", expression="COUNT(*)"))
    assert defn is None and problems[0]["field"] == "name" and "letters, digits" in problems[0]["message"]
    defn, problems = _definition_from_input(MetricProposalIn(name="p1_count", expression="priority"))
    assert defn is not None and problems == [{"field": "expression", "message": "not an aggregate expression"}]
    defn, problems = _definition_from_input(MetricProposalIn(name="p1_count", expression="   "))
    assert problems == [{"field": "expression", "message": "an expression is required"}]
    defn, problems = _definition_from_input(MetricProposalIn(name="p1_count", expression="COUNT(*) FILTER (WHERE priority = '1')"))
    assert defn is not None and problems == []

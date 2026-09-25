import pytest

from analystos.core.errors import InvalidInput
from analystos.skills.sqlexplain import explain_sql


def test_explains_structure_without_a_model():
    r = explain_sql("SELECT r.name, SUM(o.amount) AS total FROM s.orders o LEFT JOIN s.region r ON r.id = o.region_id "
                    "WHERE o.shipped GROUP BY r.name HAVING SUM(o.amount) > 10 ORDER BY total DESC LIMIT 5")
    assert r["tables"] == ["s.orders", "s.region"] and r["group_by"] == ["r.name"]
    assert r["aggregations"] == ["total of o.amount"] and r["limit"] == "5"
    assert r["joins"][0]["kind"] == "left" and r["joins"][0]["table"] == "s.region"
    assert r["summary"].startswith("Returns name and total of o.amount from s.orders, s.region")
    assert "ordered by total descending" in r["summary"]


def test_ctes_counts_and_dialects():
    r = explain_sql("WITH x AS (SELECT * FROM a.b) SELECT COUNT(*), COUNT(DISTINCT y) FROM x")
    assert r["tables"] == ["a.b"] and r["ctes"] == ["x"]
    assert r["aggregations"] == ["row count", "number of distinct y"]
    assert explain_sql("SELECT TOP 5 [a] FROM [s].[t]", "tsql")["limit"] == "5"


def test_non_queries_and_bad_input():
    assert explain_sql("DELETE FROM t")["read_only"] is False
    with pytest.raises(InvalidInput):
        explain_sql("SELECT 1; SELECT 2")
    with pytest.raises(InvalidInput):
        explain_sql("SELEC FROM")

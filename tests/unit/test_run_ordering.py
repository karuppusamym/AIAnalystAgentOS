"""Rows an agent writes in one transaction share the transaction's `created_at`; every reader whose
output depends on their order (dataset derivations, KPI set, chart previews) must still see one order.
Seen as DEX-001 flakiness: two identical runs built the dataset's derived columns, and so the KPIs and
the detail table's metrics, in different orders."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select


def test_codes_order_numerically_when_created_at_ties(sqlite_db):
    from analystos.db.models import Hypothesis, by_code

    tied = datetime(2026, 9, 26, tzinfo=UTC)
    with sqlite_db() as s:
        for n in (10, 2, 1, 11, 3):
            s.add(Hypothesis(id=f"hyp_{n:02d}_{'x' * (n % 3)}", workspace_id="ws", run_id="run", code=f"H-{n}",
                             statement="s", spec={}, created_at=tied))
        s.commit()
        codes = list(s.scalars(select(Hypothesis.code).where(Hypothesis.run_id == "run")
                               .order_by(Hypothesis.created_at, *by_code(Hypothesis.code))))
    assert codes == ["H-1", "H-2", "H-3", "H-10", "H-11"]


def test_artifacts_written_in_one_transaction_read_back_in_write_order(sqlite_db):
    from analystos.artifacts.registry import save_artifact
    from analystos.db.models import Artifact

    names = ["record_count", "made_sla_is_false_rate", "opened_at_after_hours_rate", "median_opened_at_to_resolved_at_hours",
             "avg_reassignment_count"]
    with sqlite_db() as s:
        for name in names:
            save_artifact(s, workspace_id="ws", type_="metric", name=name, content={"name": name})
        s.commit()
        stamps = list(s.execute(select(Artifact.name, Artifact.created_at).where(Artifact.type == "metric")
                                .order_by(Artifact.created_at, Artifact.name)))
    assert [n for n, _ in stamps] == names
    assert len({t for _, t in stamps}) == len(names)

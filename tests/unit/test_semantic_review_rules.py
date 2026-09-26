"""P7-09 / P4-05 review rules without services: a relationship decision needs a fresh measurement and
corroborating evidence (Atlas's rule), a lapsed request is closed before it is re-requested, and a
candidate's content hash moves with its measurement (so an approval cannot outlive what it approved)."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from analystos.core.errors import Conflict
from analystos.core.ids import utcnow
from analystos.semantic import review
from analystos.skills.relationships import RelationshipCandidate


def _row(**kw):
    base = {"measured_at": utcnow(), "assessment": {"approvable": True, "warnings": []}}
    return SimpleNamespace(**(base | kw))


def test_a_decision_needs_a_fresh_corroborated_measurement():
    review._check_acceptable(_row())
    with pytest.raises(Conflict, match="older than 7 days"):
        review._check_acceptable(_row(measured_at=utcnow() - timedelta(days=8)))
    with pytest.raises(Conflict, match="matching names and types") as refused:
        review._check_acceptable(_row(assessment={"approvable": False, "warnings": ["GENERIC_COLUMN_NAME"]}))
    assert "GENERIC_COLUMN_NAME" in refused.value.message


def test_a_lapsed_request_is_closed_before_it_is_re_requested():
    pending = SimpleNamespace(status="pending", reason=None)
    review._lapse(pending)
    assert pending.status == "invalidated" and "re-requested" in pending.reason
    decided = SimpleNamespace(status="approved", reason=None)
    review._lapse(decided)
    assert decided.status == "approved"
    review._lapse(None)


def test_the_content_hash_moves_with_the_measurement():
    c = RelationshipCandidate(from_asset="s.orders", from_column="customer_id", to_asset="s.customers", to_column="customer_id",
                              cardinality="many_to_one", confidence=0.9,
                              evidence={"fk_rows": 10, "fk_distinct": 4, "matched_rows": 10, "target_rows": 4, "target_distinct": 4})
    same = c.model_copy(deep=True)
    grown = c.model_copy(deep=True)
    grown.evidence["target_distinct"] = 3
    grown.cardinality = "many_to_many"
    assert review._content_hash(c) == review._content_hash(same) != review._content_hash(grown)

"""P4-U04 trust-field rules for a human edit in the knowledge studio (no services)."""
from __future__ import annotations

import pytest

from analystos.core.errors import InvalidInput
from analystos.knowledge.studio import _checked_frontmatter

PREV = {"type": "Glossary Term", "title": "Reopen rate",
        "verified": [{"by": "process:analystos-rev", "at": "2026-09-01T00:00:00+00:00"}],
        "analystos": {"kind": "term", "review": {"suggestion_id": "ksug_1", "origin": "crawler.enrichment"}}}


def test_verified_entries_can_be_kept_or_dropped_but_never_forged():
    import datetime as dt

    # YAML reads the instant as a datetime, the browser sends it back as text: still the same entry
    prev = {**PREV, "verified": [{"by": "process:analystos-rev", "at": dt.datetime(2026, 9, 1, tzinfo=dt.UTC)}]}
    kept = _checked_frontmatter({**PREV}, prev, "u1", False)
    assert [e["by"] for e in kept["verified"]] == ["process:analystos-rev"]
    dropped = _checked_frontmatter({**PREV, "verified": []}, PREV, "u1", False)
    assert "verified" not in dropped
    with pytest.raises(InvalidInput, match="mark_reviewed"):
        _checked_frontmatter({**PREV, "verified": [*PREV["verified"], {"by": "human:u2"}]}, PREV, "u1", False)
    with pytest.raises(InvalidInput):
        _checked_frontmatter({**PREV, "verified": [{"by": "human:u1", "at": "2020-01-01T00:00:00Z"}]}, PREV, "u1", False)
    mine = _checked_frontmatter({**PREV}, PREV, "u1", True)
    assert [e["by"] for e in mine["verified"]] == ["process:analystos-rev", "human:u1"]
    again = _checked_frontmatter({**mine}, mine, "u1", True)
    assert [e["by"] for e in again["verified"]] == ["process:analystos-rev", "human:u1"]  # one entry per person


def test_a_human_edit_turns_a_queue_draft_into_owner_content():
    out = _checked_frontmatter({**PREV}, PREV, "u1", False)
    assert "review" not in out["analystos"] and out["analystos"]["reviewed_draft"]["suggestion_id"] == "ksug_1"
    forged = _checked_frontmatter({"type": "Note", "analystos": {"review": {"suggestion_id": "x"}}}, None, "u1", False)
    assert forged["analystos"] == {"origin": "user"}  # a person cannot mark a document as replaceable by the queue
    kept = _checked_frontmatter({**out}, out, "u1", False)
    assert kept["analystos"]["reviewed_draft"]["suggestion_id"] == "ksug_1"


def test_a_human_edit_of_a_crawler_document_is_curated_so_no_crawler_replaces_it():
    from analystos.knowledge.drafts import is_curated

    crawled = {"type": "Table", "title": "sn.incident", "status": "draft", "analystos": {"kind": "table", "origin": "crawler:crl_1"}}
    assert not is_curated(crawled)
    edited = _checked_frontmatter({**crawled, "description": "One row per incident."}, crawled, "u1", False)
    assert is_curated(edited) and edited["analystos"]["origin"] == "user"
    assert edited["analystos"]["origin_before_edit"] == "crawler:crl_1"
    again = _checked_frontmatter({**edited}, edited, "u1", False)
    assert again["analystos"]["origin_before_edit"] == "crawler:crl_1"  # the machine origin is not lost on a second edit


def test_type_and_staleness_are_checked():
    with pytest.raises(InvalidInput, match="type"):
        _checked_frontmatter({"title": "x"}, None, "u1", False)
    with pytest.raises(InvalidInput, match="stale_after"):
        _checked_frontmatter({"type": "Note", "stale_after": "soon"}, None, "u1", False)
    assert "stale_after" not in _checked_frontmatter({"type": "Note", "stale_after": ""}, None, "u1", False)

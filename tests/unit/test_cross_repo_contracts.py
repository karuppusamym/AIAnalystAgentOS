"""P4-G01 (ADR-0017, proposed): the contracts offered to Atlas and DataPilot stay equal to the code
that implements them here — the OKF pin, the decision-purpose schema and the approval-hash vectors."""
from __future__ import annotations

import json
import re

import pytest
from jsonschema import Draft202012Validator

from analystos.core.config import REPO_ROOT

CONTRACTS = REPO_ROOT / "contracts"


def _schema() -> dict:
    return json.loads((CONTRACTS / "decision_purpose.schema.json").read_text())


def test_okf_pin_matches_the_code():
    from analystos.knowledge import okf

    pin = json.loads((CONTRACTS / "okf_profile_pin.json").read_text())
    assert (pin["repository"], pin["path"], pin["revision"], pin["sha256"], pin["okf_version"], pin["conformance_status"]) == (
        okf.OKF_SPEC_REPOSITORY, okf.OKF_SPEC_PATH, okf.OKF_SPEC_REVISION, okf.OKF_SPEC_SHA256, okf.OKF_VERSION, okf.CONFORMANCE_STATUS)
    profile = (REPO_ROOT / "docs" / "10-architecture" / "okf-profile.md").read_text()
    assert pin["revision"] in profile and pin["sha256"] in profile


def test_decision_schema_enums_equal_the_code():
    from analystos.decisions.types import AUTHORITY_CLASSES, BACKENDS

    schema = _schema()
    Draft202012Validator.check_schema(schema)
    defs = schema["$defs"]
    assert sorted(c["const"] for c in defs["authority_class"]["oneOf"]) == sorted(AUTHORITY_CLASSES)
    assert defs["backend"]["enum"] == list(BACKENDS)
    assert defs["kind"]["enum"] == ["choice", "probability", "scores"]


def test_every_configured_purpose_validates():
    from analystos.decisions.config import load

    validator = Draft202012Validator(_schema())
    purposes = load().purposes
    assert purposes
    for spec in purposes.values():
        errors = [e.message for e in validator.iter_errors(spec.model_dump())]
        assert not errors, (spec.name, errors)


@pytest.mark.parametrize("body", [
    {"kind": "choice", "authority": "route", "backends": ["jev"]},  # no rules path
    {"kind": "probability", "authority": "escalate_only", "backends": ["jev", "rules"]},  # rules must come first
    {"kind": "probability", "authority": "bounded_stop", "backends": ["jev", "rules"]},
    {"kind": "choice", "authority": "grant", "backends": ["rules"]},  # no such class
    {"kind": "choice", "authority": "route", "backends": ["rules", "gpt"]},  # no such backend
])
def test_schema_refuses_what_the_code_refuses(body):
    from analystos.decisions.config import PurposeSpec

    with pytest.raises(ValueError):
        PurposeSpec(name="p", **body)
    assert list(Draft202012Validator(_schema()).iter_errors({"name": "p", **body}))


def test_decision_record_shape_matches_a_decision():
    from analystos.decisions.types import Decision

    record = _schema()["$defs"]["decision_record"]
    fields = set(Decision.__dataclass_fields__)
    assert set(record["required"]) <= fields
    assert set(record["properties"]) - {"cost_usd", "latency_ms"} <= fields


def test_approval_hash_vectors():
    from analystos.core.ids import canonical_json, stable_hash
    from analystos.runtime.plan import plan_hash

    text = (CONTRACTS / "approval_hash.md").read_text()
    vectors = json.loads(re.search(r"## 4\. Test vectors\s+```json\n(.*?)```", text, re.S).group(1))
    assert len(vectors) >= 5
    for v in vectors:
        assert canonical_json(v["payload"]) == v["canonical"]
        assert stable_hash(v["payload"]) == v["sha256"]
    got = plan_hash({"tasks": [{"key": "profile", "agent": "profiler"}]}, constraints={"filters": []}, scope_hash="0" * 64,
                    plan_version=1, capabilities=["playbook.investigate@1", "agent.profiler@1"])
    assert got in text

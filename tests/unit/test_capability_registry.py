"""Capability manifests and registry (ADR-0011, P4-X01 foundation)."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from analystos.capabilities import registry as reg
from analystos.contracts.capability import CapabilityManifest


def _m(**kw):
    return {"kind": "Method", "id": "method.demo", "summary": "demo", **kw}


def test_manifest_rules():
    m = CapabilityManifest.model_validate(_m())
    assert m.side_effect == "write_external" and m.needs_approval  # unclassified = most dangerous
    assert not m.autonomous_ok and m.ref == "method.demo@1.0.0"
    for bad in (_m(id="tool.demo"), _m(id="Method.Demo"), _m(version="v1"), _m(entry="shell:rm -rf")):
        with pytest.raises(ValidationError):
            CapabilityManifest.model_validate(bad)


def test_legacy_capabilities_are_discoverable_and_resolve():
    snap = reg.load(packs_dir=None, entry_points=False)
    kinds = {m.kind for m in snap.list()}
    assert {"Tool", "Skill", "Agent"} <= kinds
    assert snap.get("tool.superset_publish").needs_approval
    assert snap.get("tool.sql_execute").side_effect == "read_source"
    assert callable(reg.resolve_entry(snap.get("skill.sql_explanation")))
    assert snap.find("skill.pii_*") and snap.bind(["skill.crawl_diff"]) == ["skill.crawl_diff@1.0.0"]


def _pack(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "packs" / "demo"
    d.mkdir(parents=True)
    (d / "m.yaml").write_text(body)
    return tmp_path / "packs"


def test_pack_with_unknown_requirement_fails_the_load(tmp_path):
    packs = _pack(tmp_path, "apiVersion: analystos/v1\nkind: Method\nid: method.cohort\nsummary: s\nrequires: [skill.no_such]\n")
    with pytest.raises(reg.CapabilityLoadError, match="requires unknown capability skill.no_such"):
        reg.load(packs_dir=packs, entry_points=False)
    lenient = reg.load(packs_dir=packs, entry_points=False, strict=False)
    assert lenient.problems and "method.cohort" in lenient.manifests


def test_pack_cannot_shadow_a_builtin_and_versions_change_the_digest(tmp_path):
    packs = _pack(tmp_path, "apiVersion: analystos/v1\nkind: Skill\nid: skill.crawl_diff\nsummary: evil\n")
    with pytest.raises(reg.CapabilityLoadError, match="duplicate capability id"):
        reg.load(packs_dir=packs, entry_points=False)
    base = reg.load(packs_dir=None, entry_points=False)
    extra = reg.load(packs_dir=None, entry_points=False,
                     extra=[("test", _m(id="method.extra", side_effect="read_source"))])
    assert base.digest != extra.digest and "method.extra" in extra.manifests


def test_reload_swaps_snapshot_but_old_snapshot_keeps_its_versions():
    old = reg.reload(packs_dir=None, entry_points=False)
    new = reg.reload(packs_dir=None, entry_points=False, extra=[("test", _m(id="method.hot", version="2.0.0"))])
    assert "method.hot" in new.manifests and "method.hot" not in old.manifests
    assert reg.current() is new
    reg.reload(packs_dir=None, entry_points=False)

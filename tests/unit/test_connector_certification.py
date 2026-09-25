"""Connector SDK on the capability model (P4-X08): every source kind is a `kind: Connector` manifest
generated from the kind catalog, and a kind is certified only while its dated live evidence file exists."""
from __future__ import annotations

from types import SimpleNamespace

from analystos.capabilities import registry
from analystos.connectors import certification as cert
from analystos.connectors import kinds


def _evidence(folder, kind="mysql", day="20260925", *, result="pass", suffix="", **over) -> None:
    meta = {"kind": kind, "date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "engine": "MySQL 8.4 (docker mysql:8.4)",
            "test": "tests/integration/test_generic_sources.py -k mysql", "result": result, **over}
    body = "---\n" + "\n".join(f"{k}: {v}" for k, v in meta.items() if v is not None) + "\n---\n# evidence\n"
    (folder / f"connector-{kind}-{day}{suffix}.md").write_text(body)


def test_certified_flag_flips_only_when_the_evidence_file_exists(tmp_path):
    assert cert.statuses(tmp_path)["mysql"]["status"] == "tested"
    _evidence(tmp_path)
    s = cert.statuses(tmp_path)
    assert s["mysql"]["status"] == "certified" and s["mysql"]["evidence_date"] == "2026-09-25"
    assert all(v["status"] == "tested" for k, v in s.items() if k != "mysql")
    (tmp_path / "connector-mysql-20260925.md").unlink()
    assert cert.statuses(tmp_path)["mysql"]["status"] == "tested"  # recomputed: no stale cache


def test_invalid_evidence_does_not_certify(tmp_path):
    _evidence(tmp_path, result="fail")
    _evidence(tmp_path, day="20260926", engine=None)  # frontmatter incomplete
    _evidence(tmp_path, day="20260927", suffix="-x", kind="mysql", date="2026-01-01")  # date mismatch
    (tmp_path / "connector-mysql-2026.md").write_text("---\nkind: mysql\n---\n")  # not a dated name
    (tmp_path / "connector-oracle-20260925.md").write_text("no frontmatter")
    (tmp_path / "connector-postgres-20260925.md").write_text(
        "---\nkind: mysql\ndate: 2026-09-25\nengine: x\ntest: y\nresult: pass\n---\n")  # names another kind
    s = cert.statuses(tmp_path)
    assert {k for k, v in s.items() if v["status"] == "certified"} == set()
    ok, why = cert.parse_evidence(tmp_path / "connector-mysql-20260925.md", "mysql")
    assert ok is None and "not pass" in why


def test_newest_valid_evidence_wins(tmp_path):
    _evidence(tmp_path, day="20260901")
    _evidence(tmp_path, day="20260925", suffix="-rerun")
    ev = cert.evidence_for("mysql", tmp_path)
    assert ev.date.isoformat() == "2026-09-25" and ev.path.endswith("connector-mysql-20260925-rerun.md")


def test_every_catalog_kind_is_a_connector_manifest(tmp_path):
    _evidence(tmp_path, kind="sqlite", engine="SQLite 3")
    manifests = cert.connector_manifests(tmp_path)
    assert [m["id"] for m in manifests] == [f"connector.{k}" for k in kinds.kind_ids()]  # one list: the catalog
    by = {m["id"]: m for m in manifests}
    assert by["connector.sqlite"]["certification"]["status"] == "certified"
    assert by["connector.servicenow"]["certification"]["status"] == "tested"
    assert by["connector.postgres"]["spec"]["pushdown_allowed"] and by["connector.postgres"]["side_effect"] == "read_source"
    snap = registry.load(builtin_dir=tmp_path / "none", packs_dir=None, entry_points=False, legacy=False)
    assert {m.id for m in snap.list("Connector")} == {f"connector.{k}" for k in kinds.kind_ids()}
    assert all(m.certification.evidence for m in snap.list("Connector"))


def test_repository_certifications_match_the_evidence_on_disk():
    """What the product claims is exactly what the evidence folder proves (no hand-kept list)."""
    certified = {k for k, v in cert.statuses().items() if v["status"] == "certified"}
    on_disk = {k for k in kinds.kind_ids() if cert.evidence_for(k) is not None}
    assert certified == on_disk
    assert "servicenow" not in certified  # only ever tested against the bundled mock (spec v1 §62)


def test_source_kinds_api_exposes_certified(monkeypatch, tmp_path):
    from analystos.api.routers import catalog
    from analystos.services import platform_settings

    monkeypatch.setattr(platform_settings, "get", lambda: SimpleNamespace(sources=SimpleNamespace(enabled_kinds=[])))
    monkeypatch.setattr(cert, "EVIDENCE_DIR", tmp_path)
    _evidence(tmp_path, kind="duckdb", engine="DuckDB 1.x")
    rows = {r["kind"]: r for r in catalog.source_kinds(user=None)}
    assert rows["duckdb"]["certified"] is True and rows["mysql"]["certified"] is False
    assert rows["duckdb"]["certification"]["evidence"].endswith("connector-duckdb-20260925.md")
    assert {"kind", "label", "driver_installed", "enabled", "execution_mode"} <= set(rows["mysql"])  # existing fields kept

"""Domain packs (P4-X07, spec v3 §3.6): ITSM specifics live in packs/itsm as data, the core engine is
domain-neutral, and a workspace's runs use the packs its catalog (or its policy) enables."""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path
from types import SimpleNamespace

import pytest

from analystos.agents.investigator import proposals_for_table, validate_spec
from analystos.capabilities import packs
from analystos.contracts.analysis import AnalysisSpec
from analystos.skills import catalog as cat
from analystos.skills import hypothesis_templates as tmpl

SRC = Path(__file__).resolve().parents[2] / "src" / "analystos"

# ServiceNow table and column names. `priority` is checked separately: it is also the platform's own
# hypothesis-priority field. Names are matched as whole words in string literals, docstrings and
# comments (a column name reaches core code as a string; `resolved_at` is also an ORM attribute of
# the platform's own Alert model, which is not a ServiceNow reference).
SERVICENOW_NAMES = ("made_sla", "assignment_group", "reassignment_count", "reopen_count", "cmdb_ci", "caller_id", "sys_id",
                    "opened_at", "resolved_at", "u_name", "sys_user_group", "caused_by", "contact_type", "change_request",
                    "sys_created_on", "sys_updated_on", "work_notes", "incident")
# Only the connector that speaks ServiceNow (and its synthetic data / mock) may name its columns.
EXEMPT = ("connectors/servicenow.py", "connectors/servicenow_mock.py", "connectors/synthetic_servicenow.py")
# Known remaining hits in files owned by other increment-4 rows. Each entry must still match (so the
# list only shrinks) and must be removed with the file's owner (P4-X07 open item).
ALLOWED = {("agents/prompts.py", "reassignment_count"): "few-shot example text; prompts are derived from the method registry in P4-X04"}


def _texts(path: Path) -> list[tuple[int, str]]:
    if path.suffix != ".py":
        return list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT) or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
            out.append((tok.start[0], tok.string))
    return out


def _core_hits() -> dict[tuple[str, str], list[int]]:
    rx = re.compile(r"\b(" + "|".join(SERVICENOW_NAMES) + r")\b")
    hits: dict[tuple[str, str], list[int]] = {}
    for path in sorted(SRC.rglob("*")):
        rel = path.relative_to(SRC).as_posix()
        if not path.is_file() or path.suffix not in (".py", ".yaml", ".yml", ".json", ".md", ".sql", ".j2", ".html") \
                or rel in EXEMPT or "__pycache__" in rel:
            continue
        for line, text in _texts(path):
            for m in rx.finditer(text):
                hits.setdefault((rel, m.group(1)), []).append(line)
    return hits


def test_no_servicenow_column_name_remains_in_core():
    hits = _core_hits()
    unexpected = {k: v for k, v in hits.items() if k not in ALLOWED}
    assert not unexpected, "ServiceNow names in core (move them to packs/itsm): " + \
        "; ".join(f"{f}:{lines} {name}" for (f, name), lines in sorted(unexpected.items()))
    stale = [k for k in ALLOWED if k not in hits]
    assert not stale, f"allow-list entries no longer needed, remove them: {stale}"


def test_priority_column_is_not_special_cased_in_core():
    """`priority` may appear as the hypothesis-priority key, never as a column the code looks for."""
    for rel in ("agents/investigator.py", "agents/sql_agent.py", "skills/hypothesis_templates.py"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert not re.search(r"""["']priority["'](?!\s*[:\]),])""", src), rel
        assert not re.search(r"""==\s*["']priority["']""", src), rel


# ------------------------------------------------------------------------------------ loading
def _pack(name: str) -> packs.DomainPack:
    return next(p for p in packs.installed() if p.name == name)


def test_repository_packs_load_as_knowledge_pack_manifests():
    ids = {p.id for p in packs.installed()}
    assert {"pack.itsm", "pack.sales"} <= ids
    for name in ("itsm", "sales"):
        p = _pack(name)
        assert p.source == f"pack:{name}" and p.version == "1.0.0"
        assert p.templates["templates"] and p.kpis and p.benchmark and p.knowledge
        assert all(d.kind in ("term", "metric", "rule") and d.body for d in p.knowledge)
    itsm = {d.name: d for d in _pack("itsm").knowledge}
    assert itsm["SLA breach"].maps_to == ("incident.made_sla",) and "missed SLA" in itsm["SLA breach"].synonyms
    assert {"MTTR", "Reassignment", "Configuration item"} <= set(itsm)


def test_every_template_names_a_known_method_and_outcome():
    methods = set(AnalysisSpec.model_fields["method"].annotation.__args__)
    for p in packs.installed():
        doc = p.templates
        for t in doc["templates"]:
            assert t["method"] in methods, (p.id, t["id"])
            if t.get("outcome"):
                assert t["outcome"] in doc["outcomes"], (p.id, t["id"])
        for k in p.kpis:
            assert k.get("aggregate") == "count" or k["outcome"] in doc["outcomes"], (p.id, k["name"])


def test_knowledge_documents_need_frontmatter():
    doc = packs.parse_knowledge("---\nkind: rule\nname: X\nmaps_to: [t.c]\n---\nBody text.\n")
    assert doc.kind == "rule" and doc.maps_to == ("t.c",) and doc.body == "Body text."
    with pytest.raises(ValueError):
        packs.parse_knowledge("no frontmatter")
    with pytest.raises(ValueError):
        packs.parse_knowledge("---\nkind: poem\nname: X\n---\nx")


def test_a_broken_pack_is_skipped_not_fatal(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    manifest = "apiVersion: analystos/v1\nkind: KnowledgePack\nid: pack.{n}\nsummary: s\nside_effect: none\nspec: {{templates: {f}}}\n"
    (good / "pack.yaml").write_text(manifest.format(n="good", f="t.yaml"))
    (good / "t.yaml").write_text("templates: []\n")
    (bad / "pack.yaml").write_text(manifest.format(n="bad", f="missing.yaml"))
    loaded = packs.load_packs(tmp_path, entry_points=False)
    assert [p.id for p in loaded] == ["pack.good"]


def test_pack_data_paths_cannot_escape_the_pack(tmp_path):
    with pytest.raises(ValueError):
        packs._data(tmp_path, "../outside.yaml", {})


# ------------------------------------------------------------------------------------ enablement
def test_packs_auto_enable_from_the_selected_catalog_or_follow_the_policy():
    ids = lambda ps: [p.id for p in ps]  # noqa: E731
    assert ids(packs.enabled_for(["incident", "change_request"])) == ["pack.itsm"]
    assert ids(packs.enabled_for(["orders", "customer"])) == ["pack.sales"]
    assert ids(packs.enabled_for(["patients", "encounters"])) == []
    assert ids(packs.enabled_for(["patients"], explicit=["sales"])) == ["pack.sales"]
    assert ids(packs.enabled_for(["incident"], explicit=[])) == []  # an explicit empty list disables packs
    scope = SimpleNamespace(assets=["src_sn.incident"], columns={"src_sn.incident": ["number"]})
    assert ids(packs.for_scope(scope, SimpleNamespace(domain_packs=None))) == ["pack.itsm"]
    assert ids(packs.for_scope(scope, SimpleNamespace(domain_packs=["pack.sales"]))) == ["pack.sales"]


def test_workspace_policy_carries_the_pack_choice():
    from analystos.contracts.policy import WorkspacePolicyDoc

    assert WorkspacePolicyDoc().domain_packs is None
    assert WorkspacePolicyDoc(domain_packs=["itsm"]).domain_packs == ["itsm"]


# ------------------------------------------------------------------------------------ hints
def test_installed_pack_hints_reach_the_deterministic_skills():
    h = packs.hints()
    assert "sys_id" in h.key_columns and h.acronyms["sla"] == "SLA" and "caller" in h.person_nouns
    from analystos.skills.quality import temporal_pairs

    assert ("opened_at", "resolved_at") in temporal_pairs(["opened_at", "resolved_at", "closed_at"])
    assert cat.classify_pii("caller_id_name", "text").category == "person_name"
    sem = cat.infer_table_semantics(cat.DiscoveredAsset(source_name="incident", name="incident", columns=[
        cat.DiscoveredColumn(name="number", data_type="text"), cat.DiscoveredColumn(name="priority", data_type="integer")]))
    assert sem.domain == "it_operations"


def test_merged_hints_only_add(tmp_path):
    a = packs.DomainPack(id="pack.a", version="1", summary="", root=None, applies_when={}, templates={}, kpis=[], knowledge=(),
                         benchmark=None, hints={"key_columns": ["k1"], "domain_keywords": {"x": ["w1"]}, "acronyms": {"ab": "AB"}})
    b = packs.DomainPack(id="pack.b", version="1", summary="", root=None, applies_when={}, templates={}, kpis=[], knowledge=(),
                         benchmark=None, hints={"key_columns": ["k1", "k2"], "domain_keywords": {"x": ["w2"]}})
    h = packs.merge_hints([a, b])
    assert h.key_columns == ("k1", "k2") and h.domain_keywords["x"] == {"w1", "w2"} and h.acronyms == {"ab": "AB"}


# ------------------------------------------------------------------------------------ templates
def _incident_columns() -> list[tmpl.Col]:
    C = tmpl.Col  # noqa: N806
    return [C("sys_id", "id", "identifier", {"distinct": 20000}), C("opened_at", "datetime", "timestamp"),
            C("resolved_at", "datetime", "timestamp"), C("priority", "numeric", "dimension",
                                                         {"distinct": 5, "max": 5, "top_values": [{"value": 3}, {"value": 1}]}),
            C("category", "categorical", "dimension", {"distinct": 5}),
            C("assignment_group_name", "categorical", "name", {"distinct": 12}),
            C("cmdb_ci_name", "categorical", "name", {"distinct": 40}),
            C("reassignment_count", "numeric", "measure", {"distinct": 10, "max": 9}),
            C("made_sla", "boolean", "flag", {"distinct": 2}), C("contact_type", "categorical", "dimension", {"distinct": 5}),
            C("short_description", "categorical", "text", {"distinct": 20})]


def test_itsm_templates_reproduce_the_servicenow_playbook():
    props = proposals_for_table("sn.incident", _incident_columns(), [_pack("itsm")])
    statements = [p["statement"] for p in props]
    assert statements[:6] == [
        "The rate of missed SLA differs materially across reassignment count.",
        "The rate of missed SLA differs materially across assignment group name.",
        "The rate of missed SLA differs materially across category.",
        "The rate of missed SLA differs materially across contact type.",
        "The rate of missed SLA differs materially across opened after hours.",
        "Resolution hours differs materially across opened after hours."]
    by_method = {p["spec"]["method"]: p for p in props}
    rate = props[0]["spec"]
    assert rate["outcome"]["type"] == "equals" and rate["outcome"]["value"] is False
    assert rate["segment"] == {**rate["segment"], "type": "bucket", "column": "reassignment_count", "edges": [0, 1, 2, 3]}
    pareto = by_method["pareto"]
    assert pareto["spec"]["segment"]["column"] == "cmdb_ci_name" and pareto["spec"]["filters"] == [
        {"column": "priority", "op": "=", "value": 1}]
    assert pareto["question"].startswith("Are priority-1 records") and pareto["priority"] == "high"
    assert by_method["trend"]["spec"]["time"] == {"type": "date_trunc", "column": "opened_at", "grain": "week"}
    assert len(by_method["driver_model"]["spec"]["drivers"]) == 5
    assert all(p.get("pack") == "pack.itsm" for p in props if p["spec"]["method"] != "numeric_by_segment" or "pack" in p)
    scope = SimpleNamespace(assets=["sn.incident"], columns={"sn.incident": [c.name for c in _incident_columns()]}, denied_columns=[])
    types = {"sn.incident": {c.name: c.semantic_type for c in _incident_columns()}}
    assert all(not validate_spec(AnalysisSpec.model_validate(p["spec"]), scope, types) for p in props)


def test_pareto_template_runs_unfiltered_without_a_severity_column():
    cols = [c for c in _incident_columns() if c.name != "priority"]
    pareto = next(p for p in proposals_for_table("sn.incident", cols, [_pack("itsm")]) if p["spec"]["method"] == "pareto")
    assert "filters" not in pareto["spec"] and pareto["question"] == "Are records concentrated in a few cmdb ci name values?"


def test_without_packs_the_core_playbook_is_role_driven_and_still_covers_flags():
    props = proposals_for_table("sn.incident", _incident_columns(), [])
    assert props and all("pack" not in p for p in props)
    flags = [p for p in props if p["spec"]["method"] == "rate_by_segment"]
    assert flags and all(p["spec"]["outcome"] == {**p["spec"]["outcome"], "type": "is_true", "column": "made_sla"} for p in flags)
    assert {p["spec"]["segment"]["column"] for p in flags} <= {"category", "assignment_group_name", "contact_type"}
    with_pack = proposals_for_table("sn.incident", _incident_columns(), [_pack("itsm")])
    assert not any(p["spec"].get("outcome", {}).get("type") == "is_true" for p in with_pack)  # the pack owns the flag


def test_a_template_binds_by_role_and_pattern_not_by_column_name():
    C = tmpl.Col  # noqa: N806
    cols = [C("ticket_ref", "id", "identifier"), C("reported_on", "datetime", "timestamp"), C("closed_on", "datetime", "timestamp"),
            C("met_target", "boolean", "flag", {"distinct": 2}), C("team_name", "categorical", "name", {"distinct": 8}),
            C("hops_count", "numeric", "measure", {"distinct": 6, "max": 7})]
    props = tmpl.propose("x.tickets", cols, _pack("itsm").templates, packs.hints().acronyms)
    first = props[0]
    assert first["spec"]["outcome"]["column"] == "met_target" and first["spec"]["outcome"]["value"] is False
    assert first["statement"] == "The rate of missed target differs materially across hops count."
    assert any(p["spec"]["method"] == "numeric_by_segment" and p["spec"]["outcome"]["end_column"] == "closed_on" for p in props)


def test_sales_templates_ask_the_canonical_questions_first():
    C = tmpl.Col  # noqa: N806
    cols = [C("order_id", "id", "identifier"), C("order_date", "datetime", "date"), C("channel", "categorical", "dimension", {"distinct": 4}),
            C("sales_region", "categorical", "geo", {"distinct": 4}), C("customer_segment", "categorical", "dimension", {"distinct": 3}),
            C("net_amount", "numeric", "amount", {"distinct": 900}), C("shipping_days", "numeric", "duration", {"distinct": 11}),
            C("returned", "boolean", "flag", {"distinct": 2})]
    props = tmpl.propose("s.orders", cols, _pack("sales").templates, packs.hints().acronyms)
    firsts = [(p["spec"]["outcome"]["column"], p["spec"]["segment"]["column"], p["priority"]) for p in props[:3]]
    assert firsts == [("returned", "channel", "high"), ("net_amount", "customer_segment", "high"),
                      ("shipping_days", "sales_region", "high")]
    kpis = {k["name"]: k for k in tmpl.bind_kpis(cols, _pack("sales").templates, _pack("sales").kpis)}
    assert kpis["return_rate"]["derivation"]["column"] == "returned" and kpis["order_count"]["derivation"] is None
    assert kpis["net_revenue"]["derivation"]["column"] == "net_amount"


def test_display_preference_comes_from_packs():
    assert tmpl.prefer_display(["category", "group_name"], [r"_name$"]) == ["group_name", "category"]
    assert tmpl.prefer_display(["category", "group_name"], []) == ["category", "group_name"]

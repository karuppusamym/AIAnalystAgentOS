"""P4-08 transfer failure class: the same data under column names in another language (or opaque, or reordered)
reaches the same verdicts. The held-out v2 run missed planted effects on German and abbreviated names because a
capped playbook kept segments by column position and a key named in German was profiled as a measure. Uses a
development seed, never a held-out one. No services (DuckDB in process)."""
from __future__ import annotations

import pytest
from evaluation import analytical as A
from evaluation.heldout import generators as G

from analystos.agents.investigator import proposals_for_table
from analystos.connectors.base import DiscoveredColumn
from analystos.skills import catalog as cat
from analystos.skills.lexicon import canonical_tokens, fold, raw_tokens
from analystos.skills.profiling import infer_semantic_type, surrogate_key_shape

ENGLISH = ["receivable_id", "issue_date", "customer_tier", "payment_terms", "collector", "invoice_currency",
           "outstanding_amount", "days_overdue", "written_off"]
RENAMES = {
    "german": ["forderung_nr", "belegdatum", "kundenklasse", "zahlungsziel", "sachbearbeiter", "waehrung",
               "offener_betrag", "tage_ueberfaellig", "abgeschrieben"],
    "german_unicode": ["Forderung_Nr", "Belegdatum", "Kundenklasse", "Zahlungsziel", "Sachbearbeiter", "Währung",
                       "Offener_Betrag", "Tage_Überfällig", "Abgeschrieben"],
    "spanish": ["numero_factura", "fecha_emision", "clase_cliente", "plazo_pago", "gestor_cobro", "moneda",
                "importe_pendiente", "dias_vencido", "cancelado"],
    "tamil": ["விலைப்பட்டியல்_எண்", "வெளியீட்டு_தேதி", "வாடிக்கையாளர்_வகை", "கட்டண_காலம்", "வசூலிப்பவர்",
              "நாணயம்", "நிலுவை_தொகை", "தாமத_நாட்கள்", "தள்ளுபடி_செய்யப்பட்டது"],
    "opaque": ["k0", "t1", "c1", "c2", "c3", "c4", "n1", "n2", "b1"],
}


def _verdicts(ds, back: dict[str, str]) -> set[tuple]:
    score = A.run_component("transfer", 11, ds=ds)
    assert score.status == "COMPLETED"
    return {(f.method, back.get(f.outcome, f.outcome), back.get(f.segment, f.segment), None if f.top is None else str(f.top))
            for f in score.findings}


@pytest.fixture(scope="module")
def english():
    ds = G.finance_receivable(11, n=2000)
    return ds, _verdicts(ds, {})  # English names in the generator's column order


def test_english_verdicts_find_the_planted_effects(english):
    _, verdicts = english
    assert ("numeric_by_segment", "days_overdue", "payment_terms", "NET90") in verdicts
    assert ("rate_by_segment", "written_off", "customer_tier", "Small") in verdicts


@pytest.mark.parametrize("language", sorted(RENAMES))
def test_renamed_columns_reach_the_same_verdicts(english, language):
    ds, verdicts = english
    mapping = dict(zip(ENGLISH, RENAMES[language], strict=True))
    # renamed, with the columns and rows reordered (a different order per language, as in the held-out transfer suite)
    renamed = G.transfer(ds, {"rename": mapping, "table": "t_" + language}, 11 + sorted(RENAMES).index(language))
    assert _verdicts(renamed, {v: k for k, v in mapping.items()}) == verdicts


def test_column_order_never_decides_which_hypotheses_are_proposed():
    """Every low-cardinality segment is crossed with every outcome (the held-out tables have four or five), so
    reordering the columns reorders the playbook but never drops a hypothesis from it."""
    ds = G.sales_opportunity(12, n=1500)
    orders = [list(ds.frame.columns), list(reversed(ds.frame.columns))]
    seen = []
    for order in orders:
        frame = ds.frame[order]
        run_sql = A._duck(type(ds)(ds.domain, ds.table, frame, {}, [], []))
        cols = A._describe(run_sql, f"{A.SCHEMA}.{ds.table}")
        seen.append([(p["spec"]["method"], (p["spec"].get("outcome") or {}).get("column"),
                      (p["spec"].get("segment") or {}).get("column")) for p in proposals_for_table("s.t", cols, [])])
    assert sorted(seen[0], key=str) == sorted(seen[1], key=str)
    assert {s for _, _, s in seen[0]} >= {"lead_source", "industry", "sales_team", "product_line"}


# ------------------------------------------------------------------------------------ the pieces
def test_folding_and_tokens_are_transliteration_safe():
    assert fold("Tage_Überfällig") == "Tage_Ueberfaellig" and fold("Straße") == "Strasse"
    assert fold("número") == "numero"
    assert raw_tokens("வாடிக்கையாளர்_எண்") == ["வாடிக்கையாளர்", "எண்"]  # vowel signs stay with their word
    assert canonical_tokens("வாடிக்கையாளர்_எண்") == ["customer", "no"]
    assert canonical_tokens("kundenklasse") == ["customer", "class"]
    assert canonical_tokens("rechnungsbetrag") == ["invoice", "amount"]
    assert canonical_tokens("fechaCreación")[:1] == ["date"]


@pytest.mark.parametrize("name", ["receivable_id", "SLADueDate", "customerID", "order_count", "total_amount", "tag",
                                  "quote_status", "lager_volume", "created_at", "is_active", "resolution_hours"])
def test_english_names_canonicalise_to_themselves(name):
    assert canonical_tokens(name) == cat.split_tokens(name)


@pytest.mark.parametrize("name,dtype,role", [
    ("belegdatum", "date", "date"), ("tage_ueberfaellig", "integer", "duration"), ("offener_betrag", "double", "amount"),
    ("anzahl_positionen", "integer", "measure"), ("fecha_emision", "date", "date"), ("importe_total", "numeric", "amount"),
    ("கட்டண_தொகை", "double", "amount"), ("waehrung", "text", "dimension"), ("beschreibung", "text", "text"),
])
def test_roles_read_through_the_lexicon(name, dtype, role):
    assert cat.infer_column_semantics(DiscoveredColumn(name=name, data_type=dtype)).semantic_role == role


def test_a_generated_key_is_an_id_whatever_its_name():
    shape = {"row_count": 500, "non_null": 500, "distinct": 500, "min_value": 800001, "max_value": 800500}
    assert surrogate_key_shape("BIGINT", non_null=500, distinct=500, min_value=800001, max_value=800500)
    for name in ("receivable_id", "forderung_nr", "oid", "k0", "விலைப்பட்டியல்_எண்"):
        assert infer_semantic_type(name, "numeric", data_type="BIGINT", **shape) == "id", name
    # a unique integer measure spreads far wider than its count; a double never is a generated key
    assert infer_semantic_type("amount_cents", "numeric", data_type="BIGINT", row_count=500, non_null=500, distinct=500,
                               min_value=12, max_value=9_000_000) == "numeric"
    assert infer_semantic_type("score", "numeric", data_type="DOUBLE", **shape) == "numeric"
    assert infer_semantic_type("kunden_nr", "numeric", row_count=100, non_null=100, distinct=95) == "id"


@pytest.mark.parametrize("language", sorted(RENAMES))
def test_a_rename_in_place_yields_the_same_playbook_in_the_same_order(language):
    """The platform tests a budget of hypotheses from the front of the playbook, so a rename that keeps the column
    order must keep the proposal order too (the key named in another language no longer takes a measure's place)."""
    ds = G.finance_receivable(13, n=800)
    mapping = dict(zip(ENGLISH, RENAMES[language], strict=True))
    back = {v: k for k, v in mapping.items()}
    seen = []
    for frame, table in ((ds.frame, ds.table), (ds.frame.rename(columns=mapping), "t_" + language)):
        run_sql = A._duck(type(ds)(ds.domain, table, frame, {}, [], []))
        cols = A._describe(run_sql, f"{A.SCHEMA}.{table}")
        seen.append([(p["spec"]["method"], back.get((p["spec"].get("outcome") or {}).get("column")) or
                      (p["spec"].get("outcome") or {}).get("column"),
                      back.get((p["spec"].get("segment") or {}).get("column")) or (p["spec"].get("segment") or {}).get("column"))
                     for p in proposals_for_table("s.t", cols, []) if p["spec"]["method"] != "trend"])
    assert seen[0] == seen[1]

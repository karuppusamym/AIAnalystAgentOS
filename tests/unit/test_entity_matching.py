"""General entity matching (INT-004, N-7): normalisation, hashed features, blocking, bands, one-to-one links,
measured precision/recall on the seeded synthetic people with known truth, and the service's pure checks
(scope, PII keys, bounded gateway reads, one-to-one crosswalk, approval payload, reviewed join keys)."""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest
from evaluation import entity_match_datasets as D

from analystos.contracts.policy import DataScope
from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.skills import entity_matching as em

PII_LEFT = {"full_name", "email", "phone", "birth_date"}
PII_RIGHT = {"holder_name", "contact_email", "tel", "dob"}


def _spec(**over) -> em.MatchSpec:
    return em.MatchSpec.model_validate(D.spec(**over))


# ------------------------------------------------------------------------------------ normalisation
def test_normalisers() -> None:
    assert em.norm_name("Dr. SMITH, John") == em.norm_name("john smith") == "john smith"
    assert em.norm_name("Zoë Ødegaard") == em.norm_name("zoe odegaard")
    assert em.norm_email(" John.Smith+billing@GMail.com ") == em.norm_email("johnsmith@googlemail.com") == "johnsmith@gmail.com"
    assert em.norm_email("a.b+x@example.com") == "a.b@example.com"  # dots matter outside gmail
    assert em.norm_phone("+1 (538) 393-6607") == em.norm_phone("538.393.6607") == "5383936607"
    assert em.norm_address("4015 Willow Lane") == em.norm_address("4015 WILLOW LN.") == "4015 willow ln"
    assert em.norm_postcode("12430-1234") == em.norm_postcode("12430") == "12430"
    assert em.norm_postcode("sw1a 1aa") == "SW1A1AA"
    assert em.norm_id("000123") == em.norm_id("123")
    assert em.date_readings("1977-07-10") == ["1977-07-10"]
    assert em.date_readings("10 Jul 1977") == ["1977-07-10"]
    assert set(em.date_readings("07/10/1977")) == {"1977-10-07", "1977-07-10"}  # day/month ambiguous: both kept
    assert em.soundex("Robert") == em.soundex("Rupert") == "R163"
    assert em.soundex("Ashcraft") == "A261"


def test_similarity_by_type() -> None:
    f = em.feature
    assert em.similarity(f("a@x.com", "email"), f("A@X.COM", "email"), "email") == 1.0
    assert em.similarity(f("a@x.com", "email"), f("a@y.com", "email"), "email") == 0.7
    assert em.similarity(f("07/10/1977", "date"), f("1977-07-10", "date"), "date") == 0.9
    assert em.similarity(f("1977-07-10", "date"), f("1977-07-11", "date"), "date") == 0.0
    assert em.similarity(f("555 123 4567", "phone"), f("(444) 123-4567", "phone"), "phone") == 0.8
    assert 0.6 < em.similarity(f("Kimberly Johnson", "name"), f("JOHNSON, Kimmberly", "name"), "name") < 1.0
    assert em.similarity(f("ACC-1", "id"), f("ACC-2", "id"), "id") == 0.0
    assert f(None, "name") is None and f("  ", "name") is None


def test_hashed_features_compare_like_plain_ones_and_hold_no_value() -> None:
    h = em.workspace_hasher("secret", "ws_1")
    for a, b, t in [("Kimberly Johnson", "JOHNSON, Kimmberly", "name"), ("a.b@x.com", "a.b+z@x.com", "email"),
                    ("12 Main Street", "12 MAIN ST.", "address"), ("5551234567", "+1 555 123 4567", "phone"),
                    ("03/04/1990", "1990-04-03", "date")]:
        plain = em.similarity(em.feature(a, t), em.feature(b, t), t)
        hashed = em.similarity(em.feature(a, t, h), em.feature(b, t, h), t)
        assert plain == hashed
    hf = em.feature("Kimberly Johnson", "name", h)
    dump = repr(hf).lower()
    assert "kimberly" not in dump and "johnson" not in dump and "kim" not in dump
    # the key differs per workspace: the same value gives another digest elsewhere
    assert em.feature("x@y.com", "email", h).exact != em.feature("x@y.com", "email", em.workspace_hasher("secret", "ws_2")).exact


# ------------------------------------------------------------------------------------ spec, blocking, bands
def test_spec_validation() -> None:
    with pytest.raises(ValueError, match="review_threshold"):
        _spec(match_threshold=0.5, review_threshold=0.7)
    with pytest.raises(ValueError, match="plain identifiers"):
        _spec(left={"asset": "crm.customers", "key": "id; drop"})
    with pytest.raises(ValueError, match="twice"):
        _spec(fields=[{"left": "a", "right": "b", "type": "name"}, {"left": "a", "right": "b", "type": "text"}])
    s = _spec()
    assert s.left_columns()[0] == "customer_id" and s.fields[1].effective_weight == em.DEFAULT_WEIGHTS["email"]


def _rows(key: str, names: list[str]) -> list[dict]:
    return [{key: f"{key}{i}", "n": n} for i, n in enumerate(names)]


NAME_SPEC = {"name": "t", "left": {"asset": "a.l", "key": "lk"}, "right": {"asset": "a.r", "key": "rk"},
             "fields": [{"left": "n", "right": "n", "type": "name"}], "match_threshold": 0.9, "review_threshold": 0.5}


def test_oversized_blocks_are_skipped_and_the_budget_refuses() -> None:
    spec = em.MatchSpec.model_validate({**NAME_SPEC, "max_block_size": 3})
    left = _rows("lk", ["Ann Smith"] * 4 + ["Bob Jones"])
    right = _rows("rk", ["Ann Smith"] * 4 + ["Bob Jones"])
    res = em.link_records(left, right, spec)
    assert res.stats["blocks_skipped"] >= 1  # the Smith/Ann block (4 x 4) separates nothing
    assert [(p.left_key, p.right_key) for p in res.pairs] == [("lk4", "rk4")]
    tight = em.MatchSpec.model_validate({**NAME_SPEC, "max_comparisons": 3})
    with pytest.raises(em.BlockingTooLoose):
        em.link_records(_rows("lk", ["Ann Smith"] * 3), _rows("rk", ["Ann Smith"] * 3), tight)


def test_bands_and_one_to_one() -> None:
    spec = em.MatchSpec.model_validate(NAME_SPEC)
    left = _rows("lk", ["Kimberly Johnson", "Robert White"])
    right = _rows("rk", ["Kimberly Johnson", "Kimberley Johnson", "Robest White"])
    res = em.link_records(left, right, spec)
    got = {(p.left_key, p.right_key): p.band for p in res.pairs}
    assert got[("lk0", "rk0")] == "match"
    assert ("lk0", "rk1") not in got and res.stats["superseded"] >= 1  # one link per record, strongest first
    assert got[("lk1", "rk2")] == "review"
    many = em.link_records(left, right, em.MatchSpec.model_validate({**NAME_SPEC, "one_to_one": False}))
    assert {("lk0", "rk0"), ("lk0", "rk1")} <= {(p.left_key, p.right_key) for p in many.pairs}


def test_thin_evidence_is_reviewed_however_high_it_scores() -> None:
    body = {**NAME_SPEC, "fields": [{"left": "n", "right": "n", "type": "name"},
                                     {"left": "e", "right": "e", "type": "email"}]}
    left, right = [{"lk": "1", "n": "Ann Lee", "e": None}], [{"rk": "x", "n": "Ann Lee", "e": "ann@x.com"}]
    thin = em.link_records(left, right, em.MatchSpec.model_validate(body))
    assert thin.pairs[0].score == 1.0 and thin.pairs[0].band == "review" and thin.stats["thin_evidence"] == 1
    loose = em.link_records(left, right, em.MatchSpec.model_validate({**body, "min_match_coverage": 0.3}))
    assert loose.pairs[0].band == "match"


def test_keys_must_be_unique_and_rows_without_key_are_counted() -> None:
    spec = em.MatchSpec.model_validate(NAME_SPEC)
    with pytest.raises(ValueError, match="not unique"):
        em.link_records([{"lk": "1", "n": "Ann Lee"}, {"lk": "1", "n": "Bo Chan"}], [{"rk": "x", "n": "Ann Lee"}], spec)
    res = em.link_records([{"lk": None, "n": "Ann Lee"}, {"lk": "2", "n": "Ann Lee"}], [{"rk": "x", "n": "Ann Lee"}], spec)
    assert res.stats["left_rows_without_key"] == 1 and len(res.pairs) == 1
    with pytest.raises(ValueError, match="hasher"):
        em.link_records([], [], spec, pii_left={"n"})


# ------------------------------------------------------------------------------------ measured on known truth
@pytest.fixture(scope="module")
def measured():
    left, right, truth = D.people(400, seed=11)
    spec = _spec()
    plain = em.link_records(left, right, spec)
    hashed = em.link_records(left, right, spec, pii_left=PII_LEFT, pii_right=PII_RIGHT,
                             hasher=em.workspace_hasher("k", "ws"))
    return SimpleNamespace(left=left, right=right, truth=truth, plain=plain, hashed=hashed,
                           metrics=em.evaluate(plain.pairs, truth))


def test_precision_and_recall_on_synthetic_truth(measured) -> None:
    m = measured.metrics
    assert m["truth"] == 280
    assert m["match_band"]["precision"] >= 0.99  # the auto band is never a namesake or a relative
    assert m["match_band"]["recall"] >= 0.85
    assert m["match_and_review"]["recall"] >= 0.97  # a reviewer accepting the true review pairs finds nearly all
    assert m["match_and_review"]["precision"] >= 0.97


def test_hard_negatives_land_in_the_review_band_only(measured) -> None:
    false = [p for p in measured.plain.pairs if (p.left_key, p.right_key) not in measured.truth]
    assert all(p.band == "review" for p in false)


def test_pii_hashing_does_not_change_any_link(measured) -> None:
    key = [(p.left_key, p.right_key, p.band, p.score) for p in measured.plain.pairs]
    assert key == [(p.left_key, p.right_key, p.band, p.score) for p in measured.hashed.pairs]
    assert measured.plain.stats["left_version"] != measured.hashed.stats["left_version"]  # digests, not values
    raw = {str(v).lower() for r in measured.left for c, v in r.items() if c in PII_LEFT and v}
    stored = repr([(p.left_key, p.right_key, p.fields) for p in measured.hashed.pairs]).lower()
    assert not any(v in stored for v in list(raw)[:200])


def test_seeded_fixture_is_reproducible() -> None:
    assert D.people(50, seed=3) == D.people(50, seed=3)
    assert D.people(50, seed=3)[2] != D.people(50, seed=4)[2] or D.people(50, seed=3)[1] != D.people(50, seed=4)[1]


# ------------------------------------------------------------------------------------ service checks (no services)
def _scope(**over) -> DataScope:
    body = {"workspace_id": "ws", "user_id": "u", "role": "analyst", "source_ids": ["src_a", "src_b"],
            "assets": ["crm.customers", "billing.accounts"],
            "asset_sources": {"crm.customers": "src_a", "billing.accounts": "src_b"},
            "columns": {"crm.customers": D.LEFT_COLUMNS, "billing.accounts": D.RIGHT_COLUMNS}}
    body.update(over)
    return DataScope(**body)


def test_check_spec_refusals() -> None:
    from analystos.services import entity_matching as svc

    spec = _spec()
    svc.check_spec(spec, _scope(), {})
    with pytest.raises(Forbidden, match="not in the authorized scope"):
        svc.check_spec(spec, _scope(assets=["crm.customers"]), {})
    with pytest.raises(Forbidden, match="not an asset of source"):
        svc.check_spec(_spec(left={"asset": "crm.customers", "key": "customer_id", "source_id": "src_b"}), _scope(), {})
    with pytest.raises(InvalidInput, match="no column"):
        svc.check_spec(_spec(fields=[{"left": "nope", "right": "holder_name", "type": "name"}]), _scope(), {})
    with pytest.raises(Forbidden, match="not readable"):
        svc.check_spec(spec, _scope(denied_columns=["billing.accounts.contact_email"]), {})
    with pytest.raises(InvalidInput, match="is PII"):
        svc.check_spec(spec, _scope(), {"crm.customers": {"customer_id"}})


def test_pii_columns_from_tags_and_policy() -> None:
    from analystos.services.entity_matching import pii_columns

    tags = {"email": ["pii"], "full_name": None, "segment": ["sensitive"], "phone": []}
    assert pii_columns("crm.customers", tags, ["*.phone"]) == {"email", "phone"}


class _Runner:
    dialect = "postgres"

    def __init__(self, truncated: bool = False) -> None:
        self.calls: list[dict] = []
        self.truncated = truncated

    def __call__(self, sql, **kw):
        self.calls.append({"sql": sql, **kw})
        return SimpleNamespace(truncated=self.truncated, query_id="qry_1",
                               records=lambda: [{"customer_id": "C1", "full_name": "A B"}])


def test_read_side_is_bounded_quoted_and_retains_nothing() -> None:
    from analystos.services.entity_matching import read_side

    spec = _spec()
    r = _Runner()
    rows, qid = read_side(r, spec.left, ["customer_id", "full_name"], max_rows=100, purpose="entity_match:x:left")
    assert rows and qid == "qry_1"
    call = r.calls[0]
    assert call["sql"] == 'SELECT "customer_id", "full_name" FROM "crm"."customers"'
    assert call["retain_rows"] is False and call["use_cache"] is False and call["max_rows"] == 100
    with pytest.raises(InvalidInput, match="row cap"):
        read_side(_Runner(truncated=True), spec.left, ["customer_id"], max_rows=10, purpose="p")


def test_crosswalk_is_one_to_one_and_hashed_stably() -> None:
    from analystos.services.entity_matching import crosswalk

    pairs = [{"left_key": "L2", "right_key": "R2", "score": 0.9, "band": "match", "decided_by": "user:u"},
             {"left_key": "L1", "right_key": "R1", "score": 0.7, "band": "review", "decided_by": "user:u"}]
    rows, digest = crosswalk(pairs)
    assert [r["left_key"] for r in rows] == ["L1", "L2"]
    assert crosswalk(list(reversed(pairs)))[1] == digest
    with pytest.raises(Conflict, match="more than one pair"):
        crosswalk([*pairs, {"left_key": "L1", "right_key": "R9", "score": 0.6, "band": "review", "decided_by": "u"}])


def test_promotion_payload_and_join_keys() -> None:
    from analystos.services.entity_matching import ACTION, join_keys, promotion_payload
    from analystos.skills.federation import JoinProposal

    run = SimpleNamespace(id="emr_1", spec_hash="s", left_asset="crm.customers", right_asset="billing.accounts",
                          left_version="lv", right_version="rv")
    p = promotion_payload(run, "xh", 12, "xwalk_crm_billing")
    assert p["action"] == ACTION and p["crosswalk_hash"] == "xh" and p["left_version"] == "lv"
    keys = [JoinProposal.model_validate(k) for k in join_keys(_spec(), "src_o.xwalk_crm_billing")]
    assert [(k.from_asset, k.from_column, k.to_asset, k.to_column) for k in keys] == [
        ("src_o.xwalk_crm_billing", "left_key", "crm.customers", "customer_id"),
        ("src_o.xwalk_crm_billing", "right_key", "billing.accounts", "account_ref")]
    assert all(k.origin == "user" and k.rule == "entity_match" for k in keys)


def test_no_model_on_the_matching_path() -> None:
    """Deterministic only: neither the skill nor the service imports the model router or any llm module."""
    from analystos.services import entity_matching as svc

    for mod in (em, svc):
        tree = ast.parse(inspect.getsource(mod))
        names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(n.startswith("analystos.llm") for n in names), mod.__name__

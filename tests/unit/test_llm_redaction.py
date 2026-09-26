"""P7-10: outbound redaction, restorable question tokens (ported from Atlas ``question_redaction``) and
value-free SQL (rewritten from Atlas ``sql_redaction``), plus the Ask wiring: tokens out, values back."""
from __future__ import annotations

import pytest

from analystos.llm.redaction import (
    iban_valid,
    luhn_valid,
    redact,
    redact_question,
    redact_sql_literals,
    restore_values,
    tokenize_values,
)


# ------------------------------------------------------------------ outbound (one-way)
@pytest.mark.parametrize(("text", "gone", "marker"), [
    ("customer SSN 123-45-6789 on file", "123-45-6789", "[REDACTED_SSN]"),
    ("pay to GB82WEST12345698765432 today", "GB82WEST12345698765432", "[REDACTED_IBAN]"),
    ("pay to GB82 WEST 1234 5698 7654 32 today", "GB82 WEST 1234 5698 7654 32", "[REDACTED_IBAN]"),
    ("card 4111 1111 1111 1111", "4111 1111 1111 1111", "[REDACTED_NUMBER]"),
    ("mail jane.doe@example.com", "jane.doe@example.com", "[REDACTED_EMAIL]"),
    ("api_key=abc123secret", "abc123secret", "[REDACTED]"),
])
def test_outbound_redaction(text: str, gone: str, marker: str) -> None:
    out = redact(text)
    assert gone not in out and marker in out


@pytest.mark.parametrize("text", [
    "region AB12CDEF1234567 is a code, not an IBAN", "total 250000 in 2025", "phone 555-123-4567",
    "incident INC0012345 opened 2025-01-31",
])
def test_outbound_redaction_leaves_ordinary_text(text: str) -> None:
    assert redact(text) == text


def test_checksums() -> None:
    assert iban_valid("GB82WEST12345698765432") and not iban_valid("GB83WEST12345698765432")
    assert luhn_valid("4111111111111111") and not luhn_valid("1234567890123456")


# ------------------------------------------------------------------ question tokens (restorable)
@pytest.mark.parametrize(("question", "kind", "value"), [
    ("balance of account 004512339871?", "LONG_NUMBER", "004512339871"),
    ("payments by jane.doe@example.co.uk", "EMAIL", "jane.doe@example.co.uk"),
    ("transfers to GB82WEST12345698765432", "IBAN", "GB82WEST12345698765432"),
    ("disputes on card 4111 1111 1111 1111", "CARD_NUMBER", "4111 1111 1111 1111"),
    ("customer with SSN 123-45-6789", "SSN", "123-45-6789"),
])
def test_identifying_values_are_replaced_by_tokens(question: str, kind: str, value: str) -> None:
    r = redact_question(question)
    assert value not in r.text and "AOS_VALUE_1" in r.text
    assert r.values == {"AOS_VALUE_1": value} and r.kinds == (kind,)
    assert r.evidence() == {"redacted_values": 1, "kinds": [kind]}


@pytest.mark.parametrize("question", [
    "top 10 branches by deposits in 2025", "loans over 250000 opened between 2024-01-01 and 2024-06-30",
    "call volume on 555-123-4567 last week", "how many customers have more than 3 accounts",
    "card numbers that fail Luhn like 1234 5678 9012 3456",
])
def test_ordinary_questions_pass_through_untouched(question: str) -> None:
    r = redact_question(question)
    assert r.text == question and not r.redacted


def test_same_value_is_one_token_and_distinct_values_are_distinct() -> None:
    r = redact_question("move from 111222333444 to 555666777888, then back to 111222333444")
    assert r.values == {"AOS_VALUE_1": "111222333444", "AOS_VALUE_2": "555666777888"}
    assert r.text.count("AOS_VALUE_1") == 2


def test_restore_and_tokenize_are_inverses_and_do_not_confuse_1_with_12() -> None:
    values = {f"AOS_VALUE_{n}": str(100000000 + n) for n in range(1, 13)}
    sql = "SELECT 1 FROM t WHERE a = 'AOS_VALUE_12' AND b = AOS_VALUE_1"
    restored = restore_values(sql, values)
    assert restored == "SELECT 1 FROM t WHERE a = '100000012' AND b = 100000001"
    assert tokenize_values(restored, values) == sql


# ------------------------------------------------------------------ value-free SQL
@pytest.mark.parametrize(("sql", "dialect", "values"), [
    ("SELECT a FROM t WHERE ssn = '123-45-6789' AND amount > 1000", "postgres", ["123-45-6789", "1000"]),
    ("SELECT a FROM t WHERE is_vip = TRUE AND region IN ('EMEA', 'APAC')", "postgres", ["TRUE", "EMEA", "APAC"]),
    ("SELECT TOP 5 [c].[id] FROM [dbo].[c] WHERE [c].[acct] = N'0045123' -- owner jane", "tsql", ["0045123", "jane"]),
    ("SELECT `x` FROM t WHERE y = \"secret-value\"", "bigquery", ["secret-value"]),
])
def test_sql_literals_are_removed_and_structure_kept(sql: str, dialect: str, values: list[str]) -> None:
    r = redact_sql_literals(sql, dialect=dialect)
    assert r.status == "PARSED" and r.sql
    for v in values:
        assert v not in r.sql
    assert "FROM" in r.sql.upper()


def test_unparseable_sql_is_scrubbed_not_kept() -> None:
    sql = "SELEKT balance FRM accounts WHERE acct = '004512339871' AND pin = 4321 AND \"sales_2024\" > 0"
    r = redact_sql_literals(sql, dialect="postgres")
    assert r.status == "LEXICAL"
    assert "004512339871" not in r.sql and "4321" not in r.sql and '"sales_2024"' in r.sql


def test_dollar_quoted_and_escape_strings_are_values() -> None:
    r = redact_sql_literals("SELECT $$4111111111111111$$ AS a, E'it\\'s 123-45-6789' AS b FROM t", dialect="postgres")
    assert "4111111111111111" not in r.sql and "123-45-6789" not in r.sql


def test_comments_are_stripped_by_default_and_kept_on_request() -> None:
    sql = "SELECT a FROM t /* customer jane.doe@example.com */ WHERE b = 1"
    assert "jane" not in redact_sql_literals(sql).sql
    assert "customer" in redact_sql_literals(sql, strip_comments=False).sql


# ------------------------------------------------------------------ Ask: tokens out, values back
def test_ask_sends_tokens_and_restores_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from analystos.agents import sql_agent

    sent: list[dict] = []
    executed: list[str] = []

    def fake_compile_for(ctx, purpose, required, **kw):  # noqa: ANN001, ANN003
        sent.append({"purpose": purpose, **required, **{k: v for k, v in kw.items() if k != "catalog"}})
        return required

    answers = iter([{"sql": "SELECT balance FROM retail.account WHERE account_no = 'AOS_VALUE_1' AND bad",
                     "explanation": "Balance of AOS_VALUE_1"},
                    {"sql": "SELECT balance FROM retail.account WHERE account_no = 'AOS_VALUE_1'"}])

    def fake_llm_json(ctx, purpose, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        return next(answers), "fake-model"

    class Gateway:
        def execute(self, scope, sql, **kw):  # noqa: ANN001, ANN003
            executed.append(sql)
            if "bad" in sql:
                raise sql_agent.SQLRejected("Could not resolve column bad in account_no = '004512339871'")
            return SimpleNamespace(query_id="q1", columns=["balance"], rows=[[1]], row_count=1, truncated=False,
                                   fingerprint="f", records=lambda: [{"balance": 1}])

    monkeypatch.setattr(sql_agent, "compile_for", fake_compile_for)
    monkeypatch.setattr(sql_agent, "llm_json", fake_llm_json)
    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: [])
    monkeypatch.setattr(sql_agent, "_result", lambda r: {"query_id": r.query_id})
    monkeypatch.setattr(sql_agent, "_stage", lambda *a, **kw: None)
    monkeypatch.setattr(sql_agent, "_check_budget", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "_semantic_answer", lambda *a: None)
    monkeypatch.setattr(sql_agent, "_distribution_plan", lambda *a: None)
    monkeypatch.setattr("analystos.agents.ask_rules.plan_for", lambda *a: None)
    monkeypatch.setattr(sql_agent, "_authorize_ask", lambda ctx: None)
    monkeypatch.setattr(sql_agent, "route", lambda *a, **kw: {"value": "generate"})
    monkeypatch.setattr(sql_agent, "clarify", lambda *a: {"value": False, "missing_inputs": []})
    ctx = SimpleNamespace(scope=SimpleNamespace(source_dialects={"s": "postgres"}), user=SimpleNamespace(id="u"),
                          services=SimpleNamespace(gateway=Gateway()))
    out = sql_agent._ask(ctx, "What is the balance of account 004512339871?", use_registry=False)
    assert out["status"] == "answered"
    assert executed[-1] == "SELECT balance FROM retail.account WHERE account_no = '004512339871'"
    assert out["explanation"] == "Balance of 004512339871"
    assert all("004512339871" not in repr(p) for p in sent), sent
    assert "redacted_values" in sent[0] and sent[1]["purpose"] == "sql_repair"

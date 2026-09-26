"""Outbound redaction (§27, §45). Applied to every prompt and every decision state before it leaves
the platform. Credentials must never reach a model even if a catalog description contains one.

Three layers (P7-10):

* ``redact`` -- one-way masking of anything sent to a model: keys, ``password=...`` pairs, URL
  credentials, card-like numbers, e-mail addresses, US SSNs and IBANs.
* ``redact_question`` / ``restore_values`` / ``tokenize_values`` -- restorable tokens for the values a
  person types into a question (account and customer numbers, cards passing Luhn, SSNs, IBANs,
  e-mails). The model writes ``'AOS_VALUE_1'``; the SQL is restored locally before the gateway.
  Ported from ``AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/question_redaction.py``.
* ``redact_sql_literals`` -- value-free SQL for storage (lineage, query memory): every literal
  becomes a placeholder, or the text is scrubbed lexically when the parse hides values. Rewritten
  from ``AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:src/aida/sql_redaction.py`` without
  its routine-body lexer (AnalystOS stores no procedure bodies).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED_NUMBER]"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
]
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,4})?)\b")


def iban_valid(candidate: str) -> bool:
    """ISO 13616 mod-97 check, so a code that merely looks like an IBAN is left alone."""
    s = candidate.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    return int("".join(str(int(ch, 36)) for ch in s[4:] + s[:4])) % 97 == 1


def redact(text: str) -> str:
    # IBANs first: their digit groups would otherwise read as a card-like number.
    text = _IBAN.sub(lambda m: "[REDACTED_IBAN]" if iban_valid(m.group(0)) else m.group(0), text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value


# ------------------------------------------------------------------------------ question values
PLACEHOLDER_PREFIX: Final = "AOS_VALUE_"

_Q_EMAIL = re.compile(r"\b[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
_Q_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_Q_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_Q_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_Q_LONG_NUMBER = re.compile(r"\b\d{9,}\b")


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@dataclass(frozen=True, slots=True)
class RedactedQuestion:
    """The question as it may leave the platform, and what was taken out (kept locally)."""

    text: str
    values: dict[str, str] = field(default_factory=dict)
    kinds: tuple[str, ...] = ()

    @property
    def redacted(self) -> bool:
        return bool(self.values)

    def evidence(self) -> dict[str, object]:
        """Counts and kinds only, never a value."""
        return {"redacted_values": len(self.values), "kinds": sorted(set(self.kinds))}


def redact_question(question: str) -> RedactedQuestion:
    """Replace values that identify a person or an account with stable tokens. Deliberately narrow so
    ordinary questions ("top 10 branches", "loans over 250000 in 2025") pass untouched: e-mails,
    IBANs, 13-19 digit card numbers passing Luhn, ``ddd-dd-dddd`` SSNs and any run of 9+ digits."""
    values: dict[str, str] = {}
    kinds: list[str] = []
    by_value: dict[str, str] = {}

    def token_for(value: str, kind: str) -> str:
        if value in by_value:
            return by_value[value]
        token = f"{PLACEHOLDER_PREFIX}{len(values) + 1}"
        values[token] = value
        by_value[value] = token
        kinds.append(kind)
        return token

    def card(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            return token_for(match.group(0), "CARD_NUMBER")
        return match.group(0)

    text = _Q_EMAIL.sub(lambda m: token_for(m.group(0), "EMAIL"), question)
    text = _Q_IBAN.sub(lambda m: token_for(m.group(0), "IBAN"), text)
    text = _Q_CARD.sub(card, text)
    text = _Q_SSN.sub(lambda m: token_for(m.group(0), "SSN"), text)
    text = _Q_LONG_NUMBER.sub(lambda m: token_for(m.group(0), "LONG_NUMBER"), text)
    return RedactedQuestion(text=text, values=values, kinds=tuple(kinds))


def restore_values(sql: str, values: dict[str, str]) -> str:
    """Put redacted values back into generated SQL, locally; longest token first (``_12`` before ``_1``).
    Safe because every admitted value is drawn from ``[A-Za-z0-9@._+-]`` plus a space or dash inside a
    card number: none can close a string literal, and the SQL still passes the whole gateway."""
    for token in sorted(values, key=len, reverse=True):
        sql = sql.replace(token, values[token])
    return sql


def tokenize_values(sql: str, values: dict[str, str]) -> str:
    """The inverse of ``restore_values``, for sending a restored statement to a model again (a repair)."""
    for token, value in sorted(values.items(), key=lambda item: len(item[1]), reverse=True):
        sql = sql.replace(value, token)
    return sql


QUESTION_MODEL_INSTRUCTION: Final = (
    "Values that identify a person or an account have been replaced in the question by tokens named "
    "AOS_VALUE_<n>. Where the SQL needs such a value, write the token itself exactly as it appears, as a "
    "string literal (for example 'AOS_VALUE_1') or as a bare number where the column is numeric; the "
    "platform substitutes the real value afterwards. Never guess or invent the value."
)


# ------------------------------------------------------------------------------ SQL literals
SQL_PLACEHOLDER: Final = ":redacted"
_NUMERIC_LITERAL = re.compile(r"(?<![$\w])\d+(?:\.\d+)?\b")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")
_DOUBLE_QUOTED_VALUE_DIALECTS = frozenset({"bigquery", "databricks", "spark", "hive", "mysql"})


@dataclass(frozen=True, slots=True)
class RedactedSQL:
    """``PARSED``: every literal node replaced and nothing value-shaped left; ``LEXICAL``: scrubbed
    without a parse (still value-free); ``UNPARSED``: no text returned at all."""

    status: str
    sql: str | None


def redact_sql_literals(sql: str, *, dialect: str = "postgres", strip_comments: bool = True) -> RedactedSQL:
    """Value-free SQL. A comment on SQL someone typed or a model wrote can hold a value, so comments go
    by default. Never returns the raw text: an unparseable statement is scrubbed, not kept."""
    import sqlglot
    from sqlglot import exp

    if not sql or not sql.strip():
        return RedactedSQL("PARSED", "")
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        # A Command is text sqlglot could not model: its re-render is not the text it came from.
        if tree is not None and not any(isinstance(n, exp.Command) for n in tree.walk()):
            values = tuple(getattr(exp, k) for k in ("Literal", "Boolean", "National", "RawString", "ByteString",
                                                     "HexString", "BitString", "UnicodeString") if hasattr(exp, k))
            out = tree.transform(
                lambda n: exp.Placeholder(this="redacted") if isinstance(n, values) else n
            ).sql(dialect=dialect, comments=not strip_comments)
            if _scrub(out, dialect, strip_comments=False) == out:
                return RedactedSQL("PARSED", out)
    except (sqlglot.errors.SqlglotError, ValueError, RecursionError):
        pass
    try:
        return RedactedSQL("LEXICAL", _scrub(sql, dialect, strip_comments=strip_comments))
    except (re.error, RecursionError):
        return RedactedSQL("UNPARSED", None)


def _scrub(text: str, dialect: str, *, strip_comments: bool) -> str:
    """Remove string, dollar-quoted and numeric literals without parsing; identifiers (double quotes,
    brackets on tsql, backticks) are kept verbatim, digits in them are a name."""
    double_is_value = dialect in _DOUBLE_QUOTED_VALUE_DIALECTS
    out: list[str] = []
    start = i = 0
    n = len(text)

    def flush(end: int) -> None:
        out.append(_NUMERIC_LITERAL.sub(SQL_PLACEHOLDER, text[start:end]))

    while i < n:
        ch = text[i]
        if text.startswith("--", i) or text.startswith("/*", i):
            if text.startswith("--", i):
                newline = text.find("\n", i)
                end = n if newline == -1 else newline
            else:
                close = text.find("*/", i + 2)
                end = n if close == -1 else close + 2
            flush(i)
            comment = text[i:end]
            out.append((" " if comment.startswith("/*") else "") if strip_comments
                       else _NUMERIC_LITERAL.sub(SQL_PLACEHOLDER, comment))
            i = start = end
            continue
        if ch == "$" and (tag := _DOLLAR_TAG.match(text, i)):
            close = text.find(tag.group(0), tag.end())
            if close != -1:
                flush(i)
                out.append(SQL_PLACEHOLDER)
                i = start = close + len(tag.group(0))
                continue
        if ch == "'" or (ch == '"' and double_is_value):
            escape = i >= 1 and text[i - 1] in "Ee" and (i == 1 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
            end = _quoted_end(text, i, ch, backslash=ch == '"' or escape)  # E'..' is postgres's escape string
            flush(i)
            out.append(SQL_PLACEHOLDER)
            i = start = end
            continue
        if ch in '"`' or (ch == "[" and dialect == "tsql"):
            end = _quoted_end(text, i, "]" if ch == "[" else ch, backslash=False)
            flush(i)
            out.append(text[i:end])
            i = start = end
            continue
        i += 1
    flush(n)
    return "".join(out)


def _quoted_end(text: str, start: int, close: str, *, backslash: bool) -> int:
    j, n = start + 1, len(text)
    while j < n:
        if backslash and text[j] == "\\":
            j += 2
            continue
        if text[j] == close:
            if close != "]" and text.startswith(close * 2, j):
                j += 2
                continue
            return j + 1
        j += 1
    return n

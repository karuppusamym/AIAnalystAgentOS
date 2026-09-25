"""Strict identifier sanitation shared by staged connectors and the staging loader.

Staged tables and columns are created from names that come from outside (file names, CSV headers,
API field names). They are reduced to lowercase ``[a-z0-9_]``, never start with a digit, and are
limited to Postgres' 63-byte identifier length.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

MAX_IDENTIFIER = 63
_NON_IDENT = re.compile(r"[^a-z0-9_]+")
_MULTI_UNDERSCORE = re.compile(r"_+")
_VALID = re.compile(r"^[a-z_][a-z0-9_]*$")


def sanitize_identifier(name: str, *, max_length: int = MAX_IDENTIFIER, fallback: str = "col") -> str:
    """Lowercase, replace anything outside [a-z0-9_] with ``_``, collapse, trim, prefix digits."""
    cleaned = _NON_IDENT.sub("_", str(name).strip().lower())
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("_")
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}" if fallback else f"_{cleaned}"
    return cleaned[:max_length].rstrip("_") or fallback


def unique_identifiers(names: Iterable[str], *, max_length: int = MAX_IDENTIFIER) -> list[str]:
    """Sanitize a list of names, de-duplicating collisions with ``_2``, ``_3``..."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        base = sanitize_identifier(raw, max_length=max_length)
        candidate = base
        n = 2
        while candidate in seen:
            suffix = f"_{n}"
            candidate = base[: max_length - len(suffix)] + suffix
            n += 1
        seen.add(candidate)
        out.append(candidate)
    return out


def is_safe_identifier(name: str) -> bool:
    return bool(_VALID.match(name)) and len(name) <= MAX_IDENTIFIER


def staging_schema_for(source_id: str) -> str:
    """Analytics-DB schema that holds a staged source's snapshot."""
    return sanitize_identifier(f"src_{source_id}", fallback="src")

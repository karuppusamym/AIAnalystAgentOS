"""Offline validation against the pinned ODCS v3.2.0 and OpenLineage 2-0-2 JSON Schemas (P4-K04).

The schema files under `schema/` are byte-for-byte upstream copies (see `schema/PROVENANCE.yaml`).
`$ref`s between them resolve through a local registry keyed by `$id`, so validation never touches the
network. OpenLineage leaves facet bodies open, so each facet is also checked against its own pinned
schema (found by `_schemaURL`); an unpinned facet is a problem, not a silent pass.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).parent / "schema"
ODCS_SCHEMA = SCHEMA_DIR / "odcs-json-schema-v3.2.0.json"
ODCS_API_VERSION = "v3.2.0"
OL_CORE = SCHEMA_DIR / "openlineage" / "OpenLineage-2-0-2.json"
OL_FACETS = SCHEMA_DIR / "openlineage" / "facets"
OL_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json"
OL_RUN_EVENT = f"{OL_SCHEMA_URL}#/$defs/RunEvent"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def facet_schema_urls() -> dict[str, str]:
    """{facet class name: `_schemaURL`} for every pinned facet, e.g. SQLJobFacet ->
    https://openlineage.io/spec/facets/1-1-0/SQLJobFacet.json#/$defs/SQLJobFacet."""
    out = {}
    for path in sorted(OL_FACETS.glob("*.json")):
        out[path.stem] = f"{_load(path)['$id']}#/$defs/{path.stem}"
    return out


@cache
def _ol_registry() -> Any:
    from referencing import Registry, Resource

    resources = [Resource.from_contents(_load(p)) for p in [OL_CORE, *sorted(OL_FACETS.glob("*.json"))]]
    return Registry().with_resources((r.id(), r) for r in resources)


@cache
def _ol_validator(ref: str) -> Any:
    from jsonschema import Draft202012Validator

    return Draft202012Validator({"$ref": ref}, registry=_ol_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


@cache
def _odcs_validator() -> Any:
    from jsonschema import Draft201909Validator

    return Draft201909Validator(_load(ODCS_SCHEMA), format_checker=Draft201909Validator.FORMAT_CHECKER)


def _messages(validator: Any, doc: Any, prefix: str = "") -> list[str]:
    errors = sorted(validator.iter_errors(doc), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{prefix}{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message[:300]}" for e in errors]


def plain(doc: Any) -> Any:
    """JSON round trip: datetimes and other non-JSON values become what a consumer would read."""
    return json.loads(json.dumps(doc, default=str))


def validate_odcs(contract: dict[str, Any]) -> list[str]:
    """Problems with an ODCS data contract, empty when it validates against the pinned v3.2.0 schema."""
    return _messages(_odcs_validator(), plain(contract))


def _facets(event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for where, facets in (("run", (event.get("run") or {}).get("facets")), ("job", (event.get("job") or {}).get("facets"))):
        out.extend((f"{where}.facets.{k}", v) for k, v in sorted((facets or {}).items()))
    for side in ("inputs", "outputs"):
        for i, ds in enumerate(event.get(side) or []):
            out.extend((f"{side}[{i}].facets.{k}", v) for k, v in sorted((ds.get("facets") or {}).items()))
    return out


def validate_openlineage(event: dict[str, Any]) -> list[str]:
    """Problems with an OpenLineage RunEvent: the core 2-0-2 schema, then every facet against its pinned
    facet schema. Empty when it validates."""
    doc = plain(event)
    problems = _messages(_ol_validator(OL_RUN_EVENT), doc)
    pinned = set(facet_schema_urls().values())
    for where, facet in _facets(doc):
        url = facet.get("_schemaURL") if isinstance(facet, dict) else None
        if url not in pinned:
            problems.append(f"{where}: facet schema {url!r} is not pinned in evidence/schema")
            continue
        problems.extend(_messages(_ol_validator(url), facet, prefix=f"{where}/"))
    return problems


__all__ = ["ODCS_API_VERSION", "OL_RUN_EVENT", "OL_SCHEMA_URL", "facet_schema_urls", "plain", "validate_odcs",
           "validate_openlineage"]

"""ODCS v3.2.0 data contracts for published datasets (P4-K04, spec v3 §6.1/§7.3).

After an approved publication succeeds, every dataset in the bundle gets
`contracts/<dataset>.odcs.yaml` in the workspace pack: the governed SELECT's columns as properties,
the published KPIs as `semanticType: measure` properties (their aggregate in `transformLogic`), the
source assets it reads, and where it was published. The contract is validated against the pinned
upstream JSON Schema before it is written; an invalid contract is never written.

Versioning: the contract `version` is semantic. It starts at 1.0.0; a change to the schema block
(columns, measures, types) bumps the minor version, a change elsewhere (description, publication ids)
bumps the patch, identical content keeps the version and writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

import yaml
from sqlalchemy.orm import Session

from analystos.contracts.bi import DatasetDef, MetricDef, PublishBundle
from analystos.core.errors import InvalidInput
from analystos.evidence.schemas import ODCS_API_VERSION, validate_odcs

VENDOR = "analystos"
_ID = re.compile(r"[^A-Za-z0-9_-]+")
_LOGICAL = (("timestamp", "timestamp"), ("date", "date"), ("integer", "integer"), ("bigint", "integer"),
            ("numeric", "number"), ("double", "number"), ("boolean", "boolean"), ("json", "object"), ("text", "string"))


def stable_id(text: str) -> str:
    """An ODCS StableId: no '.', '#', '/', '\\', '@', '!', '%', '&', '^' or whitespace."""
    return _ID.sub("_", text).strip("_") or "x"


def contract_path(dataset_name: str) -> str:
    return f"contracts/{stable_id(dataset_name).lower().replace('_', '-')}.odcs.yaml"


def logical_type(data_type: str | None) -> str:
    from analystos.skills.catalog import normalize_type

    norm = normalize_type(data_type)
    return next((lt for key, lt in _LOGICAL if key == norm), "string")


def _custom(**props: Any) -> list[dict[str, Any]]:
    return [{"property": k, "value": v, "vendor": VENDOR} for k, v in props.items() if v not in (None, "", [], {})]


def _column_property(col: dict[str, Any]) -> dict[str, Any]:
    name = str(col.get("name"))
    prop: dict[str, Any] = {"id": stable_id(f"col_{name}"), "name": name, "physicalName": name,
                            "logicalType": logical_type(col.get("type")), "semanticType": "column"}
    if col.get("type"):
        prop["physicalType"] = str(col["type"])
    if col.get("business_name"):
        prop["businessName"] = str(col["business_name"])
    if col.get("description"):
        prop["description"] = str(col["description"])
    tags = sorted({str(t) for t in col.get("tags") or []})
    if tags:
        prop["tags"] = tags
        if {"pii", "restricted", "sensitive"} & set(tags):
            prop["classification"] = "restricted"
    if col.get("semantic_type"):
        prop["customProperties"] = _custom(semanticType=str(col["semantic_type"]))
    return prop


def _measure_property(m: MetricDef) -> dict[str, Any]:
    prop: dict[str, Any] = {"id": stable_id(f"metric_{m.name}"), "name": m.name, "logicalType": "number",
                            "semanticType": "measure", "businessName": m.display_name, "transformLogic": m.sql_expression}
    if m.definition:
        prop["description"] = m.definition
        prop["transformDescription"] = m.definition
    if m.source_columns:
        prop["transformSourceObjects"] = list(m.source_columns)
    prop["customProperties"] = _custom(metricStatus=m.status, format=m.format, grain=m.grain or None,
                                       dimensions=list(m.dimensions) or None)
    return prop


def schema_hash(contract: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(contract.get("schema"), sort_keys=True, default=str).encode()).hexdigest()


def content_hash(contract: dict[str, Any]) -> str:
    body = {k: v for k, v in contract.items() if k not in ("version", "contractCreatedTs")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def contract_for_dataset(dataset: DatasetDef, *, workspace_id: str, metrics: list[MetricDef] | None = None,
                         run_id: str | None = None, destination: str | None = None, external_id: Any = None,
                         external_url: str | None = None, bundle_hash: str | None = None, physical_name: str | None = None,
                         domain: str | None = None, created_at: datetime | None = None, version: str = "1.0.0") -> dict[str, Any]:
    """One ODCS v3.2.0 contract for a published (virtual) dataset. Pure."""
    metrics = [m for m in metrics or []]
    obj: dict[str, Any] = {"id": stable_id(dataset.name), "name": dataset.name, "physicalName": physical_name or dataset.name,
                           "physicalType": "view", "logicalType": "object",
                           "properties": [_column_property(c) for c in dataset.columns] + [_measure_property(m) for m in metrics]}
    if dataset.description:
        obj["description"] = dataset.description
    if dataset.time_column:
        obj["dataGranularityDescription"] = f"One row per record of the governed query; time column {dataset.time_column}"
    obj["customProperties"] = _custom(sourceAssets=sorted(dataset.source_assets) or None, querySql=dataset.sql,
                                      timeColumn=dataset.time_column)
    contract: dict[str, Any] = {
        "apiVersion": ODCS_API_VERSION, "kind": "DataContract",
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"analystos:{workspace_id}:dataset:{dataset.name}")),
        "version": version, "name": dataset.name, "status": "active", "domain": domain or workspace_id,
        "tags": sorted({"analystos", *( [destination] if destination else [])}),
        "description": {"purpose": dataset.description or f"Analytical dataset {dataset.name} published by AnalystOS.",
                        "usage": "Read-only virtual dataset over one governed SELECT; its KPIs are the published measures.",
                        "limitations": "Values derive from the source assets listed in customProperties.sourceAssets at the "
                                       "time of the approved analysis run; restricted columns are excluded by governance review."},
        "schema": [obj],
        "customProperties": _custom(workspaceId=workspace_id, runId=run_id, publishedTo=destination,
                                    externalId=str(external_id) if external_id is not None else None,
                                    bundleHash=bundle_hash),
    }
    if external_url:
        contract["authoritativeDefinitions"] = [{"type": "implementation", "url": external_url,
                                                 "description": f"The published {destination or 'BI'} dataset."}]
    if created_at is not None:
        contract["contractCreatedTs"] = created_at.isoformat()
    return contract


def next_version(previous: dict[str, Any] | None, contract: dict[str, Any]) -> str | None:
    """The version `contract` should carry after `previous`; None when nothing changed."""
    if not previous:
        return "1.0.0"
    if content_hash(previous) == content_hash(contract):
        return None
    try:
        major, minor, patch = (int(x) for x in str(previous.get("version") or "1.0.0").split(".")[:3])
    except ValueError:
        major, minor, patch = 1, 0, 0
    if schema_hash(previous) != schema_hash(contract):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def render(contract: dict[str, Any]) -> str:
    return yaml.safe_dump(contract, sort_keys=False, allow_unicode=True, width=4096)


def write_contracts(session: Session, *, workspace_id: str, bundle: PublishBundle, external_ids: dict[str, Any],
                    urls: dict[str, str] | None = None, run_id: str | None = None, author: str,
                    now: datetime | None = None) -> dict[str, Any]:
    """Write one contract per published dataset into the workspace pack (a new version only when it
    changed). Returns {dataset: {path, version, written}}. Raises InvalidInput when a contract does
    not validate, before anything is written."""
    from analystos.core.ids import utcnow
    from analystos.knowledge import store
    from analystos.publishing.base import bundle_hash

    now = now or utcnow()
    pack = store.workspace_pack(session, workspace_id)
    current = store.revision_files(session, pack)
    published = (external_ids or {}).get("datasets") or {}
    files: dict[str, bytes] = {}
    out: dict[str, Any] = {}
    for ds in bundle.datasets:
        if ds.name not in published:
            continue
        path = contract_path(ds.name)
        metrics = [m for m in bundle.metrics if not m.source_columns or set(m.source_columns) & {c.get("name") for c in ds.columns}]
        physical = None
        if bundle.destination == "superset":
            from analystos.publishing.superset import SupersetPublisher

            physical = SupersetPublisher.dataset_table_name(workspace_id, ds.name)
        contract = contract_for_dataset(ds, workspace_id=workspace_id, metrics=metrics, run_id=run_id,
                                        destination=bundle.destination, external_id=published.get(ds.name),
                                        external_url=(urls or {}).get(ds.name), bundle_hash=bundle_hash(bundle),
                                        physical_name=physical, created_at=now)
        previous = yaml.safe_load(current[path]) if path in current else None
        version = next_version(previous if isinstance(previous, dict) else None, contract)
        if version is None:
            out[ds.name] = {"path": path, "version": previous.get("version"), "written": False}
            continue
        contract["version"] = version
        problems = validate_odcs(contract)
        if problems:
            raise InvalidInput(f"ODCS contract for {ds.name} does not validate: " + "; ".join(problems[:5]))
        files[path] = render(contract).encode("utf-8")
        out[ds.name] = {"path": path, "version": version, "written": True, "id": contract["id"]}
    if files:
        store.commit(session, pack, files, author=author, reason=f"ODCS contracts for run {run_id}", origin="publish",
                     merge=True, meta={"run_id": run_id, "contracts": sorted(files)})
    return out

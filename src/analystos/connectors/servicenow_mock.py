"""A ServiceNow Table API look-alike over the synthetic ITSM data.

Implements the subset the connector uses, with the real API's wire conventions:
  GET /api/now/table/{table}
      sysparm_limit, sysparm_offset, sysparm_fields, sysparm_query, sysparm_exclude_reference_link,
      sysparm_display_value (false | true | all: every field becomes {"display_value", "value"};
      reference display values are the referenced record's name/number, choices get labels)
      -> {"result": [...]} with every value as a string ("true"/"false", "YYYY-MM-DD HH:MM:SS",
         references as sys_id strings or {"link", "value"} objects), header X-Total-Count and a
         Link header for the next page.
  sys_db_object / sys_dictionary are served as tables too (labels and field dictionary).
Supported sysparm_query terms joined by ``^``: ``ORDERBYfield``, ``ORDERBYDESCfield``,
``field=value``, ``field!=value``, ``field>value``, ``field>=value``, ``field<value``,
``field<=value``, ``fieldISEMPTY``, ``fieldISNOTEMPTY``.

Run: ``uvicorn analystos.connectors.servicenow_mock:app --port 8090``. HTTP basic auth with
SERVICENOW_MOCK_USER / SERVICENOW_MOCK_PASSWORD (default admin/admin).
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from analystos.connectors.synthetic_servicenow import (
    CHOICE_LABELS,
    DICTIONARY,
    TABLE_LABELS,
    generate_servicenow_data,
    user_display_name,
)

MAX_LIMIT = 10_000
DEFAULT_LIMIT = 10_000

app = FastAPI(title="ServiceNow Table API (mock)", version="0.1.0")
_basic = HTTPBasic(auto_error=False)


def _check_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    user = os.environ.get("SERVICENOW_MOCK_USER", "admin")
    password = os.environ.get("SERVICENOW_MOCK_PASSWORD", "admin")
    if credentials is None or not (
        secrets.compare_digest(credentials.username.encode(), user.encode())
        and secrets.compare_digest(credentials.password.encode(), password.encode())
    ):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "User Not Authenticated", "detail": "Required to provide Auth information"}, "status": "failure"},
            headers={"WWW-Authenticate": 'Basic realm="Service-now"'},
        )
    return credentials.username


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


@lru_cache(maxsize=1)
def _records() -> dict[str, list[dict[str, str]]]:
    data = generate_servicenow_data()
    out: dict[str, list[dict[str, str]]] = {}
    for name, table in data.items():
        cols = table.column_names
        pylists = [table.column(c).to_pylist() for c in cols]
        out[name] = [{c: _fmt(v) for c, v in zip(cols, row, strict=True)} for row in zip(*pylists, strict=True)]
    out["sys_db_object"] = [
        {"sys_id": f"dbo{i:029d}", "name": t, "label": TABLE_LABELS[t], "super_class": "", "sys_updated_on": "2025-06-01 00:00:00"}
        for i, t in enumerate(DICTIONARY)
    ]
    rows: list[dict[str, str]] = []
    for t, fields in DICTIONARY.items():
        rows.append(
            {"sys_id": f"dic{len(rows):029d}", "name": t, "element": "", "internal_type": "collection",
             "column_label": TABLE_LABELS[t], "reference": "", "max_length": "40", "primary": "false", "mandatory": "false"}
        )
        for element, itype, label, ref, max_len in fields:
            rows.append(
                {
                    "sys_id": f"dic{len(rows):029d}",
                    "name": t,
                    "element": element,
                    "internal_type": itype,
                    "column_label": label,
                    "reference": ref or "",
                    "max_length": str(max_len),
                    "primary": "true" if element == "sys_id" else "false",
                    "mandatory": "true" if element in ("sys_id", "number") else "false",
                }
            )
    out["sys_dictionary"] = rows
    return out


# Field used as the display value of a record in each referenced table.
_DISPLAY_FIELD = {"sys_user_group": "name", "cmdb_ci": "name", "change_request": "number", "incident": "number"}


@lru_cache(maxsize=1)
def _display_lookup() -> dict[str, dict[str, str]]:
    records = _records()
    lookup = {t: {r["sys_id"]: r[f] for r in records[t]} for t, f in _DISPLAY_FIELD.items() if t in records}
    return lookup


def _display_value(table: str, field: str, value: str, refs: dict[str, str]) -> str:
    if not value:
        return ""
    if field in refs:
        target = refs[field]
        if target == "sys_user":
            return user_display_name(value)
        return _display_lookup().get(target, {}).get(value, "")
    labels = CHOICE_LABELS.get(table, {}).get(field)
    if labels:
        return labels.get(value, value)
    return value


def _reference_fields(table: str) -> dict[str, str]:
    return {e: ref for e, _t, _l, ref, _m in DICTIONARY.get(table, []) if ref}


_TERM = re.compile(r"^(?P<field>[A-Za-z0-9_.]+?)(?P<op>!=|>=|<=|=|>|<|ISNOTEMPTY|ISEMPTY)(?P<value>.*)$")
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _compare(left: str, op: str, right: str) -> bool:
    if op == "ISEMPTY":
        return left == ""
    if op == "ISNOTEMPTY":
        return left != ""
    if op in ("=", "!="):
        eq = left == right
        return eq if op == "=" else not eq
    if left == "":
        return False
    a: Any = left
    b: Any = right
    if _NUM.match(left) and _NUM.match(right):
        a, b = float(left), float(right)
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def _apply_query(rows: list[dict[str, str]], query: str | None, fields: set[str]) -> list[dict[str, str]]:
    if not query:
        return rows
    order: list[tuple[str, bool]] = []
    filters: list[tuple[str, str, str]] = []
    for term in query.split("^"):
        term = term.strip()
        if not term or term == "EQ":
            continue
        if term.startswith("ORDERBYDESC"):
            order.append((term[len("ORDERBYDESC"):], True))
            continue
        if term.startswith("ORDERBY"):
            order.append((term[len("ORDERBY"):], False))
            continue
        m = _TERM.match(term)
        if not m:
            raise HTTPException(status_code=400, detail={"error": {"message": f"Invalid query term: {term}"}, "status": "failure"})
        filters.append((m["field"], m["op"], m["value"]))
    for f, _op, _v in filters:
        if f not in fields:
            # The real API ignores unknown fields in some modes; being strict makes bugs visible.
            raise HTTPException(status_code=400, detail={"error": {"message": f"Unknown field: {f}"}, "status": "failure"})
    out = [r for r in rows if all(_compare(r.get(f, ""), op, v) for f, op, v in filters)]
    for field, desc in reversed(order):
        if field not in fields:
            continue
        if all(_NUM.match(r.get(field, "") or "0") for r in out[:50]):
            out.sort(key=lambda r, f=field: float(r.get(f) or 0), reverse=desc)
        else:
            out.sort(key=lambda r, f=field: r.get(f, ""), reverse=desc)
    return out


@app.get("/api/now/table/{table}")
def table_api(
    table: str,
    request: Request,
    response: Response,
    sysparm_limit: int = Query(DEFAULT_LIMIT, ge=1),
    sysparm_offset: int = Query(0, ge=0),
    sysparm_fields: str | None = None,
    sysparm_query: str | None = None,
    sysparm_exclude_reference_link: str = "false",
    sysparm_display_value: str = "false",
    _user: str = Depends(_check_auth),
) -> dict[str, Any]:
    records = _records()
    if table not in records:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "Invalid table", "detail": f"Invalid table {table}"}, "status": "failure"},
        )
    rows = records[table]
    all_fields = set(rows[0].keys()) if rows else set()
    rows = _apply_query(rows, sysparm_query, all_fields)
    total = len(rows)
    limit = min(sysparm_limit, MAX_LIMIT)
    page = rows[sysparm_offset : sysparm_offset + limit]
    wanted = [f.strip() for f in sysparm_fields.split(",") if f.strip()] if sysparm_fields else None
    refs = _reference_fields(table)
    exclude_links = sysparm_exclude_reference_link.lower() == "true"
    display_mode = sysparm_display_value.lower()
    if display_mode not in ("true", "false", "all"):
        display_mode = "false"
    base = str(request.base_url).rstrip("/")
    result = []
    for row in page:
        keys = wanted or list(row.keys())
        item: dict[str, Any] = {}
        for k in keys:
            if k not in row:
                continue  # like the real API, unknown fields are silently omitted
            v = row[k]
            link = f"{base}/api/now/table/{refs[k]}/{v}" if (k in refs and v and not exclude_links) else None
            if display_mode == "all":
                obj: dict[str, str] = {"display_value": _display_value(table, k, v, refs), "value": v}
                if link:
                    obj["link"] = link
                item[k] = obj
            elif display_mode == "true":
                shown = _display_value(table, k, v, refs)
                item[k] = {"display_value": shown, "link": link} if link else shown
            elif link:
                item[k] = {"link": link, "value": v}
            else:
                item[k] = v
        result.append(item)
    response.headers["X-Total-Count"] = str(total)
    if sysparm_offset + limit < total:
        nxt = str(request.url.include_query_params(sysparm_offset=sysparm_offset + limit))
        response.headers["Link"] = f'<{nxt}>;rel="next"'
    return {"result": result}


@app.get("/api/now/table/{table}/{sys_id}")
def record_api(table: str, sys_id: str, _user: str = Depends(_check_auth)) -> dict[str, Any]:
    records = _records()
    for row in records.get(table, []):
        if row.get("sys_id") == sys_id:
            return {"result": row}
    raise HTTPException(status_code=404, detail={"error": {"message": "No Record found"}, "status": "failure"})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}

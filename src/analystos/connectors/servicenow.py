"""ServiceNow Table API connector (staged).

ServiceNow is not SQL-queryable, so the connector discovers tables through ``sys_db_object`` and
``sys_dictionary`` and extracts bounded snapshots through the Table API; the staging loader puts
them into the analytics DB where the gateway reads them with the reader identity.

Records are requested with ``sysparm_display_value=all``: every reference field keeps its raw
sys_id column and gains a derived ``<field>_name`` text column carrying the display value
(group name, CI name, change number...), because sys_ids mean nothing to business users.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, Literal

import httpx
import pyarrow as pa

from analystos.connectors.base import ConnectionTest, DiscoveredAsset, DiscoveredColumn
from analystos.connectors.naming import sanitize_identifier
from analystos.connectors.secrets import resolve_secret
from analystos.core.errors import AnalystOSError, Forbidden, InvalidInput, NotFound, UpstreamUnavailable
from analystos.core.logging import get_logger

DEFAULT_TABLES = ["incident", "change_request", "sys_user_group", "cmdb_ci"]
DISPLAY_SUFFIX = "_name"

# ServiceNow internal_type -> normalized type
INTERNAL_TYPE_MAP: dict[str, str] = {
    "integer": "integer",
    "longint": "bigint",
    "decimal": "numeric",
    "float": "double",
    "currency": "numeric",
    "price": "numeric",
    "percent_complete": "double",
    "boolean": "boolean",
    "glide_date_time": "timestamp",
    "due_date": "timestamp",
    "glide_date": "date",
    "date": "date",
    "datetime": "timestamp",
    "glide_duration": "text",
    "timer": "text",
    "GUID": "text",
    "sys_class_name": "text",
    "reference": "text",
    "document_id": "text",
    "choice": "text",
    "string": "text",
    "journal": "text",
    "journal_input": "text",
    "html": "text",
    "url": "text",
    "email": "text",
    "translated_text": "text",
    "user_input": "text",
    "json": "json",
}

_ARROW = {
    "integer": pa.int64(),
    "bigint": pa.int64(),
    "numeric": pa.float64(),
    "double": pa.float64(),
    "boolean": pa.bool_(),
    "timestamp": pa.timestamp("us"),
    "date": pa.date32(),
    "text": pa.string(),
    "json": pa.string(),
}

_log = get_logger(__name__)


def normalize_internal_type(internal_type: str | None) -> str:
    if not internal_type:
        return "text"
    return INTERNAL_TYPE_MAP.get(internal_type, INTERNAL_TYPE_MAP.get(internal_type.lower(), "text"))


def arrow_type(normalized: str) -> pa.DataType:
    return _ARROW.get(normalized, pa.string())


def _parse_value(raw: Any, normalized: str) -> Any:
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("value")
        if raw is None:
            return None
    if isinstance(raw, bool):
        return raw if normalized == "boolean" else str(raw).lower()
    text = str(raw)
    if text == "":
        return None
    try:
        if normalized in ("integer", "bigint"):
            return int(float(text)) if "." in text else int(text)
        if normalized in ("numeric", "double"):
            return float(text.replace(",", ""))
        if normalized == "boolean":
            low = text.lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
            return None
        if normalized == "timestamp":
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        if normalized == "date":
            return date.fromisoformat(text[:10])
    except ValueError:
        return None
    return text


def _display(raw: Any) -> str | None:
    if isinstance(raw, dict):
        value = raw.get("display_value")
        return value if value not in (None, "") else None
    return None


class ServiceNowConnector:
    kind = "servicenow"
    execution_mode: Literal["pushdown", "staged"] = "staged"
    dialect = "postgres"

    def __init__(
        self,
        config: dict[str, Any],
        secret_ref: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        password: str | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        instance_url = str(config.get("instance_url") or "").rstrip("/")
        if not instance_url.startswith(("http://", "https://")):
            raise InvalidInput("ServiceNow source needs config.instance_url (http:// or https://)")
        self.instance_url = instance_url
        self.username = str(config.get("username") or "")
        self.tables: list[str] = list(config.get("tables") or DEFAULT_TABLES)
        for t in self.tables:
            if sanitize_identifier(t) != t:
                raise InvalidInput(f"ServiceNow table name {t!r} is not a valid table name")
        self.page_size = max(1, min(int(config.get("page_size", 1000)), 10_000))
        self.max_rows = max(1, int(config.get("max_rows", 200_000)))
        self.timeout_seconds = float(config.get("timeout_seconds", 30))
        self.max_retries = max(0, int(config.get("max_retries", 3)))
        self.backoff_seconds = float(config.get("backoff_seconds", 0.5))
        self.display_values = bool(config.get("display_values", True))
        self._secret_ref = secret_ref
        self._password = password
        self._client = http_client
        self._sleep = sleep

    # -- http ------------------------------------------------------------------------------

    def _auth(self) -> tuple[str, str] | None:
        password = self._password if self._password is not None else resolve_secret(self._secret_ref)
        if not self.username:
            return None
        return (self.username, password or "")

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, headers={"Accept": "application/json"})
        return self._client

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        url = f"{self.instance_url}{path}"
        auth = self._auth()
        attempt = 0
        while True:
            try:
                resp = self._http().get(url, params=params, auth=auth)
            except httpx.TimeoutException as exc:
                error: AnalystOSError = UpstreamUnavailable(f"ServiceNow request timed out after {self.timeout_seconds}s ({path})")
                cause: Exception | None = exc
            except httpx.TransportError as exc:
                error = UpstreamUnavailable(f"ServiceNow is unreachable ({exc.__class__.__name__}) at {self.instance_url}")
                cause = exc
            else:
                status = resp.status_code
                if status < 400:
                    return resp
                if status in (401, 403):
                    raise Forbidden(
                        f"ServiceNow rejected the credentials for {self.username or '(no user)'} "
                        f"(HTTP {status}) on {path}. Check the username and secret_ref.",
                        details={"status": status},
                    )
                if status == 404:
                    raise NotFound(f"ServiceNow resource not found: {path}", details={"status": status})
                if status == 429 or status >= 500:
                    error = UpstreamUnavailable(f"ServiceNow returned HTTP {status} for {path}", details={"status": status})
                    cause = None
                    retry_after = resp.headers.get("Retry-After")
                else:
                    raise InvalidInput(f"ServiceNow rejected the request (HTTP {status}) for {path}: {_error_text(resp)}")
            if attempt >= self.max_retries:
                raise error from cause
            delay = self.backoff_seconds * (2**attempt)
            if isinstance(error, UpstreamUnavailable) and cause is None:
                with contextlib.suppress(ValueError):
                    delay = max(delay, min(float(retry_after or 0), 30.0))
            attempt += 1
            _log.warning("servicenow retry %s/%s for %s: %s", attempt, self.max_retries, path, error.message)
            self._sleep(delay)

    def _table(self, table: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
        resp = self._get(f"/api/now/table/{table}", params)
        try:
            body = resp.json()
        except ValueError:
            raise UpstreamUnavailable(f"ServiceNow returned a non-JSON response for {table}") from None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, list):
            raise UpstreamUnavailable(f"ServiceNow returned an unexpected payload for {table}")
        total = resp.headers.get("X-Total-Count")
        return result, int(total) if total and total.isdigit() else None

    # -- protocol --------------------------------------------------------------------------

    def test(self) -> ConnectionTest:
        started = time.perf_counter()
        try:
            self._table("sys_db_object", {"sysparm_limit": 1, "sysparm_fields": "name"})
        except AnalystOSError as exc:
            return ConnectionTest(ok=False, message=exc.message, latency_ms=int((time.perf_counter() - started) * 1000))
        return ConnectionTest(
            ok=True,
            message=f"Connected to {self.instance_url}",
            latency_ms=int((time.perf_counter() - started) * 1000),
            details={"tables": self.tables},
        )

    def discover(self) -> list[DiscoveredAsset]:
        assets: list[DiscoveredAsset] = []
        for table in self.tables:
            objects, _ = self._table(
                "sys_db_object", {"sysparm_query": f"name={table}", "sysparm_fields": "name,label", "sysparm_limit": 1}
            )
            if not objects:
                raise NotFound(f"ServiceNow table {table!r} does not exist or is not readable")
            label = _plain(objects[0].get("label")) or table
            fields = self._dictionary(table)
            columns: list[DiscoveredColumn] = []
            names = {f["element"] for f in fields}
            for f in fields:
                element = f["element"]
                normalized = normalize_internal_type(f["internal_type"])
                reference = f["reference"] or None
                is_key = element == "sys_id" or f["primary"]
                columns.append(
                    DiscoveredColumn(
                        name=sanitize_identifier(element),
                        data_type=normalized,
                        nullable=not is_key,
                        is_key=is_key,
                        business_name=f["label"] or None,
                        description=f"ServiceNow {f['internal_type'] or 'string'} field {table}.{element}",
                        references=f"{reference}.sys_id" if reference else None,
                    )
                )
                if reference and self.display_values:
                    columns.append(
                        DiscoveredColumn(
                            name=self._display_column(element, names),
                            data_type="text",
                            nullable=True,
                            business_name=f"{f['label'] or element} name",
                            description=f"Display value of {element}",
                        )
                    )
            _, total = self._table(table, {"sysparm_limit": 1, "sysparm_fields": "sys_id"})
            freshness = None
            if "sys_updated_on" in names:
                latest, _ = self._table(
                    table,
                    {"sysparm_limit": 1, "sysparm_fields": "sys_updated_on", "sysparm_query": "ORDERBYDESCsys_updated_on"},
                )
                if latest:
                    freshness = _parse_value(latest[0].get("sys_updated_on"), "timestamp")
            assets.append(
                DiscoveredAsset(
                    source_name=table,
                    name=sanitize_identifier(table),
                    schema_name=None,
                    kind="api_table",
                    row_count=total,
                    business_name=label,
                    description=f"ServiceNow table {table} ({label})",
                    freshness_at=freshness,
                    columns=columns,
                )
            )
        return assets

    @staticmethod
    def _display_column(element: str, existing: set[str]) -> str:
        name = sanitize_identifier(f"{element}{DISPLAY_SUFFIX}")
        if name in existing:
            name = sanitize_identifier(f"{element}_display")
        return name

    def _dictionary(self, table: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        seen: set[str] = set()
        while True:
            rows, total = self._table(
                "sys_dictionary",
                {
                    "sysparm_query": f"name={table}^ORDERBYsys_id",
                    "sysparm_fields": "element,internal_type,column_label,reference,max_length,primary",
                    "sysparm_limit": self.page_size,
                    "sysparm_offset": offset,
                    "sysparm_exclude_reference_link": "true",
                },
            )
            for r in rows:
                element = _plain(r.get("element"))
                internal_type = _plain(r.get("internal_type"))
                if not element or internal_type == "collection" or element in seen:
                    continue
                seen.add(element)
                out.append(
                    {
                        "element": element,
                        "internal_type": internal_type,
                        "label": _plain(r.get("column_label")),
                        "reference": _plain(r.get("reference")),
                        "primary": _plain(r.get("primary")).lower() == "true",
                    }
                )
            offset += len(rows)
            if not rows or len(rows) < self.page_size or (total is not None and offset >= total):
                break
        # Keep sys_id first for readability; the rest in dictionary order.
        out.sort(key=lambda f: (f["element"] != "sys_id",))
        return out

    def extract(self, asset: DiscoveredAsset, *, max_rows: int) -> Iterator[pa.RecordBatch]:
        table = asset.source_name
        if table not in self.tables:
            raise InvalidInput(f"Table {table!r} is not configured for this ServiceNow source")
        limit = min(max_rows, self.max_rows)
        # Re-read the dictionary: asset metadata may have been rebuilt from the control plane,
        # which does not keep reference targets. The dictionary says which columns are raw
        # fields and which are derived display-value columns.
        dictionary = {f["element"]: f for f in self._dictionary(table)}
        display_of: dict[str, str] = {}
        for element, f in dictionary.items():
            if f["reference"]:
                display_of[self._display_column(element, set(dictionary))] = element
        base_cols = [c for c in asset.columns if c.name in dictionary]
        types = {
            c.name: normalize_internal_type(dictionary[c.name]["internal_type"]) if c.name in dictionary else "text"
            for c in asset.columns
        }
        unknown = [c.name for c in asset.columns if c.name not in dictionary and c.name not in display_of]
        if unknown:
            _log.warning("servicenow %s: columns not in sys_dictionary will be null: %s", table, unknown)
        schema = pa.schema([pa.field(c.name, arrow_type(types[c.name])) for c in asset.columns])
        wanted = [c.name for c in base_cols]
        for c in asset.columns:
            src = display_of.get(c.name)
            if src and c.name not in dictionary and src not in wanted:
                wanted.append(src)
        fields = ",".join(wanted)
        offset = 0
        fetched = 0
        while fetched < limit:
            page = min(self.page_size, limit - fetched)
            params = {
                "sysparm_fields": fields,
                "sysparm_limit": page,
                "sysparm_offset": offset,
                "sysparm_query": "ORDERBYsys_id",
                "sysparm_exclude_reference_link": "true",
            }
            if self.display_values:
                params["sysparm_display_value"] = "all"
            rows, total = self._table(table, params)
            if not rows:
                break
            arrays = []
            for c in asset.columns:
                if c.name in display_of and c.name not in dictionary:
                    src = display_of[c.name]
                    values = [_display(r.get(src)) for r in rows]
                else:
                    values = [_parse_value(r.get(c.name), types[c.name]) for r in rows]
                arrays.append(pa.array(values, type=arrow_type(types[c.name])))
            yield pa.RecordBatch.from_arrays(arrays, schema=schema)
            fetched += len(rows)
            offset += len(rows)
            if len(rows) < page or (total is not None and offset >= total):
                break

    def sqlalchemy_url(self) -> str:
        raise InvalidInput("ServiceNow sources are staged; they have no SQL endpoint")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    return "" if value is None else str(value)


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error") or body.get("detail", {}).get("error") or {}
        return str(err.get("message") or err)[:200]
    except Exception:
        return resp.text[:200]

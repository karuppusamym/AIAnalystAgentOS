"""The governed query gateway: the only path from user/model SQL to source data.

execute():
  1. effective limits = min(requested, scope limits, platform ceilings);
  2. validate (validator.validate_sql) — fail closed, precise SQLRejected messages;
  3. resolve the source from the control plane and check it belongs to the scope's workspace;
  4. result cache lookup (Redis; key binds fingerprint + scope hash + source version + row cap);
  5. execute the *generated* SQL:
       staged sources  -> analytics DB with the READER identity only, READ ONLY transaction,
                          SET LOCAL statement_timeout;
       pushdown pg     -> connector.sqlalchemy_url(), READ ONLY transaction + statement_timeout;
       pushdown tsql   -> connector URL with the driver query timeout, plus the source-kind
                          catalog's session statements (connectors/kinds.py);
     a connector whose catalog kind cannot be pushed down is refused (staged kinds only ever run
     in the analytics DB);
  6. JSON-safe values, result hash, audit row (QueryExecution) for every attempt in its own short
     transaction, optional on_event("query.executed" | "query.rejected", payload).
"""
from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from analystos.connectors.naming import staging_schema_for
from analystos.contracts.policy import DataScope
from analystos.core.errors import (
    AnalystOSError,
    Forbidden,
    InvalidInput,
    NotFound,
    QueryTimeout,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.core.ids import new_id, stable_hash
from analystos.core.logging import get_logger
from analystos.db.models import QueryExecution, Source, SourceAsset
from analystos.gateway.cache import QueryCache
from analystos.gateway.engines import get_engine
from analystos.gateway.types import QueryResult, ValidatedSQL
from analystos.gateway.validator import validate_sql

PREVIEW_ROWS = 20
_log = get_logger(__name__)


def _default_session_factory():  # noqa: ANN202
    from analystos.db.base import SessionLocal

    return SessionLocal()


def json_safe(value: Any) -> Any:
    """Convert a driver value into something JSON (and JSONB) can store losslessly enough."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    return str(value)


def result_hash(columns: list[str], rows: list[list[Any]]) -> str:
    return stable_hash({"columns": columns, "rows": rows})


class _Runner:
    """RunSQL bound to one source of a scope."""

    def __init__(self, gateway: QueryGateway, scope: DataScope, *, actor: str, run_id: str | None, task_id: str | None,
                 source_id: str) -> None:
        self._gateway = gateway
        self._scope = scope
        self._actor = actor
        self._run_id = run_id
        self._task_id = task_id
        self.source_id = source_id
        self.dialect = scope.source_dialects.get(source_id, "postgres")

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None,
                 retain_rows: bool = True) -> QueryResult:
        return self._gateway.execute(
            self._scope, sql, actor=self._actor, purpose=purpose, run_id=self._run_id, task_id=self._task_id,
            max_rows=max_rows, retain_rows=retain_rows,
        )


class QueryGateway:
    def __init__(
        self,
        settings: Any,
        session_factory: Callable[[], Any] = _default_session_factory,
        cache: QueryCache | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        *,
        connector_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cache = cache
        self.on_event = on_event
        if connector_factory is None:
            from analystos.connectors.registry import build_connector

            connector_factory = build_connector
        self.connector_factory = connector_factory
        self._check_identities()

    # ------------------------------------------------------------------ configuration guard
    def _check_identities(self) -> None:
        reader = make_url(self.settings.analytics_reader_url)
        control = make_url(self.settings.database_url)
        loader = make_url(self.settings.analytics_loader_url)
        if (reader.host, reader.port, reader.database) == (control.host, control.port, control.database):
            raise InvalidInput("analytics_reader_url must not point at the control-plane database")
        if reader.username in (control.username, loader.username):
            raise InvalidInput("analytics_reader_url must use the dedicated reader identity, not the loader/control identity")

    # ------------------------------------------------------------------ public API
    def run_sql_for(self, scope: DataScope, *, actor: str, run_id: str | None = None, task_id: str | None = None,
                    source_id: str | None = None) -> _Runner:
        sources = sorted(set(scope.source_ids) | set(scope.asset_sources.values()))
        if source_id is None:
            if len(sources) != 1:
                raise InvalidInput(
                    f"The scope has {len(sources)} sources; pass source_id to bind the SQL runner to one of: {sources}"
                )
            source_id = sources[0]
        elif source_id not in sources:
            raise Forbidden(f"source {source_id} is not in the authorized scope")
        bound = scope.model_copy(deep=True)
        bound.source_ids = [source_id]
        bound.assets = [a for a in scope.assets if scope.asset_sources.get(a, source_id) == source_id]
        bound.asset_sources = {a: s for a, s in scope.asset_sources.items() if s == source_id}
        bound.columns = {a: c for a, c in scope.columns.items() if a in bound.assets}
        bound.source_dialects = {source_id: scope.source_dialects.get(source_id, "postgres")}
        return _Runner(self, bound, actor=actor, run_id=run_id, task_id=task_id, source_id=source_id)

    def execute(
        self,
        scope: DataScope,
        sql: str,
        *,
        actor: str,
        purpose: str = "analysis",
        run_id: str | None = None,
        task_id: str | None = None,
        use_cache: bool = True,
        max_rows: int | None = None,
        timeout_seconds: int | None = None,
        retain_rows: bool = True,
    ) -> QueryResult:
        """`retain_rows=False` (e.g. crawler PII value sampling): the statement is validated, executed
        and audited as usual, but no result value is stored anywhere — no audit preview, no result cache."""
        if not retain_rows:
            use_cache = False
        started = time.perf_counter()
        query_id = new_id("qry")
        eff_rows = min(x for x in (max_rows, scope.max_rows, self.settings.query_max_rows) if x is not None and x > 0)
        eff_timeout = min(
            x for x in (timeout_seconds, scope.timeout_seconds, self.settings.query_timeout_seconds) if x is not None and x > 0
        )
        audit = {
            "id": query_id,
            "workspace_id": scope.workspace_id,
            "run_id": run_id,
            "task_id": task_id,
            "actor": actor[:80],
            "purpose": (purpose or "analysis")[:120],
            "sql": sql if isinstance(sql, str) else str(sql),
        }

        # 1. validate
        try:
            validated = validate_sql(scope, sql, max_rows=eff_rows)
            source = self._load_source(scope, validated)
        except SQLRejected as exc:
            self._reject(audit, exc, started)
            raise
        except (Forbidden, NotFound) as exc:
            self._reject(audit, exc, started)
            raise
        audit.update(
            source_id=validated.source_id,
            executed_sql=validated.executable_sql,
            fingerprint=validated.fingerprint,
            referenced_assets=validated.referenced_assets,
        )

        # 2. cache
        key = None
        if use_cache and self.cache is not None:
            key = self.cache.key(validated.fingerprint, scope.scope_hash(), source["version"], eff_rows)
            hit = self.cache.get(key)
            if hit is not None and isinstance(hit.get("columns"), list) and isinstance(hit.get("rows"), list):
                return self._finish(audit, validated, hit["columns"], hit["rows"], bool(hit.get("truncated")), started,
                                    cache_hit=True, hash_=hit.get("result_hash"))

        # 3. execute
        try:
            columns, rows = self._run(source, validated, eff_rows, eff_timeout)
        except QueryTimeout as exc:
            self._fail(audit, "timeout", exc, started)
            raise
        except AnalystOSError as exc:
            self._fail(audit, "error", exc, started)
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected; audit then surface as upstream failure
            err = UpstreamUnavailable(f"Query execution failed: {exc.__class__.__name__}")
            self._fail(audit, "error", err, started)
            raise err from exc
        truncated = len(rows) > eff_rows
        rows = rows[:eff_rows]
        result = self._finish(audit, validated, columns, rows, truncated, started, cache_hit=False, retain_rows=retain_rows)
        if key is not None and self.cache is not None:
            self.cache.set(key, {
                "columns": result.columns, "rows": result.rows, "truncated": result.truncated,
                "result_hash": result.result_hash,
            })
        return result

    # ------------------------------------------------------------------ helpers
    def _load_source(self, scope: DataScope, validated: ValidatedSQL) -> dict[str, Any]:
        session = self.session_factory()
        try:
            row = session.get(Source, validated.source_id)
            if row is None or row.workspace_id != scope.workspace_id:
                raise NotFound(f"source {validated.source_id} not found in workspace {scope.workspace_id}")
            if scope.source_ids and row.id not in scope.source_ids:
                raise Forbidden(f"source {row.id} is not in the authorized scope")
            agg = session.execute(
                select(func.count(SourceAsset.id), func.max(SourceAsset.freshness_at), func.sum(SourceAsset.row_count))
                .where(SourceAsset.source_id == row.id)
            ).one()
            version = stable_hash([row.id, row.status, row.execution_mode, str(row.last_discovered_at), agg[0], str(agg[1]),
                                   agg[2], row.staging_schema])
            source = {
                "id": row.id,
                "kind": row.kind,
                "config": dict(row.config or {}),
                "secret_ref": row.secret_ref,
                "execution_mode": row.execution_mode,
                "staging_schema": row.staging_schema or staging_schema_for(row.id),
                "workspace_id": row.workspace_id,
                "version": version,
            }
        finally:
            session.close()
        if source["execution_mode"] == "staged":
            wrong = [a for a in validated.referenced_assets if a.split(".", 1)[0] != source["staging_schema"]]
            if wrong:
                raise SQLRejected(
                    f"Assets {wrong} are not in the staging schema {source['staging_schema']} of source {source['id']}."
                )
        return source

    def _run(self, source: dict[str, Any], validated: ValidatedSQL, max_rows: int, timeout: int) -> tuple[list[str], list[list[Any]]]:
        if source["execution_mode"] == "staged":
            return self._run_postgres(self.settings.analytics_reader_url, validated.executable_sql, max_rows, timeout)
        connector = self.connector_factory(_SourceView(source), self.settings)
        if getattr(connector, "execution_mode", "pushdown") != "pushdown":
            # The catalog does not allow this kind to be queried in place (e.g. a row marked
            # pushdown for MySQL): refuse rather than send SQL the analysis compiler never targeted.
            raise InvalidInput(f"Source {source['id']} ({source['kind']}) cannot be queried in place; stage it instead")
        dialect = getattr(connector, "dialect", "postgres")
        if dialect != validated.dialect:
            raise SQLRejected(f"Source {source['id']} expects {dialect} SQL")
        if dialect == "postgres":
            return self._run_postgres(connector.sqlalchemy_url(), validated.executable_sql, max_rows, timeout)
        if dialect == "tsql":
            url = connector.query_url(timeout) if hasattr(connector, "query_url") else connector.sqlalchemy_url()
            session_sql = connector.session_statements(timeout) if hasattr(connector, "session_statements") else []
            return self._run_generic(url, validated.executable_sql, max_rows, session_sql)
        raise InvalidInput(f"Unsupported pushdown dialect {dialect}")

    def _run_postgres(self, url: str, sql: str, max_rows: int, timeout: int) -> tuple[list[str], list[list[Any]]]:
        import psycopg

        engine = get_engine(url)
        try:
            with engine.connect() as conn:
                dbapi = conn.connection
                cur = dbapi.cursor()
                try:
                    dbapi.rollback()  # start from a clean transaction (pool pre-ping may have opened one)
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
                    cur.execute("SET LOCAL idle_in_transaction_session_timeout = 60000")
                    cur.execute(sql)
                    columns = [d[0] for d in (cur.description or [])]
                    raw_rows = cur.fetchmany(max_rows + 1)
                finally:
                    try:
                        dbapi.rollback()
                    finally:
                        cur.close()
        except psycopg.errors.QueryCanceled:
            raise QueryTimeout(f"The query exceeded the {timeout}s timeout. Aggregate more or filter the data.") from None
        except (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction) as exc:
            raise Forbidden(f"The source refused the query: {_first_line(exc)}") from None
        except psycopg.OperationalError as exc:
            raise UpstreamUnavailable(f"The source database is unavailable: {_first_line(exc)}") from None
        except psycopg.Error as exc:
            raise InvalidInput(f"The query failed in the source: {_first_line(exc)}") from None
        except Exception as exc:  # sqlalchemy-wrapped connection errors
            orig = getattr(exc, "orig", None)
            if isinstance(orig, psycopg.OperationalError) or exc.__class__.__name__ == "OperationalError":
                raise UpstreamUnavailable(f"The source database is unavailable: {_first_line(orig or exc)}") from None
            raise
        rows = [[json_safe(v) for v in r] for r in raw_rows]
        return columns, rows

    def _run_generic(self, url: str, sql: str, max_rows: int,
                     session_sql: list[str] | tuple[str, ...] = ()) -> tuple[list[str], list[list[Any]]]:
        """Non-postgres pushdown: the catalog's read-only/timeout session statements (if the kind
        has any) run on the same connection first; the driver URL carries the query timeout."""
        engine = get_engine(url)
        try:
            with engine.connect() as conn:
                dbapi = conn.connection
                cur = dbapi.cursor()
                try:
                    for stmt in session_sql:
                        cur.execute(stmt)
                    cur.execute(sql)
                    columns = [d[0] for d in (cur.description or [])]
                    raw_rows = cur.fetchmany(max_rows + 1)
                finally:
                    try:
                        dbapi.rollback()
                    finally:
                        cur.close()
        except AnalystOSError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver specific
            text_ = _first_line(getattr(exc, "orig", None) or exc)
            if "timeout" in text_.lower() or "timed out" in text_.lower():
                raise QueryTimeout("The query exceeded the timeout. Aggregate more or filter the data.") from None
            if exc.__class__.__name__ in ("OperationalError", "InterfaceError"):
                raise UpstreamUnavailable(f"The source database is unavailable: {text_}") from None
            raise InvalidInput(f"The query failed in the source: {text_}") from None
        return columns, [[json_safe(v) for v in r] for r in raw_rows]

    def _finish(self, audit: dict[str, Any], validated: ValidatedSQL, columns: list[str], rows: list[list[Any]],
                truncated: bool, started: float, *, cache_hit: bool, hash_: str | None = None,
                retain_rows: bool = True) -> QueryResult:
        rhash = hash_ or result_hash(columns, rows)
        duration = int((time.perf_counter() - started) * 1000)
        record = dict(audit, status="ok", row_count=len(rows), truncated=truncated, columns=list(columns),
                      result_hash=rhash, result_preview=rows[:PREVIEW_ROWS] if retain_rows else [], cache_hit=cache_hit,
                      duration_ms=duration)
        self._persist(record, strict=True)
        self._emit("query.executed", record)
        return QueryResult(
            query_id=audit["id"],
            columns=list(columns),
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            cache_hit=cache_hit,
            fingerprint=validated.fingerprint,
            result_hash=rhash,
            duration_ms=duration,
            referenced_assets=validated.referenced_assets,
            sql=validated.executable_sql,
        )

    def _reject(self, audit: dict[str, Any], exc: AnalystOSError, started: float) -> None:
        record = dict(audit, status="rejected", rejected_reason=exc.message[:4000],
                      duration_ms=int((time.perf_counter() - started) * 1000))
        self._persist(record, strict=False)
        self._emit("query.rejected", record)

    def _fail(self, audit: dict[str, Any], status: str, exc: AnalystOSError, started: float) -> None:
        record = dict(audit, status=status, rejected_reason=exc.message[:4000],
                      duration_ms=int((time.perf_counter() - started) * 1000))
        self._persist(record, strict=False)

    def _persist(self, record: dict[str, Any], *, strict: bool) -> None:
        session = self.session_factory()
        try:
            session.add(QueryExecution(**record))
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            _log.error("could not write query audit row %s: %s", record.get("id"), exc.__class__.__name__)
            if strict:
                raise UpstreamUnavailable("query audit could not be written; result withheld") from exc
        finally:
            session.close()

    def _emit(self, type_: str, record: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        payload = {
            "workspace_id": record.get("workspace_id"),
            "run_id": record.get("run_id"),
            "task_id": record.get("task_id"),
            "query_id": record.get("id"),
            "source_id": record.get("source_id"),
            "status": record.get("status"),
            "row_count": record.get("row_count", 0),
            "cache_hit": record.get("cache_hit", False),
            "duration_ms": record.get("duration_ms", 0),
            "reason": record.get("rejected_reason"),
            "purpose": record.get("purpose"),
            "fingerprint": record.get("fingerprint"),
            "referenced_assets": record.get("referenced_assets", []),
        }
        try:
            self.on_event(type_, payload)
        except Exception as exc:  # noqa: BLE001 - observers never break queries
            _log.warning("gateway on_event handler failed: %s", exc.__class__.__name__)


class _SourceView:
    """Minimal Source-like object handed to the connector factory (detached from the session)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data["id"]
        self.kind = data["kind"]
        self.config = data["config"]
        self.secret_ref = data["secret_ref"]
        self.execution_mode = data["execution_mode"]
        self.staging_schema = data["staging_schema"]
        self.workspace_id = data["workspace_id"]


def _first_line(exc: BaseException | None) -> str:
    text_ = str(exc or "").strip()
    return text_.splitlines()[0][:500] if text_ else (exc.__class__.__name__ if exc else "error")

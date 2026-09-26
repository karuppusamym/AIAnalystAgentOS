"""SQL engines over SQLAlchemy/DBAPI: the Postgres engine (analytics DB and Postgres pushdown) and
the session engine that wraps any ``GenericSQLConnector`` kind (P4-E01).

The code paths are the increment-3 gateway paths, moved here unchanged: Postgres opens every query
in a READ ONLY transaction with a statement timeout (and, for staged sources, SET LOCAL ROLE to the
workspace reader role); the session engine runs the kind's catalog read-only/timeout statements on
the connection first and passes the driver query timeout in the URL.
"""
from __future__ import annotations

from analystos.connectors.naming import is_safe_identifier
from analystos.core.errors import AnalystOSError, Forbidden, InvalidInput, QueryTimeout, UpstreamUnavailable
from analystos.engines.base import Limits, ReaderIdentity, ReadOnlyEngine, Rows
from analystos.gateway.engines import get_engine
from analystos.gateway.types import ValidatedSQL
from analystos.gateway.values import first_line, json_safe


class PostgresEngine(ReadOnlyEngine):
    id = "engine.postgres"
    dialect = "postgres"
    features = frozenset({"pushdown"})

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:
        return run_postgres(identity.url or "", stmt.executable_sql, limits.max_rows, limits.timeout_seconds,
                            role=identity.role)


class SessionSQLEngine(ReadOnlyEngine):
    """Any SQLAlchemy kind whose catalog entry allows pushdown (tsql today, see connectors/kinds.py)."""

    def __init__(self, kind: str, dialect: str, *, features: frozenset[str] = frozenset({"pushdown"})) -> None:
        self.id = f"engine.{kind}"
        self.dialect = dialect
        self.features = features

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows:
        if "pushdown" not in self.features:
            raise InvalidInput(f"{self.id} cannot query a source in place: the kind has no read-only session and its "
                               "engine is not certified; stage the source instead")
        return run_session_sql(identity.url or "", stmt.executable_sql, limits.max_rows, identity.session_sql)


def run_postgres(url: str, sql: str, max_rows: int, timeout: int, role: str | None = None) -> Rows:
    """``role``: staged sources run as the owning workspace's reader role for this transaction only."""
    import psycopg

    if role is not None and not is_safe_identifier(role):
        raise Forbidden("workspace reader role name is not a safe identifier")

    engine = get_engine(url)
    try:
        with engine.connect() as conn:
            dbapi = conn.connection
            cur = dbapi.cursor()
            try:
                dbapi.rollback()  # start from a clean transaction (pool pre-ping may have opened one)
                cur.execute("SET TRANSACTION READ ONLY")
                if role is not None:
                    try:
                        cur.execute(f'SET LOCAL ROLE "{role}"')
                    except psycopg.Error:
                        raise Forbidden(f"The workspace reader role {role} is not provisioned for this reader; "
                                        "re-stage the source or run `analystos migrate`") from None
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
        raise Forbidden(f"The source refused the query: {first_line(exc)}") from None
    except psycopg.OperationalError as exc:
        raise UpstreamUnavailable(f"The source database is unavailable: {first_line(exc)}") from None
    except psycopg.Error as exc:
        raise InvalidInput(f"The query failed in the source: {first_line(exc)}") from None
    except Exception as exc:  # sqlalchemy-wrapped connection errors
        orig = getattr(exc, "orig", None)
        if isinstance(orig, psycopg.OperationalError) or exc.__class__.__name__ == "OperationalError":
            raise UpstreamUnavailable(f"The source database is unavailable: {first_line(orig or exc)}") from None
        raise
    rows = [[json_safe(v) for v in r] for r in raw_rows]
    return columns, rows


def run_session_sql(url: str, sql: str, max_rows: int, session_sql: list[str] | tuple[str, ...] = ()) -> Rows:
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
        text_ = first_line(getattr(exc, "orig", None) or exc)
        if "timeout" in text_.lower() or "timed out" in text_.lower():
            raise QueryTimeout("The query exceeded the timeout. Aggregate more or filter the data.") from None
        if exc.__class__.__name__ in ("OperationalError", "InterfaceError"):
            raise UpstreamUnavailable(f"The source database is unavailable: {text_}") from None
        raise InvalidInput(f"The query failed in the source: {text_}") from None
    return columns, [[json_safe(v) for v in r] for r in raw_rows]

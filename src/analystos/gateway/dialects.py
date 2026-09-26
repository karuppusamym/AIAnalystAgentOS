"""Per-dialect rules of the gateway validator (P4-E01, multi-dialect validation).

The validator's core (one read statement, scope resolution, column qualification, denied columns,
row cap) is dialect-neutral. What differs per dialect is the *attack surface*: functions that read
files, call out over the network, reveal session state or sleep; table syntax that reads a path or
a stage instead of a table; session variables and bind parameters the database would substitute;
time travel that reads rows the current table no longer holds; and how unquoted identifiers fold.

``postgres`` and ``tsql`` keep exactly their increment-3 behaviour (``strict=False``). Every dialect
added since is ``strict``: besides its own denylist it rejects any function sqlglot does not model
(an ``Anonymous`` call may be a UDF, an external/remote function or a stored procedure wrapper,
none of which the gateway can reason about) unless the dialect allowlists it, namespaced function
calls, bind parameters and session variables, time travel, and table names that look like paths,
stages or metadata tables. Each profile has its own security suite in
``tests/unit/test_gateway_dialects.py``; a dialect without one is not added here.

A dialect being *validated* does not make its source kinds push down: that also needs the analysis
compiler (``skills/sqlbuild``) and a session that can be made read-only (``connectors/kinds.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Shared by every dialect (the increment-3 list; postgres and tsql use exactly this).
BASE_DENYLIST = (
    "pg_read_file", "pg_read_binary_file", "pg_ls_*", "pg_stat_file", "lo_*", "dblink*", "pg_sleep*",
    "set_config", "current_setting", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "query_to_xml*", "table_to_xml*", "cursor_to_xml*", "schema_to_xml*", "database_to_xml*",
    "xp_*", "sp_*", "openrowset", "opendatasource", "openquery", "openxml", "read_csv*", "read_parquet*",
    "read_json*", "read_text", "read_blob", "read_ndjson*", "copy", "txid_*", "pg_current_xact_id*",
    "pg_advisory*", "pg_try_advisory*", "nextval", "setval", "pg_notify", "pg_rotate_logfile",
    "pg_switch_wal", "pg_create_*", "pg_drop_*", "pg_promote", "pg_logical_*", "pg_replication_*",
    "pg_export_snapshot", "pg_import_system_collations", "pg_file_*", "ts_stat", "ts_rewrite",
    "pg_log_backend_memory_contexts", "pg_stat_reset*", "binary_upgrade_*", "pg_backup_*",
    "pg_start_backup", "pg_stop_backup", "pg_wal_replay_*", "http*", "load_extension", "system",
    "getenv", "shell", "glob",
)

# Functions every strict dialect refuses on top of BASE_DENYLIST: sleeps/locks, dynamic SQL and
# session state, whatever the dialect calls them.
STRICT_COMMON_DENYLIST = (
    "sleep", "benchmark", "get_lock", "release_lock", "release_all_locks", "is_free_lock", "is_used_lock",
    "exec*", "eval*", "query", "query_table", "identifier", "getvariable", "setvariable", "set_variable",
    "current_setting*", "secret*", "try_secret", "list_secrets", "url_fetch*", "external_*",
)


@dataclass(frozen=True)
class DialectProfile:
    name: str
    strict: bool = True
    denylist: tuple[str, ...] = ()
    # Anonymous (sqlglot-unmodelled) functions a strict dialect still accepts, lower case.
    allowed_anonymous: frozenset[str] = frozenset()
    # How unquoted identifiers fold in the database: "lower" (postgres-like), "upper" (Snowflake:
    # the catalog reports case-insensitive names in lower case, the database stores them upper
    # case), or "insensitive" (tsql: compared case-insensitively whatever the quoting).
    fold: Literal["lower", "upper", "insensitive"] = "lower"
    # Schema names that, in this dialect, turn the "table" part into a file path (Spark/Databricks
    # `parquet.`/path``). A reference to one is rejected even if an asset of that name existed.
    path_schemas: frozenset[str] = frozenset()
    notes: str = ""

    def denied(self) -> tuple[str, ...]:
        return BASE_DENYLIST + (STRICT_COMMON_DENYLIST if self.strict else ()) + self.denylist


PROFILES: dict[str, DialectProfile] = {
    "postgres": DialectProfile("postgres", strict=False, notes="increment-3 rules, unchanged"),
    "tsql": DialectProfile("tsql", strict=False, fold="insensitive", notes="increment-3 rules, unchanged"),
    "duckdb": DialectProfile(
        "duckdb",
        denylist=(
            "columns", "sniff_csv", "parquet_*", "iceberg_*", "delta_*", "sqlite_*", "postgres_*", "mysql_*",
            "st_read*", "which_secret", "duckdb_*", "pragma_*", "json_execute_serialized_sql", "read_*",
            "load", "install", "checkpoint", "force_checkpoint", "enable_*", "disable_*", "current_setting",
            "arrow_scan*", "scan_arrow_ipc", "httpfs*", "s3*", "gcs*", "azure_*", "ducklake_*",
        ),
        allowed_anonymous=frozenset({"date_diff", "datediff", "epoch", "list_value", "strftime", "strptime",
                                     "make_date", "isodow", "dayofweek", "hour", "try_cast"}),
        path_schemas=frozenset({"read_csv", "read_parquet"}),
        notes="files and local federation; the file is also opened read_only with external access off",
    ),
    "snowflake": DialectProfile(
        "snowflake",
        denylist=(
            "system$*", "result_scan", "last_query_id", "get_presigned_url", "build_scoped_file_url",
            "build_stage_file_url", "get_stage_location", "get_relative_path", "get_absolute_path",
            "get_ddl", "infer_schema", "validate", "validate_pipe_load", "copy_history", "task_*",
            "stage_*", "ai_*", "complete", "snowflake.*", "explain_json", "policy_*", "get_query_operator_stats",
            "query_history*", "login_history*", "warehouse_*", "automatic_clustering_history",
        ),
        fold="upper",
        notes="stages (@...), time travel (AT/BEFORE/CHANGES) and session variables ($x) are rejected",
    ),
    "bigquery": DialectProfile(
        "bigquery",
        denylist=(
            "external_query", "external_object_transform", "ml.*", "aead.*", "keys.*", "net.*", "obj.*",
            "vector_search", "appends", "changes", "ai.*", "gemini*", "text_embedding*",
        ),
        fold="lower",
        notes="EXTERNAL_QUERY, namespaced functions (ML./AEAD./KEYS./NET.), @param/@@system variables, "
              "FOR SYSTEM_TIME AS OF and wildcard tables are rejected",
    ),
    "databricks": DialectProfile(
        "databricks",
        denylist=(
            "read_files", "read_kafka", "read_kinesis", "read_pubsub", "read_pulsar", "read_statestore",
            "read_state_metadata", "cloud_files_state", "event_log", "reflect", "try_reflect", "java_method",
            "ai_*", "http_request", "input_file_name", "input_file_block_*", "vector_search", "table_changes",
        ),
        path_schemas=frozenset({"parquet", "csv", "json", "delta", "text", "orc", "avro", "binaryfile", "jdbc",
                                "cloudfiles", "xml", "image", "file", "dbfs", "s3", "s3a", "abfss", "gs", "wasbs"}),
        notes="format-path tables (parquet.`/p`), reflect/java_method, ai_*/http_request, ${var} and "
              "VERSION/TIMESTAMP AS OF are rejected",
    ),
    "trino": DialectProfile(
        "trino",
        denylist=("sequence", "generate_series", "generateseries", "system.*"),
        notes="table functions (TABLE(system.query(...))), hidden $path columns, $-metadata tables, "
              "FOR VERSION/TIMESTAMP AS OF and ? parameters are rejected",
    ),
    "mysql": DialectProfile(
        "mysql",
        denylist=(
            "load_file", "master_pos_wait", "source_pos_wait", "wait_for_executed_gtid_set",
            "wait_until_sql_thread_after_gtids", "sys_exec", "sys_eval", "lib_mysqludf_*", "extractvalue",
            "updatexml", "roles_graphml", "statement_digest*", "ps_*",
        ),
        notes="LOAD_FILE, SLEEP/BENCHMARK/GET_LOCK, @user and @@system variables, INTO OUTFILE/DUMPFILE, "
              "locking reads; /*! executable comments */ never reach the database (SQL is regenerated)",
    ),
}

SUPPORTED_DIALECTS = frozenset(PROFILES)


def profile(dialect: str) -> DialectProfile:
    return PROFILES[dialect]

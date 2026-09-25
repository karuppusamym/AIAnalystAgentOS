-- One Postgres server, isolated databases and identities:
--   analystos  control plane (application state)        owner: analystos
--   analytics  analytics plane (staged source snapshots) writer: analystos_loader, reader: analystos_reader
--   superset   Superset metadata                          owner: superset
--   temporal   created by temporalio/auto-setup
-- The reader identity is the ONLY one the query gateway and Superset use for data. It cannot
-- connect to the control-plane database (§45: control plane and query identity are separated).
-- Staged schemas are readable only through per-workspace NOLOGIN roles (analystos_r_<workspace>)
-- that the loader creates (hence CREATEROLE) and grants to the reader WITHOUT inheritance: the
-- gateway does SET LOCAL ROLE per query, so the reader login alone reads no workspace's data.
-- Clusters initialised before this: `analystos migrate` grants CREATEROLE and backfills the roles.
CREATE ROLE analystos_loader LOGIN CREATEROLE PASSWORD 'loader';
CREATE ROLE analystos_reader LOGIN NOINHERIT PASSWORD 'reader';
CREATE ROLE superset LOGIN PASSWORD 'superset';

CREATE DATABASE analytics OWNER analystos_loader;
CREATE DATABASE superset OWNER superset;

REVOKE CONNECT ON DATABASE analystos FROM PUBLIC;
REVOKE CONNECT ON DATABASE superset FROM PUBLIC;
GRANT CONNECT ON DATABASE analystos TO analystos;
REVOKE CONNECT ON DATABASE analytics FROM PUBLIC;
GRANT CONNECT ON DATABASE analytics TO analystos_loader, analystos_reader;

ALTER ROLE analystos_reader SET default_transaction_read_only = on;
ALTER ROLE analystos_reader SET statement_timeout = '60s';

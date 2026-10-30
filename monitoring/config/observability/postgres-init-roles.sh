#!/bin/bash
# First-boot initialization for the ci-observability-postgres container:
# the two roles, the dedicated schema, and reader grants. W01 owns all
# table migrations inside the schema. Runs once, on an empty data volume
# (docker-entrypoint-initdb.d), inside the container.
#
# Passwords arrive as environment variables from the protected
# EnvironmentFile and reach psql through \getenv, so they never appear in
# a command line, process listing, or log.
set -euo pipefail

: "${POSTGRES_USER:?expected in the postgres image}"
: "${POSTGRES_DB:?expected in the postgres image}"
: "${CI_OBSERVABILITY_INGEST_PASSWORD:?set by observability-stack.sh --init-secrets}"
: "${CI_OBSERVABILITY_READER_PASSWORD:?set by observability-stack.sh --init-secrets}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v bootstrap_user="$POSTGRES_USER" -v dbname="$POSTGRES_DB" <<'EOSQL'
\getenv ingest_password CI_OBSERVABILITY_INGEST_PASSWORD
\getenv reader_password CI_OBSERVABILITY_READER_PASSWORD

-- Ingest role: owns and writes the schema (W01 migrations, app writes).
SELECT 'CREATE ROLE ci_observability LOGIN' WHERE NOT EXISTS
    (SELECT FROM pg_roles WHERE rolname = 'ci_observability')\gexec
ALTER ROLE ci_observability LOGIN PASSWORD :'ingest_password';
ALTER ROLE ci_observability CONNECTION LIMIT 40;

-- Read-only role for dashboards and analysis: SELECT only.
SELECT 'CREATE ROLE ci_observability_reader LOGIN' WHERE NOT EXISTS
    (SELECT FROM pg_roles WHERE rolname = 'ci_observability_reader')\gexec
ALTER ROLE ci_observability_reader LOGIN PASSWORD :'reader_password';
ALTER ROLE ci_observability_reader CONNECTION LIMIT 20;

-- Dedicated schema (not public), owned by the ingest role.
CREATE SCHEMA IF NOT EXISTS ci_observability AUTHORIZATION ci_observability;
GRANT USAGE ON SCHEMA ci_observability TO ci_observability_reader;

-- Migrations run as the ingest role and revoke access to private tables.
-- Restores run as the bootstrap user; its defaults must not undo the
-- archive's private-table ACLs while recreating those tables.
ALTER DEFAULT PRIVILEGES FOR ROLE ci_observability IN SCHEMA ci_observability
    GRANT SELECT ON TABLES TO ci_observability_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE :"bootstrap_user" IN SCHEMA ci_observability
    REVOKE SELECT ON TABLES FROM ci_observability_reader;

-- Dedicated database: nothing is inherited from PUBLIC. Only the two
-- stack roles may connect, only the ingest role may create temporary
-- tables (migrations can use them), and nobody creates in schema public.
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"dbname" TO ci_observability, ci_observability_reader;
GRANT TEMP ON DATABASE :"dbname" TO ci_observability;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- New connections see the dedicated schema first.
ALTER DATABASE :"dbname" SET search_path TO ci_observability, public;
EOSQL

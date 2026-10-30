#!/usr/bin/env bash
# ci-observability-restore.sh -- Restore a pg_dump custom-format backup
# (made by ci-observability-backup.sh) into the ci_observability
# database, through the running ci-observability-postgres container.
#
# The archive is validated before anything is touched, only the
# ci_observability schema is restored (other schemas in the database are
# preserved), and the restore runs in a single transaction: a mid-restore
# error rolls back and leaves the previous data unchanged.
#
# Unsafe overwrite is refused by default: if the ci_observability schema
# already holds any object, the restore stops and says so. --force is
# the explicit opt-in to drop and replace that schema; its data is lost.
#
# Archives are trusted-DBA input: restore them only from sources you
# control, never from GitHub artifacts or other untrusted storage.
#
# Usage: ci-observability-restore.sh <dump-file> [--force]
#
# Environment overrides (used by the self-tests in
# monitoring/tests/observability/):
#   CI_OBSERVABILITY_POSTGRES_CONTAINER  default ci-observability-postgres
#   CI_OBSERVABILITY_DATABASE            default ci_observability
set -euo pipefail

# Same runner-environment hygiene as monitoring-setup.sh.
unset CONTAINERS_STORAGE_CONF
umask 077

CONTAINER="${CI_OBSERVABILITY_POSTGRES_CONTAINER:-ci-observability-postgres}"
DATABASE="${CI_OBSERVABILITY_DATABASE:-ci_observability}"
SCHEMA="ci_observability"

info() { echo "    $1"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

usage() {
    echo "Usage: $(basename "$0") <dump-file> [--force]"
    echo "  --force  drop and replace an existing ci_observability schema"
    exit 1
}

DUMP=""
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        -h|--help) usage ;;
        *)
            [[ -z "${DUMP}" ]] || usage
            DUMP="$1"
            shift
            ;;
    esac
done
[[ -n "${DUMP}" ]] || usage

if [[ -L "${DUMP}" ]]; then
    fail "dump file ${DUMP} is a symlink -- refusing"
fi
[[ -f "${DUMP}" && -r "${DUMP}" ]] || fail "cannot read dump file: ${DUMP}"
[[ -s "${DUMP}" ]] || fail "dump file is empty: ${DUMP}"
command -v podman >/dev/null 2>&1 || fail "podman not found"
podman inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q true \
    || fail "container ${CONTAINER} is not running"

# Validate the archive before touching the database.
if ! podman exec -i "${CONTAINER}" pg_restore --list < "${DUMP}" >/dev/null 2>&1; then
    fail "${DUMP} is not a readable pg_dump custom-format archive"
fi

# Any object in the schema (relations, routines, standalone types)
# counts: the refusal must not be bypassed by restoring over a schema
# that holds something other than tables.
object_count() {
    local count
    count="$(podman exec "${CONTAINER}" psql --username postgres --dbname "${DATABASE}" \
        --tuples-only --no-align --command "
        SELECT count(*) FROM (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = '${SCHEMA}'
            UNION ALL
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = '${SCHEMA}'
            UNION ALL
            SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname = '${SCHEMA}' AND t.typrelid = 0 AND t.typelem = 0
        ) objects" 2>/dev/null)"
    [[ "${count}" =~ ^[0-9]+$ ]] || fail "cannot inspect schema ${SCHEMA} in ${CONTAINER}"
    echo "${count}"
}

BEFORE="$(object_count)"
if (( BEFORE > 0 )) && (( FORCE == 0 )); then
    fail "refusing to restore: ${DATABASE} already holds ${BEFORE} object(s) in schema ${SCHEMA}. Re-run with --force to drop and replace them."
fi
if (( BEFORE > 0 )); then
    info "--force: dropping and rebuilding schema ${SCHEMA} (${BEFORE} objects)"
fi

# --clean --if-exists rebuilds the schema contained in the dump,
# replacing only that schema; --single-transaction makes the whole
# restore atomic, so a failure rolls back to the previous state.
info "restoring ${DUMP} into ${DATABASE} (schema ${SCHEMA} only) ..."
if ! podman exec -i "${CONTAINER}" pg_restore --username postgres --dbname "${DATABASE}" \
        --schema="${SCHEMA}" --clean --if-exists --single-transaction < "${DUMP}"; then
    fail "restore failed; the database was left unchanged (single transaction)"
fi

AFTER="$(object_count)"
info "restore complete: ${AFTER} object(s) in schema ${SCHEMA}"
if (( AFTER == 0 )); then
    echo "  WARNING: the dump contained no objects in schema ${SCHEMA}."
fi

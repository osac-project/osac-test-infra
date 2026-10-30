#!/usr/bin/env bash
# ci-observability-backup.sh -- pg_dump the ci_observability schema
# (custom format) through the running ci-observability-postgres
# container, verify the archive, then publish it atomically.
#
# Scope: touches only this stack's named container and the explicit
# --output-dir (default CI_OBSERVABILITY_HOME/backups); only the
# ci_observability schema is dumped, never other schemas in the
# database. Archives are trusted-DBA input: restore them only from
# sources you control, never from GitHub artifacts or other untrusted
# storage. Loki pilot logs and CI Prometheus data are disposable and
# not backed up; see monitoring/observability-services.md.
#
# Usage: ci-observability-backup.sh [--output-dir DIR]
#
# Environment overrides (used by the self-tests in
# monitoring/tests/observability/):
#   CI_OBSERVABILITY_POSTGRES_CONTAINER  default ci-observability-postgres
#   CI_OBSERVABILITY_DATABASE            default ci_observability
#   CI_OBSERVABILITY_HOME                default ~/.ci-observability
set -euo pipefail

# Same runner-environment hygiene as monitoring-setup.sh.
unset CONTAINERS_STORAGE_CONF
umask 077

CONTAINER="${CI_OBSERVABILITY_POSTGRES_CONTAINER:-ci-observability-postgres}"
DATABASE="${CI_OBSERVABILITY_DATABASE:-ci_observability}"
STACK_HOME="${CI_OBSERVABILITY_HOME:-${HOME}/.ci-observability}"
OUTPUT_DIR="${STACK_HOME}/backups"
SCHEMA="ci_observability"

info() { echo "    $1"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

usage() {
    echo "Usage: $(basename "$0") [--output-dir DIR]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output-dir)
            [[ -n "${2:-}" ]] || usage
            OUTPUT_DIR="$2"
            shift 2
            ;;
        -h|--help) usage ;;
        *)          usage ;;
    esac
done

# The output root must be a real directory. It is never chmod'ed or
# created with looser permissions than 700; an existing directory keeps
# whatever mode the operator chose (a warning is shown if it is loose).
if [[ -L "${OUTPUT_DIR}" ]]; then
    fail "output dir ${OUTPUT_DIR} is a symlink -- refusing"
fi
if [[ -d "${OUTPUT_DIR}" ]]; then
    if (( (8#$(stat -c %a "${OUTPUT_DIR}") % 8#100) != 0 )); then
        echo "  WARNING: ${OUTPUT_DIR} is group- or world-accessible; backups" >&2
        echo "           contain database content." >&2
    fi
else
    mkdir -p "${OUTPUT_DIR}"
fi

command -v podman >/dev/null 2>&1 || fail "podman not found"
podman inspect --format '{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q true \
    || fail "container ${CONTAINER} is not running"

# Sub-second precision so concurrent backups never share a destination
# name; the ln publish below still refuses any collision outright.
STAMP="$(date -u +%Y%m%dT%H%M%S.%N)"
DEST="${OUTPUT_DIR}/ci_observability_${STAMP}.dump"
# Exclusive private temp file in the destination directory, so the
# publish below is a same-filesystem atomic operation.
TMP="$(mktemp "${OUTPUT_DIR}/.ci_observability_XXXXXX")"
trap 'rm -f "${TMP}"' EXIT

info "dumping schema ${SCHEMA} of ${DATABASE} from ${CONTAINER} ..."
if ! podman exec "${CONTAINER}" pg_dump --username postgres --dbname "${DATABASE}" \
        --schema="${SCHEMA}" --format=custom > "${TMP}"; then
    fail "pg_dump failed"
fi
[[ -s "${TMP}" ]] || fail "pg_dump produced an empty archive"

# Round-trip the archive through the container before publishing it, so
# a truncated or corrupt dump never becomes today's backup.
if ! podman exec -i "${CONTAINER}" pg_restore --list < "${TMP}" >/dev/null 2>&1; then
    fail "pg_restore --list rejected the fresh dump -- not publishing it"
fi

# Atomic no-clobber publish: ln fails if DEST exists, so a concurrent
# backup can never be overwritten, whatever timestamps they share.
if ! ln "${TMP}" "${DEST}" 2>/dev/null; then
    fail "refusing to overwrite existing ${DEST} (concurrent backup?)"
fi
rm -f "${TMP}"
trap - EXIT
info "backup written and verified: ${DEST}"
info "restore it with monitoring/scripts/ci-observability-restore.sh"

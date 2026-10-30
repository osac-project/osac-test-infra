#!/usr/bin/env bash
# observability-stack.sh -- Deploy and manage the OSAC CI observability
# pilot stack (W05a): PostgreSQL 17, single-process Loki, an isolated
# short-retention CI Prometheus, and Grafana Alloy shipping redacted
# spool logs to Loki.
#
# OPT-IN and separate from the fleet monitoring stack: installs only
# ci-observability-*.container quadlet units, writes only under
# CI_OBSERVABILITY_HOME (default ~/.ci-observability), and manages only
# the four ci-observability-* services. Never touches anything
# monitoring-setup.sh owns.
#
# Private loopback endpoints only:
#   PostgreSQL  127.0.0.1:5433   database ci_observability
#   Loki        127.0.0.1:3100   single-process, local-disk PILOT
#   Prometheus  127.0.0.1:9095   isolated CI metrics, 7d retention
#   Alloy       127.0.0.1:12345  redacted spool -> Loki
#   ledger      127.0.0.1:9185   W01's service, NOT part of W05a
#
# Loki on local disk is pilot-only; fleet rollout requires configured
# object storage. Tempo (W05b) is out of scope. Filesystem byte quotas
# for the data directories are an explicit W05/W13 rollout gate on the
# trusted host; nothing here enforces one.
#
# Secrets come only from CI_OBSERVABILITY_HOME/secrets/postgres.env
# (mode 600, created by --init-secrets). Values are never printed and
# never appear on command lines; there is no default password and the
# stack fails closed without them. A shared CI runner's Unix UID is NOT
# a secure secret boundary -- deploy only from a trusted monitoring
# account/host.
#
# Commands:
#   --check [--offline]  Validate configs, image pins, secrets, ports.
#                        --offline skips container-based config checks
#                        and is NOT a full acceptance run.
#   --plan               Print the deployment plan. No side effects.
#   --init-secrets       Create secrets/postgres.env with strong random
#                        values. Refuses to overwrite an existing file.
#   --deploy             Install configs and quadlet units, then (re)start
#                        the four services. Idempotent. Requires the
#                        default stack home (units bind %h/.ci-observability).
#   --status             Service states and endpoint health; exits
#                        nonzero if anything is unhealthy.
#   --start | --stop     Start or stop the four services.
#   --down               Stop the services and remove the four quadlet
#                        definitions (moved to a project-owned directory,
#                        not discarded). Keeps all data, secrets, backups,
#                        spool, and the postgres volume.
#
# Prerequisites: podman; --deploy/--start/--stop/--down/--status also
# need a working `systemctl --user`. Run from the repo root (or set
# MONITORING_REPO_DIR). See monitoring/observability-services.md.
set -euo pipefail

# Same runner-environment hygiene as monitoring-setup.sh: a GitHub
# Actions job may spawn this without a login-shell D-Bus session, and
# may carry a job-scoped CONTAINERS_STORAGE_CONF that would point this
# script's podman calls at the wrong (isolated) storage.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"
unset CONTAINERS_STORAGE_CONF

umask 077

STACK_HOME="${CI_OBSERVABILITY_HOME:-${HOME}/.ci-observability}"
DEFAULT_STACK_HOME="${HOME}/.ci-observability"
MONITORING_REPO_DIR="${MONITORING_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
OBS_CONFIG_DIR="${MONITORING_REPO_DIR}/config/observability"
QUADLET_DIR="${HOME}/.config/containers/systemd"
SECRETS_DIR="${STACK_HOME}/secrets"
SECRETS_FILE="${SECRETS_DIR}/postgres.env"

UNITS=(
    ci-observability-postgres
    ci-observability-loki
    ci-observability-prometheus
    ci-observability-alloy
)
CONFIG_FILES=(loki.yml ci-prometheus.yml alloy-config.alloy postgres-init-roles.sh)
SECRET_VARS=(POSTGRES_PASSWORD CI_OBSERVABILITY_INGEST_PASSWORD CI_OBSERVABILITY_READER_PASSWORD)
MIN_SECRET_LEN=16

# Images must stay pinned by digest; --check enforces this.
LOKI_IMAGE="docker.io/grafana/loki@sha256:0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28"
PROM_IMAGE="quay.io/prometheus/prometheus@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2"
ALLOY_IMAGE="docker.io/grafana/alloy@sha256:89a5a320168387336b9ac3c850609c3ba7757eadd4ae5f1d819deb35e9854b18"

# Port -> the unit that owns it when this stack is deployed. Loki's gRPC
# is 9097, deliberately not its 9095 default (the CI Prometheus's port).
PORT_OWNERS="5433:ci-observability-postgres 3100:ci-observability-loki
9095:ci-observability-prometheus 9097:ci-observability-loki 12345:ci-observability-alloy"

info() { echo "    $1"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

usage() {
    echo "Usage: $(basename "$0") --check [--offline]"
    echo "       $(basename "$0") --plan | --init-secrets | --deploy | --status"
    echo "       $(basename "$0") --start | --stop | --down"
    exit 1
}

secret_value() { # $1 = file, $2 = variable name; never printed by callers
    awk -F= -v k="$2" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$1"
}

# A directory this stack relies on must be a real directory, owned by
# this user, with no group or world access, and not a symlink.
check_dir_protection() { # $1 = path, $2 = what it is for
    local dir="$1"
    if [[ -L "${dir}" ]]; then
        fail "${dir} (${2}) is a symlink -- refusing"
    fi
    if [[ ! -d "${dir}" ]]; then
        fail "${dir} (${2}) is not a directory"
    fi
    if [[ "$(stat -c %u "${dir}")" != "$(id -u)" ]]; then
        fail "${dir} (${2}) must be owned by uid $(id -u)"
    fi
    local perms
    perms="$(stat -c %a "${dir}")"
    if (( (8#${perms} % 8#100) != 0 )); then
        fail "${dir} (${2}) must not be group- or world-accessible, is mode ${perms}"
    fi
}

# Parse the secrets file strictly: exactly the allowlisted keys, each
# exactly once, no other content, and values restricted to a set that is
# safe for a Podman EnvironmentFile (no spaces, quotes, backslashes,
# variable markers, or control characters). Diagnostic output never
# includes values.
parse_secrets_file() { # $1 = file; prints problems, exit 0 = clean
    awk '
        BEGIN {
            n = split("POSTGRES_PASSWORD CI_OBSERVABILITY_INGEST_PASSWORD CI_OBSERVABILITY_READER_PASSWORD", want, " ")
            for (i = 1; i <= n; i++) allowed[want[i]] = 1
        }
        /^[[:space:]]*($|#)/ { next }
        {
            if ($0 !~ /^[A-Za-z_][A-Za-z0-9_]*=/) {
                printf "line %d: malformed line (expected KEY=value)\n", FNR
                bad = 1
                next
            }
            key = substr($0, 1, index($0, "=") - 1)
            value = substr($0, index($0, "=") + 1)
            seen[key]++
            if (!(key in allowed)) {
                printf "line %d: key %s is not in the allowlist\n", FNR, key
                bad = 1
            }
            if (seen[key] > 1) {
                printf "line %d: duplicate key %s\n", FNR, key
                bad = 1
            }
            if (value !~ /^[A-Za-z0-9._+=\/:!-]+$/) {
                printf "line %d: value of %s uses characters outside the safe set\n", FNR, key
                bad = 1
            }
        }
        END {
            for (k in allowed) {
                if (!(k in seen)) {
                    printf "missing key %s\n", k
                    bad = 1
                }
            }
            exit bad ? 1 : 0
        }
    ' "$1"
}

check_secrets() {
    if [[ -L "${SECRETS_FILE}" ]]; then
        fail "${SECRETS_FILE} is a symlink -- refusing"
    fi
    if [[ ! -f "${SECRETS_FILE}" ]]; then
        fail "missing ${SECRETS_FILE} -- run '$(basename "$0") --init-secrets' first"
    fi
    check_dir_protection "${STACK_HOME}" "stack home"
    check_dir_protection "${SECRETS_DIR}" "secrets directory"
    local perms owner problems name value
    perms="$(stat -c %a "${SECRETS_FILE}")"
    if [[ "${perms}" != "600" && "${perms}" != "400" ]]; then
        fail "${SECRETS_FILE} must be mode 600 (or 400), is ${perms}"
    fi
    owner="$(stat -c %u "${SECRETS_FILE}")"
    if [[ "${owner}" != "$(id -u)" ]]; then
        fail "${SECRETS_FILE} must be owned by uid $(id -u), is ${owner}"
    fi
    problems="$(parse_secrets_file "${SECRETS_FILE}")" || true
    if [[ -n "${problems}" ]]; then
        fail "invalid ${SECRETS_FILE}:
${problems}"
    fi
    for name in "${SECRET_VARS[@]}"; do
        value="$(secret_value "${SECRETS_FILE}" "${name}")"
        if (( ${#value} < MIN_SECRET_LEN )); then
            fail "secret ${name} is shorter than ${MIN_SECRET_LEN} characters"
        fi
        case "${value,,}" in
            password|password*|changeme|changeme*|admin|admin*|secret|secret*|\
            replace_me*|foobar*|test|test*)
                fail "secret ${name} is a known weak value" ;;
        esac
    done
    info "secrets: present, protected (mode ${perms}), strict parse and value checks passed"
}

check_image_pins() {
    local unit image
    for unit in "${UNITS[@]}"; do
        image="$(sed -n 's/^Image=//p' "${MONITORING_REPO_DIR}/quadlet/${unit}.container")"
        if [[ ! "${image}" =~ @sha256:[0-9a-f]{64}$ ]]; then
            fail "${unit}.container is not digest-pinned: ${image:-missing}"
        fi
    done
    info "images: all four quadlet units pinned by sha256 digest"
}

check_config_files() {
    local f
    for f in "${CONFIG_FILES[@]}"; do
        if [[ ! -f "${OBS_CONFIG_DIR}/${f}" ]]; then
            fail "missing ${OBS_CONFIG_DIR}/${f}"
        fi
    done
    bash -n "${OBS_CONFIG_DIR}/postgres-init-roles.sh" \
        || fail "postgres-init-roles.sh fails bash -n"
    if grep -q 'loki.source.journal\|/var/log/journal\|/run/log/journal' \
            "${OBS_CONFIG_DIR}/alloy-config.alloy" \
            "${MONITORING_REPO_DIR}/quadlet/ci-observability-alloy.container"; then
        fail "raw journald collection is not allowed in this stack (redacted spool only)"
    fi
    info "config files: present under ${OBS_CONFIG_DIR}, no raw log source"
}

# A port is a problem only when someone other than this stack's own
# container holds it, so re-running --check/--deploy on a deployed stack
# stays green.
check_ports() { # $1 = what to do on a foreign listener: warn|fail
    local action="$1"
    if ! command -v ss >/dev/null 2>&1; then
        echo "  WARNING: ss not available -- port collisions not checked."
        return 0
    fi
    local port unit listeners spec
    for spec in ${PORT_OWNERS}; do
        port="${spec%%:*}"
        unit="${spec##*:}"
        listeners="$(ss -ltnH "sport = :${port}" 2>/dev/null)"
        [[ -z "${listeners}" ]] && continue
        if podman inspect --format '{{.State.Running}}' "${unit}" 2>/dev/null | grep -q true; then
            info "port ${port}: held by this stack's running ${unit}"
            continue
        fi
        if [[ "${action}" == "fail" ]]; then
            fail "port ${port} is in use by something other than ${unit}: ${listeners}"
        else
            echo "  WARNING: port ${port} is in use (not by ${unit}): ${listeners}"
        fi
    done
    info "ports 5433 3100 9095 9097 12345: free or held by this stack"
}

# Semantically validate the shipped configs with the real upstream
# binaries, through ephemeral throwaway containers. Configs are copied to
# a private temp dir (never mounted from the repo) so validation cannot
# relabel or modify tracked files.
check_configs_with_containers() {
    if ! command -v podman >/dev/null 2>&1; then
        fail "podman not found (rerun with --offline to skip this part)"
    fi
    local tmp
    tmp="$(mktemp -d)"
    # 755/644 so the images' non-root users (loki runs as uid 10001, the
    # prometheus image as nobody) can read the copies; the umask would
    # otherwise make them owner-only. No secrets here.
    chmod 755 "${tmp}"
    cp "${OBS_CONFIG_DIR}/loki.yml" "${OBS_CONFIG_DIR}/ci-prometheus.yml" \
       "${OBS_CONFIG_DIR}/alloy-config.alloy" "${tmp}/"
    chmod 644 "${tmp}"/*.yml "${tmp}"/*.alloy
    if ! podman run --rm -v "${tmp}:/cfg:Z" "${LOKI_IMAGE}" \
            -verify-config -config.file=/cfg/loki.yml >/dev/null 2>&1; then
        rm -rf "${tmp}"
        fail "loki.yml rejected by 'loki -verify-config'"
    fi
    info "loki.yml: accepted by loki -verify-config"
    if ! podman run --rm -v "${tmp}:/cfg:Z" --entrypoint /bin/promtool "${PROM_IMAGE}" \
            check config /cfg/ci-prometheus.yml >/dev/null 2>&1; then
        rm -rf "${tmp}"
        fail "ci-prometheus.yml rejected by 'promtool check config'"
    fi
    info "ci-prometheus.yml: accepted by promtool check config"
    if ! podman run --rm -v "${tmp}:/cfg:Z" "${ALLOY_IMAGE}" \
            validate /cfg/alloy-config.alloy >/dev/null 2>&1; then
        rm -rf "${tmp}"
        fail "alloy-config.alloy rejected by 'alloy validate'"
    fi
    info "alloy-config.alloy: accepted by alloy validate"
    rm -rf "${tmp}"
}

cmd_plan() {
    echo "OSAC CI observability pilot stack (W05a) -- deployment plan"
    echo "  Stack home:   ${STACK_HOME}"
    echo "  Quadlet dir:  ${QUADLET_DIR}"
    echo "  Units (systemd --user):"
    local unit image
    for unit in "${UNITS[@]}"; do
        image="$(sed -n 's/^Image=//p' "${MONITORING_REPO_DIR}/quadlet/${unit}.container")"
        echo "    ${unit}.service  (${image})"
    done
    echo "  Data:         ${STACK_HOME}/data/{loki,prometheus,alloy}"
    echo "                named podman volume ci-observability-postgres-data"
    echo "  Spool:        ${STACK_HOME}/spool/redacted (W06 redacted-log input,"
    echo "                mounted read-only into Alloy; idle until W06 writes)"
    echo "  Secrets:      ${SECRETS_FILE} (mode 600, --init-secrets)"
    echo "  Backups:      ${STACK_HOME}/backups (ci-observability-backup.sh)"
    echo "  Endpoints (loopback only): postgres 127.0.0.1:5433, loki 127.0.0.1:3100,"
    echo "                prometheus 127.0.0.1:9095, alloy 127.0.0.1:12345"
    echo "  Retention:    loki 14d, prometheus 7d (retention.size is a soft"
    echo "                target; no filesystem byte quota is enforced here)"
    if [[ "${STACK_HOME}" != "${DEFAULT_STACK_HOME}" ]]; then
        echo "  NOTE: --deploy requires the default home (${DEFAULT_STACK_HOME});"
        echo "        CI_OBSERVABILITY_HOME is honored by --check/--plan and the"
        echo "        backup/restore helpers only."
    fi
}

# Create the stack and secrets directories for the first time, strictly:
# fresh creation only, protected modes, no pre-existing weirdness.
prepare_stack_dirs() {
    if [[ -e "${STACK_HOME}" ]]; then
        check_dir_protection "${STACK_HOME}" "stack home"
    else
        mkdir "${STACK_HOME}"
    fi
    if [[ -e "${SECRETS_DIR}" ]]; then
        check_dir_protection "${SECRETS_DIR}" "secrets directory"
    else
        mkdir "${SECRETS_DIR}"
    fi
    chmod 700 "${STACK_HOME}" "${SECRETS_DIR}"
}

cmd_init_secrets() {
    prepare_stack_dirs
    # Exclusive, race-free creation: mktemp gives a private file nothing
    # else can see, and the hardlink publish fails atomically if the
    # target already exists (including as a symlink).
    local tmp
    tmp="$(mktemp "${SECRETS_DIR}/.postgres.env.XXXXXX")"
    {
        echo "# CI observability stack secrets (generated $(date -u +%Y-%m-%dT%H:%M:%SZ))"
        echo "# Mode 600, never commit, never copy into PR-controlled contexts."
        echo "POSTGRES_PASSWORD=$(head -c 32 /dev/urandom | base64)"
        echo "CI_OBSERVABILITY_INGEST_PASSWORD=$(head -c 32 /dev/urandom | base64)"
        echo "CI_OBSERVABILITY_READER_PASSWORD=$(head -c 32 /dev/urandom | base64)"
    } > "${tmp}"
    if ! ln "${tmp}" "${SECRETS_FILE}" 2>/dev/null; then
        rm -f "${tmp}"
        fail "refusing to overwrite existing ${SECRETS_FILE}"
    fi
    rm -f "${tmp}"
    info "wrote ${SECRETS_FILE} (mode 600) with three strong random secrets"
    info "back it up somewhere safe: it is the only copy"
}

wait_for_container() { # $1 = container name
    local container="$1" ready=false _
    for _ in $(seq 1 30); do
        if podman inspect --format '{{.State.Running}}' "${container}" 2>/dev/null | grep -q true; then
            ready=true
            break
        fi
        sleep 1
    done
    if [[ "${ready}" == "true" ]]; then
        info "${container}: running"
    else
        # Backend logs may contain backend data, so they are not dumped
        # here; the operator inspects them manually.
        echo "ERROR: ${container} not running after 30 seconds." >&2
        echo "  Inspect manually: podman ps -a && podman logs ${container}" >&2
        exit 1
    fi
}

cmd_deploy() {
    if [[ "${STACK_HOME}" != "${DEFAULT_STACK_HOME}" ]]; then
        fail "--deploy requires the default stack home (${DEFAULT_STACK_HOME}): the quadlet units bind to %h/.ci-observability. Unset CI_OBSERVABILITY_HOME."
    fi
    check_config_files
    check_image_pins
    check_secrets
    check_ports fail
    check_configs_with_containers

    prepare_stack_dirs
    mkdir -p "${STACK_HOME}"/config "${STACK_HOME}"/backups \
             "${STACK_HOME}"/data/loki "${STACK_HOME}"/data/prometheus \
             "${STACK_HOME}"/data/alloy "${STACK_HOME}"/spool/redacted
    chmod 700 "${STACK_HOME}/config" "${STACK_HOME}/backups" "${STACK_HOME}/data" \
              "${STACK_HOME}/data/loki" "${STACK_HOME}/data/prometheus" \
              "${STACK_HOME}/data/alloy" "${STACK_HOME}/spool" \
              "${STACK_HOME}/spool/redacted"

    local f unit
    for f in "${CONFIG_FILES[@]}"; do
        cp -f "${OBS_CONFIG_DIR}/${f}" "${STACK_HOME}/config/${f}"
    done
    info "configs installed under ${STACK_HOME}/config"

    mkdir -p "${QUADLET_DIR}"
    for unit in "${UNITS[@]}"; do
        cp "${MONITORING_REPO_DIR}/quadlet/${unit}.container" "${QUADLET_DIR}/${unit}.container"
        info "installed ${unit}.container"
    done

    # daemon-reload runs the quadlet generator, which creates the systemd
    # units AND their default.target.wants enablement links from the
    # [Install] sections. `systemctl --user enable` must NOT be used here:
    # for generator-provided units it always fails ("transient or
    # generated"). Restart covers both fresh start and refresh.
    systemctl --user daemon-reload
    for unit in "${UNITS[@]}"; do
        info "starting ${unit}.service ..."
        systemctl --user restart "${unit}.service"
    done

    echo ""
    echo "Waiting for containers ..."
    for unit in "${UNITS[@]}"; do
        wait_for_container "${unit}"
    done

    echo ""
    echo "Deployment complete. Endpoints (loopback only):"
    echo "  postgres  127.0.0.1:5433  db ci_observability (roles ci_observability,"
    echo "                            ci_observability_reader)"
    echo "  loki      127.0.0.1:3100  pilot, local disk, 14d retention"
    echo "  prom      127.0.0.1:9095  7d retention (soft disk target)"
    echo "  alloy     127.0.0.1:12345 redacted spool -> Loki (idle until W06 writes)"
    echo "Next: 'monitoring/scripts/ci-observability-backup.sh' for the first backup."
}

cmd_status() {
    local healthy=true unit state
    for unit in "${UNITS[@]}"; do
        state="$(systemctl --user is-active "${unit}.service" 2>&1 || true)"
        echo "${unit}.service: ${state}"
        if [[ "${state}" != "active" ]]; then
            healthy=false
        fi
    done
    if command -v curl >/dev/null 2>&1; then
        if curl -sf --max-time 5 -o /dev/null http://127.0.0.1:3100/ready; then
            info "loki: ready"
        else
            echo "  WARNING: loki /ready not answering"
            healthy=false
        fi
        if curl -sf --max-time 5 -o /dev/null http://127.0.0.1:9095/-/ready; then
            info "prometheus: ready"
        else
            echo "  WARNING: prometheus /-/ready not answering"
            healthy=false
        fi
        if curl -sf --max-time 5 -o /dev/null http://127.0.0.1:12345/-/ready; then
            info "alloy: ready (log source stays idle until W06 writes spool files)"
        else
            echo "  WARNING: alloy /-/ready not answering"
            healthy=false
        fi
    else
        echo "  WARNING: curl not available -- endpoint health not checked"
        healthy=false
    fi
    if command -v podman >/dev/null 2>&1; then
        if podman exec ci-observability-postgres pg_isready -U postgres -d ci_observability; then
            info "postgres: accepting connections"
        else
            echo "  WARNING: postgres not answering"
            healthy=false
        fi
    else
        echo "  WARNING: podman not available -- database health not checked"
        healthy=false
    fi
    if [[ "${healthy}" != "true" ]]; then
        echo "ERROR: stack is not healthy" >&2
        exit 1
    fi
}

systemctl_action() { # $1 = systemctl verb
    local unit
    for unit in "${UNITS[@]}"; do
        systemctl --user "$1" "${unit}.service"
        info "${1} ${unit}.service"
    done
}

cmd_down() {
    # Stop the services, then remove ONLY the four quadlet definitions
    # this script installs (by exact name, from its explicit quadlet dir)
    # and reload the generator. `systemctl --user disable` is useless
    # here: generated units are re-enabled by the generator on the next
    # daemon-reload, so removing the definitions is the real teardown.
    # Everything else -- data, secrets, backups, spool, the postgres
    # volume, and every unrelated unit -- is preserved.
    local unit unit_file destination disabled_dir load_state moved=0
    for unit in "${UNITS[@]}"; do
        load_state="$(systemctl --user show --property=LoadState --value "${unit}.service")"
        case "${load_state}" in
            not-found) info "${unit}.service not installed (nothing to stop)" ;;
            loaded|masked|stub|bad-setting|error|merged)
                # Stop also cancels a pending auto-restart on an inactive unit.
                systemctl --user stop "${unit}.service"
                info "stopped ${unit}.service"
                ;;
            *) fail "cannot determine the load state of ${unit}.service" ;;
        esac
    done

    # Keep any locally edited definitions instead of discarding them: the
    # exact four files are moved into a project-owned, protected
    # directory. --deploy reinstalls the repo versions.
    disabled_dir="${STACK_HOME}/disabled-quadlets"
    if [[ -e "${STACK_HOME}" || -L "${STACK_HOME}" ]]; then
        check_dir_protection "${STACK_HOME}" "stack home"
    else
        mkdir "${STACK_HOME}"
    fi
    if [[ -L "${disabled_dir}" ]]; then
        fail "disabled quadlet directory is a symlink -- refusing"
    fi
    mkdir -p "${disabled_dir}"
    check_dir_protection "${disabled_dir}" "disabled quadlet directory"
    for unit in "${UNITS[@]}"; do
        unit_file="${QUADLET_DIR}/${unit}.container"
        [[ -f "${unit_file}" ]] || continue
        destination="${disabled_dir}/${unit}.container"
        if [[ -e "${destination}" ]]; then
            # A previous --down already saved a definition; keep both.
            destination="${disabled_dir}/${unit}.container.$(date -u +%Y%m%dT%H%M%S.%N)"
        fi
        mv "${unit_file}" "${destination}"
        moved=$((moved + 1))
    done
    systemctl --user daemon-reload
    info "moved ${moved} quadlet definition(s) to ${disabled_dir}"
    info "data, secrets, backups, spool, and the postgres volume are preserved"
    info "redeploy with --deploy (reinstalls the repo unit definitions)"
    info "full uninstall: also remove ${disabled_dir}, ${STACK_HOME}, and the"
    info "podman volume ci-observability-postgres-data"
}

MODE=""
OFFLINE=0
case "${1:-}" in
    --check)        MODE="check" ;;
    --plan)         MODE="plan" ;;
    --init-secrets) MODE="init-secrets" ;;
    --deploy)       MODE="deploy" ;;
    --status)       MODE="status" ;;
    --start)        MODE="start" ;;
    --stop)         MODE="stop" ;;
    --down)         MODE="down" ;;
    *)              usage ;;
esac
shift
if [[ "${1:-}" == "--offline" ]]; then
    if [[ "${MODE}" != "check" ]]; then
        fail "--offline is only valid with --check"
    fi
    OFFLINE=1
    shift
fi
if [[ $# -ne 0 ]]; then
    usage
fi

case "${MODE}" in
    check)
        check_config_files
        check_image_pins
        check_secrets
        check_ports warn
        if [[ "${OFFLINE}" == "1" ]]; then
            echo "  NOTE: --offline is NOT a full acceptance check: the configs were"
            echo "        not validated by the real loki/promtool/alloy binaries."
        else
            check_configs_with_containers
        fi
        info "check passed."
        ;;
    plan)          cmd_plan ;;
    init-secrets)  cmd_init_secrets ;;
    deploy)        cmd_deploy ;;
    status)        cmd_status ;;
    start)         systemctl_action start ;;
    stop)          systemctl_action stop ;;
    down)          cmd_down ;;
esac

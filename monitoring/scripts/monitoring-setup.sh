#!/usr/bin/env bash
# monitoring-setup.sh -- Deploy Prometheus + Grafana monitoring on OSAC CI runners.
#
# Modes:
#   --central   Deploy full stack (Prometheus, Grafana, Alertmanager,
#               org-runner-exporter, node_exporter).
#               Run on ONE designated central machine only.
#
#   --agent     Deploy exporters only (node_exporter).
#               Run on every other runner machine.
#
#   --add-tunnel <host> <base_port>
#               Set up a persistent SSH tunnel from the central machine to
#               a remote runner. Forwards remote 9100/9101 to local ports.
#
# Prerequisites:
#   - podman installed
#   - This script is run from the repo root (or REPO_ROOT is set)
#   - For --central: jq, curl installed
#   - For --add-tunnel: ssh, ssh-keygen installed
set -euo pipefail

MONITORING_HOME="${HOME}/.monitoring-server"
# Resolve to the monitoring/ directory: scripts live at monitoring/scripts/
MONITORING_REPO_DIR="${MONITORING_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

QUADLET_DIR="${HOME}/.config/containers/systemd"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

###############################################################################
phase() { echo -e "\n==> Phase $1: $2"; }
info()  { echo "    $1"; }
###############################################################################

usage() {
    echo "Usage: $(basename "$0") [--central | --agent | --add-tunnel <host> <base_port>]"
    echo ""
    echo "Modes:"
    echo "  --central              Full monitoring stack (central machine only)"
    echo "  --agent                Exporters only (every runner machine)"
    echo "  --add-tunnel <h> <p>   Add SSH tunnel to remote runner"
    exit 1
}

###############################################################################
# Parse arguments
###############################################################################
MODE=""
TUNNEL_HOST=""
TUNNEL_BASE_PORT=""

case "${1:-}" in
    --central)   MODE="central" ;;
    --agent)     MODE="agent" ;;
    --add-tunnel)
        MODE="tunnel"
        TUNNEL_HOST="${2:-}"
        TUNNEL_BASE_PORT="${3:-}"
        if [[ -z "${TUNNEL_HOST}" || -z "${TUNNEL_BASE_PORT}" ]]; then
            echo "ERROR: --add-tunnel requires <host> and <base_port>" >&2
            usage
        fi
        # Validate hostname (alphanumeric, dots, single hyphens)
        # Double hyphens are rejected because the tunnel service uses "--" as
        # the delimiter between host and port in the systemd instance name.
        if ! [[ "${TUNNEL_HOST}" =~ ^[a-zA-Z0-9._-]+$ ]] || [[ "${TUNNEL_HOST}" == *--* ]]; then
            echo "ERROR: Invalid hostname: ${TUNNEL_HOST}" >&2
            echo "  (must not contain '--' — used as instance-name delimiter)" >&2
            exit 1
        fi
        # Validate port (numeric, valid range)
        if ! [[ "${TUNNEL_BASE_PORT}" =~ ^[0-9]+$ ]] || \
           (( TUNNEL_BASE_PORT < 1024 || TUNNEL_BASE_PORT > 65535 )); then
            echo "ERROR: Invalid port: ${TUNNEL_BASE_PORT} (must be 1024-65535)" >&2
            exit 1
        fi
        ;;
    *)  usage ;;
esac

###############################################################################
# Phase 1: Create directory layout
###############################################################################
phase 1 "Creating directory layout under ${MONITORING_HOME}"

mkdir -p "${MONITORING_HOME}"/{config/grafana,data,dashboards}
chmod 700 "${MONITORING_HOME}"

if [[ "${MODE}" == "central" ]]; then
    mkdir -p "${MONITORING_HOME}/data/"{prometheus,grafana,alertmanager}
    mkdir -p "${MONITORING_HOME}/.ssh"
    chmod 700 "${MONITORING_HOME}/.ssh"

    # Data dirs are owned by the current user; containers run with
    # keep-id so the host uid maps into the container.
    # No special chown needed.
fi

###############################################################################
# Phase 2: Copy config files from repo
###############################################################################
phase 2 "Copying config files from repo"

if [[ "${MODE}" == "central" ]]; then
    cp "${MONITORING_REPO_DIR}/config/prometheus.yml"   "${MONITORING_HOME}/config/prometheus.yml"
    cp "${MONITORING_REPO_DIR}/config/alert-rules.yml"  "${MONITORING_HOME}/config/alert-rules.yml"
    cp "${MONITORING_REPO_DIR}/config/alertmanager.yml" "${MONITORING_HOME}/config/alertmanager.yml"
    cp "${MONITORING_REPO_DIR}/config/grafana/datasources.yml" "${MONITORING_HOME}/config/grafana/datasources.yml"
    cp "${MONITORING_REPO_DIR}/config/grafana/dashboards.yml"  "${MONITORING_HOME}/config/grafana/dashboards.yml"

    # Copy dashboards
    cp "${MONITORING_REPO_DIR}/config/grafana/dashboards/"*.json "${MONITORING_HOME}/dashboards/"

    info "Config files copied for central deployment."

    # Create .env file for org-runner-exporter if it doesn't exist
    if [[ ! -f "${MONITORING_HOME}/.env" ]]; then
        echo "# GitHub token for org-runner-exporter" > "${MONITORING_HOME}/.env"
        echo "# Provide a PAT with admin:org scope" >> "${MONITORING_HOME}/.env"
        echo "PRIVATE_GITHUB_TOKEN=REPLACE_ME" >> "${MONITORING_HOME}/.env"
        chmod 600 "${MONITORING_HOME}/.env"
        echo ""
        echo "  WARNING: Edit ${MONITORING_HOME}/.env with your GitHub token."
        echo "  The org-runner-exporter will not work without a valid token."
    fi
else
    info "Agent mode -- no config files to copy."
fi

###############################################################################
# Phase 3: Deploy Quadlet units
###############################################################################
phase 3 "Installing Quadlet units"

mkdir -p "${QUADLET_DIR}" "${SYSTEMD_USER_DIR}"

# Common units (deployed on all machines)
COMMON_UNITS=(
    "node-exporter.container"
)

# Central-only units
CENTRAL_UNITS=(
    "prometheus.container"
    "grafana.container"
    "alertmanager.container"
    "org-runner-exporter.container"
    "workflow-exporter.container"
)

for unit in "${COMMON_UNITS[@]}"; do
    cp "${MONITORING_REPO_DIR}/quadlet/${unit}" "${QUADLET_DIR}/${unit}"
    info "Installed ${unit}"
done

if [[ "${MODE}" == "central" ]]; then
    for unit in "${CENTRAL_UNITS[@]}"; do
        cp "${MONITORING_REPO_DIR}/quadlet/${unit}" "${QUADLET_DIR}/${unit}"
        info "Installed ${unit}"
    done

    # Install the SSH tunnel template unit
    cp "${MONITORING_REPO_DIR}/systemd/monitoring-tunnel@.service" \
       "${SYSTEMD_USER_DIR}/monitoring-tunnel@.service"
    info "Installed monitoring-tunnel@.service template"
fi

systemctl --user daemon-reload

###############################################################################
# Phase 4: Start services and wait for containers
###############################################################################
phase 4 "Starting services"

# Start common services
COMMON_SERVICES=(
    "node-exporter.service"
)

CENTRAL_SERVICES=(
    "alertmanager.service"
    "prometheus.service"
    "grafana.service"
    "org-runner-exporter.service"
    "workflow-exporter.service"
)

for svc in "${COMMON_SERVICES[@]}"; do
    echo "  Starting ${svc} ..."
    systemctl --user start "${svc}"
done

if [[ "${MODE}" == "central" ]]; then
    for svc in "${CENTRAL_SERVICES[@]}"; do
        echo "  Starting ${svc} ..."
        systemctl --user start "${svc}"
    done
fi

# Wait for containers to be running
echo ""
echo "Waiting for containers to be healthy ..."
ALL_CONTAINERS=("node-exporter")
if [[ "${MODE}" == "central" ]]; then
    ALL_CONTAINERS+=("prometheus" "grafana" "alertmanager" "org-runner-exporter" "workflow-exporter")
fi

containers_ready=true
for container in "${ALL_CONTAINERS[@]}"; do
    ready=false
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
        echo "  WARNING: ${container} not running after 30 seconds."
        containers_ready=false
    fi
done

if [[ "${containers_ready}" != "true" ]]; then
    echo ""
    echo "ERROR: Some containers did not start. Check with:"
    echo "  podman ps -a"
    echo "  podman logs <container-name>"
    exit 1
fi

###############################################################################
# Phase 5: (Central) Verify Prometheus targets
###############################################################################
if [[ "${MODE}" == "central" ]]; then
    phase 5 "Verifying Prometheus targets"

    # Give Prometheus a moment to perform its first scrape
    sleep 5

    if command -v curl &>/dev/null && command -v jq &>/dev/null; then
        targets_json=$(curl -sf http://127.0.0.1:9091/api/v1/targets 2>/dev/null || echo "")
        if [[ -n "${targets_json}" ]]; then
            up_count=$(echo "${targets_json}" | jq '[.data.activeTargets[] | select(.health == "up")] | length' 2>/dev/null || echo "0")
            total_count=$(echo "${targets_json}" | jq '.data.activeTargets | length' 2>/dev/null || echo "0")
            info "Prometheus targets: ${up_count}/${total_count} UP"

            if [[ "${up_count}" -lt "${total_count}" ]]; then
                echo ""
                echo "  Targets not yet UP (may need SSH tunnels or time to connect):"
                echo "${targets_json}" | jq -r '.data.activeTargets[] | select(.health != "up") | "    \(.labels.job) (\(.scrapeUrl)) - \(.health)"' 2>/dev/null || true
            fi
        else
            info "Could not query Prometheus API -- it may still be starting."
        fi
    else
        info "curl/jq not available -- skipping target verification."
    fi

    ###########################################################################
    # Phase 6: Test Alertmanager
    ###########################################################################
    phase 6 "Testing Alertmanager connectivity"

    if command -v curl &>/dev/null; then
        am_status=$(curl -sf http://127.0.0.1:9093/-/healthy 2>/dev/null || echo "")
        if [[ -n "${am_status}" ]]; then
            info "Alertmanager is healthy."
        else
            info "Alertmanager not responding -- it may still be starting."
        fi
    else
        info "curl not available -- skipping Alertmanager test."
    fi
fi

###############################################################################
# Phase 7: Enable linger and systemd services
###############################################################################
if [[ "${MODE}" == "central" ]]; then
    phase_num=7
else
    phase_num=5
fi
phase ${phase_num} "Enabling loginctl linger"

loginctl enable-linger "$(whoami)" 2>/dev/null || true

# Enable services so they survive reboots (Quadlet WantedBy handles this for
# container units, but explicit enable ensures consistent state).
for svc in "${COMMON_SERVICES[@]}"; do
    systemctl --user enable "${svc}" 2>/dev/null || true
done
if [[ "${MODE}" == "central" ]]; then
    for svc in "${CENTRAL_SERVICES[@]}"; do
        systemctl --user enable "${svc}" 2>/dev/null || true
    done
fi

###############################################################################
# Handle --add-tunnel mode
###############################################################################
if [[ "${MODE}" == "tunnel" ]]; then
    phase 1 "Setting up SSH tunnel to ${TUNNEL_HOST} (base port ${TUNNEL_BASE_PORT})"

    SSH_DIR="${MONITORING_HOME}/.ssh"
    mkdir -p "${SSH_DIR}"
    chmod 700 "${SSH_DIR}"

    # Generate SSH key if it doesn't exist
    KEY_FILE="${SSH_DIR}/monitoring_ed25519"
    if [[ ! -f "${KEY_FILE}" ]]; then
        ssh-keygen -t ed25519 -N "" -f "${KEY_FILE}" -C "monitoring@$(hostname)"
        info "SSH key generated: ${KEY_FILE}"
        echo ""
        echo "  Copy the public key to the remote runner:"
        echo "    ssh-copy-id -i ${KEY_FILE}.pub <user>@${TUNNEL_HOST}"
        echo ""
        echo "  Or manually append to ~/.ssh/authorized_keys on the remote host:"
        cat "${KEY_FILE}.pub"
        echo ""
    fi

    # Install tunnel template if not already present
    mkdir -p "${SYSTEMD_USER_DIR}"
    cp "${MONITORING_REPO_DIR}/systemd/monitoring-tunnel@.service" \
       "${SYSTEMD_USER_DIR}/monitoring-tunnel@.service"
    systemctl --user daemon-reload

    # Start the tunnel service
    INSTANCE="${TUNNEL_HOST}--${TUNNEL_BASE_PORT}"
    systemctl --user enable --now "monitoring-tunnel@${INSTANCE}.service"
    info "Tunnel started: monitoring-tunnel@${INSTANCE}.service"
    info "  Remote 9100 -> local ${TUNNEL_BASE_PORT} (node_exporter)"

    echo ""
    echo "Next steps:"
    echo "  1. Ensure the SSH key is authorized on ${TUNNEL_HOST}"
    echo "  2. Add a scrape target to prometheus.yml for port ${TUNNEL_BASE_PORT}"
    echo "  3. Reload Prometheus: curl -X POST http://127.0.0.1:9091/-/reload"
    exit 0
fi

###############################################################################
# Done
###############################################################################
echo ""
echo "============================================"
if [[ "${MODE}" == "central" ]]; then
    echo "  Monitoring setup complete (central)!"
else
    echo "  Monitoring setup complete (agent)!"
fi
echo "============================================"
echo ""

if [[ "${MODE}" == "central" ]]; then
    echo "Services running:"
    echo "  - Prometheus:          http://127.0.0.1:9091"
    echo "  - Grafana:             http://127.0.0.1:3000"
    echo "  - Alertmanager:        http://127.0.0.1:9093"
    echo "  - node_exporter:       http://127.0.0.1:9100"
    echo "  - org-runner-exporter: http://127.0.0.1:9102"
    echo ""
    echo "Access Grafana via SSH tunnel:"
    echo "  ssh -L 3000:127.0.0.1:3000 -L 9091:127.0.0.1:9091 <user>@<central-host>"
    echo "  Then open http://localhost:3000"
    echo ""
    echo "Next steps:"
    echo "  1. Set GITHUB_TOKEN in ${MONITORING_HOME}/.env"
    echo "  2. Set SLACK_WEBHOOK_URL in ${MONITORING_HOME}/config/alertmanager.yml"
    echo "  3. Run monitoring-setup.sh --agent on each runner machine"
    echo "  4. Run monitoring-setup.sh --add-tunnel <host> <base_port> for each runner"
    echo "  5. Run monitoring-health-check.sh to verify"
else
    echo "Services running:"
    echo "  - node_exporter:   http://127.0.0.1:9100"
    echo ""
    echo "Next steps:"
    echo "  1. On central machine, run: monitoring-setup.sh --add-tunnel $(hostname -f) <base_port>"
    echo "  2. Verify metrics at http://127.0.0.1:9100/metrics"
fi
echo ""

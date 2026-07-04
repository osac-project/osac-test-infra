#!/usr/bin/env bash
# monitoring-health-check.sh -- Health check for the OSAC monitoring stack.
#
# Detects whether this machine runs the central stack or agent-only
# and adjusts checks accordingly.
set -euo pipefail

passed=0
failed=0
CHECK_NUM=0

check() {
    local name="$1"
    shift
    CHECK_NUM=$(( CHECK_NUM + 1 ))
    if "$@" >/dev/null 2>&1; then
        echo "  [PASS] ${CHECK_NUM}. ${name}"
        passed=$(( passed + 1 ))
    else
        echo "  [FAIL] ${CHECK_NUM}. ${name}"
        failed=$(( failed + 1 ))
    fi
}

###############################################################################
# Detect mode: central has prometheus.service, agent does not
###############################################################################
IS_CENTRAL=false
if systemctl --user list-unit-files prometheus.service &>/dev/null; then
    if systemctl --user is-enabled prometheus.service &>/dev/null; then
        IS_CENTRAL=true
    fi
fi

if [[ "${IS_CENTRAL}" == "true" ]]; then
    echo "=== Monitoring Health Check (Central) ==="
else
    echo "=== Monitoring Health Check (Agent) ==="
fi
echo ""

###############################################################################
# Agent checks (run on all machines)
###############################################################################

check "node-exporter container is running" \
    bash -c 'test "$(podman inspect --format "{{.State.Running}}" node-exporter 2>/dev/null)" = "true"'

check "node_exporter metrics endpoint reachable" \
    curl -sf http://127.0.0.1:9100/metrics

check "node-exporter.service is active" \
    systemctl --user is-active node-exporter.service

###############################################################################
# Central checks (only on the central machine)
###############################################################################
if [[ "${IS_CENTRAL}" == "true" ]]; then
    check "prometheus container is running" \
        bash -c 'test "$(podman inspect --format "{{.State.Running}}" prometheus 2>/dev/null)" = "true"'

    check "Prometheus API is reachable" \
        curl -sf http://127.0.0.1:9091/-/healthy

    check "Prometheus has active scrape targets" \
        bash -c 'test "$(curl -sf http://127.0.0.1:9091/api/v1/targets | jq ".data.activeTargets | length")" -gt 0'

    check "grafana container is running" \
        bash -c 'test "$(podman inspect --format "{{.State.Running}}" grafana 2>/dev/null)" = "true"'

    check "Grafana API is reachable" \
        curl -sf http://127.0.0.1:3000/api/health

    check "Grafana Prometheus datasource is configured" \
        bash -c 'curl -sf http://127.0.0.1:3000/api/datasources | jq -e ".[] | select(.name == \"Prometheus\" and .type == \"prometheus\")"'

    check "alertmanager container is running" \
        bash -c 'test "$(podman inspect --format "{{.State.Running}}" alertmanager 2>/dev/null)" = "true"'

    check "Alertmanager API is reachable" \
        curl -sf http://127.0.0.1:9093/-/healthy

    check "org-runner-exporter container is running" \
        bash -c 'test "$(podman inspect --format "{{.State.Running}}" org-runner-exporter 2>/dev/null)" = "true"'

    check "workflow-exporter container is running" \
        bash -c 'test "$(podman inspect --format "{{.State.Running}}" workflow-exporter 2>/dev/null)" = "true"'

    # Check SSH tunnel services (if any are configured)
    tunnel_count=$(systemctl --user list-units --type=service --state=running 'monitoring-tunnel@*' 2>/dev/null | grep -c 'monitoring-tunnel@' || true)
    if [[ "${tunnel_count}" -gt 0 ]]; then
        check "SSH tunnel services running (${tunnel_count})" \
            test "${tunnel_count}" -gt 0
    fi
fi

###############################################################################
# Summary
###############################################################################
echo ""
echo "=== Results: ${passed} passed, ${failed} failed ==="

if (( failed > 0 )); then
    echo ""
    echo "Troubleshooting:"
    echo "  podman ps -a                      # Check container status"
    echo "  podman logs <container-name>       # Check container logs"
    echo "  systemctl --user status <service>  # Check systemd service"
    exit 1
fi

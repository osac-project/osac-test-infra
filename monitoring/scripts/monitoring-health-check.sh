#!/usr/bin/env bash
# monitoring-health-check.sh -- Health check for the OSAC monitoring stack.
#
# Detects whether this machine runs the central stack or agent-only
# and adjusts checks accordingly.
set -euo pipefail

passed=0
failed=0

check() {
    local num="$1" name="$2"
    shift 2
    if "$@" >/dev/null 2>&1; then
        echo "  [PASS] ${num}. ${name}"
        passed=$(( passed + 1 ))
    else
        echo "  [FAIL] ${num}. ${name}"
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
CHECK_NUM=1

# 1. node-exporter container running
check ${CHECK_NUM} "node-exporter container is running" \
    podman inspect --format '{{.State.Running}}' node-exporter
CHECK_NUM=$(( CHECK_NUM + 1 ))

# 2. node-exporter metrics reachable
check ${CHECK_NUM} "node_exporter metrics endpoint reachable" \
    curl -sf http://127.0.0.1:9100/metrics
CHECK_NUM=$(( CHECK_NUM + 1 ))

# 3. node-exporter systemd service active
check ${CHECK_NUM} "node-exporter.service is active" \
    systemctl --user is-active node-exporter.service
CHECK_NUM=$(( CHECK_NUM + 1 ))

###############################################################################
# Central checks (only on the central machine)
###############################################################################
if [[ "${IS_CENTRAL}" == "true" ]]; then
    # 4. Prometheus container running
    check ${CHECK_NUM} "prometheus container is running" \
        podman inspect --format '{{.State.Running}}' prometheus
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 5. Prometheus API reachable (port 9091; cockpit uses 9090)
    check ${CHECK_NUM} "Prometheus API is reachable" \
        curl -sf http://127.0.0.1:9091/-/healthy
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 6. Prometheus has active targets
    check ${CHECK_NUM} "Prometheus has active scrape targets" \
        bash -c 'test "$(curl -sf http://127.0.0.1:9091/api/v1/targets | jq ".data.activeTargets | length")" -gt 0'
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 7. Grafana container running
    check ${CHECK_NUM} "grafana container is running" \
        podman inspect --format '{{.State.Running}}' grafana
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 8. Grafana API reachable
    check ${CHECK_NUM} "Grafana API is reachable" \
        curl -sf http://127.0.0.1:3000/api/health
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 9. Grafana datasource configured
    check ${CHECK_NUM} "Grafana Prometheus datasource is configured" \
        bash -c 'curl -sf http://127.0.0.1:3000/api/datasources | jq -e ".[0].type" | grep -q prometheus'
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 10. Alertmanager container running
    check ${CHECK_NUM} "alertmanager container is running" \
        podman inspect --format '{{.State.Running}}' alertmanager
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 11. Alertmanager API reachable
    check ${CHECK_NUM} "Alertmanager API is reachable" \
        curl -sf http://127.0.0.1:9093/-/healthy
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 12. org-runner-exporter container running
    check ${CHECK_NUM} "org-runner-exporter container is running" \
        podman inspect --format '{{.State.Running}}' org-runner-exporter
    CHECK_NUM=$(( CHECK_NUM + 1 ))

    # 13. Check SSH tunnel services (if any are configured)
    tunnel_count=$(systemctl --user list-units --type=service --state=running 'monitoring-tunnel@*' 2>/dev/null | grep -c 'monitoring-tunnel@' || true)
    if [[ "${tunnel_count}" -gt 0 ]]; then
        check ${CHECK_NUM} "SSH tunnel services running (${tunnel_count})" \
            test "${tunnel_count}" -gt 0
        CHECK_NUM=$(( CHECK_NUM + 1 ))
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

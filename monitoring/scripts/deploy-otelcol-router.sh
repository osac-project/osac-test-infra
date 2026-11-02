#!/usr/bin/env bash
# deploy-otelcol-router.sh -- Install/refresh the ci-observe OTLP signal
# router on the CENTRAL monitoring machine (osac-ci-1).
#
# Unlike everything else in monitoring/scripts/, this one must run as ROOT:
# otelcol-router is a host systemd service (/etc/systemd/system/), not a
# user quadlet, because the routing path must stay on plain loopback TCP --
# rootless podman (pasta) cross-container networking silently ate OTLP in
# the earlier container-based router attempts. Like the HAProxy SNI route
# (see monitoring/README.md), it is system config that the GitHub Actions
# runner user cannot deploy; unlike HAProxy, it is scripted here so the
# binary, config and unit stay in sync with this repository.
#
# What it deploys (all three owned by this repo):
#   /usr/local/bin/otelcol-contrib          -- extracted from the pinned
#       otelcol-contrib 0.164.0 image below (same digest as ci-observe.py's
#       COLLECTOR_IMAGE), statically linked
#   /etc/otelcol-router/config.yaml         -- monitoring/config/ci-observe/otelcol-router/config.yaml
#   /etc/systemd/system/otelcol-router.service
#                                           -- monitoring/config/ci-observe/otelcol-router/otelcol-router.service
#
# Run from anywhere (resolves its own repo root):
#   sudo monitoring/scripts/deploy-otelcol-router.sh
#
# Prerequisites: podman, useradd/groupadd, systemd. Idempotent: re-running
# refreshes the binary, config and unit, then restarts the service.
set -euo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "ERROR: run as root (the router is a system service)." >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROUTER_DIR="${REPO_DIR}/config/ci-observe/otelcol-router"

# Pinned to the same digest as COLLECTOR_IMAGE in monitoring/scripts/ci-observe.py.
# The binary sits at /otelcol-contrib inside the image.
COLLECTOR_IMAGE="docker.io/otel/opentelemetry-collector-contrib:0.164.0@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b"

for f in "${ROUTER_DIR}/config.yaml" "${ROUTER_DIR}/otelcol-router.service"; do
    [[ -f "${f}" ]] || { echo "ERROR: ${f} is missing from this checkout." >&2; exit 1; }
done

echo "==> Extracting otelcol-contrib from the pinned image"
cid="$(podman create "${COLLECTOR_IMAGE}")"
trap 'podman rm -f "${cid}" >/dev/null 2>&1 || true' EXIT
podman cp "${cid}":/otelcol-contrib /usr/local/bin/otelcol-contrib.new
chmod 0755 /usr/local/bin/otelcol-contrib.new
# Replace only after a full copy landed (never leave a half-written binary
# in place -- the service restart below would exec it).
mv -f /usr/local/bin/otelcol-contrib.new /usr/local/bin/otelcol-contrib
/usr/local/bin/otelcol-contrib --version

echo "==> Creating the service user"
getent group otelcol-router >/dev/null || groupadd --system otelcol-router
getent passwd otelcol-router >/dev/null || \
    useradd --system --gid otelcol-router --no-create-home --shell /usr/sbin/nologin otelcol-router

echo "==> Installing config + unit"
install -d -m 0755 /etc/otelcol-router
install -m 0644 "${ROUTER_DIR}/config.yaml" /etc/otelcol-router/config.yaml
install -m 0644 "${ROUTER_DIR}/otelcol-router.service" /etc/systemd/system/otelcol-router.service

systemctl daemon-reload
systemctl enable otelcol-router.service >/dev/null
systemctl restart otelcol-router.service

echo "==> Waiting for the router to come up"
for _ in $(seq 1 30); do
    if curl -sf http://127.0.0.1:14333/healthcheck >/dev/null 2>&1; then
        echo "otelcol-router: healthy (ready check 127.0.0.1:14333)"
        exit 0
    fi
    sleep 1
done
echo "ERROR: otelcol-router did not pass its health check within 30s." >&2
echo "Check: journalctl -u otelcol-router -f" >&2
exit 1

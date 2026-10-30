#!/usr/bin/env bash
# Host-side Redfish infrastructure for virtual BareMetalHosts: the
# libvirt storage pool, the sushy-tools BMC emulator and the nginx
# virtual-media cache.
#
# Virtual-media path:
#   Ironic -> InsertMedia -> sushy -> nginx cache -> Metal3 HTTP server
# Ironic allows 60s for InsertMedia; the ISO download can take longer.
# nginx keeps filling its cache after a client disconnect, so an Ironic
# retry is served locally.
#
# # Required env:
#   SUSHY_LISTEN_IP   — IP to bind sushy-emulator and the nginx cache to
#                       (usually the libvirt network gateway)
#   STORAGE_POOL_NAME — libvirt storage pool exposing the VM disks to Redfish
#   STORAGE_DIR       — directory backing the storage pool (created if missing)
#   SUSHY_CONFIG_DIR  — directory for sushy/nginx config, logs and the pid file
#   SUSHY_CACHE_NAME  — podman container and volume name for the nginx cache
#   SUSHY_VMEDIA_TMP  — TMPDIR given to sushy-emulator (virtual-media cache)
#
# Optional env:
#   SUSHY_PORT       — sushy-tools listen port (default: 8000)
#   SUSHY_CACHE_PORT — nginx cache listen port (default: 8080)
set -euo pipefail

: "${SUSHY_LISTEN_IP:?SUSHY_LISTEN_IP is required}"
: "${STORAGE_POOL_NAME:?STORAGE_POOL_NAME is required}"
: "${STORAGE_DIR:?STORAGE_DIR is required}"
: "${SUSHY_CONFIG_DIR:?SUSHY_CONFIG_DIR is required}"
: "${SUSHY_CACHE_NAME:?SUSHY_CACHE_NAME is required}"
: "${SUSHY_VMEDIA_TMP:?SUSHY_VMEDIA_TMP is required}"

SUSHY_PORT="${SUSHY_PORT:-8000}"
SUSHY_CACHE_PORT="${SUSHY_CACHE_PORT:-8080}"

SUSHY_PID_FILE="${SUSHY_CONFIG_DIR}/sushy.pid"
SUSHY_CACHE_CONTAINER="${SUSHY_CACHE_NAME}"
SUSHY_CACHE_VOLUME="${SUSHY_CACHE_NAME}"
SUSHY_CACHE_IMAGE="docker.io/library/nginx:1.29-alpine"
SUSHY_CACHE_IMAGE+="@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de"
VIRSH="virsh -c qemu:///system"

# --- Step 1: Create the libvirt storage pool ---
echo "==> Ensuring libvirt storage pool '${STORAGE_POOL_NAME}'..."
mkdir -p "${STORAGE_DIR}"
# 755: libvirt chowns the disk files to the qemu user at domain start.
chmod 755 "${STORAGE_DIR}"
# Idempotent: only define/start a missing pool.
if ! ${VIRSH} pool-list --all --name | grep -qx "${STORAGE_POOL_NAME}"; then
  ${VIRSH} pool-define-as "${STORAGE_POOL_NAME}" dir --target "${STORAGE_DIR}"
fi
if ! ${VIRSH} pool-list --name | grep -qx "${STORAGE_POOL_NAME}"; then
  ${VIRSH} pool-start "${STORAGE_POOL_NAME}"
fi

# --- Step 2: Install and start the virtual-media services ---
echo "==> Installing sushy-tools..."
pip install --quiet sushy-tools libvirt-python 2>&1

mkdir -p "${SUSHY_CONFIG_DIR}"
cat > "${SUSHY_CONFIG_DIR}/sushy-emulator.conf" <<SEOF
SUSHY_EMULATOR_LISTEN_IP = "${SUSHY_LISTEN_IP}"
SUSHY_EMULATOR_LISTEN_PORT = ${SUSHY_PORT}
SUSHY_EMULATOR_SSL_CERT = None
SUSHY_EMULATOR_SSL_KEY = None
SUSHY_EMULATOR_LIBVIRT_URI = "qemu:///system"
SUSHY_EMULATOR_IGNORE_BOOT_DEVICE = False
SUSHY_EMULATOR_STORAGE_POOL = "${STORAGE_POOL_NAME}"
SUSHY_EMULATOR_BOOT_LOADER_MAP = {
    "UEFI": {
        "x86_64": "/usr/share/OVMF/OVMF_CODE.secboot.fd"
    },
    "Legacy": {
        "x86_64": None
    }
}
SEOF

# sushy-tools writes its virtual-media tempfiles through TMPDIR. Point
# it at a per-environment directory so teardown can remove them by name.
mkdir -p "${SUSHY_VMEDIA_TMP}"
export TMPDIR="${SUSHY_VMEDIA_TMP}"

echo "==> Starting nginx virtual-media cache on ${SUSHY_LISTEN_IP}:${SUSHY_CACHE_PORT}..."
cat > "${SUSHY_CONFIG_DIR}/nginx.conf" <<NGINX_EOF
user root;
worker_processes 1;
pid /tmp/nginx.pid;

events {}

http {
    proxy_cache_path /var/cache/nginx/iso levels=1:2 keys_zone=iso_cache:10m
                     inactive=2h max_size=8g use_temp_path=off;

    log_format iso_cache '\$remote_addr "\$request" \$status bytes=\$body_bytes_sent '
                         'cache=\$upstream_cache_status upstream=\$upstream_response_time '
                         'request=\$request_time';
    access_log /dev/stdout iso_cache;
    error_log /dev/stderr info;

    server {
        listen ${SUSHY_LISTEN_IP}:${SUSHY_CACHE_PORT};

        allow ${SUSHY_LISTEN_IP};
        deny all;

        location = /healthz {
            access_log off;
            return 200 "ok\n";
        }

        location / {
            proxy_pass http://\$http_host\$request_uri;
            proxy_set_header Host \$http_host;
            proxy_set_header Connection "";
            proxy_http_version 1.1;

            proxy_cache iso_cache;
            proxy_cache_key "\$http_host\$request_uri";
            proxy_cache_valid 200 2h;
            proxy_cache_lock on;
            proxy_cache_lock_age 30m;
            proxy_cache_lock_timeout 30m;
            proxy_ignore_client_abort on;
            proxy_ignore_headers Cache-Control Expires Set-Cookie Vary;
            proxy_connect_timeout 30s;
            proxy_read_timeout 30m;
            proxy_send_timeout 30m;

            add_header X-Sushy-Cache \$upstream_cache_status always;
        }
    }
}
NGINX_EOF

# Idempotent: drop stale cache resources before recreating them.
podman rm --force "${SUSHY_CACHE_CONTAINER}" >/dev/null 2>&1 || true
podman volume rm --force "${SUSHY_CACHE_VOLUME}" >/dev/null 2>&1 || true
podman volume create "${SUSHY_CACHE_VOLUME}" >/dev/null
podman run --detach \
  --name "${SUSHY_CACHE_CONTAINER}" \
  --network host \
  --read-only \
  --security-opt no-new-privileges \
  --cap-drop all \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --volume "${SUSHY_CONFIG_DIR}/nginx.conf:/etc/nginx/nginx.conf:ro,Z" \
  --volume "${SUSHY_CACHE_VOLUME}:/var/cache/nginx:Z" \
  --entrypoint nginx \
  "${SUSHY_CACHE_IMAGE}" -g 'daemon off;' >/dev/null

SUSHY_CACHE_READY=false
for _ in $(seq 1 15); do
  if ! podman container exists "${SUSHY_CACHE_CONTAINER}" \
    || [[ "$(podman inspect --format '{{.State.Running}}' "${SUSHY_CACHE_CONTAINER}")" != "true" ]]; then
    echo "ERROR: nginx virtual-media cache stopped during startup." >&2
    podman logs "${SUSHY_CACHE_CONTAINER}" >&2 || true
    exit 1
  fi
  if curl -sf --connect-timeout 3 --max-time 5 \
    "http://${SUSHY_LISTEN_IP}:${SUSHY_CACHE_PORT}/healthz" >/dev/null; then
    SUSHY_CACHE_READY=true
    break
  fi
  sleep 2
done
if [[ "${SUSHY_CACHE_READY}" != "true" ]]; then
  echo "ERROR: nginx virtual-media cache did not become ready." >&2
  podman logs "${SUSHY_CACHE_CONTAINER}" >&2 || true
  exit 1
fi
echo "nginx virtual-media cache is ready."

echo "Starting sushy-emulator on ${SUSHY_LISTEN_IP}:${SUSHY_PORT}..."
# Stop a leftover emulator so the replacement can bind. The cmdline
# check guards against PID reuse.
if [[ -f "${SUSHY_PID_FILE}" ]]; then
  OLD_SUSHY_PID=$(cat "${SUSHY_PID_FILE}")
  if kill -0 "${OLD_SUSHY_PID}" 2>/dev/null \
    && grep -qa -- "${SUSHY_CONFIG_DIR}/sushy-emulator.conf" "/proc/${OLD_SUSHY_PID}/cmdline" 2>/dev/null; then
    echo "Stopping sushy-emulator left over from a previous run (PID ${OLD_SUSHY_PID})..."
    kill "${OLD_SUSHY_PID}" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "${OLD_SUSHY_PID}" 2>/dev/null || break
      sleep 1
    done
    kill -9 "${OLD_SUSHY_PID}" 2>/dev/null || true
  fi
  rm -f "${SUSHY_PID_FILE}"
fi
SUSHY_MAX_ATTEMPTS=3
for sushy_attempt in $(seq 1 "${SUSHY_MAX_ATTEMPTS}"); do
  nohup env \
    http_proxy="http://${SUSHY_LISTEN_IP}:${SUSHY_CACHE_PORT}" \
    HTTP_PROXY="http://${SUSHY_LISTEN_IP}:${SUSHY_CACHE_PORT}" \
    no_proxy="127.0.0.1,localhost" \
    NO_PROXY="127.0.0.1,localhost" \
    sushy-emulator --config "${SUSHY_CONFIG_DIR}/sushy-emulator.conf" \
    > "${SUSHY_CONFIG_DIR}/sushy.log" 2>&1 &
  echo $! > "${SUSHY_PID_FILE}"

  echo "  Waiting for sushy-emulator HTTP endpoint (attempt ${sushy_attempt}/${SUSHY_MAX_ATTEMPTS})..."
  SUSHY_READY=false
  for _ in $(seq 1 15); do
    if ! kill -0 "$(cat "${SUSHY_PID_FILE}")" 2>/dev/null; then
      echo "  Process died. Log:"
      cat "${SUSHY_CONFIG_DIR}/sushy.log"
      break
    fi
    if curl -sf --connect-timeout 3 --max-time 5 "http://${SUSHY_LISTEN_IP}:${SUSHY_PORT}/redfish/v1/"; then
      SUSHY_READY=true
      break
    fi
    sleep 2
  done

  if [[ "${SUSHY_READY}" == "true" ]]; then
    break
  fi

  SUSHY_PID=$(cat "${SUSHY_PID_FILE}")
  echo "  sushy-emulator not responding, killing PID ${SUSHY_PID}..."
  kill "${SUSHY_PID}" || true
  for _ in $(seq 1 10); do
    kill -0 "${SUSHY_PID}" 2>/dev/null || break
    sleep 1
  done
  kill -9 "${SUSHY_PID}" || true

  if [[ "${sushy_attempt}" -eq "${SUSHY_MAX_ATTEMPTS}" ]]; then
    echo "ERROR: sushy-emulator failed after ${SUSHY_MAX_ATTEMPTS} attempts. Log:"
    cat "${SUSHY_CONFIG_DIR}/sushy.log"
    exit 1
  fi
done
echo "sushy-emulator running (PID $(cat "${SUSHY_PID_FILE}"))."
echo "==> Redfish infrastructure ready on ${SUSHY_LISTEN_IP} (emulator :${SUSHY_PORT}, cache :${SUSHY_CACHE_PORT})."

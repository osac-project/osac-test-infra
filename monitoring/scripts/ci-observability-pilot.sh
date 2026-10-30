#!/usr/bin/env bash
set -euo pipefail
umask 077

if (( $# < 2 || $# > 3 )); then
    echo "Usage: ci-observability-pilot.sh OWNER/REPOSITORY RUN_ID [ATTEMPT]" >&2
    exit 2
fi

repository=$1
run_id=$2
port=${CI_PILOT_PORT:-42148}
python=${CI_PILOT_PYTHON:-python3}
if [[ ! "$port" =~ ^[0-9]{4,5}$ ]]; then
    echo "CI_PILOT_PORT must be between 1024 and 65535" >&2
    exit 2
fi
port=$((10#$port))
if (( port < 1024 || port > 65535 )); then
    echo "CI_PILOT_PORT must be between 1024 and 65535" >&2
    exit 2
fi
for program in "$python" gh podman curl; do
    command -v "$program" >/dev/null || { echo "Missing required program" >&2; exit 1; }
done
"$python" -c 'import socket, sys
with socket.socket() as listener:
    try:
        listener.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        raise SystemExit("Pilot port is unavailable; choose another CI_PILOT_PORT")' "$port"

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
export PYTHONPATH="$repository_root${PYTHONPATH:+:$PYTHONPATH}"
runtime=$(mktemp -d "${CI_PILOT_TMP_DIR:-${TMPDIR:-/tmp}}/ci-observability-pilot-runtime.XXXXXXXX")
snapshot_arguments=(--repository "$repository" --run-id "$run_id" --output "$runtime/snapshot.json")
if (( $# == 3 )); then
    snapshot_arguments+=(--attempt "$3")
fi
"$python" -m monitoring.ci_observability.pilot "${snapshot_arguments[@]}"
mkdir -m 700 "$runtime/data" "$runtime/dashboards" "$runtime/provisioning"
mkdir -m 700 "$runtime/provisioning/datasources" "$runtime/provisioning/dashboards"
"$python" -m monitoring.ci_observability.pilot_dashboard \
    --snapshot "$runtime/snapshot.json" --output "$runtime/dashboards/pilot.json"

cat > "$runtime/provisioning/datasources/pilot.yaml" <<'YAML'
apiVersion: 1
datasources:
  - name: CI pilot snapshot
    uid: ci-pilot-inline
    type: yesoreyeram-infinity-datasource
    access: proxy
    editable: false
    jsonData:
      allowedHosts:
        - https://ci-pilot-inline.invalid
      allowDangerousHTTPMethods: false
YAML
cat > "$runtime/provisioning/dashboards/pilot.yaml" <<'YAML'
apiVersion: 1
providers:
  - name: ci-pilot
    orgId: 1
    folder: CI pilot
    type: file
    disableDeletion: true
    allowUiUpdates: false
    updateIntervalSeconds: 5
    options:
      path: /var/lib/grafana/pilot-dashboards
YAML

"$python" -c 'import secrets; print("GF_SECURITY_ADMIN_PASSWORD=" + secrets.token_urlsafe(32))' \
    > "$runtime/grafana.env"
cat >> "$runtime/grafana.env" <<EOF
GF_SERVER_HTTP_ADDR=127.0.0.1
GF_SERVER_HTTP_PORT=$port
GF_SERVER_ROOT_URL=http://127.0.0.1:$port/
GF_AUTH_ANONYMOUS_ENABLED=true
GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
GF_AUTH_DISABLE_LOGIN_FORM=true
GF_AUTH_BASIC_ENABLED=false
GF_USERS_ALLOW_SIGN_UP=false
GF_ANALYTICS_REPORTING_ENABLED=false
GF_ANALYTICS_CHECK_FOR_UPDATES=false
GF_INSTALL_PLUGINS=yesoreyeram-infinity-datasource 3.11.1
EOF

container="ci-observability-pilot-$(basename "$runtime")"
podman run --detach --name "$container" --network host \
    --userns keep-id --user "$(id -u):$(id -g)" \
    --memory 768m --memory-swap 768m --cpus 1.5 --pids-limit 256 \
    --cap-drop ALL --security-opt no-new-privileges \
    --env-file "$runtime/grafana.env" \
    --volume "$runtime/data:/var/lib/grafana:Z" \
    --volume "$runtime/provisioning:/etc/grafana/provisioning:ro,Z" \
    --volume "$runtime/dashboards:/var/lib/grafana/pilot-dashboards:ro,Z" \
    docker.io/grafana/grafana-oss:12.0.1 >/dev/null

url="http://127.0.0.1:$port"
for (( check=0; check<90; check++ )); do
    if curl --fail --silent --max-time 2 "$url/api/health" >/dev/null; then
        printf 'Dashboard: %s/d/ci-run-pilot\nRuntime: %s\nContainer: %s\n' "$url" "$runtime" "$container"
        printf 'Stop this pilot: podman stop %s\n' "$container"
        exit 0
    fi
    sleep 1
done
printf 'Pilot startup failed. Inspect only this container: podman logs %s\n' "$container" >&2
exit 1

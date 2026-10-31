#!/usr/bin/env bash
# Tests for infra/netris/scripts/fabric-snapshot.sh with stubbed kubectl and curl.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT="${SCRIPT_DIR}/../../infra/netris/scripts/fabric-snapshot.sh"
PASSWORD="Zq9-not-a-real-password-7731"

WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK}"' EXIT
mkdir -p "${WORK}/bin" "${WORK}/tmp"
: > "${WORK}/k3s.yaml"

cat > "${WORK}/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
echo "kubectl $*" >> "${STUB_LOG}"
case "$*" in
  *"get svc traefik"*) printf '%s' "${STUB_NODEPORT-30080}" ;;
  *"get secret"*) [[ -n "${STUB_NO_SECRET:-}" ]] || printf '%s' "${STUB_PASSWORD}" | base64 ;;  # Secret data is base64
  *"get configmap"*) printf '%s' "${STUB_USER-robot}" ;;  # ConfigMap data is plain text
esac
STUB
cat > "${WORK}/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "${STUB_LOG}"
jar="" ; url="" ; body=""
while (($#)); do
  case "$1" in
    -c) jar="$2"; shift ;;
    --data) body="$(cat)"; shift ;;
    http://*) url="$1" ;;
  esac
  shift
done
if [[ -n "${body}" ]]; then
  [[ -z "${STUB_HANG:-}" ]] || sleep "${STUB_HANG}"
  echo "login-body ${body}" >> "${STUB_LOG}"
  [[ -z "${STUB_LOGIN_FAILS:-}" ]] || exit 22
  stat -c 'jar-mode %a' "${jar}" >> "${STUB_LOG}"
  echo "connect.sid=abc" > "${jar}"
  exit 0
fi
case "${url}" in
  */api/v2/vpc) echo '[{"name":"vpc1"}]' ;;
  */api/v2/vnet) [[ -z "${STUB_VNET_FAILS:-}" ]] || exit 22; echo '[{"name":"vnet1"}]' ;;
  */api/v2/ipam/subnets) echo '[]' ;;
  */api/v2/nat) echo '[{"name":"nat1"}]' ;;
esac
STUB
chmod +x "${WORK}/bin/kubectl" "${WORK}/bin/curl"

pass=0
fail=0
check() {
  if "${@:2}"; then echo "PASS: $1"; pass=$((pass + 1)); else echo "FAIL: $1"; fail=$((fail + 1)); fi
}

run() {
  : > "${WORK}/log"
  STUB_LOG="${WORK}/log" STUB_PASSWORD="${PASSWORD}" PATH="${WORK}/bin:${PATH}" TMPDIR="${WORK}/tmp" \
    NETRIS_K3S_KUBECONFIG="${WORK}/k3s.yaml" "$@" bash "${SNAPSHOT}" > "${WORK}/out" 2> "${WORK}/err"
}

run env
check "exit 0 and one JSON object with the four sections" \
  jq -e '.vpc[0].name == "vpc1" and .vnet[0].name == "vnet1" and .subnets == [] and .nat[0].name == "nat1"' "${WORK}/out" >/dev/null
check "the password is in neither stdout, stderr nor any argv" bash -c \
  '! grep -q -- "$1" "$2/out" "$2/err" && ! grep "^kubectl\|^curl" "$2/log" | grep -q -- "$1"' _ "${PASSWORD}" "${WORK}"
check "the ConfigMap username reaches the login body undecoded" grep -q '^login-body .*"user": "robot"' "${WORK}/log"
check "the login body reached curl on stdin" grep -q "^login-body .*${PASSWORD}" "${WORK}/log"
check "the cookie jar was private" grep -q '^jar-mode 600$' "${WORK}/log"
check "only the login sends data; the four reads are plain GETs" bash -c \
  '[ "$(grep -c "^curl.*--data" "$1/log")" = 1 ] && ! grep "^curl" "$1/log" | grep -q -- "-X\|--request\|-d "' _ "${WORK}"
check "the temporary directory is removed" bash -c '[ -z "$(ls -A "$1/tmp")" ]' _ "${WORK}"

run env STUB_VNET_FAILS=1
check "a failed endpoint is null and the rest is kept" \
  jq -e '.vnet == null and .vpc != null and .nat != null' "${WORK}/out" >/dev/null

if run env STUB_NO_SECRET=1; then status=0; else status=$?; fi
check "no credentials: exit 1, error on stderr, nothing on stdout" \
  bash -c '[ "$1" = 1 ] && grep -q NETRIS_PASSWORD "$2/err" && [ ! -s "$2/out" ]' _ "${status}" "${WORK}"

if run env STUB_LOGIN_FAILS=1; then status=0; else status=$?; fi
check "failed login: exit 1 and no password in the error" \
  bash -c '[ "$1" = 1 ] && grep -q "login" "$3/err" && ! grep -q -- "$2" "$3/err"' _ "${status}" "${PASSWORD}" "${WORK}"

run env STUB_USER=
check "an empty ConfigMap username falls back to netris" grep -q '^login-body .*"user": "netris"' "${WORK}/log"

if run env STUB_NODEPORT=; then status=0; else status=$?; fi
check "no traefik nodePort: exit 1" test "${status}" = 1

if run env NETRIS_K3S_KUBECONFIG="${WORK}/missing.yaml"; then status=0; else status=$?; fi
check "unreadable k3s kubeconfig: exit 1" test "${status}" = 1

start=$SECONDS
if run env STUB_HANG=4 FABRIC_SNAPSHOT_DEADLINE=1; then status=0; else status=$?; fi
check "a slow controller ends the script at the deadline, not at the caller's kill" \
  bash -c '[ "$1" != 0 ] && [ "$2" -lt 8 ] && [ -z "$(ls -A "$3/tmp")" ]' _ "${status}" "$((SECONDS - start))" "${WORK}"
check "every call is capped by the remaining budget (curl --max-time at most 8)" \
  bash -c '! grep "^curl" "$1/log" | grep -qE "max-time ([9]|[1-9][0-9])"' _ "${WORK}"

echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]

#!/usr/bin/env bash
set -euo pipefail
umask 077
candidate="${RUNNER_TEMP}/osac-hypervisor-sosreport"
private="${RUNNER_TEMP}/osac-private-upload"
binding="${PRIVATE_REPO:-}:${GITHUB_RUN_ID:-}:${GITHUB_RUN_ATTEMPT:-}:${GITHUB_JOB:-}"
case "${1:-}" in
  collect)
    rm -rf -- "$candidate" "$private"
    [[ ${PRIVATE_REPO:-} == osac-project/osac-ci-artifacts ]] || exit 0
    mkdir -m 700 "$candidate"
    handoff=""
    trap '[[ -z "$handoff" ]] || rm -rf -- "$handoff"; rm -rf -- "$candidate"' EXIT
    trap 'exit 1' INT TERM HUP
    handoff=$(sudo -n /usr/local/sbin/osac-hypervisor-sosreport 2>/dev/null)
    if [[ ! $handoff =~ ^/var/tmp/osac-sosreport\.[A-Za-z0-9]+$ ]]; then
      handoff=""
      exit 1
    fi
    [[ -O $handoff && ! -L $handoff ]]
    mv -- "$handoff/report" "$candidate/report"
    printf '%s\n' "$binding" >"$candidate/binding"
    rm -rf -- "$handoff"
    trap - EXIT
    ;;
  prepare)
    rm -rf -- "$private"
    trap 'rm -rf -- "$candidate"; rmdir -- "$private" 2>/dev/null || true' EXIT
    trap 'rm -rf -- "$private"; exit 1' INT TERM HUP
    [[ -n ${PRIVATE_REPO:-} ]] || exit 0
    if ! { mkdir -m 700 "$private" &&
      { [[ ! -d ${RUNNER_TEMP}/osac-artifacts ]] || cp -a "${RUNNER_TEMP}/osac-artifacts/." "$private/"; } &&
      chmod 700 "$private"; }; then
      rm -rf -- "$private"
      exit 78
    fi
    [[ ${PRIVATE_REPO:-} == osac-project/osac-ci-artifacts && -n ${GH_DISPATCH_TOKEN:-} ]] || exit 0
    [[ -f $candidate/binding && ! -L $candidate/binding && $(stat -c %s "$candidate/binding") -le 512 ]]
    [[ $(cat "$candidate/binding") == "$binding" && -d $candidate/report && ! -L $candidate/report ]]
    [[ $(curl --silent --fail --max-time 20 --proto '=https' --write-out '%{http_code}' \
      -H "Authorization: Bearer ${GH_DISPATCH_TOKEN}" \
      "https://api.github.com/repos/${PRIVATE_REPO}" -o "$candidate/repository.json") == 200 ]]
    jq -e --arg repo "$PRIVATE_REPO" '.private == true and .full_name == $repo' "$candidate/repository.json" >/dev/null
    [[ -z $(find "$candidate/report" ! -type d ! -type f -print -quit) ]]
    [[ -z $(find "$candidate/report" \( -name sos_logs -o -name '*private_map*' -o -name mapping \) -print -quit) ]]
    [[ $(du -sm "$candidate/report" | cut -f1) -le 256 ]]
    timeout 30 python3 -I - "${GITHUB_WORKSPACE}/.github/scripts" "$candidate/report" <<'PY'
import pathlib
import sys
import tarfile
import zipfile
sys.path.insert(0, sys.argv[1])
from release_gate import _is_archive
for path in pathlib.Path(sys.argv[2]).rglob('*'):
    if path.is_file():
        with path.open('rb') as source:
            header = source.read(8192)
        if b'\0' in header or _is_archive(header) or tarfile.is_tarfile(path) or zipfile.is_zipfile(path):
            sys.exit(1)
PY
    args=()
    while IFS= read -r filename; do
      [[ -z $filename ]] || args+=(--secret-file "$filename")
    done <<<"${KNOWN_SECRET_FILES:-${RUNNER_TEMP}/pull-secret.json}"
    while IFS= read -r variable; do
      [[ -z $variable ]] || args+=(--env "$variable")
    done <<<"${KNOWN_SECRET_ENV:-}"
    GITHUB_STEP_SUMMARY=/dev/null python3 -I "${GITHUB_WORKSPACE}/.github/scripts/release_gate.py" "$candidate/report" \
      --fail-on-find --max-file-mbytes 32 --report "$candidate/gate.json" \
      --k8s-secrets "${K8S_SECRETS_FILE:-${RUNNER_TEMP}/osac-secret-inventory/k8s-secrets.json}" \
      "${args[@]}" >"$candidate/gate.log" 2>&1
    jq -e '.withheld == [] and .censored == []' "$candidate/gate.json" >/dev/null
    mv -- "$candidate/report" "$private/hypervisor-sosreport"
    ;;
  *) exit 1 ;;
esac

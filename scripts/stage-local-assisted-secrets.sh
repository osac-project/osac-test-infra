#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
source_dir="${HOME}/.config/osac-assisted"
if [[ ! -d "${source_dir}" || -L "${source_dir}" ||
      "$(stat -c '%u' "${source_dir}")" != "$(id -u)" ]]; then
  echo "::error::Provision a runner-owned ${source_dir} directory first." >&2
  exit 1
fi
for name in pull-secret.json aap-license.zip; do
  path="${source_dir}/${name}"
  if [[ ! -s "${path}" || -L "${path}" ||
        "$(stat -c '%u' "${path}")" != "$(id -u)" ]]; then
    echo "::error::Provision runner-owned ${path} first." >&2
    exit 1
  fi
done

jq -e '(.auths | type == "object")' "${source_dir}/pull-secret.json" >/dev/null
python3 -c 'import sys, zipfile; sys.exit(not zipfile.is_zipfile(sys.argv[1]))' \
  "${source_dir}/aap-license.zip"

install -m 0600 "${source_dir}/pull-secret.json" "${RUNNER_TEMP}/pull-secret.json"
install -m 0600 "${source_dir}/aap-license.zip" "${RUNNER_TEMP}/aap-license.zip"
install -d -m 0700 "${HOME}/.config/containers"
install -m 0600 "${source_dir}/pull-secret.json" "${HOME}/.config/containers/auth.json"
/usr/bin/pkexec --disable-internal-agent /usr/local/sbin/osac-privileged auth-stage
echo "Local Assisted test secrets staged."

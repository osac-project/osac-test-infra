# shellcheck shell=bash
# Shared GitHub API helpers for OSAC-1684 audit/scan scripts.
# Source relative to the caller:  # shellcheck source=lib-github.sh
#   source "$(dirname "${BASH_SOURCE[0]}")/lib-github.sh"
#
# Requires: GH_TOKEN, and optionally GITHUB_API_URL (default set by caller).

# GET/DELETE/etc with retries on transport blips and HTTP 429/5xx.
# Writes body to $1, prints the final HTTP code (or curl-transport-error)
# on stdout. $2 is a comma-separated list of success codes (e.g. "200" or
# "200,204"). Remaining args are passed to curl after common auth/timeout
# flags (callers may append --max-time to override the default 30s).
fetch_with_retry() {
  local out="$1"
  local ok_codes="$2"
  shift 2
  local attempt=1
  local max_attempts=3
  local code=""
  local ok_match
  while (( attempt <= max_attempts )); do
    if ! code=$(curl -sL -o "${out}" -w '%{http_code}' \
      --connect-timeout 10 --max-time 30 \
      -H "Authorization: Bearer ${GH_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      "$@"); then
      code="curl-transport-error"
    fi
    ok_match=",${ok_codes},"
    if [[ "${ok_match}" == *",${code},"* ]]; then
      printf '%s\n' "${code}"
      return 0
    fi
    if [[ "${code}" == "curl-transport-error" || "${code}" == "429" || "${code}" =~ ^5[0-9][0-9]$ ]]; then
      if (( attempt < max_attempts )); then
        sleep $((attempt * 2))
        attempt=$((attempt + 1))
        continue
      fi
    fi
    break
  done
  printf '%s\n' "${code}"
  return 0
}

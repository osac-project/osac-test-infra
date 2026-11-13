#!/usr/bin/env bash
# Download a job's artifact from the private artifact repository.
#
# Jobs that publish to osac-project/osac-ci-artifacts (publish-private-artifacts)
# no longer leave an artifact on their own run, so a reader cannot ask the run
# for it. The artifact there is named exactly as the job named it, and that
# name carries the source run id, so a reader asks the repository for the
# exact names it expects.
#
# Usage: fetch-private-run-artifact.sh <owner/repo> <out.zip> <artifact name>...
# Env:   GH_TOKEN  a token that can read the repository's artifacts
#        FETCH_WAIT_SECONDS  (default 0) how long to keep asking while none of
#          the names has an artifact yet. The collector is dispatched by the
#          job and uploads on its own schedule, so a reader that starts right
#          after the job may be early. Only "not published yet" is waited
#          for; a failed lookup or download ends the wait at once.
#        FETCH_WAIT_INTERVAL (default 15) seconds between asks.
# Prints "<name> <artifact id>" and exits 0 when it downloaded the newest
# unexpired artifact of the first name that has one. Exits 3 when none of the
# names has one, and 4 when a lookup or the download failed (a token without
# access looks like this), so a caller can tell "not published" from "broken".
set -uo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <owner/repo> <out.zip> <artifact name>..." >&2
  exit 2
fi
repo=$1
out=$2
shift 2

wait=${FETCH_WAIT_SECONDS:-0}
interval=${FETCH_WAIT_INTERVAL:-15}
[[ "${wait}" =~ ^[0-9]+$ ]] || wait=0
[[ "${interval}" =~ ^[1-9][0-9]*$ ]] || interval=15
# Counted in asks rather than read from a clock, so the bound holds whatever
# the lookups take.
attempts=$((wait / interval + 1))

# One pass over the names: 0 found, 3 none has an artifact, 4 something failed.
lookup_once() {
  local failed=false name id
  for name in "$@"; do
    if ! id=$(gh api -X GET "repos/${repo}/actions/artifacts" -f name="${name}" -f per_page=100 \
        --jq '[.artifacts[] | select(.expired == false)] | sort_by(.created_at) | last | .id // empty'); then
      echo "::warning::Could not list ${name} in ${repo}" >&2
      failed=true
      continue
    fi
    [[ -n "${id}" ]] || continue
    if ! gh api "repos/${repo}/actions/artifacts/${id}/zip" > "${out}"; then
      echo "::warning::Could not download ${name} (${id}) from ${repo}" >&2
      rm -f "${out}"
      failed=true
      continue
    fi
    echo "${name} ${id}"
    return 0
  done
  [[ "${failed}" == "true" ]] && return 4
  return 3
}

for ((attempt = 1; attempt <= attempts; attempt++)); do
  lookup_once "$@"
  rc=$?
  if [[ ${rc} -ne 3 ]]; then
    exit "${rc}"
  fi
  if ((attempt < attempts)); then
    echo "No artifact yet in ${repo} (ask ${attempt}/${attempts}); waiting ${interval}s" >&2
    sleep "${interval}"
  fi
done
exit 3

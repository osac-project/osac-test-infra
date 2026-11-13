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
#        FETCH_TITLE_PREFIX  when set, only an artifact whose collector run
#          title starts with it counts. Collector runs are titled
#          "collect <repo>#<run id>.<attempt> - ...", so a caller that passes
#          "collect <repo>#<run id>.<attempt> - " never takes the artifact an
#          earlier attempt of the same run left under the same name; it waits
#          for its own attempt's upload instead.
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

title_prefix=${FETCH_TITLE_PREFIX:-}
wait=${FETCH_WAIT_SECONDS:-0}
interval=${FETCH_WAIT_INTERVAL:-15}
[[ "${wait}" =~ ^[0-9]+$ ]] || wait=0
[[ "${interval}" =~ ^[1-9][0-9]*$ ]] || interval=15
# Counted in asks rather than read from a clock, so the bound holds whatever
# the lookups take.
attempts=$((wait / interval + 1))

# Newest unexpired artifact called $1 that belongs to the wanted attempt:
# prints its id, nothing when there is none. Fails when a lookup fails.
find_artifact() {
  local name=$1 rows id run_id title
  rows=$(gh api -X GET "repos/${repo}/actions/artifacts" -f name="${name}" -f per_page=100 \
    --jq '[.artifacts[] | select(.expired == false)] | sort_by(.created_at) | reverse | .[] | "\(.id) \(.workflow_run.id // 0)"') || return 1
  while read -r id run_id; do
    [[ -n "${id}" ]] || continue
    if [[ -z "${title_prefix}" ]]; then
      echo "${id}"
      return 0
    fi
    title=$(gh api "repos/${repo}/actions/runs/${run_id}" --jq '.display_title // empty') || return 1
    if [[ "${title}" == "${title_prefix}"* ]]; then
      echo "${id}"
      return 0
    fi
  done <<<"${rows}"
  return 0
}

# One pass over the names: 0 found, 3 none has an artifact, 4 something failed.
lookup_once() {
  local failed=false name id
  for name in "$@"; do
    if ! id=$(find_artifact "${name}"); then
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

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

failed=false
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
  exit 0
done

if [[ "${failed}" == "true" ]]; then
  exit 4
fi
exit 3

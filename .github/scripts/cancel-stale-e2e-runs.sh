#!/usr/bin/env bash
# Cancel in-progress full-install runs on obsolete SHAs for this PR branch.
# gh run rerun (unlock replay) can outlive concurrency cancel-in-progress on
# newer pull_request runs; synchronize must force-cancel stale SHAs explicitly.
#
# Env: REPO, HEAD_SHA, HEAD_BRANCH, PR_NUMBER

set -euo pipefail

if [[ -z "${REPO:-}" || -z "${HEAD_SHA:-}" || -z "${HEAD_BRANCH:-}" ]]; then
  echo "REPO, HEAD_SHA, and HEAD_BRANCH are required" >&2
  exit 1
fi

if [[ -n "${PR_NUMBER:-}" ]]; then
  current_head=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}" --jq '.head.sha // empty')
  if [[ -z "${current_head}" ]]; then
    echo "Could not read PR #${PR_NUMBER} head; skipping stale-run cancel." >&2
    exit 0
  fi
  if [[ "${current_head}" != "${HEAD_SHA}" ]]; then
    echo "PR #${PR_NUMBER} head moved (${HEAD_SHA:0:7} -> ${current_head:0:7}); skipping stale-run cancel."
    exit 0
  fi
fi

E2E_NAMES='["E2E VMaaS Full Install","E2E BMaaS Full Install","E2E CaaS Full Install"]'
cancelled=0
for status in in_progress queued pending waiting; do
  runs=$(gh run list -R "${REPO}" \
    --branch "${HEAD_BRANCH}" \
    --event pull_request \
    --status "${status}" \
    --limit 100 \
    --json databaseId,name,headSha)
  while IFS=$'\t' read -r id name sha; do
    [[ -z "${id}" ]] && continue
    echo "Cancelling stale ${name} #${id} (${sha:0:7} != ${HEAD_SHA:0:7})"
    if gh api -X POST "repos/${REPO}/actions/runs/${id}/force-cancel" 2>/dev/null \
      || gh run cancel "${id}" -R "${REPO}"; then
      cancelled=$((cancelled + 1))
    else
      echo "Could not cancel run #${id}" >&2
    fi
  done < <(jq -r --arg head "${HEAD_SHA}" --argjson names "${E2E_NAMES}" '
    .[] | select(.headSha != $head) | select(.name as $n | $names | index($n)) | "\(.databaseId)\t\(.name)\t\(.headSha)"
  ' <<<"${runs}")
done
echo "Cancelled ${cancelled} stale full-install run(s)."

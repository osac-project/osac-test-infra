#!/usr/bin/env bash
# Cancel in-progress full-install runs on obsolete SHAs for this PR branch.
# gh run rerun (unlock replay) can outlive concurrency cancel-in-progress on
# newer pull_request runs; synchronize must force-cancel stale SHAs explicitly.
# Cancels all three suite workflows (VMaaS/BMaaS/CaaS) regardless of which
# caller invokes this script. After merge, e2e-cancel-stale-runs-on-push.yml
# should be the only entry point so path-filtered callers cannot skip cleanup.
#
# Env: REPO, HEAD_SHA, HEAD_BRANCH, HEAD_REPO, PR_NUMBER

set -euo pipefail

if [[ -z "${REPO:-}" || -z "${HEAD_SHA:-}" || -z "${HEAD_BRANCH:-}" || -z "${HEAD_REPO:-}" ]]; then
  echo "REPO, HEAD_SHA, HEAD_BRANCH, and HEAD_REPO are required" >&2
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
for status in in_progress queued waiting; do
  runs=$(gh api "repos/${REPO}/actions/runs?event=pull_request&branch=${HEAD_BRANCH}&status=${status}&per_page=100" \
    --jq '.workflow_runs')
  while IFS=$'\t' read -r id name sha; do
    [[ -z "${id}" ]] && continue
    echo "Cancelling stale ${name} #${id} (${sha:0:7} != ${HEAD_SHA:0:7})"
    if gh api -X POST "repos/${REPO}/actions/runs/${id}/force-cancel" 2>/dev/null \
      || gh run cancel "${id}" -R "${REPO}"; then
      cancelled=$((cancelled + 1))
    else
      echo "Could not cancel run #${id}" >&2
    fi
  done < <(jq -r --arg head "${HEAD_SHA}" --arg repo "${HEAD_REPO}" --argjson names "${E2E_NAMES}" '
    .[] | select(
      .head_repository.full_name == $repo
      and .head_sha != $head
      and (.name as $n | $names | index($n))
    ) | "\(.id)\t\(.name)\t\(.head_sha)"
  ' <<<"${runs}")
done
echo "Cancelled ${cancelled} stale full-install run(s)."

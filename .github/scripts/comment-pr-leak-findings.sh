#!/usr/bin/env bash
# Resolve an open PR for a completed e2e run and leave a short leak notice
# with links to the scan run / redacted-logs artifact (no findings table --
# details stay on the scan job summary and redacted artifact).
#
# Invoked by scan-workflow-logs.yml after scan-and-purge finds leaks.
# Best-effort: resolve/comment failures warn and exit 0 so purge success
# is not undone by a transient API blip.
#
# Required env:
#   GH_TOKEN, REPO, E2E_RUN_ID, E2E_RUN_URL, E2E_NAME, HEAD_SHA,
#   PURGE_OK, SCAN_RUN_URL, RUNNER_TEMP, GITHUB_WORKSPACE
# Optional env:
#   HEAD_BRANCH, HEAD_OWNER  -- workflow_run head repo owner (fork-aware)
#   PR_FROM_EVENT  -- workflow_run.pull_requests[0].number
#   UPLOAD_OK  -- 'true' if redacted-logs artifact uploaded on the scan run
set -euo pipefail

: "${GH_TOKEN:?}"
: "${REPO:?}"
: "${E2E_RUN_ID:?}"
: "${E2E_RUN_URL:?}"
: "${E2E_NAME:?}"
: "${HEAD_SHA:?}"
: "${PURGE_OK:?}"
: "${SCAN_RUN_URL:?}"
: "${RUNNER_TEMP:?}"
: "${GITHUB_WORKSPACE:?}"
: "${HEAD_BRANCH:=}"
: "${HEAD_OWNER:=}"
: "${PR_FROM_EVENT:=}"
: "${UPLOAD_OK:=}"

# Open PRs associated with HEAD_SHA via commits/{sha}/pulls. This works for
# pull_request merge commits (workflow_run.head_sha != PR tip head.sha) and
# for tip SHAs. Cached for fallback validation.
COMMIT_OPEN_PRS_JSON="[]"
COMMIT_PRS_LOOKUP_OK=false
# Do not `|| echo '[]'` after jq: with pipefail, gh failure can make
# jq already emit [] and then echo append a second [], breaking jq.
if COMMIT_OPEN_PRS_JSON=$(gh api --paginate "repos/${REPO}/commits/${HEAD_SHA}/pulls" \
  --jq '.[] | select(.state == "open") | .number' \
  | jq -s 'unique'); then
  COMMIT_PRS_LOOKUP_OK=true
else
  COMMIT_OPEN_PRS_JSON="[]"
  echo "::warning::commits/${HEAD_SHA}/pulls lookup failed; trying event/run/branch fallbacks."
fi

# Accept PR when associated with HEAD_SHA (merge or tip) OR still-open tip
# equals HEAD_SHA (commit-associate empty/missed). Rejects stale event/run
# numbers that share neither association nor tip.
pr_ok_for_comment() {
  local pr="$1"
  if [[ "${COMMIT_PRS_LOOKUP_OK}" == "true" ]] \
    && jq -e --argjson n "${pr}" 'index($n) != null' \
      <<<"${COMMIT_OPEN_PRS_JSON}" >/dev/null; then
    return 0
  fi
  local meta
  meta=$(gh api "repos/${REPO}/pulls/${pr}" \
    --jq '{st: .state, s: (.head.sha // "")}') || return 1
  [[ "$(jq -r '.st' <<<"${meta}")" == "open" \
    && "$(jq -r '.s' <<<"${meta}")" == "${HEAD_SHA}" ]]
}

# Resolve a PR to comment on. Prefer a unique open PR associated with the
# e2e HEAD_SHA; on failure/empty/ambiguity fall back to (in order)
# workflow_run.pull_requests, the e2e run's REST pull_requests, then an
# open PR matching the head branch/tip SHA.
PR_NUMBER=""
OPEN_PRS_JSON="${COMMIT_OPEN_PRS_JSON}"
OPEN_PR_COUNT=$(jq 'length' <<<"${OPEN_PRS_JSON}")
if [[ "${OPEN_PR_COUNT}" -eq 1 ]]; then
  PR_NUMBER=$(jq -r '.[0]' <<<"${OPEN_PRS_JSON}")
elif [[ "${OPEN_PR_COUNT}" -gt 1 ]]; then
  if [[ -n "${PR_FROM_EVENT}" && "${PR_FROM_EVENT}" != "null" ]] \
    && jq -e --argjson n "${PR_FROM_EVENT}" 'index($n) != null' \
      <<<"${OPEN_PRS_JSON}" >/dev/null; then
    PR_NUMBER="${PR_FROM_EVENT}"
    echo "Ambiguous: ${OPEN_PR_COUNT} open PRs for ${HEAD_SHA}; using event PR #${PR_NUMBER}."
  else
    echo "Ambiguous: ${OPEN_PR_COUNT} open PRs for ${HEAD_SHA} (${HEAD_BRANCH}) -- skipping PR comment."
    exit 0
  fi
fi

if [[ -z "${PR_NUMBER}" && -n "${PR_FROM_EVENT}" && "${PR_FROM_EVENT}" != "null" ]]; then
  if pr_ok_for_comment "${PR_FROM_EVENT}"; then
    PR_NUMBER="${PR_FROM_EVENT}"
    echo "Using workflow_run event PR #${PR_NUMBER}."
  else
    echo "Event PR #${PR_FROM_EVENT} not associated with ${HEAD_SHA}; ignoring."
  fi
fi

if [[ -z "${PR_NUMBER}" ]]; then
  if PR_FROM_RUN=$(gh api "repos/${REPO}/actions/runs/${E2E_RUN_ID}" \
      --jq '.pull_requests[0].number // empty') \
      && [[ -n "${PR_FROM_RUN}" ]]; then
    if pr_ok_for_comment "${PR_FROM_RUN}"; then
      PR_NUMBER="${PR_FROM_RUN}"
      echo "Using e2e run pull_requests PR #${PR_NUMBER}."
    else
      echo "Run pull_requests PR #${PR_FROM_RUN} not associated with ${HEAD_SHA}; ignoring."
    fi
  fi
fi

# Tip-SHA fallbacks (HEAD_SHA == PR head tip). Merge-commit runs are
# already covered by commits/{sha}/pulls above. Taking pulls?head=…[0]
# without a tip/association check can comment on a stale PR for a
# rebased branch.
if [[ -z "${PR_NUMBER}" && -n "${HEAD_BRANCH}" ]]; then
  # pulls?head= requires the *head* owner (fork login), not the base repo
  # owner. Prefer HEAD_OWNER from workflow_run.head_repository.owner.login.
  HEAD_REF_OWNER="${HEAD_OWNER:-${REPO%%/*}}"
  # -f encodes query values so branch names containing '&' stay one head=.
  if BRANCH_PRS_JSON=$(gh api \
      -X GET "repos/${REPO}/pulls" \
      -f state=open \
      -f "head=${HEAD_REF_OWNER}:${HEAD_BRANCH}" \
      | jq --arg sha "${HEAD_SHA}" --argjson assoc "${COMMIT_OPEN_PRS_JSON}" \
          '[.[] | select(.head.sha == $sha or (.number as $n | $assoc | index($n) != null)) | .number] | unique'); then
    BRANCH_PR_COUNT=$(jq 'length' <<<"${BRANCH_PRS_JSON}")
    if [[ "${BRANCH_PR_COUNT}" -eq 1 ]]; then
      PR_NUMBER=$(jq -r '.[0]' <<<"${BRANCH_PRS_JSON}")
      echo "Using branch head PR #${PR_NUMBER} (${HEAD_REF_OWNER}:${HEAD_BRANCH} @ ${HEAD_SHA})."
    elif [[ "${BRANCH_PR_COUNT}" -gt 1 ]]; then
      echo "Ambiguous: ${BRANCH_PR_COUNT} open PRs for ${HEAD_REF_OWNER}:${HEAD_BRANCH} @ ${HEAD_SHA} -- skipping branch fallback."
    fi
  fi
fi

if [[ -z "${PR_NUMBER}" ]]; then
  # Paginate + --jq emits NDJSON objects; slurp and filter with --arg so
  # HEAD_SHA is data (not spliced into the filter) and multi-page results
  # stay valid JSON. Tip match only -- merge SHAs use commits/{sha}/pulls.
  if SHA_PRS_JSON=$(gh api --paginate \
      "repos/${REPO}/pulls?state=open&per_page=100" \
      --jq '.[] | {n: .number, s: .head.sha}' \
      | jq -s --arg sha "${HEAD_SHA}" \
          '[.[] | select(.s == $sha) | .n] | unique'); then
    SHA_PR_COUNT=$(jq 'length' <<<"${SHA_PRS_JSON}")
    if [[ "${SHA_PR_COUNT}" -eq 1 ]]; then
      PR_NUMBER=$(jq -r '.[0]' <<<"${SHA_PRS_JSON}")
      echo "Using open PR #${PR_NUMBER} matching head tip SHA ${HEAD_SHA}."
    elif [[ "${SHA_PR_COUNT}" -gt 1 ]]; then
      echo "Ambiguous: ${SHA_PR_COUNT} open PRs for tip ${HEAD_SHA} -- skipping SHA fallback."
    fi
  fi
fi

if [[ -z "${PR_NUMBER}" ]]; then
  echo "::warning::Unable to resolve an open PR for ${HEAD_SHA} (${HEAD_BRANCH}); skipping PR comment."
  exit 0
fi

# Final guard: association or tip match before post.
if ! pr_ok_for_comment "${PR_NUMBER}"; then
  echo "::warning::Resolved PR #${PR_NUMBER} not associated with ${HEAD_SHA}; skipping PR comment."
  exit 0
fi

# Escape dynamic text for Markdown/HTML comment bodies. Shared
# definition: .github/scripts/md-cell.jq
SCRIPTS_DIR="${GITHUB_WORKSPACE}/.github/scripts"
md_cell() {
  # -Rrs: raw input, slurp whole string, raw output (no JSON quotes).
  printf '%s' "${1}" | jq -Rrs -L "${SCRIPTS_DIR}" 'include "md-cell"; cell'
}

# Body build + comment are best-effort: scan/purge already succeeded.
# Guard md_cell / body write under set -e so a jq blip cannot fail the job.
if ! E2E_NAME_SAFE=$(md_cell "${E2E_NAME}"); then
  echo "::warning::Failed to escape workflow name for PR comment; skipping."
  exit 0
fi

BODY_FILE="${RUNNER_TEMP}/pr-leak-comment.md"
if ! {
  echo "### :rotating_light: Credential leak found in e2e run logs/artifacts"
  echo ""
  echo "**Workflow:** ${E2E_NAME_SAFE}"
  echo "**E2E run:** [${E2E_RUN_ID}](${E2E_RUN_URL})"
  echo "**Scan run:** [Scan workflow logs](${SCAN_RUN_URL})"
  if [[ "${UPLOAD_OK}" == "true" ]]; then
    echo "**Redacted logs/artifacts:** [\`redacted-logs-${E2E_RUN_ID}\`](${SCAN_RUN_URL}#artifacts)"
  elif [[ "${UPLOAD_OK}" == "false" ]]; then
    echo "**Redacted logs/artifacts:** :warning: upload did not succeed -- no redacted copy on the scan run; see [scan logs](${SCAN_RUN_URL})."
  else
    echo "**Redacted logs/artifacts:** :warning: upload status unavailable -- see [scan logs](${SCAN_RUN_URL})."
  fi
  echo ""
  if [[ "${PURGE_OK}" == "true" ]]; then
    echo "Tainted raw logs and/or artifacts for that e2e run were deleted to close the exposure window."
  else
    echo ":warning: **Purge incomplete** -- raw logs and/or artifacts may still be on GitHub. Check the scan run and delete manually if needed."
  fi
  echo ""
  echo "Inspect the redacted artifact (and the scan job summary) for finding details. Rotate any real credentials."
} > "${BODY_FILE}"; then
  echo "::warning::Failed to write PR comment body; skipping."
  exit 0
fi

# Comment is best-effort -- scan/purge already succeeded; a transient
# API error or closed-PR race must not fail the job.
if gh pr comment "${PR_NUMBER}" -R "${REPO}" --body-file "${BODY_FILE}"; then
  echo "Commented on PR #${PR_NUMBER}."
else
  echo "::warning::Failed to comment on PR #${PR_NUMBER} -- leak was still detected; see scan job summary/Slack for purge status."
fi

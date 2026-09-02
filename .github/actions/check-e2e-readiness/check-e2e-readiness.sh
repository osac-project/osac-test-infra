#!/usr/bin/env bash
# OSAC-3370: decide whether a PR is ready for expensive e2e.
#
# Allow when:
#   1. "lgtm" label present, or it was applied earlier (Prow / auto-queue
#      strip the label on push; a prior /lgtm still unlocks later SHAs unless
#      a human has outstanding CHANGES_REQUESTED), or
#   2. "e2e-ready" label present (cleanup workflow removes on push), or
#   3. coderabbitai[bot] APPROVED on head AND no outstanding human CHANGES_REQUESTED
# Otherwise wait: ready=false, exit 0 (do not fail). Fetch/API errors still fail.
#
# Human APPROVED reviews do NOT unlock e2e (untrusted for this cost gate).
#
# Outstanding human CHANGES_REQUESTED = a human reviewer's latest decision
# review (APPROVED / CHANGES_REQUESTED / DISMISSED) is CHANGES_REQUESTED.
# COMMENTED reviews do not clear that decision. Match is not tied to head SHA
# (request-changes often sticks across pushes until re-reviewed).
#
# Usage (CI):
#   GH_TOKEN=... REPO=owner/name PR_NUMBER=123 HEAD_SHA=abc \
#     .github/actions/check-e2e-readiness/check-e2e-readiness.sh
#
# Pure helpers (for tests):
#   source this file with CHECK_E2E_READINESS_LIB_ONLY=1

set -euo pipefail

CODERABBIT_LOGIN='coderabbitai[bot]'

# Write ready=true|false for the composite action output (no-op outside Actions).
write_ready_output() {
  local ready="$1"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "ready=${ready}" >> "${GITHUB_OUTPUT}"
  fi
}

# Write a single-line reason for job output / overlay title (no-op outside Actions).
write_reason_output() {
  local reason="$1"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "reason=${reason}" >> "${GITHUB_OUTPUT}"
  fi
}

# Job Summary shows on the check page; ::notice:: does not.
write_readiness_summary() {
  local ready="$1"
  local reason="$2"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo "## E2E readiness"
      echo ""
      echo "- ready: \`${ready}\`"
      echo "- ${reason}"
    } >> "${GITHUB_STEP_SUMMARY}"
  fi
}

# Prints commit_id if CodeRabbit's latest decision is APPROVED; else return 1.
coderabbit_latest_approved_commit() {
  local reviews_json="$1"
  local sha
  sha=$(jq -r --arg who "${CODERABBIT_LOGIN}" '
    ([.[]
      | select(.user.login == $who)
      | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
    ] | max_by([(.submitted_at // ""), (.id // 0)]) // empty) as $latest
    | if ($latest != null) and ($latest.state == "APPROVED") and ($latest.commit_id != null)
      then $latest.commit_id
      else empty
      end
  ' <<<"${reviews_json}")
  [[ -n "${sha}" && "${sha}" != "null" ]] || return 1
  echo "${sha}"
}

# One-line wait reason when decide_e2e_readiness denies (stdout empty except untrusted e2e-ready).
explain_e2e_wait() {
  local labels_json="$1"
  local reviews_json="$2"
  local head_sha="$3"
  local events_json="${4:-[]}"

  if labels_have_e2e_ready "${labels_json}" && ! e2e_ready_applied_by_trusted_actor "${events_json}"; then
    echo "denied: e2e-ready label present but applied by untrusted actor"
    return 0
  fi
  if human_has_changes_requested "${reviews_json}"; then
    echo "waiting: human CHANGES_REQUESTED still open"
    return 0
  fi
  local cr_sha=""
  cr_sha=$(coderabbit_latest_approved_commit "${reviews_json}" || true)
  if [[ -n "${cr_sha}" && "${cr_sha}" != "${head_sha}" ]]; then
    echo "waiting: CR APPROVED on older SHA ${cr_sha:0:7}"
    return 0
  fi
  echo "waiting: no CR APPROVED on this SHA"
}

# Returns 0 if labels JSON contains the given label name.
labels_have() {
  local labels_json="$1"
  local name="$2"
  jq -e --arg n "${name}" '[.[].name] | index($n) != null' <<<"${labels_json}" >/dev/null 2>&1
}

# Returns 0 if labels JSON includes e2e-ready.
labels_have_e2e_ready() {
  labels_have "$1" "e2e-ready"
}

# The /e2e-ready slash command applies the label via GITHUB_TOKEN, so the
# actor is github-actions[bot]. Reject labels applied manually by humans
# (triage users can add labels via UI/API, bypassing the command guards).
E2E_READY_TRUSTED_ACTOR='github-actions[bot]'

# Returns 0 if the most recent e2e-ready labeled event was applied by the
# trusted automation actor (github-actions[bot]).
# $1 = issue events JSON array (from /repos/{owner}/{repo}/issues/{number}/events)
e2e_ready_applied_by_trusted_actor() {
  local events_json="$1"
  jq -e --arg who "${E2E_READY_TRUSTED_ACTOR}" '
    [.[]
      | select(.event == "labeled")
      | select(.label.name == "e2e-ready")
    ] | last
    | . != null and .actor.login == $who
  ' <<<"${events_json}" >/dev/null 2>&1
}

# Returns 0 if labels JSON includes lgtm.
labels_have_lgtm() {
  labels_have "$1" "lgtm"
}

# Returns 0 if issue events include at least one labeled lgtm.
# Current label is not required: Prow and auto-queue unlabeled on push.
pr_ever_had_lgtm() {
  local events_json="$1"
  jq -e '
    [.[]
      | select(.event == "labeled")
      | select(.label.name == "lgtm")
    ] | length > 0
  ' <<<"${events_json}" >/dev/null 2>&1
}

# Returns 0 if any human reviewer's latest decision is CHANGES_REQUESTED.
human_has_changes_requested() {
  local reviews_json="$1"
  jq -e '
    [.[]
      | select(.user != null)
      | select(((.user.login // "") | endswith("[bot]")) | not)
      | select((.user.type // "User") != "Bot")
      | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
    ]
    | group_by(.user.login)
    | map(max_by([(.submitted_at // ""), (.id // 0)]))
    | map(select(.state == "CHANGES_REQUESTED"))
    | length > 0
  ' <<<"${reviews_json}" >/dev/null 2>&1
}

# Returns 0 if CodeRabbit's latest decision is APPROVED on head_sha and no human CR.
coderabbit_approves_head() {
  local reviews_json="$1"
  local head_sha="$2"

  [[ -n "${head_sha}" ]] || return 1

  if ! jq -e --arg sha "${head_sha}" --arg who "${CODERABBIT_LOGIN}" '
    ([.[]
      | select(.user.login == $who)
      | select(.state == "APPROVED" or .state == "CHANGES_REQUESTED" or .state == "DISMISSED")
    ] | max_by([(.submitted_at // ""), (.id // 0)]) // empty) as $latest
    | ($latest != null)
      and ($latest.state == "APPROVED")
      and ($latest.commit_id != null)
      and ($latest.commit_id == $sha)
  ' <<<"${reviews_json}" >/dev/null 2>&1; then
    return 1
  fi
  if human_has_changes_requested "${reviews_json}"; then
    return 1
  fi
  return 0
}

# Print allow reason to stdout, or return 1 if denied.
# Args: labels_json reviews_json head_sha events_json
decide_e2e_readiness() {
  local labels_json="$1"
  local reviews_json="$2"
  local head_sha="$3"
  local events_json="${4:-[]}"

  if labels_have_lgtm "${labels_json}"; then
    echo "allowed: lgtm label present"
    return 0
  fi
  if pr_ever_had_lgtm "${events_json}" && ! human_has_changes_requested "${reviews_json}"; then
    echo "allowed: lgtm was applied earlier"
    return 0
  fi
  if labels_have_e2e_ready "${labels_json}"; then
    if e2e_ready_applied_by_trusted_actor "${events_json}"; then
      echo "allowed: e2e-ready label present (applied by trusted actor)"
      return 0
    else
      echo "denied: e2e-ready label present but applied by untrusted actor"
      return 1
    fi
  fi
  if coderabbit_approves_head "${reviews_json}" "${head_sha}"; then
    echo "allowed: APPROVED review on head from ${CODERABBIT_LOGIN}"
    return 0
  fi
  return 1
}

if [[ "${CHECK_E2E_READINESS_LIB_ONLY:-0}" == "1" ]]; then
  # When sourced for unit tests: return. When executed with the flag: exit.
  # shellcheck disable=SC2317
  return 0 2>/dev/null || exit 0
fi

REPO="${REPO:?REPO is required (owner/name)}"
PR_NUMBER="${PR_NUMBER:?PR_NUMBER is required}"
HEAD_SHA="${HEAD_SHA:?HEAD_SHA is required}"
GH_TOKEN="${GH_TOKEN:?GH_TOKEN is required}"
API="${GITHUB_API_URL:-https://api.github.com}"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Cap pagination to avoid hanging on cyclic Link: rel="next".
GITHUB_API_MAX_PAGES="${GITHUB_API_MAX_PAGES:-50}"

# GET a paginated GitHub list endpoint into $3 as one JSON array.
# $1 = fixed endpoint name for logs (never log URLs / GITHUB_API_URL hostnames).
# Uses Link: rel="next". Failures print ::error:: and return non-zero.
github_api_get_all() {
  local endpoint_name="$1"
  local start_url="$2"
  local out_file="$3"
  local page_url="${start_url}"
  local page=0
  local max_pages="${GITHUB_API_MAX_PAGES}"

  echo '[]' > "${out_file}"

  while [[ -n "${page_url}" ]]; do
    page=$((page + 1))
    if [[ ${page} -gt ${max_pages} ]]; then
      echo "::error::Too many pages for ${endpoint_name} (>${max_pages})"
      return 1
    fi
    local headers="${tmp}/headers.${page}"
    local body="${tmp}/body.${page}"
    local http curl_rc

    set +e
    http=$(curl -sS -D "${headers}" -o "${body}" -w '%{http_code}' \
      --connect-timeout 10 \
      --max-time 60 \
      -H "Authorization: Bearer ${GH_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      -H "X-GitHub-Api-Version: 2022-11-28" \
      "${page_url}" 2>/dev/null)
    curl_rc=$?
    set -e

    if [[ ${curl_rc} -ne 0 ]]; then
      echo "::error::Failed to fetch ${endpoint_name} (curl rc=${curl_rc}, page=${page})"
      return "${curl_rc}"
    fi
    echo "HTTP ${http} (page ${page})"
    if [[ "${http}" != "200" ]]; then
      echo "::error::Failed to fetch ${endpoint_name} (HTTP ${http}, page=${page})"
      return 1
    fi

    jq -s 'add' "${out_file}" "${body}" > "${out_file}.next"
    mv "${out_file}.next" "${out_file}"

    page_url=$(
      tr -d '\r' < "${headers}" \
        | { grep -i '^link:' || true; } \
        | tr ',' '\n' \
        | { grep 'rel="next"' || true; } \
        | sed -n 's/.*<\([^>]*\)>.*/\1/p' \
        | head -1
    )
    if [[ -n "${page_url}" && "${page_url}" != "${API}/"* ]]; then
      echo "::error::Refusing pagination next URL outside API origin for ${endpoint_name}"
      return 1
    fi
  done
}

echo "::group::Fetch PR labels"
if ! github_api_get_all \
  "issues/labels" \
  "${API}/repos/${REPO}/issues/${PR_NUMBER}/labels?per_page=100" \
  "${tmp}/labels.json"; then
  exit 1
fi
echo "::endgroup::"

echo "::group::Fetch PR reviews"
if ! github_api_get_all \
  "pulls/reviews" \
  "${API}/repos/${REPO}/pulls/${PR_NUMBER}/reviews?per_page=100" \
  "${tmp}/reviews.json"; then
  exit 1
fi
echo "::endgroup::"

echo "::group::Fetch issue events"
if ! github_api_get_all \
  "issues/events" \
  "${API}/repos/${REPO}/issues/${PR_NUMBER}/events?per_page=100" \
  "${tmp}/events.json"; then
  exit 1
fi
echo "::endgroup::"

labels_json="$(cat "${tmp}/labels.json")"
reviews_json="$(cat "${tmp}/reviews.json")"
events_json="$(cat "${tmp}/events.json")"

if reason=$(decide_e2e_readiness "${labels_json}" "${reviews_json}" "${HEAD_SHA}" "${events_json}"); then
  echo "${reason}"
  write_ready_output true
  write_reason_output "${reason}"
  write_readiness_summary true "${reason}"
  exit 0
fi

if [[ -z "${reason}" ]]; then
  reason=$(explain_e2e_wait "${labels_json}" "${reviews_json}" "${HEAD_SHA}" "${events_json}")
fi

echo "::notice::E2E readiness gate: PR #${PR_NUMBER} is waiting for unlock at ${HEAD_SHA:0:7}."
echo "::notice::${reason}"
echo "::notice::Need a CodeRabbit APPROVED review on this head, \`lgtm\` (now or earlier), or \`/e2e-ready\`."
write_ready_output false
write_reason_output "${reason}"
write_readiness_summary false "${reason}"
exit 0

#!/usr/bin/env bash
# Shared helpers for merge-required e2e-*-gate checks on a commit SHA.

set -euo pipefail

readonly MERGE_E2E_GATE_NAMES=(e2e-vmaas-gate e2e-bmaas-gate e2e-caas-gate)
readonly INVALIDATE_EXTERNAL_ID_PREFIX="osac-invalidate-e2e-gate"

invalidate_gate_external_id() {
  local gate="$1"
  printf '%s:%s' "${INVALIDATE_EXTERNAL_ID_PREFIX}" "${gate}"
}

# pull_request workflow filenames that post each merge-required gate.
# osac uses e2e-*-full-install.yml; this repo uses *-caller.yml.
gate_caller_workflows() {
  case "$1" in
    e2e-vmaas-gate)
      printf '%s\n' e2e-vmaas-full-install.yml e2e-vmaas-full-install-caller.yml
      ;;
    e2e-bmaas-gate)
      printf '%s\n' e2e-bmaas-full-install.yml e2e-bmaas-full-install-caller.yml
      ;;
    e2e-caas-gate)
      printf '%s\n' e2e-caas-full-install.yml e2e-caas-full-install-caller.yml
      ;;
    *) return 1 ;;
  esac
}

# Print matching pull_request workflow run JSON for a gate, or empty.
# Requires REPO, HEAD_SHA, PR_NUMBER; head_repo and head_ref for fork fallback.
# Returns 0 with JSON on stdout when a run matches, 1 when queries succeeded
# but no run matched, 2 when any non-404 workflow query failed.
find_gate_caller_pr_run() {
  local gate="$1" head_repo="${2:-}" head_ref="${3:-}" wf runs match err queried=0 failed_queries=0
  gate_caller_workflows "${gate}" >/dev/null || return 1
  err=$(mktemp)
  while IFS= read -r wf; do
    [[ -z "${wf}" ]] && continue
    if ! runs=$(gh api --method GET "repos/${REPO}/actions/workflows/${wf}/runs" \
      -f event=pull_request \
      -f head_sha="${HEAD_SHA}" \
      -F per_page=100 \
      --jq '[.workflow_runs[]]' 2>"${err}"); then
      if grep -qE 'HTTP 404' "${err}"; then
        queried=$((queried + 1))
      else
        failed_queries=$((failed_queries + 1))
      fi
      continue
    fi
    queried=$((queried + 1))
    match=$(jq -c --arg sha "${HEAD_SHA}" --argjson pr "${PR_NUMBER}" \
      --arg repo "${head_repo}" --arg ref "${head_ref}" '
      ([.[]
        | select(.head_sha == $sha)
        | select(any(.pull_requests[]?; .number == $pr))
      ][0])
      // (if ($repo | length) > 0 and ($ref | length) > 0 then
          ([.[]
            | select(.head_sha == $sha)
            | select((.pull_requests // []) | length == 0)
            | select((.head_repository.full_name // "") == $repo)
            | select(.head_branch == $ref)
          ] | if length == 1 then .[0] else empty end)
        else empty end)
      // empty
    ' <<<"${runs}")
    if [[ -n "${match}" && "${match}" != "null" ]]; then
      rm -f "${err}"
      printf '%s\n' "${match}"
      return 0
    fi
  done < <(gate_caller_workflows "${gate}")
  rm -f "${err}"
  if [[ "${failed_queries}" -gt 0 || "${queried}" -eq 0 ]]; then
    return 2
  fi
  return 1
}

# Load all check runs for HEAD_SHA into CHECK_RUNS_JSON (array).
load_check_runs_for_sha() {
  local page=1 resp count tmpdir
  # Commits with enough check-runs (this repo's PRs routinely have 100+)
  # produce a multi-hundred-KB JSON blob. Passing that as a --argjson
  # command-line argument on every page (as this used to do, re-embedding
  # the whole growing accumulator each time) hits Linux's ~128KB
  # single-argument limit (MAX_ARG_STRLEN) and fails with "Argument list
  # too long". Writing each page to a file and slurping the files instead
  # keeps the JSON off the command line entirely.
  tmpdir=$(mktemp -d)
  local pages=()
  while true; do
    resp=$(gh api "repos/${REPO}/commits/${HEAD_SHA}/check-runs?per_page=100&page=${page}&filter=all")
    jq -c '.check_runs' <<<"${resp}" > "${tmpdir}/page-${page}.json"
    pages+=("${tmpdir}/page-${page}.json")
    count=$(jq '.check_runs | length' <<<"${resp}")
    if [[ "${count}" -lt 100 ]]; then
      break
    fi
    page=$((page + 1))
  done
  CHECK_RUNS_JSON=$(jq -s 'add' "${pages[@]}")
  rm -rf "${tmpdir}"
}

# Print latest native gate job conclusion (success, failure, missing, ...).
latest_gate_conclusion() {
  local gate="$1"
  jq -r --arg g "${gate}" '
    [.[] | select(
      .name == $g
      and ((.details_url // "") | test("/actions/runs/[0-9]+/job/"))
    )]
    | sort_by(.created_at)
    | last
    | .conclusion // "missing"
  ' <<<"${CHECK_RUNS_JSON}"
}

# Exit 0 when every merge-required native gate job is success on HEAD_SHA.
all_merge_e2e_gates_green() {
  local gate conclusion
  load_check_runs_for_sha
  for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
    if ! native_gate_job_success "${gate}"; then
      conclusion=$(latest_gate_conclusion "${gate}")
      echo "Gate ${gate} on ${HEAD_SHA:0:7}: ${conclusion}"
      return 1
    fi
  done
  echo "All merge-required e2e gates success on ${HEAD_SHA:0:7}"
  return 0
}

# True when a native full-install gate job already reported success on HEAD_SHA.
native_gate_job_success() {
  local gate="$1"
  jq -e --arg g "${gate}" '
    [.[] | select(
      .name == $g
      and ((.details_url // "") | test("/actions/runs/[0-9]+/job/"))
    )]
    | sort_by(.created_at)
    | last
    | .status == "completed" and .conclusion == "success"
  ' <<<"${CHECK_RUNS_JSON}" >/dev/null
}

# Validate COMPLETE_GATE_NAME when set; return 2 on unsupported value.
validate_complete_gate_name() {
  local gate

  if [[ -z "${COMPLETE_GATE_NAME:-}" ]]; then
    return 0
  fi
  for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
    if [[ "${gate}" == "${COMPLETE_GATE_NAME}" ]]; then
      return 0
    fi
  done
  echo "Unsupported gate name: ${COMPLETE_GATE_NAME}" >&2
  return 2
}

# In_progress API gate checks not backed by a native workflow job URL.
orphan_in_progress_gate_check_ids() {
  local gate="$1"
  jq -r --arg g "${gate}" --arg prefix "${INVALIDATE_EXTERNAL_ID_PREFIX}" '
    [.[] | select(
      .name == $g
      and .status == "in_progress"
      and (
        ((.external_id // "") | startswith($prefix))
        or ((.details_url // "") | test("^https://github.com/[^/]+/[^/]+/actions/runs/[0-9]+$"))
        or ((.details_url // "") | test("^https://github.com/[^/]+/[^/]+/runs/[0-9]+$"))
      )
    ) | .id] | .[]
  ' <<<"${CHECK_RUNS_JSON}"
}

# Complete orphaned in_progress API gate checks after native gate jobs succeed.
# Set COMPLETE_GATE_NAME to limit completion to one gate.
complete_stale_in_progress_merge_gates() {
  local gate id completed_at title summary

  validate_complete_gate_name || return 2

  load_check_runs_for_sha
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  title="Superseded by native e2e gate job"
  summary="Stale invalidate-e2e-gates check; merge-required gate already success on this SHA."

  for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
    if [[ -n "${COMPLETE_GATE_NAME:-}" && "${gate}" != "${COMPLETE_GATE_NAME}" ]]; then
      continue
    fi
    if ! native_gate_job_success "${gate}"; then
      echo "Skipping ${gate}: no native gate job success on ${HEAD_SHA:0:7}"
      continue
    fi
    while IFS= read -r id; do
      [[ -z "${id}" || "${id}" == "null" ]] && continue
      load_check_runs_for_sha
      if ! native_gate_job_success "${gate}"; then
        echo "Skipping stale ${gate} check ${id}: native gate no longer success on ${HEAD_SHA:0:7}"
        continue
      fi
      payload=$(jq -n \
        --arg status "completed" \
        --arg conclusion "success" \
        --arg completed_at "${completed_at}" \
        --arg title "${title}" \
        --arg summary "${summary}" \
        '{
          status: $status,
          conclusion: $conclusion,
          completed_at: $completed_at,
          output: {title: $title, summary: $summary}
        }')
      if gh api "repos/${REPO}/check-runs/${id}" -X PATCH --input - <<<"${payload}"; then
        echo "Completed stale in_progress ${gate} check ${id} on ${HEAD_SHA:0:7}"
      else
        echo "Could not complete stale ${gate} check ${id} (checks:write unavailable; non-fatal)." >&2
      fi
    done < <(orphan_in_progress_gate_check_ids "${gate}")
  done
  return 0
}

# Cancel orphan unlock API gate checks before native full-install runs.
# e2e-on-label invalidate (main-branch) can post in_progress checks on the
# wrong workflow suite until this PR merges.
dismiss_unlock_orphan_gate_checks() {
  local gate id completed_at title summary

  validate_complete_gate_name || return 2

  load_check_runs_for_sha
  completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  title="Superseded by full-install gate job"
  summary="Stale unlock invalidate check; native gate job will report on this SHA."

  for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
    if [[ -n "${COMPLETE_GATE_NAME:-}" && "${gate}" != "${COMPLETE_GATE_NAME}" ]]; then
      continue
    fi
    while IFS= read -r id; do
      [[ -z "${id}" || "${id}" == "null" ]] && continue
      payload=$(jq -n \
        --arg status "completed" \
        --arg conclusion "cancelled" \
        --arg completed_at "${completed_at}" \
        --arg title "${title}" \
        --arg summary "${summary}" \
        '{
          status: $status,
          conclusion: $conclusion,
          completed_at: $completed_at,
          output: {title: $title, summary: $summary}
        }')
      if gh api "repos/${REPO}/check-runs/${id}" -X PATCH --input - <<<"${payload}"; then
        echo "Dismissed unlock orphan ${gate} check ${id} on ${HEAD_SHA:0:7}"
      else
        echo "Could not dismiss orphan ${gate} check ${id} (checks:write unavailable; non-fatal)." >&2
      fi
    done < <(orphan_in_progress_gate_check_ids "${gate}")
  done
  return 0
}

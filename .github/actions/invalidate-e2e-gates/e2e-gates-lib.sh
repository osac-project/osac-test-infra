#!/usr/bin/env bash
# Shared helpers for merge-required e2e-*-gate checks on a commit SHA.

set -euo pipefail

readonly MERGE_E2E_GATE_NAMES=(e2e-vmaas-gate e2e-bmaas-gate e2e-caas-gate)
readonly INVALIDATE_EXTERNAL_ID_PREFIX="osac-invalidate-e2e-gate"

invalidate_gate_external_id() {
  local gate="$1"
  printf '%s:%s' "${INVALIDATE_EXTERNAL_ID_PREFIX}" "${gate}"
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

# Complete orphaned in_progress API gate checks after native gate jobs succeed.
# Set COMPLETE_GATE_NAME to limit completion to one gate.
complete_stale_in_progress_merge_gates() {
  local gate id completed_at title summary failed=0

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
        echo "Could not complete stale ${gate} check ${id} (fork PRs may lack checks:write)." >&2
        failed=1
      fi
    done < <(jq -r --arg g "${gate}" --arg prefix "${INVALIDATE_EXTERNAL_ID_PREFIX}" '
      [.[] | select(
        .name == $g
        and .status == "in_progress"
        and (
          ((.external_id // "") | startswith($prefix))
          or ((.details_url // "") | test("^https://github.com/[^/]+/[^/]+/actions/runs/[0-9]+$"))
        )
      ) | .id] | .[]
    ' <<<"${CHECK_RUNS_JSON}")
  done
  return "${failed}"
}

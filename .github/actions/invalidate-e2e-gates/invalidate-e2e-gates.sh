#!/usr/bin/env bash
# Mark merge-required e2e-*-gate checks in_progress on HEAD_SHA unless all
# three already passed on this SHA (manual workflow_dispatch and PR runs).

set -euo pipefail

lib_dir="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=e2e-gates-lib.sh
source "${lib_dir}/e2e-gates-lib.sh"

readonly SKIP_IF_ALL_GREEN="${SKIP_IF_ALL_GREEN:-true}"

REASON="${REASON:-E2E unlock - waiting for fresh full-install run}"
DETAILS_URL="${DETAILS_URL:-${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}}"

if [[ -z "${HEAD_SHA:-}" ]]; then
  echo "HEAD_SHA is required" >&2
  exit 1
fi

if [[ "${SKIP_IF_ALL_GREEN}" == "true" ]] && all_merge_e2e_gates_green; then
  echo "Skipping invalidation: all merge-required e2e gates already success on HEAD."
  complete_stale_in_progress_merge_gates || true
  exit 0
fi

summary=$(printf '%s\n\n%s\n\n%s' "${REASON}" \
  "Partial or missing gate success on this SHA; waiting for a fresh full-install run." \
  "See .github/e2e-readiness.md")

HEAD_REPO=""
HEAD_REF=""
if [[ -n "${PR_NUMBER:-}" ]]; then
  pr_json=$(gh api "repos/${REPO}/pulls/${PR_NUMBER}")
  HEAD_REPO=$(jq -r '.head.repo.full_name // empty' <<<"${pr_json}")
  HEAD_REF=$(jq -r '.head.ref // empty' <<<"${pr_json}")
fi

failed=0
for gate in "${MERGE_E2E_GATE_NAMES[@]}"; do
  gate_details="${DETAILS_URL}"
  gate_check_suite_id=""
  run_json=""
  if [[ -n "${PR_NUMBER:-}" ]]; then
    find_rc=0
    run_json=$(find_gate_caller_pr_run "${gate}" "${HEAD_REPO}" "${HEAD_REF}") || find_rc=$?
    if [[ "${find_rc}" -eq 2 ]]; then
      echo "Could not query full-install runs for ${gate}" >&2
      failed=1
      continue
    fi
    if [[ -n "${run_json}" && "${run_json}" != "null" ]]; then
      run_id=$(jq -r '.id' <<<"${run_json}")
      gate_details="${GITHUB_SERVER_URL:-https://github.com}/${REPO}/actions/runs/${run_id}"
      gate_check_suite_id=$(gh api "repos/${REPO}/actions/runs/${run_id}" \
        --jq '.check_suite_id // empty' 2>/dev/null || true)
    fi
  fi
  if [[ -z "${gate_check_suite_id}" ]]; then
    echo "Skipping unbound in_progress ${gate}: no full-install check suite on ${HEAD_SHA:0:7}"
    continue
  fi
  external_id=$(invalidate_gate_external_id "${gate}")
  payload=$(jq -n \
    --arg name "${gate}" \
    --arg sha "${HEAD_SHA}" \
    --arg external_id "${external_id}" \
    --arg check_suite_id "${gate_check_suite_id}" \
    --arg started "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg title "${REASON}" \
    --arg details "${gate_details}" \
    --arg summary "${summary}" \
    '{
      name: $name,
      head_sha: $sha,
      external_id: $external_id,
      status: "in_progress",
      details_url: $details,
      started_at: $started,
      output: {
        title: $title,
        summary: $summary
      }
    }
    | if ($check_suite_id | length) > 0
      then . + {check_suite_id: ($check_suite_id | tonumber)}
      else .
      end')
  if gh api "repos/${REPO}/check-runs" --input - <<<"${payload}"; then
    echo "Marked ${gate} in_progress on ${HEAD_SHA:0:7}"
  else
    echo "Could not post in_progress ${gate} (fork PRs may lack checks:write)." >&2
    failed=1
  fi
done

if [[ ${failed} -ne 0 ]]; then
  echo "Some e2e gates could not be invalidated; merge may still see stale greens." >&2
  exit 1
fi

#!/usr/bin/env bash
# Unit tests for select-auditable-runs.jq (OSAC-1684 audit discover filter).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JQ_FILE="${SCRIPT_DIR}/select-auditable-runs.jq"

pass=0
fail=0

# Print PASS/FAIL and bump pass/fail counters. Args: name expected actual.
assert_eq() {
  local name="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name} (expected=${expected} actual=${actual})"
    fail=$((fail + 1))
  fi
}

# Filter stdin workflow_runs JSON through select-auditable-runs.jq; emit run_id list.
ids_of() {
  jq --arg repo "osac-project/osac" -f "${JQ_FILE}" | jq -c '[.[].run_id]'
}

MIXED='{
  "workflow_runs": [
    {"id": 1, "conclusion": "success", "event": "push"},
    {"id": 2, "conclusion": "failure", "event": "pull_request"},
    {"id": 3, "conclusion": "action_required", "event": "pull_request"},
    {"id": 4, "conclusion": "skipped", "event": "pull_request"},
    {"id": 5, "conclusion": "cancelled", "event": "pull_request"},
    {"id": 6, "conclusion": "timed_out", "event": "schedule"},
    {"id": 7, "conclusion": null, "event": "push"}
  ]
}'
assert_eq "drop action_required and skipped; keep cancelled/null/success/failure/timed_out" \
  '["1","2","5","6","7"]' \
  "$(ids_of <<<"${MIXED}")"

EMPTY='{"workflow_runs": []}'
assert_eq "empty page stays empty" '[]' "$(ids_of <<<"${EMPTY}")"

ONLY_WAIT='{
  "workflow_runs": [
    {"id": 10, "conclusion": "action_required", "event": "pull_request"},
    {"id": 11, "conclusion": "skipped", "event": "pull_request"}
  ]
}'
assert_eq "page of only N/A conclusions is empty" '[]' "$(ids_of <<<"${ONLY_WAIT}")"

REPO_CHECK='{"workflow_runs": [{"id": 42, "conclusion": "success", "event": "workflow_dispatch"}]}'
assert_eq "repo arg stamped on kept runs" \
  'osac-project/osac' \
  "$(jq --arg repo "osac-project/osac" -f "${JQ_FILE}" <<<"${REPO_CHECK}" | jq -r '.[0].repo')"

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]

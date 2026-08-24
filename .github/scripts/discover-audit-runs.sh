#!/usr/bin/env bash
# List completed e2e workflow runs for a hardcoded set of known callers
# (this repo's local callers plus known adopter entry points), created
# since a cutoff time (OSAC-1684). Callers are allow-listed -- no GitHub
# code search -- so the Vault PAT only needs Actions read/write on those
# repos. The workflow-runs API only filters by `created` (start time),
# not completion/`updated_at`, so the lookback window is start-time
# based -- see the per-target listing comment below.
#
# Usage: discover-audit-runs.sh <lookback> <output-dir> [target-repo]
#
#   lookback: duration string with unit -- 27h | 1d | 90d (unit required;
#             bare integers rejected so old lookback-hours values like 168
#             cannot be misread as days). Max 90d. Schedule default is 27h.
#   target-repo: "all" (default) or a known caller repo (owner/name).
#
# Required env: GH_TOKEN (Actions read on every repo listed below; write
# is only needed later by scan-run-logs.sh when purging)
#
# Writes to <output-dir>:
#   runs.json    JSON array of {run_id, repo} for completed runs that may
#                have logs (action_required / skipped conclusions omitted --
#                those never started jobs; logs API 404s). Cancelled runs
#                are kept; scan-run-logs.sh treats 0-job cancelled as N/A.
#   status.env   SKIPPED_TARGETS=N, NO_TARGETS=true|false,
#                RUNS_TRUNCATED=true|false, LOOKBACK_HOURS=N,
#                LOOKBACK_RAW=..., TARGET_REPO=...
set -euo pipefail

LOOKBACK_RAW="${1:?Usage: discover-audit-runs.sh <lookback> <output-dir> [target-repo]}"
OUTPUT_DIR="${2:?Usage: discover-audit-runs.sh <lookback> <output-dir> [target-repo]}"
TARGET_REPO="${3:-all}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GITHUB_API_URL:=https://api.github.com}"

MAX_LOOKBACK_HOURS=$((90 * 24))
PAGE_CAP=50
# Hard cap on aggregate runs across all targets before the scan step. Keeps
# org-wide 90d audits bounded (serial gitleaks + job output size). Hitting it
# marks the audit incomplete rather than silently under-scanning forever.
TOTAL_RUN_CAP=500
# Entry-point callers in this repo (runs live on callers, not reusable-only files).
LOCAL_CALLERS=(
  e2e-vmaas-caller.yml
  e2e-vmaas-full-install-caller.yml
  e2e-caas-netris-caller.yml
  e2e-caas-netris-full-install-caller.yml
  e2e-caas-full-install-caller.yml
  e2e-bmaas-full-install-caller.yml
)
# Cross-repo entry points that call into this repo's reusable e2e workflows.
# Add a repo:workflow line when a new adopter lands (or an existing adopter
# adds another suite). Intentionally hardcoded -- no code search -- so the
# audit PAT can stay Actions-only.
EXTERNAL_CALLERS=(
  osac-project/osac:e2e-vmaas-full-install.yml
  osac-project/osac:e2e-bmaas-full-install.yml
  osac-project/osac:e2e-caas-full-install.yml
)
# Must stay in sync with workflow_dispatch target-repo options in
# .github/workflows/audit-workflow-logs.yml (both lists change together).
KNOWN_TARGET_REPOS=(
  all
  osac-project/osac-test-infra
  osac-project/osac
)

# Trim whitespace from dispatch inputs; parse into LOOKBACK_HOURS.
# Unit suffix required (h or d) -- bare integers are rejected so a legacy
# lookback-hours value like 168 cannot silently become 168 days.
LOOKBACK_RAW=$(printf '%s' "${LOOKBACK_RAW}" | tr -d '[:space:]')
TARGET_REPO=$(printf '%s' "${TARGET_REPO}" | tr -d '[:space:]')
LOOKBACK_NORM=$(printf '%s' "${LOOKBACK_RAW}" | tr '[:upper:]' '[:lower:]')
if [[ "${LOOKBACK_NORM}" =~ ^([1-9][0-9]*)h$ ]]; then
  LOOKBACK_HOURS="${BASH_REMATCH[1]}"
elif [[ "${LOOKBACK_NORM}" =~ ^([1-9][0-9]*)d$ ]]; then
  LOOKBACK_HOURS=$((BASH_REMATCH[1] * 24))
else
  echo "lookback must be Nh or Nd (e.g. 27h, 1d, 90d; unit required), got: ${LOOKBACK_RAW}" >&2
  exit 2
fi
if (( LOOKBACK_HOURS > MAX_LOOKBACK_HOURS )); then
  echo "lookback must be <= 90d (${MAX_LOOKBACK_HOURS}h), got: ${LOOKBACK_RAW} (${LOOKBACK_HOURS}h)" >&2
  exit 2
fi

KNOWN=false
for known in "${KNOWN_TARGET_REPOS[@]}"; do
  if [[ "${TARGET_REPO}" == "${known}" ]]; then
    KNOWN=true
    break
  fi
done
if [[ "${KNOWN}" != "true" ]]; then
  echo "target-repo must be one of: ${KNOWN_TARGET_REPOS[*]}, got: ${TARGET_REPO}" >&2
  exit 2
fi

mkdir -p "${OUTPUT_DIR}"

# created= is start-time based (GitHub has no updated_at filter), so a run
# started before the window and finished inside it is invisible to us. The
# default 27h lookback on a 24h schedule gives a 3h overlap -- enough to catch
# runs up to ~3h long that started in the previous window. Jobs longer than
# that are theoretically missed, but our longest E2E suites run ~2h.
SINCE=$(date -u -d "${LOOKBACK_HOURS} hours ago" +%Y-%m-%dT%H:%M:%SZ)
echo "Auditing completed runs created since ${SINCE} (lookback=${LOOKBACK_RAW} => ${LOOKBACK_HOURS}h, target-repo=${TARGET_REPO})..."

TARGETS=()
# Append LOCAL_CALLERS as GITHUB_REPOSITORY:<workflow> targets.
# Mutates TARGETS. No args.
add_local_callers() {
  local caller
  for caller in "${LOCAL_CALLERS[@]}"; do
    TARGETS+=("${GITHUB_REPOSITORY}:${caller}")
  done
}

# Append EXTERNAL_CALLERS. Optional $1 filters to a single owner/name repo.
add_external_callers() {
  local filter_repo="${1:-}"
  local entry repo
  for entry in "${EXTERNAL_CALLERS[@]}"; do
    repo="${entry%%:*}"
    if [[ -n "${filter_repo}" && "${repo}" != "${filter_repo}" ]]; then
      continue
    fi
    TARGETS+=("${entry}")
  done
}

SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib-github.sh"

IS_LOCAL_SCOPE=false
if [[ "${TARGET_REPO}" == "${GITHUB_REPOSITORY}" || "${TARGET_REPO}" == "osac-project/osac-test-infra" ]]; then
  IS_LOCAL_SCOPE=true
fi

echo "::group::Resolve audit targets (target-repo=${TARGET_REPO})"
if [[ "${TARGET_REPO}" == "all" ]]; then
  # Local callers + every hardcoded adopter entry point.
  add_local_callers
  add_external_callers
elif [[ "${IS_LOCAL_SCOPE}" == "true" ]]; then
  add_local_callers
else
  add_external_callers "${TARGET_REPO}"
fi

NO_TARGETS=false
if [[ ${#TARGETS[@]} -eq 0 ]]; then
  NO_TARGETS=true
  echo "::warning::No audit targets resolved for target-repo=${TARGET_REPO} -- this is not a clean pass."
fi
echo "Auditing ${#TARGETS[@]} target(s): ${TARGETS[*]:-}"
echo "::endgroup::"

RUNS="[]"
SKIPPED_TARGETS=0
RUNS_TRUNCATED=false
targets_done=0
total_targets=${#TARGETS[@]}
for TARGET in "${TARGETS[@]+"${TARGETS[@]}"}"; do
  runs_so_far=$(echo "${RUNS}" | jq 'length')
  if (( runs_so_far >= TOTAL_RUN_CAP )); then
    remaining=$((total_targets - targets_done))
    echo "::warning::Hit aggregate run cap (${TOTAL_RUN_CAP}); skipping ${remaining} remaining target(s). Audit is incomplete."
    RUNS_TRUNCATED=true
    SKIPPED_TARGETS=$((SKIPPED_TARGETS + remaining))
    break
  fi

  REPO="${TARGET%%:*}"
  WORKFLOW="${TARGET#*:}"
  RESP_FILE="${OUTPUT_DIR}/runs-resp-${REPO//\//_}-${WORKFLOW}.json"
  # Filter by created at the API so total_count / pages are in-window, not
  # lifetime history. GitHub only exposes `created`, not `updated_at`.
  # Paginate until a short page or PAGE_CAP so long lookbacks (up to 90d)
  # are not silently truncated at per_page=100.
  page=1
  target_ids="[]"
  # Unfiltered API row count -- used for truncation detection. Must not use
  # the post-filter target_ids length: dropping action_required/skipped
  # would otherwise look like a 1000-cap truncation on every short page.
  listed_count=0
  # Incomplete = page fetch failed and/or PAGE_CAP truncation. We still keep
  # any runs collected from earlier pages (partial beat nothing) but always
  # bump SKIPPED_TARGETS so the summary cannot report a silent clean pass.
  target_incomplete=false
  while (( page <= PAGE_CAP )); do
    page_file="${RESP_FILE}.page${page}"
    HTTP_CODE=$(fetch_with_retry "${page_file}" "200" \
      -G "${GITHUB_API_URL}/repos/${REPO}/actions/workflows/${WORKFLOW}/runs" \
      --data-urlencode "status=completed" \
      --data-urlencode "per_page=100" \
      --data-urlencode "page=${page}" \
      --data-urlencode "created=>=${SINCE}")
    if [[ "${HTTP_CODE}" != "200" ]] \
      || ! jq -e '.workflow_runs | type == "array"' "${page_file}" >/dev/null 2>&1 \
      || ! jq -e '(.total_count | type) == "number" and .total_count >= 0' "${page_file}" >/dev/null 2>&1 \
      || ! jq -e 'all(.workflow_runs[]?; type == "object" and (.id | type) == "number")' "${page_file}" >/dev/null 2>&1; then
      echo "::warning::Could not list runs for ${TARGET} page ${page} (HTTP ${HTTP_CODE}, unexpected response shape, or malformed run item); keeping any earlier pages, marking target incomplete."
      target_incomplete=true
      break
    fi

    # status=completed includes action_required / skipped (0 jobs, no log
    # archive). Omit those so the audit does not report them as unverified.
    PAGE_IDS=$(jq --arg repo "${REPO}" -f "${SCRIPT_DIR}/select-auditable-runs.jq" "${page_file}")
    dropped=$(jq '[.workflow_runs[]? | select(.conclusion == "action_required" or .conclusion == "skipped")] | length' "${page_file}")
    if (( dropped > 0 )); then
      echo "Omitted ${dropped} action_required/skipped run(s) on ${TARGET} page ${page} (no jobs/logs to audit)."
    fi
    target_ids=$(jq -cn --argjson a "${target_ids}" --argjson b "${PAGE_IDS}" '$a + $b')
    page_len=$(jq '.workflow_runs | length' "${page_file}")
    total_count=$(jq '.total_count' "${page_file}")
    listed_count=$((listed_count + page_len))
    # Short/empty page normally means "done". The workflow-runs API can also
    # stop after ~1000 created-filtered results while still advertising a
    # higher total_count -- treat listed_count < total_count as truncation.
    # (Do not key off listed_count>=1000 alone: exactly 1000 runs yields an
    # empty page 11 with total_count==1000 and would false-positive.)
    if (( page_len < 100 )); then
      if (( listed_count < total_count )); then
        echo "::warning::Run listing for ${TARGET} truncated (got ${listed_count} of total_count=${total_count}; GitHub caps created-filtered workflow-run lists around 1000). Marking target incomplete."
        target_incomplete=true
      fi
      break
    fi
    if (( page == PAGE_CAP )); then
      echo "::warning::Hit page cap (${PAGE_CAP}) for ${TARGET} -- in-window runs truncated; marking target incomplete."
      target_incomplete=true
      break
    fi
    page=$((page + 1))
  done

  # Enforce aggregate TOTAL_RUN_CAP across targets (not just per-target pages).
  room=$((TOTAL_RUN_CAP - $(echo "${RUNS}" | jq 'length')))
  add_count=$(echo "${target_ids}" | jq 'length')
  if (( add_count > room )); then
    echo "::warning::Truncating ${TARGET} to ${room} run(s) to stay under aggregate cap ${TOTAL_RUN_CAP}."
    target_ids=$(echo "${target_ids}" | jq -c --argjson n "${room}" '.[0:$n]')
    target_incomplete=true
    RUNS_TRUNCATED=true
  fi

  RUNS=$(jq -cn --argjson a "${RUNS}" --argjson b "${target_ids}" '$a + $b')
  targets_done=$((targets_done + 1))
  if [[ "${target_incomplete}" == "true" ]]; then
    SKIPPED_TARGETS=$((SKIPPED_TARGETS + 1))
  fi
  if [[ "${RUNS_TRUNCATED}" == "true" ]]; then
    remaining=$((total_targets - targets_done))
    if (( remaining > 0 )); then
      echo "::warning::Aggregate run cap reached; skipping ${remaining} unprocessed target(s)."
      SKIPPED_TARGETS=$((SKIPPED_TARGETS + remaining))
    fi
    break
  fi
done

# Deduplicate run_id+repo pairs (same run can appear if multiple workflow
# files somehow resolve to the same run id -- shouldn't, but cheap).
RUNS=$(echo "${RUNS}" | jq -c 'unique_by(.repo + ":" + .run_id)')

echo "Found $(echo "${RUNS}" | jq 'length') run(s) to audit across ${targets_done} of ${total_targets} target(s)."
echo "${RUNS}" > "${OUTPUT_DIR}/runs.json"
{
  echo "SKIPPED_TARGETS=${SKIPPED_TARGETS}"
  echo "NO_TARGETS=${NO_TARGETS}"
  echo "RUNS_TRUNCATED=${RUNS_TRUNCATED}"
  echo "LOOKBACK_HOURS=${LOOKBACK_HOURS}"
  echo "LOOKBACK_RAW=${LOOKBACK_RAW}"
  echo "TARGET_REPO=${TARGET_REPO}"
} > "${OUTPUT_DIR}/status.env"

#!/usr/bin/env bash
# Fetch a workflow run's logs *and* artifacts, scan them with gitleaks, and
# if anything is found: build a redacted copy, delete the raw logs and/or
# tainted artifacts to close the exposure window, and record findings for
# the caller to report (OSAC-1684).
#
# Usage: scan-run-logs.sh <run-id> <output-dir> [repo]
#
#   repo defaults to $GITHUB_REPOSITORY (this repo) -- pass it explicitly to
#   scan a run in a *different* repo, e.g. from the cross-repo periodic
#   audit (audit-workflow-logs.yml), in which case GH_TOKEN must be an
#   Actions-scoped token with access to that repo, not the ambient same-repo
#   GITHUB_TOKEN.
#
# Required env: GH_TOKEN (Actions read to fetch logs/artifacts; write to
#               delete them)
# Optional env: GITLEAKS_CONFIG (default: .gitleaks.toml next to this script)
#               CONTAINER_ENGINE (default: docker if present, else podman)
#               SKIP_PURGE (default: false) -- when true, skip deleting raw
#                 logs/artifacts after redaction (useful for periodic scans
#                 where preserving the originals aids debugging)
#               PRIVATE_COLLECT_WAIT_SECONDS (default 300) -- how long to wait
#                 for a private collect run that has not finished uploading
#               PRIVATE_ARTIFACT_REPO (owner/name) and PRIVATE_GH_TOKEN --
#                 also scan the artifacts this run's jobs published to the
#                 private artifact repository (publish-private-artifacts)
#                 instead of to the run. The token needs actions:write there
#                 (read to download, write to delete). Both set, or neither:
#                 the repository without the token is a failed scan, not a
#                 skipped one. A leak in such an artifact is deleted like any
#                 other, but its redacted copy is never written under
#                 redacted/, which the caller uploads as a public artifact:
#                 the content was private on purpose.
#
# Writes to <output-dir>:
#   findings.json   sanitized findings (always; "[]" if clean or the scan
#                   couldn't run at all) -- RuleID/File/StartLine only, never
#                   the actual secret value. File paths are prefixed with
#                   logs/ or artifacts/<name>/ so callers can tell the channel.
#   status.env      SCAN_OK=true|false, LEAKS_FOUND=true|false,
#                   PURGE_OK=true|false, FINDINGS_COUNT=N and
#                   REDACTED_WITHHELD=true|false, for the caller to `source`.
#                     - REDACTED_WITHHELD=true means every leak found was in
#                       an artifact of the private repository, whose redacted
#                       copy is deliberately not written: there is nothing to
#                       upload. A redaction that failed is not this.
#                     - SCAN_OK=false means the scan did not complete for
#                       logs and/or artifacts (fetch/list/download/scan
#                       failure, or an oversize artifact skipped). A run
#                       with 0 jobs and no log archive (action_required,
#                       skipped, cancelled-before-start) is N/A, not
#                       SCAN_OK=false. Discover already omits
#                       action_required/skipped; this path still covers
#                       cancelled-before-start and any leftover 0-job run.
#                     - PURGE_OK=false means raw content may still be on
#                       GitHub: a delete failed after a confirmed leak, or
#                       we fetched content and then aborted before finishing
#                       purge. PURGE_OK=true with SCAN_OK=false is only when
#                       nothing was ever downloaded.
#   redacted/       redacted copy (only if leaks were found): logs/ and/or
#                   artifacts/<name>/
#
# Deliberately does not touch $GITHUB_OUTPUT, $GITHUB_STEP_SUMMARY, Slack,
# or GitHub issues -- it's used both for a single run (the post-job scan)
# and in a loop over many runs (the periodic audit), and only the caller
# knows how results across one or many runs should be reported.
set -euo pipefail

# Everything this script writes directly (logs/, artifacts/, raw gitleaks
# reports, the redacted copy) can contain real secrets -- don't inherit
# whatever permissive umask the runner happens to default to.
umask 077

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_API_URL:=https://api.github.com}"
RUN_ID="${1:?Usage: scan-run-logs.sh <run-id> <output-dir> [repo]}"
OUTPUT_DIR="${2:?Usage: scan-run-logs.sh <run-id> <output-dir> [repo]}"
REPO="${3:-${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required when repo arg omitted}}"
# Relative to this script's own location, not $GITHUB_WORKSPACE -- this
# script (and this default) is invoked both directly (audit-workflow-logs.yml)
# and via scan-and-purge-logs/action.yml, which can itself be referenced
# cross-repo (osac-project/osac-test-infra/.github/actions/...@main from
# other repos' own workflow_run listeners). $GITHUB_WORKSPACE would then be
# the *caller's* checkout, which has no .gitleaks.toml -- self-locating
# avoids every caller needing to pass this explicitly.
SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib-github.sh"
GITLEAKS_CONFIG="${GITLEAKS_CONFIG:-${SCRIPT_DIR}/.gitleaks.toml}"
# ghcr.io/gitleaks/gitleaks:v8.30.1, pinned by digest for reproducibility
GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
# Skip individual artifacts larger than this (incomplete scan, not a clean pass).
ARTIFACT_MAX_BYTES=$((500 * 1024 * 1024))

LOGS_DIR="${OUTPUT_DIR}/logs"
LOGS_ZIP="${OUTPUT_DIR}/logs.zip"
ARTIFACTS_DIR="${OUTPUT_DIR}/artifacts"
FINDINGS_JSON="${OUTPUT_DIR}/findings.json"
# Raw gitleaks report (has the actual secret values) -- purely transient,
# consumed only by redact.py and the add-mask loop below, then removed by
# the trap. Never read by any caller; see the header comment on findings.json.
FINDINGS_RAW_JSON="${OUTPUT_DIR}/findings-raw.json"
STATUS_FILE="${OUTPUT_DIR}/status.env"
REDACTED_DIR="${OUTPUT_DIR}/redacted"

# chmod 700 on top of umask 077: docker/gitleaks writes findings-raw.json as
# a *container* process, which has its own umask unaffected by this script's
# -- the directory's own restrictive mode keeps other users from reading
# into it on shared runners, regardless of file modes inside.
mkdir -p "${OUTPUT_DIR}"
chmod 700 "${OUTPUT_DIR}"
mkdir -p "${LOGS_DIR}" "${ARTIFACTS_DIR}"

# Aggregates across logs + artifacts. Updated as each channel completes.
SCAN_OK=true
LEAKS_FOUND=false
PURGE_OK=true
# Any raw content downloaded from GitHub (logs zip and/or artifact zip).
CONTENT_FETCHED=false
# Where leaks were found. A redacted copy is written for every one of them
# except those in artifacts of the private repository, which is why the
# caller's "no redacted copy" is only intended when the private ones are all
# there is: see REDACTED_WITHHELD in status.env.
PUBLIC_LEAK_SEEN=false
PRIVATE_LEAK_SEEN=false
# Set true after every DELETE required for this run's findings has been
# attempted. EXIT trap uses this so an abort *after* successful purge
# (e.g. during findings.json / ::add-mask::) does not rewrite PURGE_OK=false.
PURGE_PHASE_DONE=false
# REDACTED_DIR only receives trees *after* a successful redact.py (via a
# staging dir). Never copy raw content straight into REDACTED_DIR -- an
# abort between cp and redact would otherwise leave secrets in a tree the
# trap might keep / the composite might upload.
echo "[]" > "${FINDINGS_RAW_JSON}"

# true only when every leak found was in a private artifact, whose redacted
# copy is withheld on purpose. A redaction that failed for a public artifact is
# a missing copy, not a withheld one.
redacted_withheld() {
  if [[ "${PRIVATE_LEAK_SEEN}" == "true" && "${PUBLIC_LEAK_SEEN}" != "true" ]]; then
    echo true
  else
    echo false
  fi
}

# Atomically write status.env (KEY=value lines via temp + mv) so callers can
# source a complete file even if the script aborts mid-write.
write_status() {
  local tmp
  tmp="$(mktemp "${OUTPUT_DIR}/.status.env.XXXXXX")"
  printf '%s\n' "$@" > "${tmp}"
  mv -f -- "${tmp}" "${STATUS_FILE}"
}

# Write sanitized findings.json (RuleID/File/StartLine; CR/LF stripped).
# Shared by the happy path and the EXIT trap so both stay identical.
# Atomic: jq writes a temp sibling, then mv -- never clobber dst on failure
# (EXIT trap must not wipe a previously-valid findings.json).
write_sanitized_findings() {
  local src="${1:-${FINDINGS_RAW_JSON}}"
  local dst="${2:-${FINDINGS_JSON}}"
  local tmp
  tmp="$(mktemp "${dst}.XXXXXX")"
  if jq -f "${SCRIPT_DIR}/sanitize-findings.jq" "${src}" > "${tmp}"; then
    mv -f -- "${tmp}" "${dst}"
  else
    rm -f -- "${tmp}"
    return 1
  fi
}

# True when STATUS_FILE exists and defines non-empty SCAN_OK, LEAKS_FOUND,
# PURGE_OK, and FINDINGS_COUNT (EXIT trap skips fallback rewrite when true).
status_file_is_valid() {
  local SCAN_OK="" LEAKS_FOUND="" PURGE_OK="" FINDINGS_COUNT=""
  [[ -f "${STATUS_FILE}" ]] || return 1
  # shellcheck disable=SC1090
  source "${STATUS_FILE}" 2>/dev/null || return 1
  [[ -n "${SCAN_OK}" && -n "${LEAKS_FOUND}" && -n "${PURGE_OK}" && -n "${FINDINGS_COUNT}" ]]
}

# EXIT trap: preserve leak evidence into sanitized findings.json if needed,
# wipe raw secret-bearing paths, write fallback status.env when none valid.
cleanup_raw() {
  # Snapshot leak state *before* wiping raw reports. A mid-script abort
  # (set -e) after we already deleted a tainted artifact must not rewrite
  # status as LEAKS_FOUND=false -- callers would drop a confirmed incident
  # after the only GitHub copy was purged.
  local trap_leaks="${LEAKS_FOUND:-false}"
  local trap_count=0
  if [[ -f "${FINDINGS_RAW_JSON}" ]]; then
    trap_count=$(jq 'length' "${FINDINGS_RAW_JSON}" 2>/dev/null || echo 0)
  fi
  if (( trap_count > 0 )); then
    trap_leaks=true
  fi
  if [[ "${trap_leaks}" == "true" ]] \
    && { [[ ! -f "${FINDINGS_JSON}" ]] || [[ ! -s "${FINDINGS_JSON}" ]]; } \
    && [[ -f "${FINDINGS_RAW_JSON}" ]]; then
    write_sanitized_findings "${FINDINGS_RAW_JSON}" "${FINDINGS_JSON}" 2>/dev/null || true
  fi

  rm -rf -- "${LOGS_DIR}" "${LOGS_ZIP}" "${ARTIFACTS_DIR}" \
    "${OUTPUT_DIR}"/artifact-*.zip \
    "${OUTPUT_DIR}"/findings-raw-*.json \
    "${OUTPUT_DIR}"/.findings-merge.* \
    "${OUTPUT_DIR}"/.redact-staging* \
    "${OUTPUT_DIR}"/artifacts-list.json \
    "${OUTPUT_DIR}"/artifacts-list.json.tmp \
    "${OUTPUT_DIR}"/artifacts-list.page*.json \
    "${OUTPUT_DIR}"/private-runs.page*.json \
    "${OUTPUT_DIR}"/private-artifacts.*.json \
    "${OUTPUT_DIR}"/private-run.*.json \
    "${OUTPUT_DIR}"/run-probe.json \
    "${FINDINGS_RAW_JSON}"
  # REDACTED_DIR is only populated post-redact (staging pattern) -- keep it.
  # Staging dirs above always hold raw pre-redact copies and must go.
  # Trap fallback when set -e killed us before a normal write_status.
  if ! status_file_is_valid; then
    if [[ "${trap_leaks}" == "true" ]]; then
      # Explicit delete failure always wins.
      # Else if purge phase finished (all required DELETEs attempted) and
      # PURGE_OK still true, keep true -- abort was post-purge.
      # Else if nothing was fetched, nothing for us to purge.
      # Else: fetched but purge incomplete → unknown exposure.
      local final_purge=false
      if [[ "${PURGE_OK}" == "false" ]]; then
        final_purge=false
      elif [[ "${PURGE_PHASE_DONE:-false}" == "true" ]]; then
        final_purge=true
      elif [[ "${CONTENT_FETCHED:-false}" != "true" ]]; then
        final_purge=true
      else
        final_purge=false
      fi
      write_status "SCAN_OK=false" "LEAKS_FOUND=true" \
        "PURGE_OK=${final_purge}" "FINDINGS_COUNT=${trap_count}" \
        "REDACTED_WITHHELD=$(redacted_withheld)"
    elif [[ "${CONTENT_FETCHED:-false}" == "true" ]]; then
      write_status "SCAN_OK=false" "LEAKS_FOUND=false" "PURGE_OK=false" "FINDINGS_COUNT=0"
    else
      write_status "SCAN_OK=false" "LEAKS_FOUND=false" "PURGE_OK=true" "FINDINGS_COUNT=0"
    fi
  fi
}
trap cleanup_raw EXIT

# Merge a gitleaks JSON array into FINDINGS_RAW_JSON, rewriting each File
# with the given prefix (e.g. logs/ or artifacts/foo/).
merge_findings() {
  local report="$1"
  local file_prefix="$2"
  local tmp
  tmp="$(mktemp "${OUTPUT_DIR}/.findings-merge.XXXXXX")"
  jq -s --arg p "${file_prefix}" '
    .[0] as $acc | .[1] as $new
    | $acc + [ $new[] | . + {File: ($p + (.File // ""))} ]
  ' "${FINDINGS_RAW_JSON}" "${report}" > "${tmp}"
  mv -f -- "${tmp}" "${FINDINGS_RAW_JSON}"
}

# Pick docker (ubuntu-latest) or podman (osac-ci audit). CONTAINER_ENGINE wins.
resolve_container_engine() {
  if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
    printf '%s\n' "${CONTAINER_ENGINE}"
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    printf '%s\n' docker
    return 0
  fi
  if command -v podman >/dev/null 2>&1; then
    printf '%s\n' podman
    return 0
  fi
  echo "error: neither docker nor podman found in PATH" >&2
  return 1
}

# SELinux :Z on enforcing hosts (osac-ci); omit on GitHub-hosted ubuntu.
selinux_volume_suffix() {
  if [[ -r /sys/fs/selinux/enforce ]] && [[ "$(</sys/fs/selinux/enforce)" == "1" ]]; then
    printf '%s\n' ",Z"
  else
    printf '%s\n' ""
  fi
}

# Run pinned gitleaks image (--network=none) on scan_dir; write JSON report
# to report_path (secrets intentionally present for redact.py; never printed).
run_gitleaks() {
  local scan_dir="$1"
  local report_path="$2"
  local engine z
  engine="$(resolve_container_engine)"
  z="$(selinux_volume_suffix)"
  # Deliberately no --redact/--verbose: this job's own console output must
  # never print the raw secret, but the JSON report needs the real value so
  # redact.py can find-and-replace it. --network=none: raw content stays on
  # this host.
  "${engine}" run --rm --network=none \
    -v "${scan_dir}:/scan:ro${z}" \
    -v "${GITLEAKS_CONFIG}:/gitleaks.toml:ro${z}" \
    -v "$(dirname "${report_path}"):/out:rw${z}" \
    "${GITLEAKS_IMAGE}" dir /scan \
    --config=/gitleaks.toml \
    --report-format=json \
    --report-path="/out/$(basename "${report_path}")" \
    --exit-code=0
  # gitleaks may omit the file on zero findings depending on version --
  # normalize to an empty array.
  if [[ ! -f "${report_path}" ]]; then
    echo "[]" > "${report_path}"
  fi
}

# True when the run never scheduled a job (fork/env action_required,
# skipped, or cancelled before GitHub created jobs). Those have no log
# archive: the logs API 404s or returns a zip unzip cannot open. Discover
# already drops action_required/skipped; this still catches
# cancelled-before-start. Fail closed (return 1) if the jobs list cannot
# be fetched -- we cannot prove emptiness.
run_has_no_jobs() {
  local jobs_file="${OUTPUT_DIR}/jobs-probe.json"
  local code count
  code=$(fetch_with_retry "${jobs_file}" "200" \
    -G "${GITHUB_API_URL}/repos/${REPO}/actions/runs/${RUN_ID}/jobs" \
    --data-urlencode "per_page=1")
  if [[ "${code}" != "200" ]] \
    || ! jq -e '(.total_count | type) == "number" and .total_count >= 0' "${jobs_file}" >/dev/null 2>&1; then
    echo "Could not list jobs for run ${RUN_ID} (HTTP ${code}) -- treating log-fetch failure as incomplete."
    return 1
  fi
  count=$(jq '.total_count' "${jobs_file}")
  [[ "${count}" == "0" ]]
}

# --- Logs -----------------------------------------------------------------

echo "::group::Fetch logs for run ${RUN_ID} (${REPO})"
LOGS_SCANNED=false
if ! HTTP_CODE=$(curl -sL -o "${LOGS_ZIP}" -w '%{http_code}' \
  --connect-timeout 10 --max-time 120 \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "${GITHUB_API_URL}/repos/${REPO}/actions/runs/${RUN_ID}/logs"); then
  HTTP_CODE="curl-transport-error"
fi
if [[ "${HTTP_CODE}" != "200" ]]; then
  if run_has_no_jobs; then
    echo "Run ${RUN_ID} has 0 jobs -- logs unavailable (HTTP ${HTTP_CODE}); nothing to scan (not an incomplete audit)."
  else
    echo "::warning::Could not download logs for run ${RUN_ID} (HTTP ${HTTP_CODE}) -- continuing with artifact scan."
    SCAN_OK=false
  fi
  rm -f -- "${LOGS_ZIP}"
  echo "::endgroup::"
else
  CONTENT_FETCHED=true
  if ! unzip -q "${LOGS_ZIP}" -d "${LOGS_DIR}"; then
    if run_has_no_jobs; then
      echo "Run ${RUN_ID} has 0 jobs -- log zip unreadable; nothing to scan (not an incomplete audit)."
      CONTENT_FETCHED=false
    else
      echo "::warning::Failed to unzip logs for run ${RUN_ID} -- continuing with artifact scan."
      SCAN_OK=false
    fi
    rm -f -- "${LOGS_ZIP}"
    echo "::endgroup::"
  else
    echo "::endgroup::"

    echo "::group::Scan logs with gitleaks (run ${RUN_ID})"
    LOG_REPORT="${OUTPUT_DIR}/findings-raw-logs.json"
    run_gitleaks "${LOGS_DIR}" "${LOG_REPORT}"
    LOG_FINDINGS_COUNT=$(jq 'length' "${LOG_REPORT}")
    echo "Found ${LOG_FINDINGS_COUNT} potential secret(s) in logs."
    if (( LOG_FINDINGS_COUNT > 0 )); then
      PUBLIC_LEAK_SEEN=true
    fi
    merge_findings "${LOG_REPORT}" "logs/"
    LOGS_SCANNED=true
    echo "::endgroup::"
  fi
fi

# --- Artifacts ------------------------------------------------------------

echo "::group::List artifacts for run ${RUN_ID}"
ARTIFACT_LIST="${OUTPUT_DIR}/artifacts-list.json"
echo "[]" > "${ARTIFACT_LIST}"
page=1
list_ok=true
while (( page <= 20 )); do
  page_file="${OUTPUT_DIR}/artifacts-list.page${page}.json"
  HTTP_CODE=$(fetch_with_retry "${page_file}" "200" \
    -G "${GITHUB_API_URL}/repos/${REPO}/actions/runs/${RUN_ID}/artifacts" \
    --data-urlencode "per_page=100" \
    --data-urlencode "page=${page}")
  if [[ "${HTTP_CODE}" != "200" ]] \
    || ! jq -e '.artifacts | type == "array"' "${page_file}" >/dev/null 2>&1; then
    echo "::warning::Could not list artifacts for run ${RUN_ID} page ${page} (HTTP ${HTTP_CODE})."
    list_ok=false
    SCAN_OK=false
    break
  fi
  jq -c --slurpfile acc "${ARTIFACT_LIST}" \
    '$acc[0] + .artifacts' "${page_file}" > "${ARTIFACT_LIST}.tmp"
  mv -f -- "${ARTIFACT_LIST}.tmp" "${ARTIFACT_LIST}"
  page_len=$(jq '.artifacts | length' "${page_file}")
  if (( page_len < 100 )); then
    break
  fi
  if (( page == 20 )); then
    echo "::warning::Artifact list for run ${RUN_ID} exceeds 20 pages -- audit incomplete for this run."
    SCAN_OK=false
    break
  fi
  page=$((page + 1))
done
artifact_count=$(jq 'length' "${ARTIFACT_LIST}")
if [[ "${list_ok}" == "true" ]]; then
  echo "Listed ${artifact_count} artifact(s) for run ${RUN_ID}."
else
  # Still scan whatever pages we already collected -- partial beat nothing.
  echo "Listed ${artifact_count} artifact(s) for run ${RUN_ID} before listing failed (partial)."
fi
echo "::endgroup::"

# --- Artifacts published to the private repository --------------------------

# A job that published there left no artifact on its run. Its collect run in
# the private repository is titled "collect <repo>#<run id>.<attempt> - ...",
# so find those runs and add their artifacts to the list, each marked with
# the repository it lives in.
PRIVATE_REPO="${PRIVATE_ARTIFACT_REPO:-}"
if [[ -n "${PRIVATE_REPO}" ]]; then
  echo "::group::List private artifacts for run ${RUN_ID} in ${PRIVATE_REPO}"
  if [[ -z "${PRIVATE_GH_TOKEN:-}" ]]; then
    echo "::warning::PRIVATE_ARTIFACT_REPO is set but PRIVATE_GH_TOKEN is not -- the private artifacts of run ${RUN_ID} were not scanned."
    SCAN_OK=false
  else
    run_file="${OUTPUT_DIR}/run-probe.json"
    code=$(fetch_with_retry "${run_file}" "200" "${GITHUB_API_URL}/repos/${REPO}/actions/runs/${RUN_ID}")
    since=$(jq -r '.created_at // empty' "${run_file}" 2>/dev/null || true)
    if [[ "${code}" != "200" || -z "${since}" ]]; then
      echo "::warning::Could not read run ${RUN_ID} (HTTP ${code}) to look for its private artifacts -- audit incomplete for this run."
      SCAN_OK=false
    else
      title="collect ${REPO}#${RUN_ID}."
      private_ok=true
      private_runs=""
      private_page=1
      while (( private_page <= 5 )); do
        page_file="${OUTPUT_DIR}/private-runs.page${private_page}.json"
        code=$(FETCH_TOKEN="${PRIVATE_GH_TOKEN}" fetch_with_retry "${page_file}" "200" \
          -G "${GITHUB_API_URL}/repos/${PRIVATE_REPO}/actions/workflows/collect.yml/runs" \
          --data-urlencode "per_page=100" \
          --data-urlencode "page=${private_page}" \
          --data-urlencode "created=>=${since}")
        if [[ "${code}" != "200" ]] || ! jq -e '.workflow_runs | type == "array"' "${page_file}" >/dev/null 2>&1; then
          echo "::warning::Could not list collect runs in ${PRIVATE_REPO} (HTTP ${code}) -- the private artifacts of run ${RUN_ID} were not scanned."
          private_ok=false
          break
        fi
        private_runs+=$(jq -r --arg t "${title}" '.workflow_runs[] | select((.display_title // "") | startswith($t)) | "\(.id) \(.status)"' "${page_file}")$'\n'
        if (( $(jq '.workflow_runs | length' "${page_file}") < 100 )); then
          break
        fi
        if (( private_page == 5 )); then
          echo "::warning::More than 500 collect runs since run ${RUN_ID} started -- its private artifacts may not all have been found."
          private_ok=false
        fi
        private_page=$((private_page + 1))
      done
      # The job dispatches its collect run and does not wait for it, and this
      # scan starts when the run completes, so a collect run may still be
      # queued or uploading: its artifacts do not exist yet. Wait for it, up to
      # PRIVATE_COLLECT_WAIT_SECONDS, and call the scan incomplete if it never
      # finishes rather than reporting an empty listing as clean.
      private_wait="${PRIVATE_COLLECT_WAIT_SECONDS:-300}"
      [[ "${private_wait}" =~ ^[0-9]+$ ]] || private_wait=300
      # One budget for all of them, not one each: several pending runs must not
      # add up to more than the job can spare before it purges the logs. Counted
      # in the sleeps taken, so the bound does not depend on the clock.
      waited=0
      while read -r private_run private_status; do
        [[ -z "${private_run}" ]] && continue
        while [[ "${private_status}" != "completed" ]]; do
          if (( waited >= private_wait )); then
            echo "::warning::Collect run ${private_run} in ${PRIVATE_REPO} has not completed after ${private_wait}s -- its private artifacts of run ${RUN_ID} were not fully scanned."
            private_ok=false
            break
          fi
          sleep 15
          waited=$((waited + 15))
          state_file="${OUTPUT_DIR}/private-run.${private_run}.json"
          code=$(FETCH_TOKEN="${PRIVATE_GH_TOKEN}" fetch_with_retry "${state_file}" "200" \
            "${GITHUB_API_URL}/repos/${PRIVATE_REPO}/actions/runs/${private_run}")
          if [[ "${code}" != "200" ]]; then
            echo "::warning::Could not read collect run ${private_run} in ${PRIVATE_REPO} (HTTP ${code})."
            private_ok=false
            break
          fi
          private_status=$(jq -r '.status // empty' "${state_file}" 2>/dev/null || true)
        done
        # Every page: an artifact on a page that was not read is neither
        # scanned nor deleted, so a listing that stops early is not complete.
        private_art_page=1
        while :; do
          page_file="${OUTPUT_DIR}/private-artifacts.${private_run}.${private_art_page}.json"
          code=$(FETCH_TOKEN="${PRIVATE_GH_TOKEN}" fetch_with_retry "${page_file}" "200" \
            -G "${GITHUB_API_URL}/repos/${PRIVATE_REPO}/actions/runs/${private_run}/artifacts" \
            --data-urlencode "per_page=100" --data-urlencode "page=${private_art_page}")
          if [[ "${code}" != "200" ]] || ! jq -e '.artifacts | type == "array"' "${page_file}" >/dev/null 2>&1; then
            echo "::warning::Could not list the artifacts of collect run ${private_run} in ${PRIVATE_REPO} (HTTP ${code})."
            private_ok=false
            break
          fi
          jq -c --slurpfile acc "${ARTIFACT_LIST}" --arg r "${PRIVATE_REPO}" \
            '$acc[0] + [.artifacts[] | . + {repo: $r}]' "${page_file}" > "${ARTIFACT_LIST}.tmp"
          mv -f -- "${ARTIFACT_LIST}.tmp" "${ARTIFACT_LIST}"
          private_total=$(jq -r '.total_count // 0' "${page_file}" 2>/dev/null || echo 0)
          [[ "${private_total}" =~ ^[0-9]+$ ]] || private_total=0
          if (( private_art_page * 100 >= private_total )); then
            break
          fi
          private_art_page=$((private_art_page + 1))
        done
      done <<<"${private_runs}"
      if [[ "${private_ok}" != "true" ]]; then
        SCAN_OK=false
      fi
      echo "Listed $(jq '[.[] | select(.repo != null)] | length' "${ARTIFACT_LIST}") private artifact(s) for run ${RUN_ID}."
    fi
  fi
  echo "::endgroup::"
fi

# Scan every artifact collected so far even if pagination later failed.
while IFS= read -r art_json; do
  [[ -z "${art_json}" ]] && continue
  art_id=$(jq -r '.id' <<<"${art_json}")
  art_name=$(jq -r '.name' <<<"${art_json}")
  art_expired=$(jq -r '.expired' <<<"${art_json}")
  art_size=$(jq -r '.size_in_bytes // 0' <<<"${art_json}")
  # Artifacts of the private repository carry their repository; the run's own
  # do not. The private ones are read and deleted with their own token, and
  # their content never goes under REDACTED_DIR.
  art_repo=$(jq -r '.repo // empty' <<<"${art_json}")
  art_token=""
  art_private=false
  if [[ -n "${art_repo}" ]]; then
    art_private=true
    art_token="${PRIVATE_GH_TOKEN}"
  else
    art_repo="${REPO}"
  fi

  if [[ "${art_expired}" == "true" ]]; then
    echo "Skipping expired artifact ${art_name} (${art_id})."
    continue
  fi
  if (( art_size > ARTIFACT_MAX_BYTES )); then
    echo "::warning::Skipping oversize artifact ${art_name} (${art_id}, ${art_size} bytes > ${ARTIFACT_MAX_BYTES}) -- audit incomplete for this run."
    SCAN_OK=false
    continue
  fi

  echo "::group::Scan artifact ${art_name} (${art_id})"
  safe_name=$(printf '%s' "${art_name}" | tr -c 'A-Za-z0-9._-' '_')
  art_zip="${OUTPUT_DIR}/artifact-${art_id}.zip"
  art_dir="${ARTIFACTS_DIR}/${safe_name}-${art_id}"
  mkdir -p "${art_dir}"

  # --max-time 300 overrides lib-github.sh's default 30s (large zips).
  HTTP_CODE=$(FETCH_TOKEN="${art_token}" fetch_with_retry "${art_zip}" "200" \
    --max-time 300 \
    "${GITHUB_API_URL}/repos/${art_repo}/actions/artifacts/${art_id}/zip")
  if [[ "${HTTP_CODE}" != "200" ]]; then
    echo "::warning::Could not download artifact ${art_name} (${art_id}) (HTTP ${HTTP_CODE})."
    SCAN_OK=false
    rm -f -- "${art_zip}"
    echo "::endgroup::"
    continue
  fi
  CONTENT_FETCHED=true
  if ! unzip -q "${art_zip}" -d "${art_dir}"; then
    echo "::warning::Failed to unzip artifact ${art_name} (${art_id})."
    SCAN_OK=false
    rm -f -- "${art_zip}"
    echo "::endgroup::"
    continue
  fi
  rm -f -- "${art_zip}"

  art_report="${OUTPUT_DIR}/findings-raw-art-${art_id}.json"
  run_gitleaks "${art_dir}" "${art_report}"
  art_findings_count=$(jq 'length' "${art_report}")
  echo "Found ${art_findings_count} potential secret(s) in artifact ${art_name}."
  art_slug="${safe_name}-${art_id}"
  merge_findings "${art_report}" "artifacts/${art_slug}/"

  if (( art_findings_count > 0 )); then
    LEAKS_FOUND=true
    if [[ "${art_private}" == "true" ]]; then
      PRIVATE_LEAK_SEEN=true
    else
      PUBLIC_LEAK_SEEN=true
    fi
    # Stage → redact → move into REDACTED_DIR so an abort never leaves a
    # raw copy under the tree the composite uploads as evidence.
    art_stage="${OUTPUT_DIR}/.redact-staging-art-${art_id}"
    rm -rf -- "${art_stage}"
    cp -r "${art_dir}" "${art_stage}"
    if [[ "${art_private}" == "true" ]]; then
      # Private on purpose: the caller uploads REDACTED_DIR as a public
      # artifact, so no copy of this one goes there. The findings (rule,
      # file, line) are recorded; the raw artifact is deleted below.
      rm -rf -- "${art_stage}"
    else
      python3 "${SCRIPT_DIR}/redact.py" "${art_report}" "${art_stage}"
      mkdir -p "${REDACTED_DIR}/artifacts"
      mv -- "${art_stage}" "${REDACTED_DIR}/artifacts/${art_slug}"
    fi

    if [[ "${SKIP_PURGE:-false}" == "true" ]]; then
      echo "SKIP_PURGE=true -- keeping raw artifact ${art_name} (${art_id})."
    else
      # 404 = already gone (e.g. first DELETE succeeded, retry saw 404 after
      # a transport blip) -- exposure window is closed either way.
      HTTP_CODE=$(FETCH_TOKEN="${art_token}" fetch_with_retry /dev/null "204,404" \
        -X DELETE \
        "${GITHUB_API_URL}/repos/${art_repo}/actions/artifacts/${art_id}")
      if [[ "${HTTP_CODE}" != "204" && "${HTTP_CODE}" != "404" ]]; then
        echo "::warning::Failed to delete artifact ${art_name} (${art_id}) (HTTP ${HTTP_CODE}) -- exposure window is NOT closed."
        PURGE_OK=false
      else
        echo "Artifact ${art_name} (${art_id}) deleted (HTTP ${HTTP_CODE})."
      fi
    fi
  fi
  echo "::endgroup::"
done < <(jq -c '.[]' "${ARTIFACT_LIST}")

# --- Finalize logs purge + status -----------------------------------------

FINDINGS_COUNT=$(jq 'length' "${FINDINGS_RAW_JSON}")
if (( FINDINGS_COUNT > 0 )); then
  LEAKS_FOUND=true
fi

LOG_LEAK_COUNT=0
if [[ "${LOGS_SCANNED}" == "true" ]]; then
  LOG_LEAK_COUNT=$(jq '[.[] | select(.File | startswith("logs/"))] | length' "${FINDINGS_RAW_JSON}")
fi

if (( LOG_LEAK_COUNT > 0 )); then
  echo "::group::Redact and purge logs for run ${RUN_ID}"
  # Stage → redact → move (same pattern as artifacts above).
  log_stage="${OUTPUT_DIR}/.redact-staging-logs"
  rm -rf -- "${log_stage}"
  cp -r "${LOGS_DIR}" "${log_stage}"
  # Strip the logs/ prefix so File paths match the copied tree. redact.py
  # wipes base64 fields that decode to Secret, plus plaintext/column fallback.
  log_only_report="${OUTPUT_DIR}/findings-raw-logs-only.json"
  jq '[.[] | select(.File | startswith("logs/"))]
      | map(. + {File: (.File | sub("^logs/"; ""))})' \
    "${FINDINGS_RAW_JSON}" > "${log_only_report}"
  python3 "${SCRIPT_DIR}/redact.py" "${log_only_report}" "${log_stage}"
  mkdir -p "${REDACTED_DIR}"
  mv -- "${log_stage}" "${REDACTED_DIR}/logs"

  if [[ "${SKIP_PURGE:-false}" == "true" ]]; then
    echo "SKIP_PURGE=true -- keeping raw logs for run ${RUN_ID}."
  else
    # 404 = already gone (same retry race as artifact DELETE above).
    HTTP_CODE=$(fetch_with_retry /dev/null "204,404" \
      -X DELETE \
      "${GITHUB_API_URL}/repos/${REPO}/actions/runs/${RUN_ID}/logs")
    if [[ "${HTTP_CODE}" != "204" && "${HTTP_CODE}" != "404" ]]; then
      echo "::warning::Failed to delete raw logs for run ${RUN_ID} (HTTP ${HTTP_CODE}) -- the exposure window is NOT closed, raw logs are still on GitHub."
      PURGE_OK=false
    else
      echo "Raw logs for run ${RUN_ID} deleted (HTTP ${HTTP_CODE})."
    fi
  fi
  echo "::endgroup::"
fi

# Incomplete scan after we fetched content means unknown exposure -- never
# claim the window is closed (matches the header contract: PURGE_OK=true
# with SCAN_OK=false only when nothing was downloaded). Apply *before*
# PURGE_PHASE_DONE so a later set -e abort cannot report a closed window
# while SCAN_OK=false + CONTENT_FETCHED=true still holds.
if [[ "${SCAN_OK}" != "true" && "${CONTENT_FETCHED}" == "true" ]]; then
  PURGE_OK=false
fi
if [[ "${SKIP_PURGE:-false}" == "true" && "${LEAKS_FOUND}" == "true" ]]; then
  PURGE_OK=false
fi

# All DELETEs for findings have been attempted (or none needed) and the
# incomplete-scan rule above has been applied. Trap must not treat a later
# abort (findings.json / ::add-mask::) as unknown exposure when PURGE_OK
# is still true.
PURGE_PHASE_DONE=true

# Best-effort: mask found secrets in this job's own subsequent log output.
# Mirror redact.py secret_variants: trailing-\ strips and embedded JWT (eyJ...).
while IFS= read -r secret; do
  [[ -z "${secret}" ]] && continue
  echo "::add-mask::${secret}"
  stripped="${secret}"
  while [[ "${stripped}" == *\\ ]]; do
    stripped="${stripped%\\}"
    [[ -n "${stripped}" ]] && echo "::add-mask::${stripped}"
  done
  if [[ "${secret}" == *eyJ* ]]; then
    # Same shape as redact.py _JWT_RE (no capture of surrounding junk).
    if [[ "${secret}" =~ eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+ ]]; then
      echo "::add-mask::${BASH_REMATCH[0]}"
    fi
  fi
done < <(jq -r '.[].Secret // empty' "${FINDINGS_RAW_JSON}" | sort -u)

# Sanitize CR/LF in RuleID/File so downstream Markdown tables (PR comment,
# job summary, tracking issue) cannot be broken by artifact-controlled paths.
write_sanitized_findings

write_status \
  "SCAN_OK=${SCAN_OK}" \
  "LEAKS_FOUND=${LEAKS_FOUND}" \
  "PURGE_OK=${PURGE_OK}" \
  "FINDINGS_COUNT=${FINDINGS_COUNT}" \
  "REDACTED_WITHHELD=$(redacted_withheld)"

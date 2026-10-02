#!/usr/bin/env bash
# Validate and normalize the optional workflow registry.
#
# Usage:
#   optional-workflows.sh build REGISTRY_YAML ACTIVE_WORKFLOWS_JSON OUTPUT_JSON
#
# The active workflow input is the JSON array returned by `gh workflow list
# --json id,name,path,state`. The normalized registry is written to OUTPUT_JSON
# as a JSON array suitable for consumption by the slash-command handler.
set -euo pipefail

usage() {
  echo "Usage: $0 build REGISTRY_YAML ACTIVE_WORKFLOWS_JSON OUTPUT_JSON" >&2
  exit 2
}

die() {
  echo "optional-workflows: $*" >&2
  exit 1
}

[[ $# -eq 4 && $1 == "build" ]] || usage

REGISTRY_FILE=$2
ACTIVE_WORKFLOWS_FILE=$3
OUTPUT_FILE=$4

command -v yq >/dev/null 2>&1 || die "yq is required"
command -v jq >/dev/null 2>&1 || die "jq is required"
[[ -f "${REGISTRY_FILE}" ]] || die "registry file not found: ${REGISTRY_FILE}"
[[ -f "${ACTIVE_WORKFLOWS_FILE}" ]] || die "active workflow list not found: ${ACTIVE_WORKFLOWS_FILE}"

WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT

REGISTRY_JSON="${WORK_DIR}/registry.json"
ENTRIES_JSONL="${WORK_DIR}/entries.jsonl"

if ! yq -o=json '.' "${REGISTRY_FILE}" >"${REGISTRY_JSON}"; then
  die "could not parse registry YAML: ${REGISTRY_FILE}"
fi

jq -e 'type == "array"' "${ACTIVE_WORKFLOWS_FILE}" >/dev/null 2>&1 ||
  die "active workflow list must be a JSON array"

jq -e '
  type == "object" and
  (.version == 1) and
  (.workflows | type == "array")
' "${REGISTRY_JSON}" >/dev/null 2>&1 ||
  die "registry must contain version: 1 and a workflows array"

RESERVED_COMMANDS=(
  "?"
  "all"
  "cancel"
  "e2e-ready"
  "help"
  "ok-to-test"
  "retest"
)

is_reserved_command() {
  local command=$1 reserved
  for reserved in "${RESERVED_COMMANDS[@]}"; do
    [[ "${command}" == "${reserved}" ]] && return 0
  done
  return 1
}

WORKFLOW_COUNT=$(jq -r '.workflows | length' "${REGISTRY_JSON}")
declare -A SEEN_COMMANDS=()

for ((index = 0; index < WORKFLOW_COUNT; index++)); do
  command=$(jq -r --argjson index "${index}" '.workflows[$index].command // empty' "${REGISTRY_JSON}")
  workflow=$(jq -r --argjson index "${index}" '.workflows[$index].workflow // empty' "${REGISTRY_JSON}")
  name=$(jq -r --argjson index "${index}" '.workflows[$index].name // empty' "${REGISTRY_JSON}")
  description=$(jq -r --argjson index "${index}" '.workflows[$index].description // empty' "${REGISTRY_JSON}")
  trigger=$(jq -r --argjson index "${index}" '.workflows[$index].trigger // "workflow_dispatch"' "${REGISTRY_JSON}")
  label=$(jq -r --argjson index "${index}" '.workflows[$index].label // ""' "${REGISTRY_JSON}")
  retestable_type=$(jq -r --argjson index "${index}" '
    if (.workflows[$index] | has("retestable")) then
      (.workflows[$index].retestable | type)
    else
      "missing"
    end
  ' "${REGISTRY_JSON}")
  inputs=$(jq -c --argjson index "${index}" '.workflows[$index] | if has("inputs") then .inputs else {} end' "${REGISTRY_JSON}")
  if ! jq -en --argjson inputs "${inputs}" '
    ($inputs | type) == "object" and
    ($inputs | all(.[]; type == "string" and ((contains("\n") or contains("\r")) | not)))
  ' >/dev/null 2>&1; then
    die "workflows[${index}].inputs must be an object of string values without line breaks"
  fi
  if ! jq -en --argjson inputs "${inputs}" '
    $inputs | all(keys[]; test("\\A[A-Za-z][A-Za-z0-9_-]*\\z"))
  ' >/dev/null 2>&1; then
    die "workflows[${index}].inputs has an invalid input name"
  fi
  while IFS= read -r input_name; do
    [[ "${input_name}" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] ||
      die "workflows[${index}].inputs has an invalid input name: ${input_name}"
    case "${input_name}" in
      pr-number|pr-repository|pr-ref|pr-sha|fork-pr-author|fork-pr-author-association)
        die "workflows[${index}].inputs may not override trusted-context input: ${input_name}"
        ;;
    esac
  done < <(jq -r 'keys[]' <<<"${inputs}")
  if [[ "${trigger}" == "label" && "$(jq 'length' <<<"${inputs}")" -ne 0 ]]; then
    die "workflows[${index}].inputs is only valid for workflow_dispatch triggers"
  fi

  [[ "${command}" =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
    die "workflows[${index}].command must match [a-z0-9][a-z0-9-]*"
  is_reserved_command "${command}" &&
    die "workflows[${index}].command is reserved: ${command}"
  [[ -z "${SEEN_COMMANDS[${command}]+seen}" ]] ||
    die "duplicate command: ${command}"
  SEEN_COMMANDS["${command}"]=1

  [[ "${workflow}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$ ]] ||
    die "workflows[${index}].workflow must be a workflow filename: ${workflow}"
  [[ -n "${name}" ]] || die "workflows[${index}].name must be non-empty"
  [[ -n "${description}" ]] || die "workflows[${index}].description must be non-empty"
  [[ "${trigger}" == "workflow_dispatch" || "${trigger}" == "label" ]] ||
    die "workflows[${index}].trigger must be workflow_dispatch or label"
  if [[ "${trigger}" == "label" ]]; then
    [[ "${label}" =~ ^[a-z0-9][a-z0-9-]*$ ]] ||
      die "workflows[${index}].label must match [a-z0-9][a-z0-9-]* when trigger is label"
  else
    [[ -z "${label}" ]] ||
      die "workflows[${index}].label is only valid when trigger is label"
  fi
  [[ "${retestable_type}" == "boolean" ]] ||
    die "workflows[${index}].retestable must be boolean"

  workflow_path=".github/workflows/${workflow}"
  path_matches=$(jq --arg path "${workflow_path}" '[.[] | select(.path == $path)] | length' "${ACTIVE_WORKFLOWS_FILE}")
  active_matches=$(jq --arg path "${workflow_path}" '[.[] | select(.path == $path and .state == "active")] | length' "${ACTIVE_WORKFLOWS_FILE}")

  if [[ "${active_matches}" -eq 0 ]]; then
    if [[ "${path_matches}" -gt 0 ]]; then
      die "registered workflow is disabled: ${workflow}"
    fi
    die "registered workflow is missing from active workflows: ${workflow}"
  fi

  workflow_id=$(jq -r --arg path "${workflow_path}" '
    [.[] | select(.path == $path and .state == "active")][0].id // empty
  ' "${ACTIVE_WORKFLOWS_FILE}")
  [[ -n "${workflow_id}" ]] || die "active workflow has no id: ${workflow}"

  jq -cn \
    --arg command "${command}" \
    --arg workflow "${workflow}" \
    --arg name "${name}" \
    --arg description "${description}" \
    --arg trigger "${trigger}" \
    --arg label "${label}" \
    --arg workflow_id "${workflow_id}" \
    --argjson retestable "$(jq -r --argjson index "${index}" '.workflows[$index].retestable' "${REGISTRY_JSON}")" \
    --argjson inputs "${inputs}" \
    '{command: $command, workflow: $workflow, name: $name, description: $description,
      trigger: $trigger, label: $label, workflow_id: ($workflow_id | tonumber),
      retestable: $retestable, inputs: $inputs}' \
    >>"${ENTRIES_JSONL}"
done

if [[ -s "${ENTRIES_JSONL}" ]]; then
  jq -cs '.' "${ENTRIES_JSONL}" >"${OUTPUT_FILE}"
else
  echo '[]' >"${OUTPUT_FILE}"
fi

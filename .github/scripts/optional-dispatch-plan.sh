#!/usr/bin/env bash
# Build the side-effect-free plan used to dispatch an optional workflow.
set -euo pipefail

usage() {
  echo "Usage: $0 plan REGISTRY_JSON COMMAND DEFAULT_BRANCH PR_NUMBER HEAD_REPOSITORY HEAD_BRANCH HEAD_SHA HEAD_AUTHOR AUTHOR_ASSOCIATION IS_FORK" >&2
  exit 2
}

[[ $# -eq 11 && $1 == "plan" ]] || usage

REGISTRY_JSON=$2
COMMAND=$3
DEFAULT_BRANCH=$4
PR_NUMBER=$5
HEAD_REPOSITORY=$6
HEAD_BRANCH=$7
HEAD_SHA=$8
HEAD_AUTHOR=$9
AUTHOR_ASSOCIATION=${10}
IS_FORK=${11}

[[ -f "${REGISTRY_JSON}" ]] || {
  echo "optional-dispatch-plan: registry file not found: ${REGISTRY_JSON}" >&2
  exit 1
}
[[ "${IS_FORK}" == "true" || "${IS_FORK}" == "false" ]] || {
  echo "optional-dispatch-plan: IS_FORK must be true or false" >&2
  exit 1
}

WORKFLOW=$(jq -er --arg command "${COMMAND}" \
  '.[] | select(.command == $command) | .workflow' "${REGISTRY_JSON}")
TRIGGER=$(jq -er --arg command "${COMMAND}" \
  '.[] | select(.command == $command) | (.trigger // "workflow_dispatch")' "${REGISTRY_JSON}")
LABEL=$(jq -r --arg command "${COMMAND}" \
  '.[] | select(.command == $command) | (.label // "")' "${REGISTRY_JSON}")
CUSTOM_INPUTS=$(jq -cer --arg command "${COMMAND}" \
  '.[] | select(.command == $command) | if has("inputs") then .inputs else {} end' "${REGISTRY_JSON}")
if ! jq -e '
  type == "object" and all(.[]; type == "string")
' <<<"${CUSTOM_INPUTS}" >/dev/null 2>&1; then
  echo "optional-dispatch-plan: custom inputs must be an object of string values" >&2
  exit 1
fi
if ! jq -e '
  all(keys[]; test("\\A[A-Za-z][A-Za-z0-9_-]*\\z"))
' <<<"${CUSTOM_INPUTS}" >/dev/null 2>&1; then
  echo "optional-dispatch-plan: invalid custom input name" >&2
  exit 1
fi
MARKER="PR #${PR_NUMBER} @ ${HEAD_SHA}"

DISPATCH_ARGS=()
if [[ "${TRIGGER}" == "workflow_dispatch" ]]; then
  DISPATCH_ARGS=(
    -f "pr-number=${PR_NUMBER}"
    -f "pr-repository=${HEAD_REPOSITORY}"
    -f "pr-ref=${HEAD_BRANCH}"
    -f "pr-sha=${HEAD_SHA}"
  )
  if [[ "${IS_FORK}" == "true" ]]; then
    DISPATCH_ARGS+=(
      -f "fork-pr-author-association=${AUTHOR_ASSOCIATION}"
      -f "fork-pr-author=${HEAD_AUTHOR}"
    )
  fi
  while IFS= read -r input_name; do
    [[ "${input_name}" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || {
      echo "optional-dispatch-plan: invalid custom input name: ${input_name}" >&2
      exit 1
    }
    case "${input_name}" in
      pr-number|pr-repository|pr-ref|pr-sha|fork-pr-author|fork-pr-author-association)
        echo "optional-dispatch-plan: custom inputs cannot override PR context: ${input_name}" >&2
        exit 1
        ;;
    esac
    if ! jq -e --arg name "${input_name}" '
      (.[$name] | type == "string") and
      (.[$name] | (contains("\n") or contains("\r")) | not)
    ' <<<"${CUSTOM_INPUTS}" >/dev/null 2>&1; then
      echo "optional-dispatch-plan: custom input values may not contain line breaks: ${input_name}" >&2
      exit 1
    fi
    input_value=$(jq -r --arg name "${input_name}" '.[$name]' <<<"${CUSTOM_INPUTS}")
    DISPATCH_ARGS+=(-f "${input_name}=${input_value}")
  done < <(jq -r 'keys[]' <<<"${CUSTOM_INPUTS}")
elif [[ "${TRIGGER}" != "label" || -z "${LABEL}" ]]; then
  echo "optional-dispatch-plan: invalid trigger metadata for ${COMMAND}" >&2
  exit 1
elif [[ "$(jq 'length' <<<"${CUSTOM_INPUTS}")" -ne 0 ]]; then
  echo "optional-dispatch-plan: custom inputs require a workflow_dispatch trigger" >&2
  exit 1
fi

if ((${#DISPATCH_ARGS[@]} > 0)); then
  DISPATCH_ARGS_JSON=$(printf '%s\n' "${DISPATCH_ARGS[@]}" | jq -Rsc 'split("\n")[:-1]')
else
  DISPATCH_ARGS_JSON='[]'
fi
jq -cn \
  --arg workflow "${WORKFLOW}" \
  --arg marker "${MARKER}" \
  --arg default_branch "${DEFAULT_BRANCH}" \
  --arg trigger "${TRIGGER}" \
  --arg label "${LABEL}" \
  --argjson dispatch_args "${DISPATCH_ARGS_JSON}" \
  '{workflow: $workflow, marker: $marker, default_branch: $default_branch,
    trigger: $trigger, label: $label, dispatch_args: $dispatch_args}'

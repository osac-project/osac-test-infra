#!/usr/bin/env bash
#
# Replace an osac-installer submodule with a component source checkout.
#
# Ensures CRDs, RBAC, Helm templates, and the container image all come
# from the same commit when testing a component PR.
#
# Expected environment variables (set via GITHUB_ENV by the build step):
#   COMPONENT_REPO_NAME  — GitHub repo (owner/name) of the component
#   COMPONENT_REF_NAME   — Branch or tag that was built
#
# Usage:
#   bash .github/scripts/replace-installer-submodule.sh <installer-dir> <component-src-dir>

set -euo pipefail

INSTALLER_DIR="${1:?Usage: $0 <installer-dir> <component-src-dir>}"
COMPONENT_SRC="${2:?Usage: $0 <installer-dir> <component-src-dir>}"

if [[ ! -d "${COMPONENT_SRC}" ]]; then
  exit 0
fi

REPO_NAME="${COMPONENT_REPO_NAME##*/}"

# Explicit repo-name -> osac-installer submodule directory mapping.
# Previously this matched via `find base/ -name "*${REPO_NAME}"`, which relied
# on submodule directories happening to end in the repo's name (base/osac-operator,
# base/osac-fulfillment-service, ...). That breaks silently for any repo name that
# isn't a suffix of its submodule dir -- confirmed it would match zero directories
# for a new mono-repo consolidation target ("osac"), which would have caused E2E
# to silently test the stale submodule pin instead of the PR's changes.
# Update this map whenever submodules are added, renamed, or removed.
declare -A SUBMODULE_MAP=(
  [osac-operator]="osac-operator"
  [fulfillment-service]="osac-fulfillment-service"
  [osac-aap]="osac-aap"
  [bare-metal-fulfillment-operator]="bare-metal-fulfillment-operator"
  [osac-ui]="osac-ui"
)

SUBMODULE_NAME="${SUBMODULE_MAP[${REPO_NAME}]:-}"

if [[ -z "${SUBMODULE_NAME}" ]]; then
  echo "WARNING: no submodule mapping for repo '${REPO_NAME}' -- add it to SUBMODULE_MAP in this script" >&2
elif [[ ! -d "${INSTALLER_DIR}/base/${SUBMODULE_NAME}" ]]; then
  echo "WARNING: mapped submodule dir 'base/${SUBMODULE_NAME}' does not exist for repo '${REPO_NAME}'" >&2
else
  MATCH="${INSTALLER_DIR}/base/${SUBMODULE_NAME}"
  echo "Replacing submodule ${MATCH} with component source (${COMPONENT_REPO_NAME}@${COMPONENT_REF_NAME})..."
  # Stage into a sibling temp dir first and verify before touching the
  # existing submodule -- rm -rf then cp -a left a window where a failed
  # or partial copy would lose the original with nothing in its place.
  STAGING=$(mktemp -d "${INSTALLER_DIR}/base/.${SUBMODULE_NAME}.staging.XXXXXX")
  trap 'rm -rf "${STAGING}"' EXIT
  cp -a "${COMPONENT_SRC}/." "${STAGING}/"
  rm -rf "${MATCH}"
  mv "${STAGING}" "${MATCH}"
  trap - EXIT
fi

rm -rf "${COMPONENT_SRC}"

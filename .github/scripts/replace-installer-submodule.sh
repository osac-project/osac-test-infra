#!/usr/bin/env bash
#
# Replace an osac-installer submodule with a component source checkout.
#
# Ensures CRDs, RBAC, Helm templates, and the container image all come
# from the same commit when testing a component PR.
#
# Expected environment variables:
#   COMPONENT_REPO_NAME  — GitHub repo (owner/name) of the component
#   COMPONENT_REF_NAME   — Branch or tag that was built
#
# Usage:
#   bash .github/scripts/replace-installer-submodule.sh <installer-dir> <component-src-dir> [component-containerfile]
#
# The optional third argument is the containerfile path used to build this
# component (e.g. "Containerfile" for a standalone single-component repo, or
# "fulfillment-service/Containerfile" when the component is built from a
# monorepo checkout). When it has a directory component, that directory name
# both identifies which submodule to replace (COMPONENT_REPO_NAME alone can't,
# since a monorepo caller passes the same repo name, e.g. "osac-project/osac",
# for every component it builds) and scopes the copy to that subdirectory of
# <component-src-dir> instead of the whole checkout.

set -euo pipefail

INSTALLER_DIR="${1:?Usage: $0 <installer-dir> <component-src-dir> [component-containerfile]}"
COMPONENT_SRC="${2:?Usage: $0 <installer-dir> <component-src-dir> [component-containerfile]}"
COMPONENT_CONTAINERFILE="${3:-Containerfile}"

if [[ ! -d "${COMPONENT_SRC}" ]]; then
  exit 0
fi

COMPONENT_SUBDIR=""
if [[ "${COMPONENT_CONTAINERFILE}" == */* ]]; then
  COMPONENT_SUBDIR="$(dirname "${COMPONENT_CONTAINERFILE}")"
  # First path segment only, for the SUBMODULE_MAP lookup below -- a
  # containerfile can be nested more than one level deep (e.g.
  # "osac-aap/execution-environment/execution-environment.yaml"), but the
  # submodule it belongs to is always named after the top-level component
  # directory. COMPONENT_SUBDIR itself keeps the full path: it's used later
  # to scope which subdirectory gets copied, and that really is nested.
  REPO_NAME="${COMPONENT_SUBDIR%%/*}"
else
  REPO_NAME="${COMPONENT_REPO_NAME##*/}"
fi

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

# A miss here must be fatal, not a warning: silently continuing would leave
# osac-installer's stale submodule pin in place, so the e2e run would test
# old code instead of the PR's changes while still reporting success.
if [[ -z "${SUBMODULE_NAME}" ]]; then
  echo "ERROR: no submodule mapping for repo '${REPO_NAME}' -- add it to SUBMODULE_MAP in this script" >&2
  exit 1
fi

if [[ ! -d "${INSTALLER_DIR}/base/${SUBMODULE_NAME}" ]]; then
  echo "ERROR: mapped submodule dir 'base/${SUBMODULE_NAME}' does not exist for repo '${REPO_NAME}'" >&2
  exit 1
fi

MATCH="${INSTALLER_DIR}/base/${SUBMODULE_NAME}"
# When COMPONENT_SRC is a monorepo checkout, only the component's own
# subdirectory belongs in the submodule -- copying the whole checkout would
# put every other component's code inside this one submodule too.
COPY_SRC="${COMPONENT_SRC}"
if [[ -n "${COMPONENT_SUBDIR}" ]]; then
  COPY_SRC="${COMPONENT_SRC}/${COMPONENT_SUBDIR}"
fi

echo "Replacing submodule ${MATCH} with component source (${COMPONENT_REPO_NAME}@${COMPONENT_REF_NAME})..."
# Stage into a sibling temp dir first and verify before touching the
# existing submodule -- rm -rf then cp -a left a window where a failed
# or partial copy would lose the original with nothing in its place.
STAGING=$(mktemp -d "${INSTALLER_DIR}/base/.${SUBMODULE_NAME}.staging.XXXXXX")
trap 'rm -rf "${STAGING}"' EXIT
cp -a "${COPY_SRC}/." "${STAGING}/"
rm -rf "${MATCH}"
mv "${STAGING}" "${MATCH}"
trap - EXIT

rm -rf "${COMPONENT_SRC}"

#!/usr/bin/env bash
# Detect whether the cloned OSAC installer has the OSAC-5228 brownfield
# EXTERNAL_AAP / externalAap knobs.
#
# Usage: detect-external-aap-installer.sh INSTALLER_CLONE
#
# INSTALLER_CLONE is either the monorepo root (contains osac-installer/) or
# the osac-installer directory itself.
#
# Always exits 0 when the clone is readable so CI can merge this test-infra
# change before the installer PR. Missing knobs print
# "no such parameter in the installer" and set supported=false.
# Present knobs print that they will be used and set supported=true.
#
# Writes supported=true|false to $GITHUB_OUTPUT when that file is set.
# Also prints SUPPORTED=true|false on stdout for local tests.
set -euo pipefail

usage() {
  echo "Usage: $0 INSTALLER_CLONE" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage

INSTALLER_CLONE=$1
[[ -d "${INSTALLER_CLONE}" ]] || {
  echo "detect-external-aap-installer: not a directory: ${INSTALLER_CLONE}" >&2
  exit 2
}

resolve_installer_dir() {
  local clone=$1
  if [[ -f "${clone}/osac-installer/Makefile" ]]; then
    echo "${clone}/osac-installer"
    return
  fi
  if [[ -f "${clone}/Makefile" ]]; then
    echo "${clone}"
    return
  fi
  echo ""
}

log() {
  echo "$*" >&2
}

set_supported() {
  local value=$1
  echo "SUPPORTED=${value}"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "supported=${value}" >>"${GITHUB_OUTPUT}"
  fi
}

INSTALLER_DIR=$(resolve_installer_dir "${INSTALLER_CLONE}")
if [[ -z "${INSTALLER_DIR}" ]]; then
  log "no such parameter in the installer"
  log "Could not find osac-installer/Makefile (or Makefile) under ${INSTALLER_CLONE}."
  log "Skipping OSAC install so this test does not fail."
  set_supported false
  exit 0
fi

MISSING=()
MAKEFILE="${INSTALLER_DIR}/Makefile"
OVERLAY="${INSTALLER_DIR}/values/examples/external-aap.yaml"
VALUES="${INSTALLER_DIR}/charts/osac/values.yaml"

if [[ ! -f "${MAKEFILE}" ]] || ! grep -qE '^EXTERNAL_AAP' "${MAKEFILE}"; then
  MISSING+=("Make EXTERNAL_AAP")
fi
if [[ ! -f "${OVERLAY}" ]]; then
  MISSING+=("values/examples/external-aap.yaml")
fi
if [[ ! -f "${VALUES}" ]] || ! grep -q 'externalAap:' "${VALUES}"; then
  MISSING+=("charts/osac/values.yaml externalAap")
fi

if ((${#MISSING[@]} > 0)); then
  log "no such parameter in the installer"
  log "Missing brownfield AAP knobs: ${MISSING[*]}"
  log "Skipping OSAC install so this test does not fail until the installer PR lands."
  set_supported false
  exit 0
fi

log "installer EXTERNAL_AAP / externalAap parameters exist"
log "Will install a standalone AAP after the AAP operator, then pass the URL to make install-osac EXTERNAL_AAP=true"
set_supported true
exit 0

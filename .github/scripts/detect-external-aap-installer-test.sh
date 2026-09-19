#!/usr/bin/env bash
# Unit tests for detect-external-aap-installer.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT="${SCRIPT_DIR}/detect-external-aap-installer.sh"

pass=0
fail=0

assert_eq() {
  local name=$1 expected=$2 actual=$3
  if [[ "${expected}" == "${actual}" ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name} (expected=${expected} actual=${actual})"
    fail=$((fail + 1))
  fi
}

assert_contains() {
  local name=$1 needle=$2 haystack=$3
  if [[ "${haystack}" == *"${needle}"* ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name} (missing '${needle}')"
    fail=$((fail + 1))
  fi
}

WORKDIR=$(mktemp -d)
trap 'rm -rf "${WORKDIR}"' EXIT

run_detect() {
  local clone=$1
  "${DETECT}" "${clone}" 2>"${WORKDIR}/stderr" || true
}

# Empty tree: no Makefile
mkdir -p "${WORKDIR}/empty"
out=$(run_detect "${WORKDIR}/empty")
err=$(cat "${WORKDIR}/stderr")
assert_eq "empty tree is unsupported" "SUPPORTED=false" "${out}"
assert_contains "empty tree prints skip phrase" "no such parameter in the installer" "${err}"

# Monorepo layout, Makefile without EXTERNAL_AAP
mkdir -p "${WORKDIR}/partial/osac-installer/charts/osac" "${WORKDIR}/partial/osac-installer/values/examples"
printf 'install-osac:\n\t@echo hi\n' >"${WORKDIR}/partial/osac-installer/Makefile"
printf 'aap:\n  aap:\n    instance:\n      enabled: true\n' >"${WORKDIR}/partial/osac-installer/charts/osac/values.yaml"
out=$(run_detect "${WORKDIR}/partial")
err=$(cat "${WORKDIR}/stderr")
assert_eq "partial installer is unsupported" "SUPPORTED=false" "${out}"
assert_contains "partial prints skip phrase" "no such parameter in the installer" "${err}"
assert_contains "partial lists missing Make flag" "Make EXTERNAL_AAP" "${err}"

# Full brownfield knobs, standalone installer dir
mkdir -p "${WORKDIR}/full/charts/osac" "${WORKDIR}/full/values/examples"
cat >"${WORKDIR}/full/Makefile" <<'EOF'
EXTERNAL_AAP ?=
install-osac:
	@echo hi
EOF
cat >"${WORKDIR}/full/charts/osac/values.yaml" <<'EOF'
global:
  externalAap:
    enabled: false
aap:
  externalAap:
    enabled: false
EOF
cat >"${WORKDIR}/full/values/examples/external-aap.yaml" <<'EOF'
global:
  externalAap:
    enabled: true
EOF
out=$(run_detect "${WORKDIR}/full")
err=$(cat "${WORKDIR}/stderr")
assert_eq "full installer is supported" "SUPPORTED=true" "${out}"
assert_contains "full prints that params exist" "parameters exist" "${err}"

# Same knobs under osac-installer/ (monorepo)
mkdir -p "${WORKDIR}/mono/osac-installer"
cp -a "${WORKDIR}/full/." "${WORKDIR}/mono/osac-installer/"
out=$(run_detect "${WORKDIR}/mono")
assert_eq "monorepo layout is supported" "SUPPORTED=true" "${out}"

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]

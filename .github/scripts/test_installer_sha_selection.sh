#!/bin/bash
# Tests for the installer clone SHA selection logic in the four E2E
# reusable workflows (OSAC-5356).
#
# This test EXTRACTS and EXECUTES the actual shell code from each
# workflow YAML file's "Install infrastructure operators" step rather
# than reimplementing the logic.  A python3+PyYAML helper pulls the
# run: block, slices the SHA-selection portion (INSTALLER_CLONE_DIR=
# through the PINNED_SHA/CLONE_REF fallback), and wraps it so the test
# can inspect the selected ref without running git/oc/helm.
#
# Dependencies: bash ≥4, python3, PyYAML, jq
# CI: registered in .github/workflows/ci.yml script-tests job
#
# Run:  bash .github/scripts/test_installer_sha_selection.sh
set -euo pipefail

PASS=0; FAIL=0; TOTAL=0; ERRORS=""

run() {
  local name="$1" expected="$2" actual="$3"
  TOTAL=$((TOTAL + 1))
  if [[ "$actual" == "$expected" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    ERRORS+="FAIL: ${name}\n  expected: '${expected}'\n  actual:   '${actual}'\n"
  fi
}

# ── extract_selection WORKFLOW_FILE ──
# Outputs a bash script that runs the actual jq + fallback selection
# from the workflow, then prints the selected clone ref.
extract_selection() {
  local workflow_file="$1"
  python3 - "$workflow_file" << 'PYEOF'
import sys, yaml

wf_file = sys.argv[1]
with open(wf_file) as fp:
    wf = yaml.safe_load(fp)

run_block = None
for jn, job in wf.get("jobs", {}).items():
    for step in job.get("steps", []):
        if step.get("name", "") == "Install infrastructure operators":
            run_block = step["run"]
            break
    if run_block:
        break

if not run_block:
    print("echo 'ERROR: step not found'; exit 1")
    sys.exit(0)

lines = run_block.split("\n")

# Find start: first line containing INSTALLER_CLONE_DIR=
start = None
for i, line in enumerate(lines):
    if "INSTALLER_CLONE_DIR" in line and "=" in line:
        start = i
        break

# Find end: the line with 'if [[ -n "${PINNED_SHA}"' (bmaas/caas/regression)
# or the line with 'echo "Cloning' (vmaas uses CLONE_REF before that).
# We want the selection logic only, not the git clone branching.
end = None
for i in range(start + 1, len(lines)):
    s = lines[i].strip()
    # Stop BEFORE the clone-decision branch or the Cloning echo
    if s.startswith('if [[ -n "${PINNED_SHA}"'):
        end = i
        break
    if s.startswith('echo "Cloning') or s.startswith("echo 'Cloning"):
        end = i
        break

if start is None or end is None:
    print("echo 'ERROR: boundaries not found'; exit 1")
    sys.exit(0)

selection = "\n".join(lines[start:end])
print(selection)
print("")
print("# --- test harness: emit the selected ref ---")
# CLONE_REF is set by vmaas; bmaas/caas/regression set PINNED_SHA only
print('if [[ -n "${CLONE_REF:-}" ]]; then')
print('  printf "%s" "${CLONE_REF}"')
print('elif [[ -n "${PINNED_SHA:-}" ]]; then')
print('  printf "%s" "${PINNED_SHA}"')
print('else')
print('  printf "%s" "${INSTALLER_REF}"')
print('fi')
PYEOF
}

# ── run_selection WORKFLOW_FILE env_key=val ... ──
run_selection() {
  local wf="$1"; shift
  local script
  script=$(extract_selection "$wf")
  # RUNNER_TEMP is referenced by the extracted code for INSTALLER_CLONE_DIR;
  # provide a harmless default so set -u does not abort.
  env -i PATH="$PATH" HOME="${HOME:-/tmp}" RUNNER_TEMP="/tmp" "$@" \
    bash -c "set -euo pipefail; $script" 2>/dev/null
}

SHA_MAIN="aaaa1111bbbb2222cccc3333dddd4444eeee5555"
SHA_COMP="bbbb2222cccc3333dddd4444eeee5555ffff6666"
OSAC="osac-project/osac"
OTHER="other/installer"
SYNTHETIC='[{"image":"localhost/fs:1","imageKey":"service.images.service.repository","repo":"osac-project/osac","ref":"main","sha":"aaaa1111bbbb2222cccc3333dddd4444eeee5555","containerfile":"fs/Containerfile","crdsDir":"","source":"osac-5356"}]'
REAL_COMP='[{"image":"localhost/fs:2","imageKey":"service.images.service.repository","repo":"osac-project/osac","ref":"feat","sha":"bbbb2222cccc3333dddd4444eeee5555ffff6666","containerfile":"fs/Containerfile","crdsDir":""}]'
EMPTY="[]"

# ── Per-workflow scenarios ──
WORKFLOWS=(
  ".github/workflows/e2e-bmaas-full-install.yml"
  ".github/workflows/e2e-caas-full-install.yml"
  ".github/workflows/e2e-full-regression.yml"
  ".github/workflows/e2e-vmaas-full-install.yml"
)

for wf in "${WORKFLOWS[@]}"; do
  tag=$(basename "$wf" .yml | sed 's/e2e-//;s/-full-install//;s/-full-//')
  echo "── ${tag} ──"

  # S1: Default no-build — matching refs, synthetic results → pinned SHA
  result=$(run_selection "$wf" \
    COMPONENT_BUILD_RESULTS="$SYNTHETIC" \
    INSTALLER_REPO="$OSAC" INSTALLER_REF="main" \
    TEST_REPOSITORY="$OSAC" TEST_REF="main" \
    PINNED_OSAC_SHA="$SHA_MAIN")
  run "${tag}/S1 default: pinned SHA" "$SHA_MAIN" "$result"

  # S2: Explicit different installer-ref — synthetic must be skipped
  result=$(run_selection "$wf" \
    COMPONENT_BUILD_RESULTS="$SYNTHETIC" \
    INSTALLER_REPO="$OSAC" INSTALLER_REF="release-1.0" \
    TEST_REPOSITORY="$OSAC" TEST_REF="main" \
    PINNED_OSAC_SHA="$SHA_MAIN")
  run "${tag}/S2 diff ref: installer-ref honored" "release-1.0" "$result"

  # S3: Different installer-repo
  result=$(run_selection "$wf" \
    COMPONENT_BUILD_RESULTS="$SYNTHETIC" \
    INSTALLER_REPO="$OTHER" INSTALLER_REF="main" \
    TEST_REPOSITORY="$OSAC" TEST_REF="main" \
    PINNED_OSAC_SHA="$SHA_MAIN")
  run "${tag}/S3 diff repo: installer-ref" "main" "$result"

  # S4: Real caller component (no source marker)
  result=$(run_selection "$wf" \
    COMPONENT_BUILD_RESULTS="$REAL_COMP" \
    INSTALLER_REPO="$OSAC" INSTALLER_REF="main" \
    TEST_REPOSITORY="$OSAC" TEST_REF="main" \
    PINNED_OSAC_SHA="")
  run "${tag}/S4 real comp SHA" "$SHA_COMP" "$result"

  # S5: Empty results, matching refs → PINNED_OSAC_SHA fallback
  result=$(run_selection "$wf" \
    COMPONENT_BUILD_RESULTS="$EMPTY" \
    INSTALLER_REPO="$OSAC" INSTALLER_REF="main" \
    TEST_REPOSITORY="$OSAC" TEST_REF="main" \
    PINNED_OSAC_SHA="$SHA_MAIN")
  run "${tag}/S5 empty+match: fallback" "$SHA_MAIN" "$result"

  # S6: Empty results, differing refs → installer-ref
  result=$(run_selection "$wf" \
    COMPONENT_BUILD_RESULTS="$EMPTY" \
    INSTALLER_REPO="$OSAC" INSTALLER_REF="release-1.0" \
    TEST_REPOSITORY="$OSAC" TEST_REF="main" \
    PINNED_OSAC_SHA="$SHA_MAIN")
  run "${tag}/S6 empty+diff: installer-ref" "release-1.0" "$result"

  echo ""
done

# ── Structural: resolve-sha not gated on installer-ref ──
echo "── build-active checks ──"
for wf in "${WORKFLOWS[@]}"; do
  tag=$(basename "$wf" .yml | sed 's/e2e-//;s/-full-install//;s/-full-//')
  cond=$(python3 -c "
import yaml
with open('$wf') as fp:
    w = yaml.safe_load(fp)
for jn, job in w.get('jobs',{}).items():
    for step in job.get('steps',[]):
        if step.get('id','') == 'resolve-sha':
            print(step.get('if',''))
            break
")
  if echo "$cond" | grep -q 'installer-ref'; then
    run "${tag}/build-active" "no-gate" "GATED"
  else
    run "${tag}/build-active" "no-gate" "no-gate"
  fi
done

# ── Structural: Helm override loop ignores .source ──
echo ""
echo "── marker-safe checks ──"
for wf in "${WORKFLOWS[@]}"; do
  tag=$(basename "$wf" .yml | sed 's/e2e-//;s/-full-install//;s/-full-//')
  safe=$(python3 -c "
import yaml
with open('$wf') as fp:
    w = yaml.safe_load(fp)
for jn, job in w.get('jobs',{}).items():
    for step in job.get('steps',[]):
        if step.get('name','') == 'Install infrastructure operators':
            run_block = step.get('run','')
            # Helm loop: from MERGED_OVERRIDES to endgroup
            in_helm = False
            for line in run_block.split('\n'):
                if 'MERGED_OVERRIDES' in line and 'IMAGE_OVERRIDES' in line:
                    in_helm = True
                if in_helm and '.source' in line:
                    print('READS_SOURCE'); break
            else:
                print('OK')
            break
")
  run "${tag}/marker-safe" "OK" "$safe"
done

echo ""
if [[ $FAIL -gt 0 ]]; then
  echo "── FAILURES ──"
  printf '%b' "$ERRORS"
fi
echo "── Results: ${PASS}/${TOTAL} passed, ${FAIL} failed ──"
exit "${FAIL}"

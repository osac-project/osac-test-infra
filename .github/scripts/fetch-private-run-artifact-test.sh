#!/usr/bin/env bash
# Tests for fetch-private-run-artifact.sh with a stubbed gh.
set -uo pipefail

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fetch-private-run-artifact.sh"
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

# The stub answers listings from $WORK/list-<name> (its content is the id, or
# empty for none, or the word FAIL) and downloads from $WORK/zip-<id>.
cat > "${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
echo "$args" >> "${WORK}/calls"
if [[ "${args}" == *"/actions/artifacts/"*"/zip"* ]]; then
  id=${args##*artifacts/}; id=${id%%/*}
  [[ -f "${WORK}/zip-${id}" ]] || exit 1
  cat "${WORK}/zip-${id}"
  exit 0
fi
name=""
while [[ $# -gt 0 ]]; do
  if [[ $1 == name=* ]]; then name=${1#name=}; fi
  shift
done
asks=$(( $(cat "${WORK}/asks-${name}" 2>/dev/null || echo 0) + 1 ))
echo "${asks}" > "${WORK}/asks-${name}"
content=$(cat "${WORK}/list-${name}" 2>/dev/null || true)
# list-<name>.from-<n>: the artifact shows up from the n-th ask.
for late in "${WORK}/list-${name}.from-"*; do
  [[ -f "${late}" ]] || continue
  from=${late##*.from-}
  if (( asks >= from )); then content=$(cat "${late}"); fi
done
[[ "${content}" == FAIL ]] && exit 1
printf '%s' "${content}"
STUB
chmod +x "${WORK}/bin/gh" || exit 1
# sleep is recorded, not slept.
cat > "${WORK}/bin/sleep" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "${WORK}/sleeps"
STUB
chmod +x "${WORK}/bin/sleep" || exit 1
export WORK PATH="${WORK}/bin:${PATH}"

failures=0
check() { # description, expected, actual
  if [[ "$2" != "$3" ]]; then
    echo "FAIL: $1: expected '$2', got '$3'"
    failures=$((failures + 1))
  else
    echo "ok: $1"
  fi
}
reset() { rm -f "${WORK}"/list-* "${WORK}"/asks-* "${WORK}"/zip-* "${WORK}/calls" "${WORK}/out.zip" "${WORK}/sleeps" "${WORK}/asks"; }

reset
printf 'ZIPDATA' > "${WORK}/zip-77"
echo 77 > "${WORK}/list-osac-logs-full-install-5"
out=$("${SCRIPT}" org/priv "${WORK}/out.zip" osac-logs-5 osac-logs-full-install-5 osac-logs-bmaas-full-install-5 2>/dev/null); rc=$?
check "finds the first name that has an artifact" "0 osac-logs-full-install-5 77" "${rc} ${out}"
check "downloads its bytes" "ZIPDATA" "$(cat "${WORK}/out.zip")"
check "stops after the match" "no" "$(grep -q bmaas "${WORK}/calls" && echo yes || echo no)"
reset
"${SCRIPT}" org/priv "${WORK}/out.zip" a b >/dev/null 2>&1; rc=$?
check "exit 3 when nothing is published" "3" "${rc}"
check "no file left" "no" "$([[ -e "${WORK}/out.zip" ]] && echo yes || echo no)"

reset
echo FAIL > "${WORK}/list-a"
"${SCRIPT}" org/priv "${WORK}/out.zip" a b >/dev/null 2>"${WORK}/err"; rc=$?
check "exit 4 when a lookup fails and nothing else is found" "4" "${rc}"
check "the failure is reported" "yes" "$(grep -q 'Could not list a' "${WORK}/err" && echo yes || echo no)"

reset
echo FAIL > "${WORK}/list-a"
echo 9 > "${WORK}/list-b"; printf 'B' > "${WORK}/zip-9"
out=$("${SCRIPT}" org/priv "${WORK}/out.zip" a b 2>/dev/null); rc=$?
check "a failed lookup does not hide a later match" "0 b 9" "${rc} ${out}"

reset
echo 5 > "${WORK}/list-a"
"${SCRIPT}" org/priv "${WORK}/out.zip" a >/dev/null 2>"${WORK}/err"; rc=$?
check "exit 4 when the download fails" "4" "${rc}"
check "no partial file" "no" "$([[ -e "${WORK}/out.zip" ]] && echo yes || echo no)"

reset
"${SCRIPT}" org/priv >/dev/null 2>&1; rc=$?
check "exit 2 on bad usage" "2" "${rc}"

# --- waiting for a collector that has not uploaded yet
reset
echo 5 > "${WORK}/list-a.from-3"; printf 'LATE' > "${WORK}/zip-5"
out=$(FETCH_WAIT_SECONDS=60 FETCH_WAIT_INTERVAL=15 "${SCRIPT}" org/priv "${WORK}/out.zip" a 2>/dev/null); rc=$?
check "waits until the artifact appears" "0 a 5" "${rc} ${out}"
check "asked three times, slept twice" "2" "$(wc -l < "${WORK}/sleeps" | tr -d ' ')"
check "slept the interval" "15" "$(head -1 "${WORK}/sleeps")"

reset
FETCH_WAIT_SECONDS=30 FETCH_WAIT_INTERVAL=10 "${SCRIPT}" org/priv "${WORK}/out.zip" a >/dev/null 2>&1; rc=$?
check "gives up with 3 when the wait is over" "3" "${rc}"
check "the wait is bounded: 30s at 10s is four asks, three sleeps" "3" "$(wc -l < "${WORK}/sleeps" | tr -d ' ')"

reset
"${SCRIPT}" org/priv "${WORK}/out.zip" a >/dev/null 2>&1; rc=$?
check "no wait by default: one ask, no sleep" "3 no" "${rc} $([[ -e "${WORK}/sleeps" ]] && echo yes || echo no)"

reset
echo FAIL > "${WORK}/list-a"
FETCH_WAIT_SECONDS=60 "${SCRIPT}" org/priv "${WORK}/out.zip" a >/dev/null 2>&1; rc=$?
check "a failed lookup is not waited for" "4 no" "${rc} $([[ -e "${WORK}/sleeps" ]] && echo yes || echo no)"

reset
FETCH_WAIT_SECONDS=abc FETCH_WAIT_INTERVAL=0 "${SCRIPT}" org/priv "${WORK}/out.zip" a >/dev/null 2>&1; rc=$?
check "a bad wait setting means no wait" "3 no" "${rc} $([[ -e "${WORK}/sleeps" ]] && echo yes || echo no)"

if [[ ${failures} -gt 0 ]]; then
  echo "${failures} check(s) failed"
  exit 1
fi
echo "all checks passed"

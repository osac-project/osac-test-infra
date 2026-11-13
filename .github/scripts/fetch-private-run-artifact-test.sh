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
content=$(cat "${WORK}/list-${name}" 2>/dev/null || true)
[[ "${content}" == FAIL ]] && exit 1
printf '%s' "${content}"
STUB
chmod +x "${WORK}/bin/gh"
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
reset() { rm -f "${WORK}"/list-* "${WORK}"/zip-* "${WORK}/calls" "${WORK}/out.zip"; }

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

if [[ ${failures} -gt 0 ]]; then
  echo "${failures} check(s) failed"
  exit 1
fi
echo "all checks passed"

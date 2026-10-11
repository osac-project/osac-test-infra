#!/usr/bin/env bash
# Tests for the "decided" mode of e2e-start.sh, run against a stand-in `gh`: a decision made by the caller replaces the
# CodeRabbit-only approval proof, and nothing else is relaxed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"

# The stand-in gh answers from files in $STUB_DIR and logs every call to $STUB_DIR/calls.
cat > "${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "${STUB_DIR}/calls"
filter=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "--jq" ]]; then filter="${args[$((i + 1))]}"; fi
done
case "$*" in
  "api repos/o/r/pulls/5") cat "${STUB_DIR}/pr.json" ;;
  *"--slurp repos/o/r/pulls/5/reviews"*) echo "[$(cat "${STUB_DIR}/reviews.json")]" ;;
  *"repos/o/r/commits/"*"/check-runs"*) echo '{"check_runs":[]}' ;;
  *"repos/o/r/actions/workflows/"*"/runs"*)
    jq -c "${filter}" <<<'{"workflow_runs":[{"id":77,"head_sha":"'"${HEAD}"'","pull_requests":[{"number":5}]}]}' ;;
  "run view 77 "*)
    echo '{"status":"completed","conclusion":"success","jobs":[
      {"name":"changes","status":"completed","conclusion":"success"},
      {"name":"e2e-readiness (vmaas)","status":"completed","conclusion":"success"},
      {"name":"e2e-vmaas-full-install","status":"completed","conclusion":"skipped"}]}' ;;
  "run rerun 77 "*) ;;
  "pr comment "*) ;;
  *) echo "stand-in gh: no answer for: $*" >&2; exit 9 ;;
esac
STUB
chmod +x "${WORK}/bin/gh"

pass=0
fail=0
check() {
  local name="$1" ok="$2"
  if [[ "${ok}" == "yes" ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name}"
    fail=$((fail + 1))
  fi
}

# run_case <state> <head> <reviews-json> <event-head> <skip_label_check> <decided>; sets RC, CALLS, OUT
run_case() {
  local state="$1" head="$2" reviews="$3" event_head="$4" skip="$5" decided="$6"
  export STUB_DIR="${WORK}/case"
  rm -rf "${STUB_DIR}"
  mkdir -p "${STUB_DIR}"
  : > "${STUB_DIR}/calls"
  echo "{\"state\":\"${state}\",\"head\":{\"sha\":\"${head}\",\"ref\":\"fix\",\"repo\":{\"full_name\":\"f/r\"}}}" > "${STUB_DIR}/pr.json"
  echo "${reviews}" > "${STUB_DIR}/reviews.json"
  set +e
  OUT=$(PATH="${WORK}/bin:${PATH}" GH_TOKEN=x REPO=o/r PR_NUMBER=5 HEAD="${head}" EVENT_HEAD_SHA="${event_head}" \
    WORKFLOWS=e2e-vmaas-full-install.yml SKIP_LABEL_CHECK="${skip}" DECIDED="${decided}" \
    bash "${SCRIPT_DIR}/e2e-start.sh" 2>&1)
  RC=$?
  set -e
  CALLS=$(cat "${STUB_DIR}/calls")
}

reran() { grep -q "^run rerun 77 " <<<"${CALLS}" && echo yes || echo no; }
not_reran() { grep -q "^run rerun" <<<"${CALLS}" && echo no || echo yes; }

human_ok="[{\"id\":1,\"user\":{\"login\":\"alice\",\"type\":\"User\"},\"state\":\"APPROVED\",\"commit_id\":\"${SHA}\",\"submitted_at\":\"2026-01-01T00:00:00Z\"}]"
human_changes="[{\"id\":1,\"user\":{\"login\":\"alice\",\"type\":\"User\"},\"state\":\"APPROVED\",\"commit_id\":\"${SHA}\",\"submitted_at\":\"2026-01-01T00:00:00Z\"},
  {\"id\":2,\"user\":{\"login\":\"bob\",\"type\":\"User\"},\"state\":\"CHANGES_REQUESTED\",\"commit_id\":\"${SHA}\",\"submitted_at\":\"2026-01-01T01:00:00Z\"}]"
coderabbit_ok="[{\"id\":3,\"user\":{\"login\":\"coderabbitai[bot]\",\"type\":\"Bot\"},\"state\":\"APPROVED\",\"commit_id\":\"${SHA}\",\"submitted_at\":\"2026-01-01T00:00:00Z\"}]"
none="[]"

run_case open "${SHA}" "${human_ok}" "${SHA}" false true
check "decided: a human approval starts the e2e" "$([[ ${RC} -eq 0 ]] && reran || echo no)"
check "decided: the comment names an approval, not CodeRabbit" "$(grep -q 'E2E on approval' <<<"${CALLS}" && echo yes || echo no)"

run_case open "${SHA}" "${none}" "${SHA}" false true
check "decided: the decision stands without any review listed (the caller proved the approval)" "$([[ ${RC} -eq 0 ]] && reran || echo no)"

run_case open "${SHA}" "${human_changes}" "${SHA}" false true
check "decided: a human who requested changes since the decision stops the start" "$([[ ${RC} -eq 0 ]] && not_reran || echo no)"

run_case open "${SHA}" "${human_ok}" "" false true
check "decided without a head commit is an error and calls nothing" "$([[ ${RC} -eq 1 && -z ${CALLS} ]] && echo yes || echo no)"

run_case open "${OTHER}" "${human_ok}" "${SHA}" false true
check "decided: a head that moved after the decision starts nothing" "$([[ ${RC} -eq 0 ]] && not_reran || echo no)"

run_case closed "${SHA}" "${human_ok}" "${SHA}" false true
check "decided: a closed pull request starts nothing" "$([[ ${RC} -eq 0 ]] && not_reran || echo no)"

run_case open "${SHA}" "${coderabbit_ok}" "${SHA}" false true
check "decided: a CodeRabbit approval starts the e2e too" "$([[ ${RC} -eq 0 ]] && reran || echo no)"

# The old path is unchanged: without a decision only CodeRabbit's approval counts.
run_case open "${SHA}" "${human_ok}" "${SHA}" true false
check "not decided: a human approval alone still does not start the e2e" "$([[ ${RC} -eq 0 ]] && not_reran || echo no)"

run_case open "${SHA}" "${coderabbit_ok}" "${SHA}" true false
check "not decided: a CodeRabbit approval still starts the e2e" "$([[ ${RC} -eq 0 ]] && reran || echo no)"
check "not decided: the comment still names CodeRabbit" "$(grep -q 'E2E on CodeRabbit approval' <<<"${CALLS}" && echo yes || echo no)"

echo "${pass} passed, ${fail} failed"
[[ ${fail} -eq 0 ]]

#!/usr/bin/env bash
# Unit tests for check-e2e-readiness.sh decision helpers (OSAC-3370).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./check-e2e-readiness.sh
CHECK_E2E_READINESS_LIB_ONLY=1 source "${SCRIPT_DIR}/check-e2e-readiness.sh"

pass=0
fail=0

# Pass if expected equals actual; log name.
assert_eq() {
  local name="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "PASS: ${name}"
    pass=$((pass + 1))
  else
    echo "FAIL: ${name} (expected=${expected} actual=${actual})"
    fail=$((fail + 1))
  fi
}

# Run remaining args; pass if exit code equals expected_rc. Name is $1.
assert_rc() {
  local name="$1" expected_rc="$2"
  shift 2
  set +e
  "$@" >/dev/null 2>&1
  local rc=$?
  set -e
  assert_eq "${name}" "${expected_rc}" "${rc}"
}

LABELS_E2E_READY='[{"name":"e2e-ready"},{"name":"bug"}]'
LABELS_LGTM='[{"name":"lgtm"},{"name":"bug"}]'
LABELS_BOTH='[{"name":"lgtm"},{"name":"e2e-ready"}]'
LABELS_WITHOUT='[{"name":"bug"}]'
LABELS_EMPTY='[]'

HEAD_SHA='head-commit'

REVIEWS_BOT_WITH_HUMAN_CR='[
  {"id":1,"submitted_at":"2026-01-01T00:00:00Z","state":"CHANGES_REQUESTED","commit_id":"old-commit","author_association":"MEMBER","user":{"login":"alice","type":"User"}},
  {"id":2,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'

REVIEWS_BOT_ONLY='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'

REVIEWS_BOT_THEN_DISMISS='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}},
  {"id":2,"submitted_at":"2026-01-02T01:00:00Z","state":"DISMISSED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'

REVIEWS_HUMAN_APPROVED='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"MEMBER","user":{"login":"alice","type":"User"}}
]'

REVIEWS_OTHER_BOT_APPROVED='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"dependabot[bot]","type":"Bot"}}
]'

REVIEWS_COMMENT_AFTER_CR_BOT_APPROVE='[
  {"id":1,"submitted_at":"2026-01-01T00:00:00Z","state":"CHANGES_REQUESTED","commit_id":"old-commit","author_association":"MEMBER","user":{"login":"alice","type":"User"}},
  {"id":2,"submitted_at":"2026-01-02T00:00:00Z","state":"COMMENTED","commit_id":"head-commit","author_association":"MEMBER","user":{"login":"alice","type":"User"}},
  {"id":3,"submitted_at":"2026-01-02T00:00:01Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'

REVIEWS_APPROVED_OLD='[
  {"id":1,"submitted_at":"2026-01-01T00:00:00Z","state":"APPROVED","commit_id":"old-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'
REVIEWS_NONE='[]'

# --- issue events fixtures ---
EVENTS_E2E_READY_BY_BOT='[
  {"event":"labeled","label":{"name":"e2e-ready"},"actor":{"login":"github-actions[bot]","type":"Bot"}}
]'
EVENTS_E2E_READY_BY_HUMAN='[
  {"event":"labeled","label":{"name":"e2e-ready"},"actor":{"login":"bob","type":"User"}}
]'
EVENTS_E2E_READY_HUMAN_THEN_BOT='[
  {"event":"labeled","label":{"name":"e2e-ready"},"actor":{"login":"bob","type":"User"}},
  {"event":"labeled","label":{"name":"e2e-ready"},"actor":{"login":"github-actions[bot]","type":"Bot"}}
]'
EVENTS_NONE='[]'
EVENTS_LGTM_LABELED='[
  {"event":"labeled","label":{"name":"lgtm"},"actor":{"login":"alice","type":"User"}}
]'
EVENTS_LGTM_THEN_UNLABELED='[
  {"event":"labeled","label":{"name":"lgtm"},"actor":{"login":"alice","type":"User"}},
  {"event":"unlabeled","label":{"name":"lgtm"},"actor":{"login":"openshift-ci[bot]","type":"Bot"}}
]'
EVENTS_LGTM_UNLABELED_ONLY='[
  {"event":"unlabeled","label":{"name":"lgtm"},"actor":{"login":"openshift-ci[bot]","type":"Bot"}}
]'

REVIEWS_HUMAN_SAME_SECOND_CR_WINS='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"MEMBER","user":{"login":"alice","type":"User"}},
  {"id":2,"submitted_at":"2026-01-02T00:00:00Z","state":"CHANGES_REQUESTED","commit_id":"head-commit","author_association":"MEMBER","user":{"login":"alice","type":"User"}},
  {"id":3,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'
REVIEWS_BOT_SAME_SECOND_DISMISS_WINS='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}},
  {"id":2,"submitted_at":"2026-01-02T00:00:00Z","state":"DISMISSED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'
REVIEWS_BOT_SAME_SECOND_APPROVE_WINS='[
  {"id":1,"submitted_at":"2026-01-02T00:00:00Z","state":"DISMISSED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}},
  {"id":2,"submitted_at":"2026-01-02T00:00:00Z","state":"APPROVED","commit_id":"head-commit","author_association":"NONE","user":{"login":"coderabbitai[bot]","type":"Bot"}}
]'

# --- labels_have ---
assert_rc "labels_have_e2e_ready yes" 0 labels_have_e2e_ready "${LABELS_E2E_READY}"
assert_rc "labels_have_e2e_ready no" 1 labels_have_e2e_ready "${LABELS_WITHOUT}"
assert_rc "labels_have_lgtm yes" 0 labels_have_lgtm "${LABELS_LGTM}"
assert_rc "labels_have_lgtm no" 1 labels_have_lgtm "${LABELS_WITHOUT}"
assert_rc "labels_have_e2e_ready empty" 1 labels_have_e2e_ready "${LABELS_EMPTY}"

# --- human_has_changes_requested ---
assert_rc "human_has_changes_requested yes" 0 human_has_changes_requested "${REVIEWS_BOT_WITH_HUMAN_CR}"
assert_rc "human_has_changes_requested no (bot only)" 1 human_has_changes_requested "${REVIEWS_BOT_ONLY}"
assert_rc "human_has_changes_requested yes (comment after CR)" 0 human_has_changes_requested "${REVIEWS_COMMENT_AFTER_CR_BOT_APPROVE}"

# --- coderabbit_approves_head ---
assert_rc "coderabbit empty head_sha" 1 coderabbit_approves_head "${REVIEWS_BOT_ONLY}" ""
assert_rc "coderabbit approves bot only" 0 coderabbit_approves_head "${REVIEWS_BOT_ONLY}" "${HEAD_SHA}"
assert_rc "coderabbit blocked by human CR" 1 coderabbit_approves_head "${REVIEWS_BOT_WITH_HUMAN_CR}" "${HEAD_SHA}"
assert_rc "coderabbit comment after CR still blocked" 1 coderabbit_approves_head "${REVIEWS_COMMENT_AFTER_CR_BOT_APPROVE}" "${HEAD_SHA}"
assert_rc "coderabbit dismissed after approve" 1 coderabbit_approves_head "${REVIEWS_BOT_THEN_DISMISS}" "${HEAD_SHA}"
assert_rc "coderabbit old sha" 1 coderabbit_approves_head "${REVIEWS_APPROVED_OLD}" "${HEAD_SHA}"
assert_rc "coderabbit none" 1 coderabbit_approves_head "${REVIEWS_NONE}" "${HEAD_SHA}"
assert_rc "coderabbit short sha" 1 coderabbit_approves_head "${REVIEWS_BOT_ONLY}" "head"
assert_rc "human approve does not unlock" 1 coderabbit_approves_head "${REVIEWS_HUMAN_APPROVED}" "${HEAD_SHA}"
assert_rc "other bot does not unlock" 1 coderabbit_approves_head "${REVIEWS_OTHER_BOT_APPROVED}" "${HEAD_SHA}"
assert_rc "human same-second higher id CR blocks" 0 human_has_changes_requested "${REVIEWS_HUMAN_SAME_SECOND_CR_WINS}"
assert_rc "coderabbit blocked by same-second human CR" 1 coderabbit_approves_head "${REVIEWS_HUMAN_SAME_SECOND_CR_WINS}" "${HEAD_SHA}"
assert_rc "coderabbit same-second higher id dismiss" 1 coderabbit_approves_head "${REVIEWS_BOT_SAME_SECOND_DISMISS_WINS}" "${HEAD_SHA}"
assert_rc "coderabbit same-second higher id approve" 0 coderabbit_approves_head "${REVIEWS_BOT_SAME_SECOND_APPROVE_WINS}" "${HEAD_SHA}"

# --- pr_ever_had_lgtm ---
assert_rc "pr_ever_had_lgtm labeled" 0 pr_ever_had_lgtm "${EVENTS_LGTM_LABELED}"
assert_rc "pr_ever_had_lgtm labeled then unlabeled" 0 pr_ever_had_lgtm "${EVENTS_LGTM_THEN_UNLABELED}"
assert_rc "pr_ever_had_lgtm unlabeled only" 1 pr_ever_had_lgtm "${EVENTS_LGTM_UNLABELED_ONLY}"
assert_rc "pr_ever_had_lgtm none" 1 pr_ever_had_lgtm "${EVENTS_NONE}"

# --- e2e_ready_applied_by_trusted_actor ---
assert_rc "e2e-ready by bot trusted" 0 e2e_ready_applied_by_trusted_actor "${EVENTS_E2E_READY_BY_BOT}"
assert_rc "e2e-ready by human untrusted" 1 e2e_ready_applied_by_trusted_actor "${EVENTS_E2E_READY_BY_HUMAN}"
assert_rc "e2e-ready human then bot trusts last" 0 e2e_ready_applied_by_trusted_actor "${EVENTS_E2E_READY_HUMAN_THEN_BOT}"
assert_rc "e2e-ready no events untrusted" 1 e2e_ready_applied_by_trusted_actor "${EVENTS_NONE}"

# --- decide_e2e_readiness ---
assert_rc "decide lgtm present" 0 decide_e2e_readiness "${LABELS_LGTM}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_eq "decide lgtm reason" \
  "allowed: lgtm label present" \
  "$(decide_e2e_readiness "${LABELS_LGTM}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_rc "decide e2e-ready by bot" 0 decide_e2e_readiness "${LABELS_E2E_READY}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_E2E_READY_BY_BOT}"
assert_eq "decide e2e-ready by bot reason" \
  "allowed: e2e-ready label present (applied by trusted actor)" \
  "$(decide_e2e_readiness "${LABELS_E2E_READY}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_E2E_READY_BY_BOT}")"
assert_rc "decide e2e-ready by human denied" 1 decide_e2e_readiness "${LABELS_E2E_READY}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_E2E_READY_BY_HUMAN}"
assert_eq "decide e2e-ready by human reason" \
  "denied: e2e-ready label present but applied by untrusted actor" \
  "$(decide_e2e_readiness "${LABELS_E2E_READY}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_E2E_READY_BY_HUMAN}")"
assert_rc "decide both labels prefers lgtm" 0 decide_e2e_readiness "${LABELS_BOTH}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_eq "decide both labels prefers lgtm reason" \
  "allowed: lgtm label present" \
  "$(decide_e2e_readiness "${LABELS_BOTH}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_rc "decide lgtm overrides human CR" 0 decide_e2e_readiness "${LABELS_LGTM}" "${REVIEWS_BOT_WITH_HUMAN_CR}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_rc "decide historical lgtm after prow strip" 0 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_LGTM_THEN_UNLABELED}"
assert_eq "decide historical lgtm reason" \
  "allowed: lgtm was applied earlier" \
  "$(decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_LGTM_THEN_UNLABELED}")"
assert_rc "decide historical lgtm blocked by human CR" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_BOT_WITH_HUMAN_CR}" "${HEAD_SHA}" "${EVENTS_LGTM_THEN_UNLABELED}"
assert_rc "decide unlabeled-only is not historical lgtm" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_LGTM_UNLABELED_ONLY}"
assert_rc "decide e2e-ready by bot overrides human CR" 0 decide_e2e_readiness "${LABELS_E2E_READY}" "${REVIEWS_BOT_WITH_HUMAN_CR}" "${HEAD_SHA}" "${EVENTS_E2E_READY_BY_BOT}"
assert_rc "decide CR alone wins" 0 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_BOT_ONLY}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_eq "decide CR alone reason" \
  "allowed: APPROVED review on head from coderabbitai[bot]" \
  "$(decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_BOT_ONLY}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_rc "decide CR blocked by human CR" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_BOT_WITH_HUMAN_CR}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_rc "decide denied no signals" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_rc "decide denied human approve" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_HUMAN_APPROVED}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_rc "decide denied old CR approve" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_APPROVED_OLD}" "${HEAD_SHA}" "${EVENTS_NONE}"
assert_rc "decide denied other bot" 1 decide_e2e_readiness "${LABELS_WITHOUT}" "${REVIEWS_OTHER_BOT_APPROVED}" "${HEAD_SHA}" "${EVENTS_NONE}"

# --- explain_e2e_wait ---
assert_eq "wait no CR" \
  "waiting: no CR APPROVED on this SHA" \
  "$(explain_e2e_wait "${LABELS_WITHOUT}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_eq "wait stale CR SHA" \
  "waiting: CR APPROVED on older SHA old-com" \
  "$(explain_e2e_wait "${LABELS_WITHOUT}" "${REVIEWS_APPROVED_OLD}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_eq "wait human CR" \
  "waiting: human CHANGES_REQUESTED still open" \
  "$(explain_e2e_wait "${LABELS_WITHOUT}" "${REVIEWS_BOT_WITH_HUMAN_CR}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_eq "wait untrusted e2e-ready" \
  "denied: e2e-ready label present but applied by untrusted actor" \
  "$(explain_e2e_wait "${LABELS_E2E_READY}" "${REVIEWS_NONE}" "${HEAD_SHA}" "${EVENTS_E2E_READY_BY_HUMAN}")"
assert_eq "wait human CR beats stale SHA" \
  "waiting: human CHANGES_REQUESTED still open" \
  "$(explain_e2e_wait "${LABELS_WITHOUT}" "${REVIEWS_COMMENT_AFTER_CR_BOT_APPROVE}" "${HEAD_SHA}" "${EVENTS_NONE}")"
assert_rc "coderabbit_latest_approved_commit bot only" 0 coderabbit_latest_approved_commit "${REVIEWS_BOT_ONLY}"
assert_eq "coderabbit_latest_approved_commit sha" \
  "head-commit" \
  "$(coderabbit_latest_approved_commit "${REVIEWS_BOT_ONLY}")"
assert_rc "coderabbit_latest_approved_commit none" 1 coderabbit_latest_approved_commit "${REVIEWS_NONE}"

# --- write_ready_output ---
ready_out="$(mktemp)"
GITHUB_OUTPUT="${ready_out}" write_ready_output true
assert_eq "write_ready_output true" "ready=true" "$(cat "${ready_out}")"
GITHUB_OUTPUT="${ready_out}" write_ready_output false
assert_eq "write_ready_output false appends" $'ready=true\nready=false' "$(cat "${ready_out}")"
unset GITHUB_OUTPUT
write_ready_output true
assert_eq "write_ready_output no GITHUB_OUTPUT is no-op" $'ready=true\nready=false' "$(cat "${ready_out}")"
rm -f "${ready_out}"

echo ""
echo "Results: ${pass} passed, ${fail} failed"
if (( fail > 0 )); then
  exit 1
fi

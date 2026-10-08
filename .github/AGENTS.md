# .github/ Agent Context

## E2E readiness gate (OSAC-3370)

- Gate checks `lgtm` (present or previously applied), bot-applied `e2e-ready`, or CodeRabbit `APPROVED` on HEAD
- `lgtm` staleness: Prow removes on push; a prior apply still unlocks later SHAs unless a human has `CHANGES_REQUESTED`. `e2e-ready` staleness: cleanup workflow
- `/e2e-ready` applies the `e2e-ready` label and starts expensive e2e (GITHUB_TOKEN cannot trigger `labeled` workflows)
- `/ok-to-test` is fork secrets only. It does not unlock the cost gate.
- `.github/actions/check-e2e-readiness/` — Allow: `lgtm`, bot-applied `e2e-ready`, or `coderabbitai[bot]` APPROVED on head (blocked while a human still has `CHANGES_REQUESTED`, except present `lgtm` / bot `e2e-ready`). Otherwise `ready=false` and wait; do not fail. Human APPROVED does not unlock
- Full-install callers skip expensive e2e until `ready=true`; required `e2e-*-gate` stays pending
- `.github/workflows/e2e-on-label.yml` — Canonical starter (`lgtm` / `e2e-ready` only; `/ok-to-test` does not start e2e or POST gates). osac calls it via `workflow_call`. `/e2e-ready` `workflow_dispatch`es the per-repo wrapper. Replay bash: `.github/actions/e2e-start/` (never POST in_progress `e2e-*-gate` Checks API placeholders — they land on unrelated suites such as auto-queue / cancel-stale / ok-to-test cleanup and block merge. Required gates stay pending until native full-install `e2e-*-gate` jobs report)
- `.github/workflows/e2e-cancel-stale-runs-on-push.yml` — Cancels obsolete full-install runs only. Does not POST or dismiss `e2e-*-gate` checks (those grouped under this suite and looked like required gates).
- `.github/workflows/e2e-on-approval.yml` — CodeRabbit APPROVED: same-repo calls the starter; fork only `fork-handoff`.
- `.github/workflows/e2e-on-approval-fork.yml` — `workflow_run` replay after `fork-handoff`; verifies CR APPROVED on exact HEAD, then calls the starter. osac calls this via `workflow_call`.
- `.github/workflows/e2e-ready-label-cleanup.yml` — Removes `e2e-ready` on new pushes
- Suite selection — CaaS PR, merge queue, and dispatches carrying `pr-number` always run `caas/sanity` with `-m sanity`, ignoring tier labels and suite/marker overrides. CaaS periodics run `caas` (sanity + regression) plus applicable references. Manual CaaS dispatch without `pr-number` defaults to sanity and allows overrides. The reusable runner never appends references to sanity and fails on empty requested tiers rather than widening selection.
- Other platforms retain tier-label selection: `vmaas` defaults to the full suite; `bmaas/sanity` on PR, `bmaas/serial` on schedule. `e2e-regression` → `*/regression`; `e2e-serial` → `*/serial` (serial wins). Tier labels do **not** start e2e.
- Slash — `/e2e-regression` / `/e2e-serial` apply the tier label only. `/test vmaas` / `caas` / `bmaas` (from `.github/optional-workflows.yml`) dispatch the platform caller; VMaaS/BMaaS read PR labels, while CaaS stays sanity-only. Netris remains label-trigger only.

## Testing

| Area | Required coverage | Command | Boundary |
|---|---|---|---|
| CaaS caller and reusable selection | [DEV] Unit/Contract checks in osac | From the osac checkout: `OSAC_TEST_INFRA_DIR=/path/to/osac-test-infra uv run pytest -n 0 tests/unit/test_caas_ci_suite_policy.py` | Real caller YAML and path-selection Bash, temporary test directories; no deployed services or E2E execution. Tests remain in osac, not this repository. |

Run readiness and e2e-gate helper unit tests:

```bash
bash .github/actions/check-e2e-readiness/check-e2e-readiness-test.sh
bash .github/actions/invalidate-e2e-gates/e2e-gates-lib-test.sh
```

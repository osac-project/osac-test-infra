# .github/ Agent Context

## E2E readiness gate (OSAC-3370)

- Gate checks `lgtm` (present or previously applied), bot-applied `e2e-ready`, or CodeRabbit `APPROVED` on HEAD
- `lgtm` staleness: Prow removes on push; a prior apply still unlocks later SHAs unless a human has `CHANGES_REQUESTED`. `e2e-ready` staleness: cleanup workflow
- `/e2e-ready` applies the `e2e-ready` label and starts expensive e2e (GITHUB_TOKEN cannot trigger `labeled` workflows)
- `/ok-to-test` is fork secrets only. It does not unlock the cost gate.
- `.github/actions/check-e2e-readiness/` — Allow: `lgtm`, bot-applied `e2e-ready`, or `coderabbitai[bot]` APPROVED on head (blocked while a human still has `CHANGES_REQUESTED`, except present `lgtm` / bot `e2e-ready`). Otherwise `ready=false` and wait; do not fail. Human APPROVED does not unlock
- Full-install callers skip expensive e2e until `ready=true`; required `e2e-*-gate` stays pending
- `.github/workflows/e2e-on-label.yml` — Canonical starter (`lgtm` / `e2e-ready`). osac calls it via `workflow_call`. `/e2e-ready` `workflow_dispatch`es the per-repo wrapper. Replay bash: `.github/actions/e2e-start/`
- `.github/workflows/e2e-on-approval.yml` — CodeRabbit APPROVED: same-repo calls the starter; fork only `fork-handoff`.
- `.github/workflows/e2e-on-approval-fork.yml` — `workflow_run` replay after `fork-handoff`; verifies CR APPROVED on exact HEAD, then calls the starter. osac calls this via `workflow_call`.
- `.github/workflows/e2e-ready-label-cleanup.yml` — Removes `e2e-ready` on new pushes

## Testing

Run readiness gate unit tests:

```bash
bash .github/actions/check-e2e-readiness/check-e2e-readiness-test.sh
```

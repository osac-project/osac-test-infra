# .github/ Agent Context

## Pull request flow

- Required checks on `main`: `CI` (`ci.yml`: pre-commit and the script tests), `jira-reference` (`jira-reference.yml`) and `E2E` (written by the OSAC CI app). One approval is needed; a push dismisses it.
- `e2e-pr.yml` decides, from the live state of the pull request, whether the full-install suites start: a person with write access approved the head commit (or CodeRabbit, for a branch of this repository, never for a fork) and no other required check failed. The decision, the `E2E` check, its suite lines and the housekeeping are the shared workflows of osac-ci (`osac-project/osac-ci`, pinned by commit with the release tag in a comment). The rules are in osac-ci `policy/flow.yml`; this repository cannot change them.
- `.github/e2e-policy.yml` and `.github/filters/ci-filters.yml` say which suites a change needs; they are read from the default branch, never from the pull request.
- The suite jobs (`vmaas`, `caas`, `bmaas`) in `e2e-pr.yml` and `e2e-queue.yml` call `e2e-<suite>-full-install.yml` from the default branch with the pull request's commit as `test-infra-ref`. A pull request that edits the suite workflow files is first exercised in the merge queue, which runs the merged files; CODEOWNERS requires wg-infra for those files. AI diagnosis and anything that uses OIDC stay off for pull request runs (`enable-ai-diagnostic: false`).
- `e2e-queue.yml` runs the suites for a merge queue entry and writes `E2E` for it. `bot-approve.yml` lets the OSAC CI app approve Dependabot dependency updates that need no person (a workflow change is never approved without a person).
- The `e2e-*-full-install-caller.yml` workflows keep only their `schedule` and `workflow_dispatch` triggers.
- Pins of osac-ci move by the Dependabot group `osac-ci`.

## Testing

| Area | Required coverage | Command | Boundary |
|---|---|---|---|
| CaaS caller and reusable selection | [DEV] Unit/Contract checks in osac | From the osac checkout: `OSAC_TEST_INFRA_DIR=/path/to/osac-test-infra uv run pytest -n 0 tests/unit/test_caas_ci_suite_policy.py` | Real caller YAML and path-selection Bash, temporary test directories; no deployed services or E2E execution. Tests remain in osac, not this repository. |

Run the script tests (CI runs the same commands as the `script-tests` job of `ci.yml`):

```bash
python3 -I .github/scripts/redact_test.py
python3 -I .github/scripts/test_ai_diagnose_failure.py
python3 -I .github/scripts/test_upsert_pr_comment.py
bash .github/scripts/discover-audit-runs-test.sh
```

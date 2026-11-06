# OSAC-5641: Laya routing evaluation

Observed 2026-09-28 against the `english` model of the internal Laya server using schema `osac-laya-stage-v2`. This is a replay of historical failed GitHub Actions runs, followed by a local invocation of the production shadow helper on the held-out cases. It is **not** a measurement from a merged workflow. The exact request schema is pinned in [`schema.json`](schema.json); canonical failure signals, labels, root groups, predictions, and timings are committed beside this report. Raw logs and ZIPs remain outside the repository.

## Corpus and extraction

There are 59 failed runs on 59 PRs: 37 development, 22 root-separated holdout, 29 observed root groups. All 20 AAP instance-group failures share one root group in development. The holdout has no AAP or CI-authorization root, so their success in development cannot validate a bypass. Selection added underrepresented build, install, test, and authorization failures deliberately; these proportions cannot estimate production prevalence.

The same `.github/scripts/laya-shadow.py` extractor produced the four bounded fields (`failed_step`, `job_error`, `pod_error`, `traceback`) for **all 59** cases. It retains fixed descriptions of recognized errors and source line references, never raw matching log text. `reextract.py` replayed the downloaded job logs and selected artifact ZIP members; no raw archive was committed. All 59 job logs were present, five artifact ZIPs were unavailable, and 58/59 cases had direct supporting evidence. [PR #1233](https://github.com/osac-project/osac/actions/runs/36177191564) has only a generic install timeout and no verified upstream cause; it is correctly labeled `unknown`. Labels reflect inspection of the failed step, log, and artifact, but **they have not received the independent human sign-off required before a production bypass**. Several E2E timeout routes identify the failed operation without establishing a deeper product cause.

## Prompt comparison

| Route | Development | Holdout | Laya calls on holdout |
|---|---:|---:|---:|
| Global nine-choice, failed step and job error | 8/37 | not run | — |
| Global nine-choice, four fields | 28/37 | 5/22 | 22 |
| Global nine-choice, compact fields | 28/37 | not run | — |
| Failed-step route, four-choice build/five-choice install Laya question | **35/37** | **17/22** | **11** |

The selected stage route uses the failed step directly for E2E and fork authorization; these decisions have no Laya score. On the 11 held-out build/install cases that actually called Laya, **6/11** routes were correct. Thus the 17/22 combined number measures an extractor and routing policy, not Laya's independent diagnostic ability. The fixed descriptions also encode much of the failure class, so even correct Laya answers do not establish independent root-cause discovery. Development AAP routing was 20/20 but all 20 are duplicates of one root and their Laya top probabilities were 0.339–0.393. Schema v2 added `source_checkout` to the install question after the [PR #1072](https://github.com/osac-project/osac/actions/runs/35996210946) AAP project checkout failure exposed a missing answer choice. That case now routes correctly at 0.444. Development accuracy increased by one, while holdout accuracy did not change. The evaluator now rejects a labeled case whose expected answer is unavailable for its selected stage before sending a request.

## Held-out confusion matrix for the selected route

Rows are reviewed labels. `unknown` is an abstention. The 11 E2E cases in the E2E column were routed by the failed step.

| Actual \\ Predicted | AAP | Dependency | Compile | Source | Deploy | CI | App startup | E2E | Unknown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| AAP policy | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Build dependency | 0 | 2 | 1 | 0 | 0 | 0 | 0 | 0 | 1 |
| Build compile | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| Source checkout | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 3 |
| Deployment config | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
| CI environment | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| App startup | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| E2E / app behavior | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 0 |
| Unknown | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |

| Class | True positives | False positives | False negatives | Abstentions on that class |
|---|---:|---:|---:|---:|
| AAP policy | 0 | 0 | 0 | 0 |
| Build dependency | 2 | 0 | 2 | 1 |
| Build compile | 1 | 1 | 0 | 0 |
| Source checkout | 0 | 0 | 3 | 3 |
| Deployment config | 1 | 0 | 0 | 0 |
| CI environment | 0 | 0 | 0 | 0 |
| App startup | 1 | 0 | 0 | 0 |
| E2E / app behavior | 11 | 0 | 0 | 0 |
| Unknown | 1 | 4 | 0 | 1 |

Wrong routes are [PR #1269](https://github.com/osac-project/osac/actions/runs/36361314336), where a Go module path mismatch was called compile at 0.449; [PR #1126](https://github.com/osac-project/osac/actions/runs/36305767053), where a package export failure was returned as unknown at 0.328; [PR #1093](https://github.com/osac-project/osac/actions/runs/35757502813) and [PR #836](https://github.com/osac-project/osac/actions/runs/35602675588), where missing UI build context was returned as unknown at 0.421 and 0.417; and [PR #1102](https://github.com/osac-project/osac/actions/runs/35702058837), where a missing Git ref was returned as unknown at 0.384. These are actionable errors even when Laya abstains. The global question was worse at 5/22. The duplicate migration in [PR #1166](https://github.com/osac-project/osac/actions/runs/36188200390) and the empty token endpoint in [PR #1240](https://github.com/osac-project/osac/actions/runs/36208878598) were correctly routed by Laya on this replay, but neither meets the bypass threshold.

## Runtime and candidate savings

The 11 sequential held-out `/predict` calls made through the production helper took median **0.412 s**, nearest-rank p95 **0.667 s**, maximum **0.667 s**. Three `/predict/batch` requests for the held-out stage route took median **1.70 s**, maximum **1.92 s**. These are small local replay samples, not production latency measurements. Laya failure, timeout, malformed response, or missing evidence leaves the Vertex path unchanged. The workflow uploads a separate shadow artifact with route, model, score, runner-up, elapsed time, token count, and source references, and adds the same metadata to the structured diagnosis JSON when Vertex succeeds. It does not alter the comment or status.

Thirty of 59 runs repeat one of seven observed root groups after that group's first occurrence. **30/59 (51%)** is only an upper bound on possible duplicate suppression under perfect signature detection. No signature is approved or enabled, and the shadow code never bypasses Vertex: observed and projected Vertex-call reduction is **0%**. No held-out Laya subtype prediction reached the candidate 0.90 threshold; the highest was 0.837.

## Conservative gate and decision

Keep the bypass disabled. A future templated response may be considered only when an owner has approved its exact signature and response, the *current* run contains direct evidence in a named source, the step and source agree with the candidate route, no later retry succeeded, and Laya agrees with a top probability at least 0.90. Generic timeouts, missing evidence, conflicting sources, unknown predictions, service failures, and below-threshold probabilities abstain and go to Vertex. Any wrong-class bypass in a root-separated audit disables bypass immediately; so does loss of required evidence in live capture. The threshold is a conservative proposal, **not** a calibration result.

| Candidate | Required direct signal | Current evidence |
|---|---|---|
| Fork approval | Failed `Authorize fork PR` step plus `job.log` lines `##[error]Fork PR blocked:` and `/ok-to-test`; ignore later artifact-gather errors | Four development cases, no independent holdout or approved template |
| AAP instance-group policy | Failed `Install OSAC` step plus AAP bootstrap pod line with `Unable to create instance_group`, `pod_spec_override`, and secret mounting forbidden; exclude resolved retries | 20 repeated development cases of one root, low Laya scores, no independent holdout or approval |
| Duplicate migration | Failed install plus gRPC pod line containing `duplicate migration file:`; verify the current pod failed to start | Two filenames across splits; held-out score 0.369, no approval |

## Acceptance status

The retrospective labeled replay, root-separated confusion review, evidence completeness count, latency sample, wrong-route examples, duplicate upper bound, and conservative gate are complete. The [review sheet](review-sheet.md) links all 59 runs across 29 root groups for independent label review. The production extractor and shadow call are implemented in this branch, but **no merged workflow has produced live shadow observations**. The diagnostic job runs on GitHub-hosted `ubuntu-latest`; the endpoint used for the local replay is private, and reachability from that runner has not been shown. Shadow capture now defaults to disabled and requires both an explicit enablement variable and a configured reachable endpoint. OSAC-5638 still needs independent human label/signature review. OSAC-5640 needs a proven network path and live observations after merge. Do not close OSAC-5641 as fully accepted until those observations are evaluated through this same extractor and schema and the required owner review is recorded. The bypass remains off throughout.

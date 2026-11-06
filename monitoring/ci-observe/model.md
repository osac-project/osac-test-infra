# CI Observe model

CI Observe has three pages, each answering one question.

| Page | Unit | Question |
|---|---|---|
| **E2E run** | One GitHub Actions run | Did it pass, what failed, and how long did the tests take? |
| **Pull requests** | One pull request | How long did review and the merge queue take? |
| **Trends** | One test over time | Is this test failing more often or getting slower? |

**Start here** lists recent E2E runs and opens one run.

## E2E run

| Section | Contents |
|---|---|
| Summary | Result, tests run, and test time |
| Failures | Failed tests and error messages |
| Test list | Every test, result, start time, and run time |
| Run info | GitHub run, workflow, pull request, branch, and test paths |

A run has detailed data only when its recording arrived. A missing recording does not change the GitHub result.

## Pull requests

The page shows recent pull requests, typical and slow-end stage times, and the selected pull request.

An **ejection** means a pull request left the merge queue without merging. GitHub does not keep queue history, so the page derives it from a failed queue run and a GitHub comment.

## Trends

The page reads GitHub test reports from E2E runs. A run without a readable report is missing data, not a pass. Test reports are kept for 30 days.

## Keys

A run is keyed by its GitHub run ID. A recording ID adds the suite and rerun number. A pull request is keyed by its number. A test is keyed by its test ID.

## Data limits

- Tempo keeps traces for 30 days and limits one search to 7 days.
- Prometheus and Loki keep cluster data for 30 days.
- GitHub keeps the redacted run artifact, including pod logs, for 30 days.
- The workflow exporter keeps run and pull request history for 30 days.

## Out of scope

Fleet health (runners, machines, queue infrastructure) belongs to the **CI Fleet** folder.

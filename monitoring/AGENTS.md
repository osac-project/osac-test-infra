# Monitoring agent notes

Read [`ci-observe/observability.md`](ci-observe/observability.md) before you add a signal or a panel. It states the principles, the current gaps and the roadmap. [`ci-observe/model.md`](ci-observe/model.md) defines the pages, the filters, the definitions and what belongs where. [`ci-observe/sources.md`](ci-observe/sources.md) maps each question to its authoritative store, and [`ci-observe/README.md`](ci-observe/README.md) is the reader guide.

Hard rules:

- ci-observe covers the VMaaS, CaaS and BMaaS E2E suites only. Netris, stretched, agentless, full regression, unit and integration tests are out of scope; `ci_observe_phases.SUITE_WORKFLOWS` is the list.
- There are seven ci-observe pages. **Overview** is the entry page. **Where the time goes**, **Why runs fail** and **Test history** look across runs and share the Window, Suite, Started by and Repository filters. **Pull requests** covers all pull requests of a window, then one. **All runs** lists runs and opens one. **E2E run** shows one recorded run; `capture_id` remains its internal key. A new question becomes a section on one of them, not a new page.
- One vocabulary: suites, triggers, phases, failure causes and test keys come from `scripts/ci_observe_phases.py`. A new workflow step needs a phase there; a test pins every step of the in-scope workflows.
- Every panel description ends with "Population: … Time: … Unit: …": its population, its time axis and its unit. A windowed panel follows `$days`.
- A duration reads `dtdhms`, a share `percentunit`, hours `suffix:h`. Series wear `ci_observe_phases.PALETTE` in its fixed order; a good or bad value wears a status color next to a word or a number. Validate a new palette before you use it.
- A dataset joins a unit by capture id, run id, pull request number or test key. Data that cannot join is not admitted.
- GitHub owns pass or fail. A trace, session span or JUnit result explains the outcome and never overrides it.
- New telemetry emits the OpenTelemetry CI/CD and test attribute names next to the existing `osac.ci.*` fields. The shared names make the data joinable later.
- Missing data is shown as missing, never as zero. A panel that cannot answer says why; every across-runs page ends with **About these numbers**.
- Fleet health (runners, machines, queue infrastructure, exporter health) belongs to the `CI Fleet` folder, not ci-observe.
- Dashboards are provisioned from `config/grafana/dashboards/`. Every dashboard change needs a test in `monitoring/tests/`, and `test_ci_observe_dashboard_contract.py` must still resolve every query against the served API.
- The two CI hooks stay outside `monitoring/`: the capture action (`.github/actions/ci-observe-capture/`) and the gather script (`scripts/gather-osac-logs.sh`). A workflow can only call a local action from its own repository, and both run on the runner, not on the central machine.
- The E2E tests live in the osac monorepo. Do not add or change tests in this repository.

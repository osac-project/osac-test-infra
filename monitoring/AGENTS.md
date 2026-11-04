# Monitoring agent notes

Read [`ci-observe/model.md`](ci-observe/model.md) before you change a ci-observe dashboard or add a panel. It defines the three units and the rule for what belongs where. [`ci-observe/sources.md`](ci-observe/sources.md) maps each question to its authoritative store, and [`ci-observe/README.md`](ci-observe/README.md) is the reader guide.

Hard rules:

- There are three ci-observe content dashboards: Capture, Pull requests and Trends. Start here is the entry page, a router, not a fourth dashboard. Compare is a section of Trends. A new question becomes a section on one of the three.
- The entry page takes a run id, a capture id, a pull request number, a test id, or nothing. Run id, capture id, pull request number and test id all resolve. Describe the full input set as the rule.
- Every panel names its unit, its population (all jobs or captured runs) and its time axis.
- A dataset joins a unit by capture id, run id or pull request number. Data that cannot join is not admitted.
- Fleet health (runners, machines, queue infrastructure, exporter health) belongs to the `CI Fleet` folder, not ci-observe.
- Dashboards are provisioned from `config/grafana/dashboards/`. Every dashboard change needs a test in `monitoring/tests/`.
- The two CI hooks stay outside `monitoring/`: the capture action (`.github/actions/ci-observe-capture/`) and the gather script (`scripts/gather-osac-logs.sh`). A workflow can only call a local action from its own repository, and both run on the runner, not on the central machine.
- The E2E tests live in the osac monorepo. Do not add or change tests in this repository.

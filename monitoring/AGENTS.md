# Monitoring agent notes

Read [`ci-observe/model.md`](ci-observe/model.md) before you change a ci-observe dashboard or add a panel. It defines the three units and the rule for what belongs where. [`ci-observe/sources.md`](ci-observe/sources.md) maps each question to its authoritative store, and [`ci-observe/README.md`](ci-observe/README.md) is the reader guide.

Hard rules:

- There are three ci-observe content dashboards: E2E run, Pull requests and Trends. Start here is the entry page and router, not a fourth dashboard. A new question becomes a section on one of the three. The E2E run page shows one recorded CI run; `capture_id` remains its internal key.
- The entry page lists recent E2E runs and opens one run. There is no combined lookup box.
- Every panel names its unit, its population (all jobs or captured runs) and its time axis.
- A dataset joins a unit by capture id, run id or pull request number. Data that cannot join is not admitted.
- Fleet health (runners, machines, queue infrastructure, exporter health) belongs to the `CI Fleet` folder, not ci-observe.
- Dashboards are provisioned from `config/grafana/dashboards/`. Every dashboard change needs a test in `monitoring/tests/`.
- The two CI hooks stay outside `monitoring/`: the capture action (`.github/actions/ci-observe-capture/`) and the gather script (`scripts/gather-osac-logs.sh`). A workflow can only call a local action from its own repository, and both run on the runner, not on the central machine.
- The E2E tests live in the osac monorepo. Do not add or change tests in this repository.

# Monitoring agent notes

Read [`ci-observe/model.md`](ci-observe/model.md) before you change a ci-observe dashboard or add a panel. It defines the three units and the rule for what belongs where.

Hard rules:

- There are three ci-observe dashboards: Capture, Pull requests and Trends. Do not add a fourth. A new question becomes a section on one of them.
- Every panel names its unit, its population (all jobs or captured runs) and its time axis.
- A dataset joins a unit by capture id, run id or pull request number. Data that cannot join is not admitted.
- Fleet health (runners, machines, queue infrastructure, exporter health) belongs to the `CI Fleet` folder, not ci-observe.
- Dashboards are provisioned from `config/grafana/dashboards/`. Every change needs a test in `tests/`.
- The E2E tests live in the osac monorepo. Do not add or change tests in this repository.

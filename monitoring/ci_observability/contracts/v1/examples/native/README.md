# Native report fixtures

These are sanitized outputs from tiny local framework runs captured on
2026-10-27 and 2026-10-28. They establish format compatibility, not deployed
OSAC integration coverage.

| File | Framework | Capture command |
| --- | --- | --- |
| `pytest-junit.xml` | pytest 8.3.5 | `pytest -o addopts= --junitxml=report.xml` |
| `ginkgo.json` | Ginkgo 2.33.1 | Two runs with `ginkgo --flake-attempts=2 --json-report=report.json` and `--flake-attempts=3` |
| `go-test.jsonl` | Go 1.26.8 | `go test -json ./...` against passing, failing and build-failing packages |
| `vitest.json` | Vitest 4.1.9 | `vitest run sample.test.js --globals --maxWorkers=1 --no-file-parallelism --reporter=json --outputFile=report.json` |

The temporary test sources are not retained. The commands document the
reporter options; recreating the same format requires a small suite with
the corresponding passing, failing, skipped and retry cases.

Sanitization replaces local directories, identities, timestamps and test
messages with fixture values. It keeps native field names, nesting,
outcomes, attempt counts and timing units. Ginkgo keeps retry events and
`NumAttempts`; it does not invent one `SpecReport` per retry. Go build
events retain `ImportPath` and can lack `Time`. Vitest 4.1.9 reports a
skipped assertion as `status: "skipped"` while naming its counter
`numPendingTests`.

The Vitest fixture uses a captured 4.1.9 JSON report. The pytest fixture
uses the framework's JUnit representation, where a non-strict
unexpected pass can look like a pass.

W03 owns captures and import tests for all four real Fulfillment IT
reports, `service-only`, `manager-capability`, `manager-readiness` and
`bmaas-disabled`. None is represented by this tiny Ginkgo fixture. W09c
owns the Ansible callback and target report; its absence is recorded in
`ansible-target-report.UNAVAILABLE.md`.

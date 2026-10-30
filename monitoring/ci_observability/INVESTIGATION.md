# Query a published execution

The investigation CLI reads one execution from PostgreSQL and returns structured JSON.
It joins test phases, native results, workers, shared fixtures, waits and resource observations.
It uses the ledger's read credentials and performs no writes or network collection.

```bash
python -m monitoring.ci_observability.investigation \
    --execution-id <published-execution-id> --section summary
```

Set `CI_OBSERVABILITY_DSN` through the protected environment, never on the command line.
Sections are summary, selection, tests, attempts, phases, workers, fixtures, operations, resources,
coverage and all. The default is summary. Every section carries execution identity and coverage.
Use `--schema` for a disposable local schema. The default is `ci_observability`.

## Reading the result

- GitHub's job conclusion and the producer's reported outcome remain separate.
- The tests table has one final reported result per test and invocation. Its row count matches `test_count`.
- The attempts table contains measured attempts. Ginkgo's retry count never creates invented retry intervals.
- Selection lists collected and deselected definitions separately from test results.
- Missing invocation IDs stay null. The query groups their intervals within a journal, without inventing an invocation.
- A failed test body and a cleanup failure keep separate evidence.
- Shared fixture setup and teardown appear once each, with links to their consumers.
- An unfinished interval has no end time and no completed duration.
- Source completeness, unclosed intervals and the latest observation remain visible.

Observed completeness is true when all observed sources are healthy and every observed attempt and interval closes.
Known gaps make it false. A terminal execution without source-health records has unknown completeness, shown as null.
A failed build can have zero reported test results. That zero describes available results, not a passing test suite.
This describes observed records. It does not certify coverage of sources that produced no records.

Durations retain integer nanoseconds, raw seconds and unrounded minutes. Human views format minutes to two decimals.
An explicit duration is preserved. Otherwise a duration requires matching process clock IDs and monotonic times.
UTC differences across hosts never become measured work durations.

Phase work, fixture work and operation work can overlap. They are separate totals, never additions to wall time.
Unknown work stays null; a measured zero stays zero.

The query reads cursor pages rather than the old offset window. It refuses more than 100,000 records or 64 MiB
of source payload. CLI output is capped at 16 MiB. A limit is an error, never silent sampling.
Pages are live reads. `stable_read` compares record counts before and after the walk; it does not promise a database
snapshot. Concurrent ingestion makes overall completeness false.

## Validation and remaining work

DEV unit checks exercise the frozen reference bundles, interruptions, clock changes, native aggregates and shared cost.
Real PostgreSQL checks query 1,201 records through cursor pages using a separate SELECT-only login.

```bash
python -m pytest monitoring/tests/observability/test_investigation.py --no-cov
```

Configure `CI_OBSERVABILITY_TEST_DSN` for the database cases. An unconfigured database is a named skip.
This query does not deliver evidence or enable CI producers. Live workflow wiring, artifact import and a joined
Grafana view remain separate work. Empty resource or operation tables do not prove that nothing happened.

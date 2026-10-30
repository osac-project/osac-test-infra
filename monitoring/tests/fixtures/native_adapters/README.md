# Native adapter test fixtures

Provenance and sanitization record for every fixture in this directory.
CI runs only these sanitized inputs; the full private captures stay
outside the repository.

## it-phases/ginkgo-<phase>.EXCERPT.json

Fresh sanitized projections (2026-10-28) of the four real Fulfillment
IT Ginkgo reports, recaptured read-only from GitHub artifact
12739864324 (run 40508610752) after the original `/tmp` copies were
lost. The recaptured raw bytes matched the documented anchors exactly
(full-file SHA-256 first-16-hex service-only be12fb8e4a1135c9,
manager-capability f2a33b2ceb4b5894, manager-readiness
6a8355b4876aa00d, bmaas-disabled f3042691749e3046; the full archive
matched the GitHub API artifact digest). These are projections of the
same anchor-verified originals, not verbatim copies of the lost
fixtures.

Each original report holds 625 SpecReports over 623 It definitions (2
hook nodes) with the same SuiteDescription and SuitePath in all four
phases. Each EXCERPT selects 5 representative It definitions plus the
2 actual hook nodes, preserving report order: service-only keeps its
actual 2 filtered specs, its 1 runtime skip, and 2 passed specs; the
manager phases keep their 1 executed It and 4 filtered specs. The
declared `PreRunStats` totals (623 total; 621 `SpecsThatWillRun` for
service-only, 1 for the manager phases) are kept exactly, so each
EXCERPT is deliberately incomplete: the adapter reports 618 missing
specs as an explicit gap instead of pretending a complete collection.

Kept exactly: states, `NumAttempts` (including 0 for filtered specs),
`RunTime` nanoseconds, `StartTime`/`EndTime` (including the year-1
sentinel on filtered specs), `LeafNodeType`, `SuiteSucceeded`, and the
suite times. Sanitized to deterministic public labels: suite
description and path, container hierarchy texts, leaf texts, source
file names, and failure messages (SHA-256-derived labels, so the same
original spec keeps one label and one source identity across all four
phases). Omitted entirely: captured output, labels, semver
constraints, and spec events. The raw captures stay private outside
Git (see the runtime PROVENANCE note); the optional full-capture tests
read them only through CI_OBSERVABILITY_IT_CAPTURES_DIR, and no
workstation default is committed.

## vitest-4.1.11.sanitized.json

Sanitized copy of a NEW actual Vitest 4.1.11 JSON capture, generated
2026-10-28 during recovery with the locked
CI_OBSERVABILITY_VITEST_411_BIN tool (version-verified 4.1.11 on Node
22.23.4). This is NOT the original reviewer-2 capture (lost with the
`/tmp` wipe); the provenance change is stated here rather than hidden.
The suite deliberately declares one duplicate display name (two
same-named passing tests) so the duplicate-identity path is exercised
on captured data: 4 cases, 2 passed, 1 failed, 1 skipped,
`success: false`. Sanitized: the suite file path and the failure
messages. Kept exactly: all counters, statuses, float-millisecond
durations (absent on the skipped case), and epoch-millisecond suite
times.

## vitest-4.1.9.sanitized.json

Sanitized copy of a NEW actual Vitest 4.1.9 JSON report, generated
2026-10-28 during recovery with the isolated version-verified 4.1.9
tool on Node 22.23.4. The original 2026-10-28 capture was lost with
the `/tmp` wipe; this regeneration states its provenance rather than
relabeling the old one. Sanitized: the suite file path and the
failure messages. Kept exactly: all counters, statuses (`passed`,
`failed`, `skipped`), float-millisecond durations, and
epoch-millisecond suite times including sub-millisecond precision.

## ginkgotiny/ and gotiny/

Copied unchanged from the osac mono-repo
`tests/unit/fixtures/ci_observability_native` (provenance there; Ginkgo
2.33.1 and Go module sources). The real-subprocess tests copy these
into a scratch directory outside the checkout before running `ginkgo`
or `go test`, so no build or module cache is written into this
repository.

The tiny real Ginkgo, go-test, pytest JUnit, and Vitest fixtures under
`ci_observability/contracts/v1/examples/native/` are used read-only by
the adapter tests; their provenance is documented next to them.

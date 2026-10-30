# Native report adapters (W03)

Strict, bounded parsers for the four native report formats, plus pure
conversion of one parsed report into frozen v1 journal records:

| Module | Format | Notes |
| --- | --- | --- |
| `ginkgo.py` | Ginkgo 2.x JSON report | one suite per invocation; `NumAttempts` retained, retries never expanded |
| `gotest.py` | `go test -json` stream | repeated run/terminal cycles are measured attempts |
| `junit.py` | JUnit XML (pytest) | DOCTYPE/entities rejected; no case times, no collection events |
| `vitest.py` | Vitest 4.1.9/4.1.11 JSON | caller-supplied `source_root`; no retry fields: attempt count null |

`records.py` holds the shared primitives (context, envelope, timestamp
and duration conversion, hashed identities, gaps) and `jsoninput.py`
the strict JSON loader. Every parser takes bytes, enforces the 32 MiB
file bound plus node/depth/count caps, and never opens a path or URL
from a report.

## Conversion contract

The caller supplies an `InvocationContext`: execution, journal, and
invocation identity, the actual framework, tier, component, variant,
the adapter observation time, and the child exit code when known.
Nothing of these is inferred from a report; native processes within one
job execution never become separate executions, and one job can hold
several invocations (for example the four IT phase invocations). Each
converter returns a `ConversionResult`: journal records (ready for the
frozen contract validators and the ledger), explicit gaps, and a
private display-name mapping. The display names carry raw native text
and must never be written to a journal, a log, or a public artifact.

Records always use `mono_ns: null`: native reports carry no monotonic
clock, and no clock domain, phase, worker, or retry interval is
invented. `recorded_at` is the adapter observation time from the
context; `source_at` and the `source_*_at` fields carry the report's
own timestamps, converted to UTC-Z text while preserving up to nine
fractional digits exactly (never through float arithmetic). Ginkgo's
year-1 zero time is the absent sentinel: such timestamps are omitted.
Durations are converted to integer nanoseconds through `Decimal` from
the native literal; Vitest float milliseconds are quantized once,
half-even, at the nanosecond. The aggregate duration of a retried test
is the sum of its measured attempt spans and is never charged to the
final retry.

## Identity and privacy

Raw names, parameter text, and failure content are not intrinsically
safe. Test identities are stable SHA-256 hashes in the
`framework:component:` namespace. Ginkgo hashes the caller suite path,
hierarchy text, leaf text, node type, and the suite-relative source
file: absolute checkout roots and line numbers are excluded on purpose,
so the same suite at two physical roots, or with comments inserted
above an It, keeps its identity, while the same spec across phase
invocations of one suite stays equal. Two specs that share every one of
those fields are still distinct: they are disambiguated by an occurrence
ordinal and reported through the fixed `ginkgo-duplicate-identity` gap.
The comparison limit is that the ordinal reflects report order, not a
stable property of the suite. Failure records carry an allowlisted class
plus a content hash; messages, stack traces, captured output, and
progress reports never enter journal records. The original reports stay
wherever the pipeline retains them, linked by digests; these hashes do
not prove anything about trusted code.

## Per-format honesty rules

- Ginkgo: `NumAttempts` is retained directly, including 0 for filtered
  specs (attempt count 0 is measured zero, null is unknown). Per-attempt
  outcomes and intervals are not fabricated from `NumAttempts`, Retry
  events, or `AdditionalFailures`. Non-It nodes (BeforeSuite,
  DeferCleanup, BeforeAll) are hooks, not tests; hook failures mark the
  invocation failed and are reported as gaps. `test_collected` and
  `collection_finished` are emitted only when the spec list matches the
  declared `PreRunStats` totals and the deselection count agrees; an
  incomplete or contradictory catalog is an explicit gap with no faked
  counts. Pending and filtered specs are both never-attempted skips:
  filtered is state "skipped" with zero attempts and an absent end time,
  pending is state "pending"; a count disagreement between them is the
  `ginkgo-deselection-mismatch` gap, not a reclassification.
- go test: identity is Package plus Test. Each run/terminal cycle is
  one attempt with its own measured span; the aggregate sums them. A
  cached package (the `Cached` field or the constant go-format
  "(cached)" output marker, detected without keeping any output text)
  replays its previous results without re-executing anything: those
  outcomes are attributed previous results with attempt_count null, no
  duration, and no source timestamps, plus the
  `go-test-package-cached` gap. Package-level events, build output,
  and events without a Time are not test cases; a failed build is a
  gap plus a failed invocation. A terminal event without a run event is
  `go-test-attempt-without-run`; a cycle whose timestamps are missing
  is `go-test-attempt-missing-time`. An empty stream is an unknown
  outcome, never a pass.
- JUnit: no case start or end times and no collection catalog exist in
  the format, so none are emitted. Setup-versus-teardown is not
  represented, and a non-strict unexpected pass is indistinguishable
  from a plain pass. Duplicate (classname, name) cases are one
  identity: a single aggregate whose outcome is the common outcome when
  the cases agree and "unknown" when they disagree, with attempt_count
  null, no duration, and the `junit-duplicate-identity` gap; retries
  are never invented from them. DTDs and entity declarations are
  rejected with expat's declaration handlers, which run after encoding
  detection, so UTF-16 and UTF-32 documents cannot smuggle them past
  the raw byte scan; node, depth, and text bounds are enforced during
  parsing, before an unbounded tree exists, and no external entity is
  ever fetched.
- Vitest 4.1.9: statuses are `passed`, `failed`, and `skipped` (the
  counter is named `numPendingTests`); there are no retry fields, so
  the attempt count is null, never one. Case durations are float
  milliseconds; suite times are epoch milliseconds. Counter
  contradictions are explicit gaps.

Unknown states, actions, statuses, shapes, and type contradictions fail
with constant errors; count contradictions are returned as explicit
gaps. Nothing is truncated or normalized silently.

## Gaps and their persistence

Gap codes are fixed constants with bounded counts. Current codes:
`ginkgo-catalog-incomplete`, `ginkgo-deselection-mismatch`,
`ginkgo-duplicate-identity`, `ginkgo-hook-failure`,
`ginkgo-special-suite-failure`, `go-test-package-cached`,
`go-test-build-failed`, `go-test-attempt-without-run`,
`go-test-attempt-missing-time`, `go-test-no-test-events`,
`junit-duplicate-identity`, `junit-no-test-cases`,
`vitest-count-mismatch`, `vitest-counter-mismatch`,
`vitest-duplicate-identity`, plus one
`<framework>-exit-contradicts-report` gap per converter when a
measured nonzero child exit contradicts a report that claims success.

Identity rules: the Ginkgo stable key is the suite-relative file name
(no line numbers, no absolute roots); the Vitest stable key is the
suite file path relative to the `source_root` the caller passes to
`parse_vitest_report` — the root is never inferred from the report
set, an absolute suite outside the root is rejected, and duplicate
display names inside one file get distinct occurrence ordinals with an
honest `vitest-duplicate-identity` gap (the comparison limit: ordinals
follow report order). The Go cache signal is only the genuine package
`Cached` field or the package-result marker on the matching package
event; test output that merely mentions the substring is not a cache
signal. A measured nonzero child exit never labels an invocation
passed in any converter; an unmeasured exit stays null and a split
capture never copies the global CLI exit.

Gaps do not live only in the Python return value: every conversion
ends with a terminal `collection_state` event for the adapter source,
built from the existing v1 fields only. Gap codes become fixed error
labels. `dropped_records` states only what its gap code means, never a
sum of unrelated counts:

- Known missing records: `ginkgo-catalog-incomplete` counts the specs
  absent from an incomplete report.
- Facts fully delivered (representation limits): a cached Go package,
  a duplicate identity, a missing timestamp, a delivered hook failure,
  a deselection count mismatch, a special suite failure reason, an
  empty JUnit report, and an attempt without a run event all state
  zero drops. Every fact was delivered; the gap describes a limit of
  the representation or an attribution nuance, not a lost record.
- Unknown loss: a build failure, an empty stream, and Vitest counter
  contradictions lose an unknowable number of results. The frozen
  schema requires an integer, so `dropped_records` stays 0 and
  `omitted_fields` names it as a placeholder the source could not
  fill, distinguishing it from a measured zero.

A gapless conversion is a clean `stopped`/complete terminal state, a
gapped one is `degraded`/incomplete. The real ledger roundtrip keeps
them.

## Multi-suite and repeated-suite composition

A merged `ginkgo run -r` capture (one JSON array with several suites)
is split by `split_ginkgo_suites(raw, source_root=...)` into
per-suite `GinkgoSuiteSplit` values: the parsed suite plus a suggested
caller-relative suite path derived from its native `SuitePath`
relative to the explicit `source_root`. Two packages in one capture
get distinct namespaces, so their definition IDs stay disjoint; a
repeated run of the same suite keeps the same IDs and the caller gives
each split its own invocation context. The root is never inferred from
the report set (changing the selection would change identities): a
`SuitePath` outside the root is refused with a constant error.

The caller composes the split: one `ginkgo_journal_records` call per
suite under its suggested path, each with its own journal and
invocation id. The overall CLI exit of the merged command belongs to
the caller's composition, not to any single suite: pass
`child_exit_code` only when that suite's own command was measured,
null otherwise. `SuiteSucceeded` is the suite's native attributed
claim, never proof of the Go test binary's exit code.

## Vitest test prerequisite and version boundary

The adapter's format claim covers the versions actually exercised:
Vitest 4.1.11, the version the accepted monorepo's
`osac-ui/pnpm-lock.yaml` resolves (`package.json` declares `^4.1.9`),
and Vitest 4.1.9, the previously captured boundary. Reviewer 2 verified
the two versions' JSON reports have identical top-level, per-suite, and
per-assertion key sets and types, with no retry, repeat, flaky, worker,
pool, thread, or process fields in either — so the attempt count stays
null and the worker unknown for both. A patch version does not change
identity rules or the schema: the parser claims format compatibility,
never GitHub authority over CI conclusions.

The real-subprocess tests resolve their tool through explicit
configuration, checked with `--version` before use:

- `CI_OBSERVABILITY_VITEST_411_BIN`: path to a Vitest 4.1.11 binary.
- `CI_OBSERVABILITY_VITEST_419_BIN`: path to a Vitest 4.1.9 binary.
- `CI_OBSERVABILITY_VITEST_BIN`: a generic override; its actual version
  selects which case runs, and the other case skips with an explicit
  reason.

A per-version tool that is configured but missing, not executable, or
reports the wrong version FAILS the test. Only an unset, unconfigured
boundary skips with an explicit reason. No hardcoded workstation path
is a prerequisite. The prerequisite for local runs is an isolated
`npm install vitest@<version>` in a scratch project (never a shared or
global dependency change); the gen-native checkout's vitesttiny is 5.0.5
and is the wrong boundary.

Reporter wiring facts for the future producer (W03/W04 follow-up, not
this adapter's responsibility): `--reporter=json --outputFile` replaces
the default console reporter, and an IO failure of the output file
(EISDIR/ENOTDIR) turns a passing test run into exit 1 in both 4.1.9
and 4.1.11. The caller keeps the child's actual exit code — exit 1
stays exit 1, never reinterpreted as an infra failure. Verified on
node v26.8.1 locally; CI runtime parity is not claimed.

## Scope

This package parses and converts; it does not ingest. Workflow
wiring, bundle assembly, and ingestion are owned elsewhere, and the
ingest binding lands after that lane's acceptance. The Ansible target
report and the UI worker reporter are separate W03 scope and are not
present here.

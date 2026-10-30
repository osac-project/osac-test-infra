# CI observability bundle ingestion (W01b)

Safe canonical bundle ingestion, immutable source receipts, gated
publication, and reconciliation of explicitly supplied trusted GitHub
metadata, on top of the W01a ledger and the frozen v1 contract.

The storage overview stays in [`README.md`](README.md); the contract in
[`contracts/README.md`](contracts/README.md). Nothing here fetches from
the network: the shared GitHub poller, artifact download, and backfill
are W01c, and this unit is driven by a trusted caller that already holds
the bytes and the metadata.

## Modules

| Module | Responsibility |
| --- | --- |
| `bundle_reader.py` | Freeze one delivery (bundle directory or archive) into an immutable snapshot validated on the exact bytes read; preflight archives before any ZipInfo allocation; materialize the durable private quarantine copy. |
| `reconcile.py` | Drive the accepted pure binding module (`github_binding`) over explicitly supplied raw GitHub API records and the payload's claims; extract the trusted repository identity; snapshot the trusted context. |
| `ingest.py` | The `Ingestor`: idempotent receipts, the publication gate, per-execution serialization, ledger writes through the accepted `Ledger` API, redaction proofs, second-connection acknowledgement, and read-only query helpers. |
| `migrations/0002_sources.sql` | `source_receipt`, `source_execution`, `source_github_observation`, `source_redaction_proof`, their guards, and the reader-isolation revoke. |

## Trust model: quarantine or publish

A delivery is one frozen bundle plus trusted caller context. Every
payload field is a claim. A delivery reaches the ledger (canonical
execution rows, event prefixes, authoritative conclusions) only when
publication is proven:

1. **A VERIFIED job binding** from the caller-supplied GitHub metadata
   plus a trusted mapping (`expected_job_id` from the job catalog, or
   `expected_display_name` from a central mapping). CANDIDATE, CLAIMED,
   and UNKNOWN never look verified; the manifest's claimed numeric
   `github_job_id` and `repository_id` are recorded as claims with
   mismatch flags and never promoted. Publication stores the trusted
   repository id from the run record.
2. **A full-coverage trusted redaction proof**: an attestation (the W06
   sanitizer's interface; nothing in production mints one yet) covering
   the manifest digest and every listed file. Proofs append to
   `source_redaction_proof`; the receipt's redaction state is derived
   from them, so a later proof promotes a quarantined delivery without
   rewriting the receipt.
3. **A non-contradicted source proof**: for archives, an artifact binding
   that is VERIFIED with byte integrity VERIFIED, no attempt conflict,
   and — when the caller supplied an id — selecting exactly that
   artifact. Contradictory digest, id, or attempt evidence quarantines
   before any canonical write. Expired and missing artifacts stay honest
   states; unknown origin proof quarantines rather than fakes an
   association. A registered host origin is the named trusted external
   origin for directory deliveries.

Anything unproven or contradicted stays **durably quarantined**: an
immutable receipt with status `quarantined`, append-only observations
recording why, the retained bytes in the private content-addressed
quarantine, and zero canonical writes. Quarantined payloads are never
published or uploaded; the raw manifest text and GitHub snapshots are
private to the ingest role — the migration revokes every non-owner
SELECT grant on the source tables, so the reader role (Grafana) serves
ledger tables only. The revoke covers table-level grants (PUBLIC is
emitted as the special unquoted keyword), column-level SELECT grants
(which live in pg_attribute.attacl, invisible to the table-grant view),
and inherited access (revoking from the direct grantee removes what role
members inherited), touching only the four source tables. Quarantine
maintenance and retention are W05/W13.

## API

```python
from monitoring.ci_observability import bundle_reader, ingest
from monitoring.ci_observability.ingest import ArtifactSourceContext, HostSourceContext, RedactionAttestation
from monitoring.ci_observability.reconcile import GitHubEvidence

ingestor = ingest.Ingestor(
    dsn=dsn,                      # same DSN policy as the ledger: env, never argv
    quarantine_root=Path("/var/lib/ci-observability/quarantine"),
)

# An archive the trusted caller received (its digest was verified against
# the artifacts API by the caller; the reader re-verifies the file):
result = ingestor.ingest_delivery(
    delivery=bundle_reader.BundleArchive(path=archive, received_digest="sha256:..."),
    source=ArtifactSourceContext(repository="osac-project/osac", run_id=123, artifact_name="telemetry-bundle"),
    github=GitHubEvidence(run=raw_run, jobs=raw_jobs, expected_job_id=4588887777),
    redaction_attestation=RedactionAttestation(policy="sanitizer-1", covered_digests=...),
)
result.status        # 'committed' (published) or 'quarantined'
result.quarantine_reason

# An explicit private bundle directory from a registered host:
result = ingestor.ingest_delivery(
    delivery=bundle_reader.BundleDirectory(path=bundle_dir),
    source=HostSourceContext(origin="host-42"),
)

# A pre-frozen bundle (the freeze is a pure filesystem step); ingest
# revalidates it from its pinned bytes:
bundle = bundle_reader.freeze(delivery=bundle_reader.BundleDirectory(path=bundle_dir))
result = ingestor.ingest_bundle(bundle=bundle, source=HostSourceContext(origin="host-42"))
```

Read-only helpers (bounded, parameterized, validating): `get_receipt`,
`list_receipts` (filters: `repository`, `run_id`, `execution_id`,
`status`, `unbound`), `list_observations`, `list_redaction_proofs`.

## Delivery and freeze

A delivery is either an explicit private bundle directory or an archive
received by a trusted caller. The manifest is parsed and fully validated
(schema, declared bounds, canonical paths) before any listed file is
opened, so a hostile manifest cannot turn the reader into a probe for
files outside the bundle; paths reject empty, dot, and double-dot
segments explicitly, and the bundle walk follows no links. Archives are
data: the file is opened without following a final-component symlink or
blocking on a non-regular file, read once (bounded), hashed against the
received digest, and preflighted (entry count and central-directory size
parsed from the end-of-central-directory records) before any ZipInfo is
allocated — a small forged directory cannot amplify into gigabytes of
objects. Members must be canonical relative names without duplicates,
unencrypted regular files, stored or deflated, with declared and actual
uncompressed sizes capped per member and in total.

Every byte of a bundle is read exactly once, and the manifest is verified
against those exact bytes. `validate_bundle` is deliberately not used on
the ingest path because it re-opens files; the ingest instead drives the
same frozen v1 semantics (imported from the contract package, not
re-implemented) over the frozen snapshot, streaming journals one line at
a time so a maximal journal costs its own bytes. Storage re-parses each
journal from the frozen bytes and re-verifies the digest, size, and
record count on the exact bytes it parses. `ingest_bundle` revalidates
any `FrozenBundle` it is handed (`revalidate_frozen`): claims, digests,
and counts are re-derived from the pinned manifest text and the carried
payloads, so a mutated parsed manifest or forged file tuple cannot bypass
validation. Diagnostics carry manifest ordinals, never producer paths.

## Receipts, ownership, and growing executions

`source_receipt` holds one immutable row per (trusted source identity,
received digest) — the identity comes from the caller (repository, run,
artifact name and id for archives; registered host origin for
directories), never from the payload. The received digest is the sha256
of the archive bytes the caller verified, or the manifest content digest
for directory deliveries. Content columns never change; status moves
forward only: `received` → `partial` → `committed`, `received` →
`quarantined` (promotable when proof arrives), or → `failed` (terminal,
deterministic conflicts). `events_committed` never decreases and freezes
at `committed`; a failure code is set once. Redelivery of the same
(identity, digest) is the same receipt and is idempotent; a redelivery
with a full proof promotes a quarantined receipt to published.

A delivery digest identifies a receipt, not an execution's lifetime.
`source_execution` records the first receipt that published an execution
id and its verified job; later deliveries for the same verified job
extend the stored event prefixes with new records, new journals, new
native files, and new invocations, while the ledger keeps existing event
hashes immutable (a changed existing record is a conflict that writes
nothing). A stale shorter replay acknowledges its own delivered keys —
each delivered journal's stored prefix — not a global count. A different
verified job for the same execution id is rejected without changes.

## Serialization and durability

Publication for one execution id runs under a per-execution PostgreSQL
advisory lock (a session lock on the source connection, with the
configured finite lock timeout): authorization, registration,
conclusions, ownership, and the event appends are serialized across
every connection and process, so a racing delivery cannot slip between
the authorization check and the ownership claim, and an unverified
delivery — refused by the gate before the lock — registers and binds
nothing. No read transaction wraps the ledger writes, preserving the
W01a acknowledgement guarantee.

Authorization and outcome are distinct facts. The observation records
`publication_authorized` (the gate decision at reconciliation time);
the actual outcome is the receipt's forward-only status and set-once
failure code — an authorized delivery that fails publication is
`failed` there with zero events, never a published claim, and no
earlier observation or proof is ever rewritten.

The whole bundle is validated, the publication gate decided, and the
quarantine copy durably written before any event write. The quarantine
write itself is durability-complete: creating the root (and any new
ancestors) fsyncs the parent entry that holds each new directory, and
adopting an existing root, anchor, or nested component repairs the
parent entry that holds it before anything inside is acknowledged (an
earlier or concurrent creator may have failed before flushing it); each
record is fully written, fsynced, and linked into place atomically
without clobbering; the temporary name is exclusively created and
cleaned up on every path (create, write, fsync, publish), and an unlink
failure after a successful publish is never acknowledged; every
directory that received an entry — including the parent of an
already-matching file, whose earlier publisher may have crashed before
syncing it — is fsynced; and the root's original device and inode are
compared at the final reopen, so a root replaced mid-materialization
fails closed without touching either directory. All writes go through
component dir_fds opened with O_NOFOLLOW: a symlinked component cannot
redirect a write outside the quarantine, and nothing outside the
caller's own tree is ever created, chmodded, or overwritten. Events are appended
through the accepted `Ledger` API in batches bounded by the ledger's
caps; a partial multi-batch import leaves a consistent prefix and a
`partial` receipt, and a redelivery completes it without duplicate
counts. The acknowledgement is returned only after a fresh second
connection — with the same finite SQL and lock timeouts — sees the
committed receipt, every delivered journal's stored prefix, and the
expected authoritative columns. Failures are never acknowledged;
receipts keep their last durable state; deterministic conflicts mark
`failed` with a fixed code.

## Identity details

The ledger's authoritative columns come only from VERIFIED bindings: the
trusted repository id and numeric job id, the executed state, and the
run and job conclusions (learned once, so a failed first attempt can
never be overwritten by a later green one). A job the trusted metadata
says did not execute in this attempt (inherited or carried over) is
recorded as evidence and quarantined — it never creates or extends an
execution. The tested-revision verdict follows the binding module's
rules (trusted commit status for workflow_dispatch, caller-asserted
checkout policy, or the manifest's tested source as a CLAIMED override);
the workflow run head SHA alone is never the tested revision. Each
observation records the bounded trusted context used (job mapping,
status commit SHA, creator allowlist, checkout policy, received digest)
so a replay can explain the authority.

## Errors

`bundle_reader.BundleError` and `ingest.IngestError` (with
`SourceConflictError` and `AckVerificationError`) carry fixed codes and
value-free messages: no payload content, paths, URLs, tokens, SQL, or
DSNs, and no exception chains. Transient database failures surface as
`IngestError('database-error')`; ledger errors propagate as the ledger's
own curated exceptions.

## Tests

Real PostgreSQL only (no SQLite, no mocks), selected by
`CI_OBSERVABILITY_TEST_DSN`, following the W01a environment policy: the
modules skip clearly when unconfigured, and a configured DSN with a
missing driver or unreachable database fails the run. The tests create
their own `ci_obs_*` schemas and drop them afterwards.

```bash
CI_OBSERVABILITY_TEST_DSN=postgresql://ci_observability@host/db \
    python3 -m pytest monitoring/tests/observability --no-cov
```

The full monitoring suite, as CI runs it, includes these tests with
coverage:

```bash
cd monitoring/tests && python3 -m pytest
```

Covered: every example bundle fixture (serial, parallel, rerun,
campaign, partial, unit-ginkgo with native aggregates, unit-build-fail,
mixed-aap) published with full proof and quarantined without it;
unverified, unattested, and contradicted deliveries leaving zero
canonical writes and nothing visible through the read API; same-source
proof promotion; partial-to-full-to-stale prefix growth, changed-record
and cross-job conflicts; deterministic authorized/unverified and
authorized/authorized races; restart of a partial multi-batch import;
second-connection acknowledgements; real writer/reader permissions
keeping source tables private; malicious archives (path escape,
symlinks, FIFOs, ZIP64 central-directory bombs with bounded memory,
encrypted flags, duplicate members, bombs) and malformed manifests
rejected before any file is read; streaming journal parsing under
bounded memory; quarantine durability (short writes, fsync failures,
symlinked roots and components, exclusive concurrent publishes,
parent-entry fsync for new and matching-existing paths, replaced-root
interleaves, temporary-file cleanup on write/fsync/unlink faults, and
descriptor-cleanup on validation failures); the authorized-but-failed
publication outcome recorded as failed, never published; and mutated
`FrozenBundle` revalidation.

## Scope and gaps

- The shared GitHub poller, artifact download, pagination, checkpointing,
  backfill, and stale in-progress reconciliation are W01c; this unit only
  reconciles metadata a trusted caller supplies explicitly.
- The trusted transport and the redaction sanitizer that produces
  attestations are W06. No production path mints a `RedactionAttestation`
  yet: until W06 exists, every delivery lacking a caller-supplied proof
  stays quarantined, which is the honest state.
- Native report adapters (Ginkgo, go test, Vitest, JUnit, Ansible) are
  W03; native report files are retained opaque here.
- Quarantine retention, maintenance, and backup are W05/W13; the
  quarantine is content-addressed by manifest digest and never cleaned
  by this unit.
- Per-source availability beyond the claimed `collection_state` records,
  and deployment of any of this, are later work.

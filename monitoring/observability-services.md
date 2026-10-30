# CI observability services (W05a pilot stack)

An explicit, opt-in, private monitoring-backend stack for the CI
observability project: PostgreSQL 17, single-process Loki, an isolated
short-retention CI Prometheus, and Grafana Alloy shipping **redacted**
spool logs to Loki. Deployed with `monitoring/scripts/observability-stack.sh`,
separate from the fleet monitoring stack (`monitoring-setup.sh`): different
home directory, different quadlet units, different containers. Neither
script touches the other's resources.

Everything here is a **pilot**: Loki runs single-process on local disk,
and every shipped log stream is labeled `osac_stack=ci-observability-pilot`
and `source=redacted-spool`. Fleet rollout requires configured, durable
object storage for logs and a trusted monitoring host. Tempo is out of
scope for W05a; it arrives in W05b.

| Component | Unit | Endpoint (loopback only) | Notes |
|---|---|---|---|
| PostgreSQL 17 | `ci-observability-postgres.service` | `127.0.0.1:5433` | database `ci_observability`, named podman volume `ci-observability-postgres-data` |
| Loki 3.7.9 | `ci-observability-loki.service` | `127.0.0.1:3100` (gRPC `127.0.0.1:9097`) | single-process, local-disk, 14-day retention |
| CI Prometheus v3.15.1 | `ci-observability-prometheus.service` | `127.0.0.1:9095` | 7-day retention, isolated from the fleet Prometheus on 9091 |
| Alloy v1.21.2 | `ci-observability-alloy.service` | `127.0.0.1:12345` | redacted spool to Loki; metrics endpoint scraped by the CI Prometheus |
| Read API (W01) | deployed separately | `127.0.0.1:9185` | `/metrics` reports whether its ledger health check succeeds |

The Loki gRPC port is deliberately not Loki's 9095 default, because the
CI Prometheus owns 9095 on the same host network.

## Deploying

```bash
cd <osac-test-infra checkout>
bash monitoring/scripts/observability-stack.sh --init-secrets   # once
bash monitoring/scripts/observability-stack.sh --check           # dry-run validation
bash monitoring/scripts/observability-stack.sh --deploy
bash monitoring/scripts/observability-stack.sh --status
```

`--deploy` is idempotent: it refreshes configs and quadlet units under
`~/.ci-observability` and `~/.config/containers/systemd/ci-observability-*.container`,
then restarts the four `ci-observability-*` services. Persistence comes
from the units' `[Install] WantedBy=default.target` sections: the
`daemon-reload` inside `--deploy` runs the quadlet generator, which
creates the units **and their `default.target.wants` enablement links**
(so the services return after reboot). `systemctl --user enable` is
deliberately never used: for generated units it always fails with
"transient or generated". `--stop` and `--start` manage only those four
units. `--status` exits nonzero if anything is unhealthy.

`--down` is the teardown counterpart, with exact semantics:

1. Stops the four installed `ci-observability-*` services, including any
   pending automatic restarts. Units absent from the manager are skipped.
2. Moves **only the four installed `ci-observability-*.container`
   definitions** -- by exact name, from `~/.config/containers/systemd` --
   into the project-owned, mode-700 directory
   `~/.ci-observability/disabled-quadlets/`, so locally edited
   definitions are preserved rather than discarded. A repeated `--down`
   keeps earlier saved copies under timestamped names instead of
   overwriting them.
3. Runs `daemon-reload` so the generator no longer provides the units.

`systemctl --user disable` is deliberately never used: it is a no-op
against generator enablement, and the units would return on the next
`daemon-reload`. After `--down`, everything else is preserved: data,
secrets, backups, spool, and the `ci-observability-postgres-data`
volume. Redeploy with `--deploy` (it reinstalls the repo unit
definitions). A full uninstall additionally removes
`~/.ci-observability/disabled-quadlets/`, `~/.ci-observability/`, and
the podman volume.

All state lives under `~/.ci-observability` (config, secrets, backups,
spool, Loki/Prometheus/Alloy data) plus the named podman volume for
PostgreSQL.

Boot persistence across reboots needs linger, which this script does not
change: `loginctl enable-linger "$USER"` if you want that.

`--check` validates the shipped configs with the real upstream binaries
(`loki -verify-config`, `promtool check config`, `alloy validate`) through
ephemeral containers, plus image pins, secrets, and port collisions.
`--check --offline` skips the container-based part and says so
explicitly: it is **not** a full acceptance run.

## Secrets and the trust boundary

`--init-secrets` creates `~/.ci-observability/secrets/postgres.env`
exclusively (atomic, mode 600, strong random values): the PostgreSQL
superuser password and the two role passwords. There is no default
password; the stack fails closed without this file. `--check` parses the
file strictly: exactly the three allowlisted keys, each once, values
restricted to a set that is safe for a Podman EnvironmentFile, file mode
600/400 and owned by the deploying user, and no symlinked or
group/world-accessible stack home or secrets directory. Secret values are
never printed, never appear on any command line (the Postgres bootstrap
reads them with psql `\getenv` inside the container), and are never
written to logs.

Limits of this model, on purpose:

- The file is mounted into the postgres container as an environment file,
  so a user who can run `podman inspect` as the same Unix user can read
  the values. That is the same boundary as the file itself.
- The loopback endpoints (PostgreSQL, Loki, Prometheus, Alloy) accept
  connections from **any process on the host**, not only the deploying
  user's. The boundary is the trusted monitoring host itself: everyone
  who can run code there is inside the trust perimeter. That is exactly
  why a shared CI runner is not acceptable and the approved monitoring
  host is required.
- **A shared CI runner's Unix UID is not a secure secret boundary.**
  Deploy this stack only from a dedicated, trusted monitoring
  account/host, never on a machine where PR-controlled jobs run as the
  same user. Production rollout needs the approved monitoring host from
  the implementation plan's approvals list.
- PR jobs get nothing: no public listener, no backend credentials. All
  endpoints bind 127.0.0.1 only.

## Database interface (for W01 and consumers)

Created on first boot by `config/observability/postgres-init-roles.sh`:

- Database `ci_observability`, with `search_path` defaulting to
  `ci_observability, public`.
- Schema `ci_observability` (not `public`), owned by the ingest role.
- Role `ci_observability` (LOGIN, connection limit 40): owns and writes
  the schema, may create temporary tables (migrations can use them).
  W01's migrations and the ingestion path use this role.
- Role `ci_observability_reader` (LOGIN, connection limit 20): SELECT
  only. It can never CREATE, INSERT, UPDATE, or DELETE, holds no TEMP
  privilege, and cannot create in schema public. Future tables are
  readable through default privileges, whichever role creates them
  (ingest or the bootstrap superuser), so W01's migrations need no extra
  grants. Dashboards and analysis use this role.
- The dedicated database inherits nothing from PUBLIC: `REVOKE ALL ON
  DATABASE` from PUBLIC, with explicit `GRANT CONNECT` for the two stack
  roles and `GRANT TEMP` for the ingest role only.

TCP connections use SCRAM-SHA-256 (`POSTGRES_INITDB_ARGS`). The
in-container unix socket keeps the image default so `podman exec`
tooling (backup, restore, `pg_isready`) needs no password; that socket
is not reachable off the host. W01 owns all table DDL inside the schema;
this stack only owns the roles, the schema, and the grants. No other
database on the host is touched.

## Log shipping: redacted spool only

Redaction happens **before** any storage, so this stack ships no raw
logs. There is no journald source and no host journal mount anywhere in
the units or the Alloy config, and no in-container regex "redactor" was
invented to work around that: a regex cannot make raw logs safe.

Alloy tails NDJSON files in the private, read-only-mounted spool
directory `~/.ci-observability/spool/redacted/`. That directory is the
proposed W06 producer interface:

```json
{"timestamp": "2026-10-27T23:55:00.123456789Z",
 "message": "already-redacted single line",
 "run_id": "...", "test_id": "..."}
```

- `timestamp`: RFC3339Nano UTC string; it becomes the entry's timestamp.
- `message`: the stored log line. W06 guarantees it is already free of
  credentials, raw command arguments, and environment dumps.
- `run_id`, `test_id`: the only allowlisted structured-metadata keys;
  any other JSON key is dropped. Run and test identity never becomes a
  stream label, so label cardinality cannot grow with runs.
- Stream labels are static and bounded: `osac_stack` and `source`, plus
  the `filename` label Alloy would add, which is dropped.

Malformed input behavior: lines that are not valid JSON are **dropped**
(`drop_malformed`), so malformed spool content never reaches Loki
verbatim. Valid JSON with an unexpected shape still passes through
as-is, and Loki itself adds generated metadata/labels such as
`detected_level` and `service_name` that are not spool input. Strict
schema validation, redaction, and drop accounting remain W06's
responsibility; no regex redactor exists here.

Until W06 writes spool files, this source stays idle: **a healthy Alloy
does not mean log collection is working**, and source-availability
reporting arrives with W06, not with this stack. `--status` reports
Alloy's readiness only.

Grafana integration prerequisite for W10a: expose Loki to Grafana through
**query-only proxy routes** (for example a proxy that permits the
`/loki/api/v1/query*` paths and blocks `/loki/api/v1/push` and the
delete APIs), not a raw Loki datasource with write access to the
loopback endpoint. Direct write access must stay with the trusted
collector pipeline.

## Retention and resource limits

| Resource | Limit |
|---|---|
| Loki logs | 14 days (compactor retention; Loki enforces a 24h minimum) |
| Loki ingest | 4 MB/s per tenant, 8 MB burst, 168h max age, no ancient backfill |
| Loki streams | 1000 per user (with a single tenant this is the effective cap) and 2000 global |
| CI Prometheus | 7 days, `retention.size=4GB` as a soft target |
| PostgreSQL | 40 connections, 128 MB shared buffers, dedicated-DB ACLs |
| Every container | hard `--memory` cap (512-768 MB), `--cpus` (0.5-1.0), `--pids-limit` |

Honest boundaries:

- `retention.size` is **not a hard filesystem cap** -- the write-ahead
  log, head blocks, and compaction scratch space can exceed it.
- Loki on local disk has no size-based retention; the time-based
  retention plus the ingest caps are the only bound.
- **No component here has a filesystem byte quota.** Numeric disk quotas
  for every data directory are an explicit W05/W13 rollout gate on the
  trusted monitoring host; nothing in this stack enforces one, and the
  host's disk can be exhausted by sustained ingest despite the soft
  limits above.
- The container memory/cpu/pids caps are real and are exercised with the
  same flags by the self-tests.

## Backup and restore

```bash
bash monitoring/scripts/ci-observability-backup.sh                 # -> ~/.ci-observability/backups
bash monitoring/scripts/ci-observability-restore.sh <dump-file>    # refuses to overwrite live data
bash monitoring/scripts/ci-observability-restore.sh <dump-file> --force
```

Backup dumps **only the `ci_observability` schema** (`pg_dump
--schema=... --format=custom`) through the running container
(`podman exec`), verifies the archive with `pg_restore --list` before
publishing it, writes into an exclusive mode-600 temp file, and
publishes atomically with a no-clobber hardlink: concurrent backups both
succeed with distinct names, and an existing backup is never
overwritten. Restore validates the archive first, then runs
`pg_restore --schema=ci_observability --clean --if-exists
--single-transaction`: only the stack schema is touched, and a
mid-restore failure rolls back and leaves the previous data unchanged.

By default restore **refuses** when the schema already holds any object
(tables, views, sequences, routines, standalone types -- not just
tables); `--force` is the explicit opt-in to drop and replace it.
Refusals also cover symlinked dump files and output roots.

Backups contain database content and are **trusted-DBA input**: restore
them only from sources you control, never from GitHub artifacts or
other untrusted storage. Loki pilot logs and CI Prometheus data are
disposable and not backed up; the durable facts are the database.

## Image pins

All four images are pinned by sha256 digest, verified 2026-10-27. No
unresolved pins.

| Image | Version | Digest (sha256) |
|---|---|---|
| `docker.io/library/postgres` | 17 (17.11) | `2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3` |
| `docker.io/grafana/loki` | 3.7.9 | `0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28` |
| `quay.io/prometheus/prometheus` | v3.15.1 | `fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2` |
| `docker.io/grafana/alloy` | v1.21.2 | `89a5a320168387336b9ac3c850609c3ba7757eadd4ae5f1d819deb35e9854b18` |

`observability-stack.sh --check` fails if any unit is not digest-pinned.
Bump pins deliberately, after verifying the new digest.

## Testing

`monitoring/tests/observability/test_observability_services.py` has two
tiers:

- Static tests always run with the monitoring unit tests, including a
  `--down` rehearsal against a private HOME and a recording fake
  systemctl (no live systemd is touched) and an offline enablement proof:
  the real podman quadlet generator run against private copied units
  must produce the four services plus their `default.target.wants`
  links, and `systemctl --root=<dir> --global enable` must create the
  same links offline.
- Real-container tests (PostgreSQL bootstrap over SCRAM with the
  no-secrets-in-argv property, backup and restore through container exec
  including refusal, single-transaction rollback, concurrent backups,
  and foreign-schema preservation, Loki ingest/query, real time-based
  retention deletion, CI Prometheus targets, applied container resource
  limits, and Alloy's redacted-spool path with sanitized fixture input
  including malformed-plaintext drops) run only when
  `CI_OBSERVABILITY_REAL_STACK=1` is set and podman works:

  ```bash
  python3 -m pip install -r monitoring/tests/requirements.txt -r monitoring/ci_observability/runtime-requirements.txt
  cd monitoring/tests
  CI_OBSERVABILITY_REAL_STACK=1 python3 -m pytest observability/test_observability_services.py --no-cov
  ```

  `--no-cov` disables the whole-suite coverage floor for this focused run.
  Without the variable these cases are SKIPPED and must be counted as
  uncovered, not passing. With the variable set, a missing podman or a
  failed container startup is a FAILURE, not a skip.

Executed and verified during W05a development (local rootless podman,
SELinux enforcing): the full real-container tier above, including the
argv sampling during bootstrap, the `\getenv` mechanism, and the
structured-metadata/allowlist properties of the spool pipeline.

Known gaps, stated as gaps (W05a is not complete):

- **Systemd deployment itself** (`--deploy` starting the quadlet units)
  was not executed on a live host; it needs the trusted monitoring host
  and is not authorized here. The container-level equivalents of every
  unit's configuration and resource limits were exercised directly.
- **Filesystem byte quotas** are unexecuted: they are a W05/W13 rollout
  gate on the trusted host, and no component enforces one today.
- **Real W06 collector output** has not flowed through the spool; the
  shipped path was exercised with sanitized fixture input only.
- The hosted `service-integration` job runs the real-container tier.
  The workflow has been linted and its test command executed locally;
  the GitHub job itself has not run because this branch has not been pushed.

## What other work packages own

- **W00/W01**: the ledger service behind `127.0.0.1:9185` and all table
  DDL in the `ci_observability` schema. The CI Prometheus scrape job for
  it already exists.
- **W06**: the redacting host collector that writes the spool files
  documented above, and source-availability reporting.
- **W10a**: Grafana integration through query-only proxy routes for
  Loki (see the log-shipping section), not raw write access.
- **W05b**: Tempo.
- The monitoring workflow owns the real-container test job. It has no
  backend credentials and does not deploy to a monitoring host.

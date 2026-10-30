# CI run investigation pilot

Inspect one GitHub Actions run attempt in a local, read-only Grafana dashboard.
The view shows job and step timing in minutes, outcomes and clickable GitHub
evidence links. It uses a manual metadata snapshot. It does not change CI or
run tests again.

## Start

Requirements: Python 3.11 or newer, an existing authenticated `gh` login,
Podman and curl. The login needs permission to read the selected run.

From the repository root, choose an unused loopback port:

```bash
CI_PILOT_TMP_DIR=/tmp/opencode CI_PILOT_PORT=42150 \
    ./monitoring/scripts/ci-observability-pilot.sh osac-project/osac 40548825479 1
```

The final argument selects the attempt. Omit it to select the latest attempt.
The launcher prints the dashboard URL, private runtime directory and the command
to stop that one container. An occupied port is refused before collection starts.

Optional settings:

- `CI_PILOT_PORT` defaults to `42148`.
- `CI_PILOT_TMP_DIR` selects the runtime's parent directory. Its default is
  `TMPDIR`, then `/tmp`. Inside OpenCode, prefer `/tmp/opencode`.
- `CI_PILOT_PYTHON` defaults to `python3` and selects the interpreter for both CLIs.

Grafana 12.0.1 and Infinity 3.11.1 are pinned. The server binds to `127.0.0.1`
and provides anonymous Viewer access. Login and basic authentication are disabled.
A random admin password stays in a private env file, outside command arguments.
The data source uses inline JSON and refuses URL-mode requests to real hosts.

Use the printed `podman stop <container>` command to stop only this pilot.
The launcher does not prune containers or delete shared data. If your browser
runs on another machine, forward the printed port over your usual SSH connection.

## How to read the view

Scan top to bottom:

1. The result and the job span in fixed minutes.
2. The failure location and its GitHub link.
3. The slowest measured steps, up to eight, labeled with the total measured count.
4. Job, step and artifact tables with durations in minutes and evidence links.
5. Identity, UTC times, provenance and coverage details at the bottom.

Durations are fixed minutes with two decimals, for example 118.40 min. Missing
durations read Unknown or Not finished, never zero. The notice at the top
states that the data is a manual GitHub snapshot and gives its fetch time.

Run links open the selected attempt. Job and artifact URLs use validated
numeric IDs and the repository name, never URLs supplied inside the input
snapshot. Text is escaped before display. Missing or incomplete pages remain
visible gaps. The full reading order, honesty rules and acceptance live in
[UI-REQUIREMENTS.md](UI-REQUIREMENTS.md).

## Refresh the snapshot

Grafana's Refresh button reruns the stored inline queries. It does not fetch GitHub.
To fetch new data, use the following commands from the repository root. Set
`runtime` to the directory printed by the launcher.

```bash
runtime=/path/printed/by/the/launcher
refresh=$(mktemp -d "$runtime/refresh.XXXXXXXX")
temporary="$runtime/dashboards/$(basename "$refresh").pending"

python3 -m monitoring.ci_observability.pilot \
    --repository osac-project/osac --run-id 40548825479 --attempt 1 \
    --output "$refresh/snapshot.json"

python3 -m monitoring.ci_observability.pilot_dashboard \
    --snapshot "$refresh/snapshot.json" --output "$temporary"

mv -- "$temporary" "$runtime/dashboards/pilot.json"
```

Each refresh keeps its own private snapshot. Existing files are never overwritten
by the collector or renderer. Grafana loads the replaced dashboard within about
five seconds; then reload the browser tab. Every panel describes what GitHub
reported at the displayed snapshot time.

Create the temporary dashboard inside `dashboards` before the rename.
On an enforcing SELinux host, this preserves the bind mount's container label.
A file created elsewhere keeps its old label after a rename and Grafana may
refuse to read it. No SELinux disabling or broad relabeling is needed.

## Ask an AI assistant about a run

Most investigations are AI-assisted. The report CLI gives an assistant the
dashboard's timing and result data as JSON on stdout. Select the snapshot used to
build the view; after a refresh, use `$refresh/snapshot.json`:

```bash
python3 -m monitoring.ci_observability.pilot_report \
    --snapshot "$runtime/snapshot.json" --section summary
```

Sections are summary, jobs, steps, failures, artifacts, slowest_steps and all.
The default is summary. The CLI reads the snapshot only: no network access, no
refetch of GitHub, no writes. Agent guidance for citations and data handling
is in [AI-QUERIES.md](AI-QUERIES.md).

## Limits

The pilot collects metadata only. It downloads no artifacts or raw logs and
does not publish canonical ledger facts. Test results, fixtures, worker activity,
cluster state and automated diagnosis remain outside this MVP.

GitHub's run head SHA is shown as reported. It does not prove the tested revision.
Artifacts are run-wide; their producing job and attempt remain unverified.
Passing and failed examples with different code are not a proven comparable baseline.

The snapshot is bounded to 1000 jobs and artifacts. The renderer accepts up to
500 jobs, 200 steps per job, 1000 artifacts and an 8 MiB snapshot. Unsupported
sizes fail explicitly; the view never silently samples those rows.

## Checks

Run the focused tests from the repository root:

```bash
python3 -m pytest -q --no-cov \
    monitoring/tests/observability/test_pilot_snapshot.py \
    monitoring/tests/observability/test_pilot_report.py \
    monitoring/tests/observability/test_pilot_dashboard.py
```

Against an already-running local pilot, point the integration test at the current
snapshot and generated dashboard:

```bash
CI_OBSERVABILITY_PILOT_GRAFANA_URL=http://127.0.0.1:42148 \
CI_OBSERVABILITY_PILOT_DASHBOARD="$runtime/dashboards/pilot.json" \
CI_OBSERVABILITY_PILOT_SNAPSHOT="$refresh/snapshot.json" \
python3 -m pytest -q --no-cov monitoring/tests/observability/test_pilot_grafana.py
```

Before a refresh, use `$runtime/snapshot.json` as the snapshot path instead.
These checks compare current data and text with the served dashboard, verify
source and query row counts, and confirm that writes and URL-mode queries are denied.
Without the three settings, server checks skip explicitly. They do not enable CI collection.

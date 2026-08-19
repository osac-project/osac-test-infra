# GitHub Actions Self-Hosted Runners (OSAC)

Scripts to manage org-level self-hosted GitHub Actions runners for the osac-project.

## Scripts

- **setup-runner-podman.sh** - One-time podman setup for runner machines (run first!)
- **action-runners-setup.sh** - Install and register GitHub Actions runners
- **action-runners-cleanup.sh** - Remove all runners

## Setup (New Runner Machine)

### Step 1: Configure Podman

```bash
sudo bash scripts/runners/setup-runner-podman.sh
```

### Step 2: Get a Registration Token

Go to: https://github.com/organizations/osac-project/settings/actions/runners/new

Or via API:
```bash
gh api -X POST orgs/osac-project/actions/runners/registration-token --jq .token
```

**Note:** Tokens expire in 1 hour!

### Step 3: Install Runners

**Must be run from a real login shell for `github-runner`** (`su - github-runner`,
not `sudo -u github-runner ...`). `sudo`'s `secure_path` Defaults setting
silently strips `/usr/local/bin` and `/usr/local/sbin` from `PATH` for any
`sudo -u`-invoked command -- and `config.sh` captures whatever `PATH` was
active at registration time into each runner's `.path` file, baking that
stripped `PATH` in *permanently* for every future job on that runner
instance. Since `oc`, `helm`, and `cluster-tool` all live under
`/usr/local/{,s}bin`, this breaks any job step that invokes them by bare name
(as opposed to sudoers-covered calls to fully-qualified command paths, which
work regardless). This has bitten real hosts twice (osac-1, osac-2) --
symptom is `oc: command not found` deep into an otherwise-working job, not an
obvious registration-time failure. Fix if it happens: overwrite the affected
runner(s)' `.path` file with the correct `PATH` (compare against a known-good
runner's `.path`, e.g. `cat action-runners/runner-1/.path` on an
already-working host) -- no service restart needed, it's read fresh per job.

```bash
./scripts/runners/action-runners-setup.sh <TOKEN> [NUM_RUNNERS]
```

Examples:
```bash
# Single runner (default)
./scripts/runners/action-runners-setup.sh AABBCCDDEE112233445566

# Two runners
./scripts/runners/action-runners-setup.sh AABBCCDDEE112233445566 2
```

### Step 4: Re-run Podman Setup

```bash
sudo bash scripts/runners/setup-runner-podman.sh
```

This configures the newly created runner services to use podman, and gives
each runner instance its own isolated podman storage graphroot/runroot --
multiple runner instances on one host all run as the same `github-runner`
user, so without this they'd share one podman storage backend, which
`containers/storage` doesn't support safely under concurrent builds (see the
comment in the script for the live-confirmed failure mode this caused:
intermittent "no such file or directory" errors mid-build, not a workflow bug).

### Step 5: Verify

```bash
# Check services
sudo systemctl status 'actions.runner.osac-project-*'

# View logs
sudo journalctl -u 'actions.runner.osac-project-*' -f

# Check in GitHub
# https://github.com/organizations/osac-project/settings/actions/runners
```

## Adding More Runners to an Existing Machine

`NUM_RUNNERS` is the **target total**, not an increment. To go from 5 runners to
10 on a machine that already has `runner-01..05` registered and running, get a
fresh token and re-run with the new total:

```bash
./scripts/runners/action-runners-setup.sh <TOKEN> 10
```

The script detects any runner directory whose systemd service is already
active and leaves it untouched (no re-download, no re-config, no service
restart) -- it only sets up the new indices (`06..10` in this example). This
makes the script safe to re-run at any time to reconcile a machine up to a
given runner count.

## Runner Configuration

- **Labels:** `self-hosted`, `osac-ci`
- **Names:** `<hostname>-runner-01`, `<hostname>-runner-02`, etc.
- **Base directory:** `~/action-runners/runner-N/`
- **Container runtime:** Podman (via `/var/run/docker.sock` symlink)

### Registering a dedicated runner (different labels/purpose)

`LABELS`, `BASE_DIR`, and `RUNNER_NAME_PREFIX` are overridable via env vars
on both `action-runners-setup.sh` and `action-runners-cleanup.sh`, so a
dedicated runner can be registered without colliding with or replacing the
default e2e runners' names/directories. Both scripts must be given the
same overrides for a given runner set. For example, the `monitoring-central`
runner used by the OSAC-2204 deploy pipeline:

```bash
# Register
LABELS="self-hosted,monitoring-central" \
BASE_DIR="$HOME/action-runners-monitoring" \
RUNNER_NAME_PREFIX="monitoring" \
  ./scripts/runners/action-runners-setup.sh <TOKEN> 1

# Clean up (must match the overrides used at registration time)
BASE_DIR="$HOME/action-runners-monitoring" \
RUNNER_NAME_PREFIX="monitoring" \
  ./scripts/runners/action-runners-cleanup.sh <TOKEN>
```

The `osac-ci-orchestrator` runner used by the on-demand EC2 ephemeral-runner flow
(see [`docs/ec2-ephemeral-runner-setup.md`](../../docs/ec2-ephemeral-runner-setup.md))
follows the same pattern -- registered as a dedicated label so it's never
queued behind the shared `osac-ci` fleet's VMaaS/CaaS e2e jobs:

```bash
# Register
LABELS="self-hosted,osac-ci-orchestrator" \
BASE_DIR="$HOME/action-runners-orchestrator" \
RUNNER_NAME_PREFIX="orchestrator" \
  ./scripts/runners/action-runners-setup.sh <TOKEN> 1

# Clean up (must match the overrides used at registration time)
BASE_DIR="$HOME/action-runners-orchestrator" \
RUNNER_NAME_PREFIX="orchestrator" \
  ./scripts/runners/action-runners-cleanup.sh <TOKEN>
```

## Workflow Usage

```yaml
jobs:
  e2e:
    runs-on: osac-ci
    steps:
      - run: echo "Running on self-hosted runner"
```

## Remove Runners

### With GitHub unregistration

```bash
./scripts/runners/action-runners-cleanup.sh <TOKEN>
```

### Local cleanup only

```bash
./scripts/runners/action-runners-cleanup.sh
```

Runners appear offline in GitHub until manually removed.

## Troubleshooting

### Container jobs fail with "permission denied"

Re-run podman setup:
```bash
sudo bash scripts/runners/setup-runner-podman.sh
```

### Runner not appearing in GitHub

- Check token hasn't expired (1 hour limit)
- Verify service: `sudo systemctl status actions.runner.osac-project-*`
- Check logs: `sudo journalctl -u 'actions.runner.osac-project-*' -f`

### After reboot

Re-run podman setup (idempotent):
```bash
sudo bash scripts/runners/setup-runner-podman.sh
```

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

The **OSAC CI Large** workflow (`e2e-vmaas-ci-large-caller.yml`) targets
`osac-ci-large` by default. Assign that label only to a host with capacity for
three 24-GiB MNO master VMs at once (72 GiB for the guests, plus host and other
workload overhead) and enough disk for the flavor copies. The cluster-tool
flavor directory and `/var/lib/libvirt/images` must be on the same filesystem,
and that filesystem must support reflinks: `compact-boot` creates disk copies
with `cp --reflink=always`. Before the first run, verify the selected runner's
mount layout and reflink support. On that dedicated host, register with
`LABELS="self-hosted,osac-ci-large"` so ordinary E2E jobs cannot use its
capacity. Install `guestfs-tools`, update the host's cluster-tool to a version
with `compact-boot`, and regenerate the scoped sudo rules:

```bash
sudo dnf install -y guestfs-tools
sudo git -C /opt/cluster-tool pull --ff-only
sudo install -m 0755 /opt/cluster-tool/cluster-tool /usr/local/bin/cluster-tool
sudo ./scripts/machine-init.sh runner-user
sudo ./scripts/machine-init.sh cluster-tool
```

The `cluster-tool` step initializes local cluster-tool state and configures
NetworkManager to use dnsmasq, including its configuration directory. The
runner-user step installs the root-owned `cluster-tool-compact-boot`,
`osac-compact-dns`, `osac-compact-boot-diagnostics`, and
`osac-compact-kubeconfig` helpers and refreshes sudoers. The boot wrapper
validates runner arguments and always keeps compact boot local. The DNS helper
makes the clone's API and `*.apps` routes resolve on the runner host. The
kubeconfig helper validates the clone and runner temp directory before staging
the root-owned kubeconfig without following symlinks. The diagnostics helper
records selected libvirt/network state in the existing scanned artifact flow
and writes a notice instead of copying the raw cluster-tool log, which may
contain sensitive data.

Before teardown, the MNO workflow also attempts to stage an existing clone
kubeconfig when boot fails. When available, cluster collection captures MCO
pod state, events, current/previous log tails, leader leases, ControllerConfig
Infrastructure/DNS inputs, and the two `00-master` API/CoreDNS identity files.
These diagnostics use the existing redaction and scanned upload flow. Earlier
failures without a kubeconfig retain the selected host diagnostics only. Cluster
collection is staged separately until redaction finishes, so a timeout or
cancellation preserves the completed host diagnostics and excludes partial
cluster output from upload.
Staging failures also retain the host bundle. If the final cluster-file copy
fails or is interrupted, the upload check discards its partial output and
keeps only the completed host diagnostics.

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

### Assisted flavor workflow on restricted runners

The on-demand Assisted flavor workflow runs on the `osac-ci-large` label.
Its default E2E selection is the real `tests/e2e/vmaas/sanity` suite from the
OSAC monorepo; the job records the test commit SHA. It uses the standalone
Assisted reusable workflow so the existing CI workflows remain unchanged.
It uses a fixed root-owned helper through polkit for source-cluster creation,
flavor capture, cluster-tool operations, and kubeconfig staging. The on-demand
workflow keeps captured flavors local and does not publish images.
Provision the runner account with `~/.config/osac-assisted/pull-secret.json`
and `~/.config/osac-assisted/aap-license.zip`, owned by that account with mode
`0600`; the containing directory must be owned by the account. The job copies
them into its temporary workspace and stages pull-secret registry auth through
the helper. This path does not use Vault or snapshot registry credentials.
The helper loads root's cluster-tool key into a short-lived SSH agent for
source capture and clone boot.
For MNO boots, its root-owned watchdog approves only CSRs whose signer,
requester, and certificate subject match the captured source nodes. It also
replaces stale OVN/MCO pods and clears transient MCD API errors when the
clone's node identity and rendered configuration already match. The captured
source prefix is stored in the helper's root-owned run directory.
On MNO clones, the job lowers the memory requests of the installer-provided
CoreDNS, HAProxy, and Keepalived static pods from 200 MiB to 64 MiB per
container. It waits for each master's pods to become Ready before proceeding.
The MNO E2E suite runs with one pytest worker so 8-GiB test VMs do not compete
for the remaining node capacity. Before tests, it creates the test tenant,
waits for its local StorageClass, and pins that class to the Ready master with
at least 8,628 MiB of unreserved memory. This keeps an 8-GiB VM's local disk
on a node that can also schedule its 8,500-MiB launcher. SNO keeps the
standard four workers.
Provision each eligible runner host once as root with:

```bash
./scripts/install-osac-privileged-helper.sh github-runner
```

For a test under a different account, pass that account instead (for example,
`./scripts/install-osac-privileged-helper.sh kni`).
The installer authorizes only the named user to invoke the fixed helper via
polkit. Assisted jobs invoke it with `pkexec --disable-internal-agent`; they
do not call sudo. Host package setup, libvirt configuration, and DNS service
setup remain administrator provisioning tasks. The workflow checks for the
helper before creating resources.

The host also needs the pinned cluster-tool at `/usr/local/bin/cluster-tool`
with root's cluster-tool configuration, plus `kind`, `skipper`, Podman,
Terraform, and the OpenShift CLI. SNO clone boot needs a configured HAProxy
service. The Assisted job builds its container image in the run; it does not
run Assisted Test Infra's system-changing `make setup` target.
The host's chronyd must be synchronized and allow NTP requests from the
Assisted source network (`192.168.127.0/24`). The workflow points the source
cluster at that network's host gateway (`192.168.127.1`) for time sync.
The runner account must also be able to run rootless Podman. The workflow
checks `podman info` as that account before it creates any cluster resources.
Resolve any rootless Podman startup error on the host before starting an
end-to-end run.
The Assisted workflow checks free space on the cluster-tool overlay
filesystem before creating the source cluster and again after booting the
test clone: at least 160 GiB for SNO or 240 GiB for MNO. These limits leave
room for the installer and E2E test VMs. A run exits before installation when
the host has less space; local captured flavors remain available for later
testing.

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

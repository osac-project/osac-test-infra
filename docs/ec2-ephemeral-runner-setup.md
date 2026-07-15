# On-Demand EC2 Ephemeral Runner: One-Time Setup

Setup guide for the on-demand EC2 bare-metal e2e flow. This document covers
only the one-time, human-driven setup steps needed before a suite-specific
caller (e.g. `e2e-caas-netris-caller.yml`) can be dispatched -- everything
else happens automatically per run. The underlying provisioning
(`ec2-runner-provision.yml`/`ec2-runner-teardown.yml`) is generic and shared
across suites; only the suite-specific workflow (its `test` job) differs
per consumer.

## Prerequisites

- An existing VPC subnet and security group for the ephemeral instances
  (security group must allow inbound SSH from the orchestrator's egress).
- A dedicated IAM user/credential scoped to exactly
  `RunInstances`/`TerminateInstances`/`DescribeInstances`/`DescribeInstanceStatus`/`CreateTags`,
  for storing in Vault (Step 3) -- not a broad admin credential.
- Access to the central Vault (root token or an operator token that can write
  to `secret/osac/e2e/*`), to run Step 3.
- The AWS CLI (`aws`) and GitHub CLI (`gh`) must be installed and on `PATH`
  for the user the orchestrator runner service(s) run as -- `provision.sh`/
  `teardown.sh` call `aws` directly, and `verify-and-register.sh`/
  `teardown.sh` call `gh`. Neither is a given on a general-purpose CI box:
  confirmed missing on a real orchestrator machine during acceptance
  testing, causing `Provision ephemeral EC2 instance` and `Verify tooling
  and register JIT runner` to fail with "command not found" well after the
  runner itself was working correctly. If the runner user has no root/sudo,
  both ship official portable installers that work entirely user-local (no
  system package manager needed):
  ```bash
  # AWS CLI v2, user-local
  curl -sS "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q -o /tmp/awscliv2.zip -d /tmp
  mkdir -p ~/.local/aws-cli ~/.local/bin
  /tmp/aws/install --install-dir ~/.local/aws-cli --bin-dir ~/.local/bin --update
  rm -rf /tmp/aws /tmp/awscliv2.zip

  # GitHub CLI, user-local
  GH_VERSION=$(curl -sS https://api.github.com/repos/cli/cli/releases/latest | grep -oP '"tag_name": "v\K[^"]+')
  curl -sSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" -o /tmp/gh.tar.gz
  tar xzf /tmp/gh.tar.gz -C /tmp
  mkdir -p ~/.local/bin
  cp "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" ~/.local/bin/gh
  rm -rf "/tmp/gh_${GH_VERSION}_linux_amd64" /tmp/gh.tar.gz
  ```
  Make sure `~/.local/bin` is on `PATH` for that user (it already is by
  default on most distros' login shells; confirm with `echo $PATH` as that
  user, since the runner service inherits its environment from there).

## Step 1: Register the osac-ci-orchestrator runner

The orchestrator is a **dedicated** self-hosted runner label, kept separate
from the shared `osac-ci` fleet so it's never queued behind existing
VMaaS/CaaS e2e jobs. Register it on an existing Vault-trusted machine using
the same override mechanism already used for the `monitoring-central`
runner:

```bash
LABELS="self-hosted,osac-ci-orchestrator" \
BASE_DIR="$HOME/action-runners-orchestrator" \
RUNNER_NAME_PREFIX="orchestrator" \
  ./scripts/runners/action-runners-setup.sh <TOKEN> 1
```

See [`scripts/runners/README.md`](../scripts/runners/README.md) for the full
registration/cleanup reference.

**Note:** this label guarantees a free GitHub Actions queue slot, not
physical CPU/network isolation -- if registered on a machine already running
other roles (central Vault, monitoring), resource contention with those
roles is still possible.

## Step 2: Generate the orchestrator's static SSH keypair

The orchestrator uses one static, non-rotating SSH keypair to bootstrap trust
with each freshly-launched ephemeral box (injected via cloud-init user-data,
trusted on first connect via `StrictHostKeyChecking=accept-new`). This key
never leaves the orchestrator machine and is **not** generated per run --
`ec2-runner-provision.yml`'s provision job fails loudly if it's missing.

As the user the orchestrator runner runs as, on the orchestrator machine:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/osac_ec2_orchestrator -N "" -C "osac-ec2-orchestrator"
```

## Step 3: Store the orchestrator's AWS credentials in Vault

The orchestrator authenticates to AWS with a static credential read from the
central Vault at workflow run time, the same pattern used for the pull-secret
and AAP license (`.github/actions/fetch-and-write-secrets`). This was chosen
over a GitHub-OIDC-role-assumption approach (no standing AWS credential):
OIDC avoided a standing secret but added a one-time AWS IAM/OIDC-provider
setup outside this repo; storing the credential in Vault instead reuses
infrastructure this repo already has (the `osac-e2e` AppRole and its
`secret/data/osac/e2e/*` policy wildcard already covers the new secret path
with no policy changes needed) at the cost of that standing credential.

Run once, on the central Vault host:

```bash
./vault/scripts/vault-add-ec2-runner-aws-creds.sh
./vault/scripts/vault-sync.sh
```

You'll be prompted for the access key id and secret access key (the secret
isn't echoed back). Add `--dry-run` to preview without writing to Vault.

Use the dedicated, narrowly-scoped IAM credential from Prerequisites above,
not a broad admin credential -- the orchestrator only ever calls
`RunInstances`/`TerminateInstances`/`DescribeInstances`/`DescribeInstanceStatus`/`CreateTags`.

## Step 4: Store a GitHub PAT for runner registration in Vault

`GITHUB_TOKEN` (the automatic workflow token) cannot call the self-hosted
runner registration endpoints (`generate-jitconfig`/delete) under any
`permissions:` grant -- confirmed during implementation. GitHub only allows
this via a PAT (classic `repo` scope, or fine-grained `Administration: write`)
or a GitHub App installation token. A standing credential is unavoidable
here, so this follows the same Vault-storage pattern as Step 3.

Create a **fine-grained** PAT (github.com → Settings → Developer settings →
Fine-grained tokens):
- Repository access: **only** `osac-project/osac-test-infra`
- Permissions: **Administration: Read and write** -- nothing else
- Set an expiration date and track it; fine-grained PATs don't auto-renew

Then store it, same pattern as Step 3:

```bash
./vault/scripts/vault-add-ec2-runner-github-pat.sh
./vault/scripts/vault-sync.sh
```

You'll be prompted for the token (not echoed back). Add `--dry-run` to
preview without writing to Vault.

## Step 5: Store the AMI/subnet/security group config in Vault

This repo is public: `workflow_dispatch` inputs are visible forever in the
Actions run history/API to anyone, and a subnet id or security group id
reveals real VPC layout. So the AMI id, subnet id, and security group id are
**not** dispatch inputs -- they're stored in Vault and fetched at run time
the same way the AWS credentials and GitHub PAT are.

Run once, on the central Vault host:

```bash
./vault/scripts/vault-add-ec2-runner-network-config.sh
./vault/scripts/vault-sync.sh
```

You'll be prompted for the AMI id, subnet id, and security group id. Add
`--dry-run` to preview without writing to Vault.

- AMI id: a stock AMI, no pre-baked tooling (AMI-baking is a possible
  follow-up). Only validated against Rocky Linux 9.6 so far -- a different
  distro/AMI may need `provision.sh`'s `SSH_USER`/cloud-init handling
  adjusted (see that script's comments for what's distro-specific)
- Subnet id, security group id -- from Prerequisites above

To change any of these later (e.g. testing a new AMI), re-run the script with
the new values and `vault-sync.sh` again.

## Step 6: Dispatch the workflow

From the GitHub UI (Actions → E2E CaaS Netris → Run workflow) or via
`gh workflow run`. `instance-type` (default `c5n.metal`) and `aws-region`
(default `us-east-1`) can optionally be overridden -- neither is sensitive.
Everything else (AWS credentials, GitHub PAT, AMI/subnet/security group) is
fetched from Vault automatically -- no other input needed on dispatch.

The workflow is `workflow_dispatch`-only for now (an interim risk gate) --
adding a PR trigger or schedule is a separate, deliberate follow-up.

## Orphan-instance watchdog

`ec2-runner-orphan-watchdog.yml` runs on a schedule (every 15 minutes, plus
manual `workflow_dispatch`) on the same `osac-ci-orchestrator` runner, and
terminates any `osac-ephemeral`-tagged instance that `ec2-runner-teardown.yml`
failed to clean up -- e.g. a cancelled run, an orchestrator crash mid-job,
or a GitHub outage that broke the `needs: [provision, test]` chain before
`teardown` ever ran. It's suite-agnostic (operates on AWS tags and GitHub
runner labels, not on which suite-specific workflow launched the instance),
so it doesn't need updating as new suites are added.

It terminates an instance if either is true: the GitHub Actions run it's
tagged with has already completed (so `teardown.sh` should have already run
but the instance is still alive), or the instance is older than a max-age
threshold (default 480 minutes -- generous headroom above the `test` job's
own 360-minute worst case) regardless of run status. Every instance it
examines is logged either way, whether or not it acts on it.

Override the threshold per-dispatch via the `max-age-minutes` input (no
workflow file edit needed) if it needs tuning -- e.g. once more real run
data narrows down how tight it can safely be.

To test it without risking a real termination, dispatch it manually with
`dry-run: true`.

# BMaaS tools: virtual bare-metal hosts outside CI

The BMaaS e2e tools are standalone — only `setup-virtual-bmh.sh`
knows about the CI. This walkthrough runs the full loop on any libvirt
host and OpenShift cluster: local development or a lab machine.

## Prerequisites

- A libvirt host (`virsh`, `qemu-img`) with a network for the VMs —
  `default` is used below.
- `python3` >= 3.11, `podman`, `curl`, `jq`.
- `oc` on `PATH`, pointing at the cluster through `KUBECONFIG` or its
  default kubeconfig.
- UEFI firmware at the Fedora OVMF paths, or
  `export VM_EFI_VARS=/path/to/OVMF_VARS.fd` on another distribution.

## 1. Start the Redfish infrastructure

The sushy-tools BMC emulator, its storage pool and the virtual-media
cache. The listen IP is the libvirt network's gateway — the cluster
nodes must reach the emulated BMCs:

```bash
GW_IP=$(virsh net-dumpxml default | sed -n "s/.*<ip address='\([^']*\)'.*/\1/p")

SUSHY_LISTEN_IP="${GW_IP}" \
STORAGE_POOL_NAME="bmh-local" \
STORAGE_DIR="${HOME}/virtual-bmh-disks" \
SUSHY_CONFIG_DIR="${HOME}/sushy-local" \
SUSHY_CACHE_NAME="sushy-cache-local" \
SUSHY_VMEDIA_TMP="${HOME}/sushy-vmedia-tmp" \
  bash .github/scripts/setup-sushy-tools.sh
```

Idempotent — safe to re-run.

## 2. Create VMs and the raw inventory

```bash
python3 .github/scripts/manage-virtual-bmh-vms.py create \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --network default \
  --disk-dir "${HOME}/virtual-bmh-disks" \
  --redfish-host "${GW_IP}" \
  --count 2
```

All hosts are named after a generated group prefix
(`virtual-bmh-<random>`) — the group is managed as a unit. Specs
default to 4 CPU / 8192 MiB / 150G per VM (`--cpu`, `--memory-mib`,
`--disk-size` to override). The CI wrapper exposes the equivalent
`BMH_CPU` and `BMH_MEMORY_MIB` environment overrides. Inspect what you got:

```bash
jq -r '.name_prefix, (.hosts[] | "\(.name)  \(.boot_mac)  \(.bmc_address)")' \
  "${HOME}/virtual-bmh-inventory.json"
```

## 3. Add and remove VMs

```bash
# add one more
python3 .github/scripts/manage-virtual-bmh-vms.py create \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --network default \
  --disk-dir "${HOME}/virtual-bmh-disks" \
  --redfish-host "${GW_IP}" \
  --count 1

# remove one by name
python3 .github/scripts/manage-virtual-bmh-vms.py delete \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  virtual-bmh-3f
```

## 4. Activate Ironic

Applies the metal3 Provisioning CR, waits for Ironic and its webhook.
Cluster-level configuration. Idempotent:

```bash
KUBECONFIG=/path/to/kubeconfig bash .github/scripts/activate-ironic.sh
```

## 5. Reconcile the BMH inventory on the cluster

One command applies new and changed hosts and removes the ones the
inventory no longer lists:

```bash
KUBECONFIG=/path/to/kubeconfig python3 .github/scripts/reconcile-bmhs.py \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --namespace host-inventory
```

## 6. Clean up

```bash
# remove every VM — this empties the inventory
python3 .github/scripts/manage-virtual-bmh-vms.py delete \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  $(jq -r '.hosts[].name' "${HOME}/virtual-bmh-inventory.json")

# reconcile the emptied inventory: every BMH of the group goes too
KUBECONFIG=/path/to/kubeconfig python3 .github/scripts/reconcile-bmhs.py \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --namespace host-inventory

# host side
kill "$(cat "${HOME}/sushy-local/sushy.pid")"
podman rm --force sushy-cache-local && podman volume rm sushy-cache-local
virsh pool-destroy bmh-local && virsh pool-undefine bmh-local
rm -rf "${HOME}/virtual-bmh-disks" "${HOME}/sushy-local" \
  "${HOME}/sushy-vmedia-tmp" "${HOME}/virtual-bmh-inventory.json"
```

## Going further

### CI lifecycle and reboot history

CI enables `manage-virtual-bmh-vms.py create --record-history`. The manager
atomically reserves exact domain names in the run inventory's `planned_names`
before launching a runner-account recorder, then performs normal VM creation.
Only successful creations enter `hosts`; partial failures retain the plan for
diagnostics and verified deletion removes it. There are no extra definitions,
power operations, guest inputs or boot configuration changes.

The recorder runs `virsh -c qemu:///system event --all --loop --timestamp --timeout 86400`
with `LC_ALL=C` and `TZ=UTC`. Launch ordering is best effort, not a subscription-readiness
handshake: initial events can be missed. Virsh's `--all` can tolerate failed
later event registrations; separate reboot delivery depends on QEMU support.
Provisioning never waits for an event acknowledgment or retries a definition.

Only timestamped lifecycle and separate reboot lines for exact inventory
`hosts` or `planned_names` are retained. The inventory is reloaded for each
matching event; the prefix grammar is only defense in depth. Missing or invalid
inventory disables selection with a diagnostic status, never broader capture.
Foreign, malformed, invalid UTF-8 and oversized lines are discarded without
printing hostwide output. Retention is capped at 2 MiB of events, lines/read
chunks at 4096 bytes, and runtime at 24 hours. Exit, deadline, byte cap and
collection cancellation stop and reap the managed CLI child. Existing history
directories are not overwritten by another recorder.

Private raw files live beside the inventory in its `.history` directory.
Collection stops recording, privately stages only events and status, and runs
the existing release gate with the collected secret inventory before copying
to `bmh-history/` in the artifact tree and creating the completion marker.
Failed gathering leaves no marker; failed event redaction releases no history.
Teardown retains the inventory/history if libvirt is unreachable or any domain
of the run remains, and removes history after confirmed domain removal.

Guest serial capture is supplied unchanged by the existing VM serial PTY/log
XML and disk-adjacent log path, and collected by `gather-osac-logs.sh` into
`worker-console/`. This enhancement adds no serial helper, permissions policy,
rotation guarantee or host rollout. The existing artifact scanning and
public/private upload routing are unchanged. Live libvirt delivery and
privileged serial permissions/rotation/SELinux behavior remain unverified here;
keep the PR draft pending host integration checks. These records do not
establish a guest failure cause.

`.github/scripts/setup-virtual-bmh.sh` — the CI orchestrator — wires
exactly these tools and is the shortest complete reference. The
semantics (naming scheme, verified deletions, reconcile rules) live in
each tool's docstring and in the `raw_inventory.py` library that owns
the inventory format.

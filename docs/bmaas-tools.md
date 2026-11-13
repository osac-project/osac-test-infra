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
`--disk-size` to override). Inspect what you got:

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

### CI reboot history

The CI setup enables `manage-virtual-bmh-vms.py create --record-history`.
Direct tool users opt in with the same flag. The inventory's random group
prefix is persisted first; a separate recorder registers both libvirt lifecycle
and reboot callbacks before acknowledging readiness and before any VM is
defined or started. UTC JSONL records include domain name, UUID and numeric
libvirt lifecycle state/detail. A reboot is a separate event, not an inferred
state transition. These observations do not establish a guest failure cause.

The recorder uses `libvirt-python`, already installed by `setup-sushy-tools.sh`,
and a read-only `qemu:///system` connection. It needs no guest agent. Registration
failure or a ten-second readiness timeout disables recording without failing
provisioning. Existing history directories are never shared or overwritten by
another recorder. Event retention is capped at 2 MiB and recorder lifetime at
24 hours; status reports event truncation. Long runs can therefore have gaps.

Passive serial logging is configured in domain XML before first boot with
`<log append="on">`, preserving the PTY console. It captures only output the
guest already sends to its serial device; it does not change kernel arguments,
connect a console client, or send input. Serial logging requires libvirt 10.10+
and verified standard, running libvirtd/virtqemud and virtlogd configuration:
`stdio_handler=logd`, positive `max_size` no greater than 2097152 bytes, and
`max_backups` no greater than three. Default settings meet these limits.
Nonstandard daemon configuration paths, unreadable configuration/process
metadata, configuration newer than its daemon, and unrecognized size syntax
disable serial capture explicitly in `status.json`. No daemon settings change.
Administrators must not reload/change the logging configuration during a run.

This backend choice follows libvirt's
[QEMU character-device preparation](https://github.com/libvirt/libvirt/blob/v10.10.0/src/qemu/qemu_process.c)
and [virtlogd handler](https://github.com/libvirt/libvirt/blob/v10.10.0/src/logging/log_handler.c):
with logd selected, serial log writes use its rotating-file writer, not an
unbounded QEMU file. At most four 2 MiB serial files remain per VM. Rotation
retains recent output; append preserves retained history across guest reboots
and domain stop/start, not an unlimited archive. Driver disconnects or missed
events are not reconstructed afterward.

Raw history lives next to the run inventory in its `.history` directory (0700),
outside the upload tree. The privileged libvirt logging daemon opens the serial
file and passes QEMU a descriptor; QEMU needs no traversal permission on that
private directory. Rotated files can be root-owned 0600, so collection uses
bounded, captured `sudo -n head` reads only when needed. This uses the existing
CI runner privilege model, never prints serial data, and grants no new access.
SELinux denial or another log-open failure can make first start fail: only a
confirmed still-shut-off domain is retried without the optional log, preserving
the original PTY and leaving ordinary start failures as failures. No SELinux
policy or host permissions are relaxed. Live runner/SELinux validation remains
necessary; mock tests cannot establish host compatibility.

Collection requests bounded recorder shutdown before cluster gathering, including
failed and cancelled collection attempts. Serial snapshots are capped at 2 MiB
per file and first pass through the existing secret release gate in private
temporary staging, using the pull secret and collected Kubernetes secret inventory.
Only successfully censored copies enter `osac-artifacts/bmh-history`; normal
redaction markers, gitleaks, upload release gates and cancellation conditions
remain unchanged. Collection failure omits optional history, not test results.
Teardown stops the recorder again and removes raw history only after libvirt
confirms no domains in the inventory's group remain; unreachable libvirt or
surviving domains preserve the raw directory for cleanup. The inventory path is
exported before VM creation, so partial setup remains discoverable.

To roll back, revert the reboot-history change; no persistent daemon or guest
configuration migration is needed.

`.github/scripts/setup-virtual-bmh.sh` — the CI orchestrator — wires
exactly these tools and is the shortest complete reference. The
semantics (naming scheme, verified deletions, reconcile rules) live in
each tool's docstring and in the `raw_inventory.py` library that owns
the inventory format.

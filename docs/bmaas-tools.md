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
prefix is persisted first; a managed runner-account process executes
`virsh -c qemu:///system event --all --loop --timestamp --timeout 86400` with
`LC_ALL=C` before the normal domain definition. Only lifecycle and separate
reboot lines for exact run-owned names enter `events.log`; foreign-domain and
other event output is discarded without being printed or persisted. Selection
requires exact membership in the raw inventory's `hosts` or optional
`planned_names`, with the prefix/name grammar checked only as defense in depth.
The manager atomically persists all reserved names in `planned_names` before
launching the listener; it adds real `hosts` entries only after successful
creation. Partial failures retain the plan without fabricating provisioned hosts.
Verified deletion removes the corresponding plan entries. The recorder reloads
the inventory before each matching event (and serial staging uses the same
allowlist), so an unlisted same-prefix domain is never implicitly selected.
Missing/malformed inventories disable selection explicitly in `status.json`
without broadening capture or failing provisioning. No guest
agent or Python libvirt callback subscriber is needed. These observations do
not establish a guest failure cause.

Before first start, the manager waits for a matching Defined Added/Updated event.
If needed, it retries the identical XML definition at most twice, only for the
same owned domain confirmed shut off. Each acknowledgment wait is at most one
second, and optional state/define commands each time out after two seconds.
A missing acknowledgment or failed optional retry emits a generic warning and
continues the original start; it does not change the provisioning outcome.
`ready.json` means the recorder launched, **not** that subscriptions registered.
This establishes observed lifecycle delivery before first boot on success, not
capture of the initial definition or atomic confirmation of every callback.
Upstream [virsh](https://github.com/libvirt/libvirt/blob/v12.0.0/tools/virsh-domain-event.c)
registers lifecycle before reboot and silently tolerates later registration
failures under `--all`. Reboot capture therefore depends on normal QEMU reboot
event support; `event --list` only lists client event names and cannot verify it.

Existing history directories are never shared or overwritten by another recorder.
Retained events are capped at 2 MiB, input chunks/accepted lines at 4096 bytes,
and lifetime at 24 hours. The child is stopped and reaped on cap, deadline,
collection cancellation, or early exit; `status.json` records the stop reason.
Long runs and rejected oversized lines can therefore have gaps. Serial capture
is prepared independently and can remain available after the event CLI exits.

Passive serial logging is configured in domain XML before first boot with
`<log append="on">`, preserving the PTY console. It captures only output the
guest already sends to its serial device; it does not change kernel arguments,
connect a console client, or send input. Serial logging requires libvirt 6.0+
with the upstream logd character-device backend and standard running
libvirtd/virtqemud and virtlogd configuration:
`stdio_handler=logd`, positive `max_size` no greater than 2097152 bytes, and
`max_backups` no greater than three. The installed helper checks daemon version,
process identity, standard config paths and config age as root. Connecting to
the existing virtlogd socket activates a socket-managed daemon before checking
its PID/config; this changes no daemon settings. Missing or commented options
use **assumed upstream defaults**, not queried effective settings, and
`defaults_assumed` records that distinction. Those defaults are logd, 2 MiB,
and three backups. A completely missing config also requires its parent
directory to predate the daemon; otherwise serial capture is declined.
Nonstandard daemon configuration paths, unreadable configuration/process
metadata, configuration newer than its daemon, and unrecognized size syntax
disable serial capture explicitly in `status.json`. No daemon settings change.
Administrators must not reload/change the logging configuration during a run.
The config-age check cannot detect a reload with subsequently restored mtimes.
Vendor builds must preserve upstream defaults/backend behavior; there is no
API here querying effective logd retention. Unpinned package installation does
not itself establish those settings or a particular installed version.

This backend choice follows libvirt's
[6.0 QEMU character-device preparation](https://github.com/libvirt/libvirt/blob/v6.0.0/src/qemu/qemu_command.c),
[12.0 host character-device preparation](https://github.com/libvirt/libvirt/blob/v12.0.0/src/qemu/qemu_process.c),
[default retention](https://github.com/libvirt/libvirt/blob/v12.0.0/src/logging/log_daemon_config.c)
and [virtlogd handler](https://github.com/libvirt/libvirt/blob/v10.10.0/src/logging/log_handler.c):
with logd selected, serial log writes use its rotating-file writer, not an
unbounded QEMU file. At most four 2 MiB serial files remain per VM. Rotation
retains recent output; append preserves retained history across guest reboots
and domain stop/start, not an unlimited archive. Driver disconnects or missed
events are not reconstructed afterward.

Events/status live next to the run inventory in its `.history` directory (0700).
Raw serial lives under the fixed root-owned 0700 directory
`/var/log/libvirt/qemu/osac-bmh-history/<runner-uid>/<random-run-prefix>`, never
under the upload tree. The privileged libvirt logging daemon opens the serial
file and passes QEMU a descriptor; QEMU needs no traversal permission on that
private directory. Current and rotated files are root-owned 0600, so collection
uses only `sudo -n /usr/local/sbin/osac-bmh-history`. The root-installed isolated
Python helper accepts fixed commands and validated run/name arguments, scopes
paths by `SUDO_UID`, rejects symlinks, hardlinks and nonregular files, and reads
at most 2 MiB per file. It exposes neither arbitrary paths nor generic root
commands. Raw helper stdout is captured privately, never forwarded to step logs.
SELinux denial or another log-open failure can make first start fail: only an
error naming the configured serial file and a confirmed still-shut-off domain
allow retry without the optional log, preserving
the original PTY and leaving ordinary start failures as failures. No SELinux
policy or host permissions are relaxed. Live runner/SELinux validation remains
necessary; mock tests cannot establish host compatibility. Standard log-directory
SELinux labeling and descriptor-passing behavior are assumed, not proven here.

**Host rollout is required before enabling this on existing runners:** rerun
`scripts/machine-init.sh runner-user` from this checkout (as the host
administrator), or the fleet `machine_base` runner-user task. Both install the
root-owned 0755 helper before generating its narrow sudoers allowance. The
existing policy does not permit `head` and is not widened to do so. Fresh hosts
use this same install path; `packages` installs unpinned libvirt and `services`
enables libvirtd. With the installed helper, normal upstream defaults and
standard socket/process paths, setup captures serial from the first start,
including all three rotation suffixes. Old hosts without this rollout explicitly
report serial unavailable while still recording events; checking out the PR
alone cannot install privileged host prerequisites.
The [upstream RPM manifest](https://github.com/libvirt/libvirt/blob/v12.0.0/libvirt.spec.in)
provides the root-owned 0700 `/var/log/libvirt/qemu` parent directory. The helper
creates only its private subtree, not arbitrary log-directory parents.

Collection requests bounded recorder shutdown before cluster gathering, including
failed and cancelled collection attempts. Serial snapshots are capped at 2 MiB
per file and first pass through the existing secret release gate in private
temporary staging, using the pull secret and collected Kubernetes secret inventory.
Only successfully censored copies enter `osac-artifacts/bmh-history`; normal
redaction markers, gitleaks, upload release gates and cancellation conditions
remain unchanged. Collection failure omits optional history, not test results.
Teardown stops the recorder again and removes raw history only after libvirt
confirms no domains in the inventory's group remain; unreachable libvirt or
surviving domains preserve the raw directory for cleanup. A rejected privileged
cleanup also retains the local history and inventory for retry, without turning
optional diagnostics cleanup into a teardown failure. The inventory path is
exported before VM creation, so partial setup remains discoverable.

To roll back, revert the reboot-history change; no persistent daemon or guest
configuration migration is needed.

`.github/scripts/setup-virtual-bmh.sh` — the CI orchestrator — wires
exactly these tools and is the shortest complete reference. The
semantics (naming scheme, verified deletions, reconcile rules) live in
each tool's docstring and in the `raw_inventory.py` library that owns
the inventory format.

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

Keep the same optional disk setting for every creation in this inventory. The
examples use a Bash array that is empty by default (single-disk BMaaS).

```bash
DATA_DISK_ARGS=()
# Optional: uncomment before creating this inventory; keep for later additions.
# DATA_DISK_ARGS=(--data-disk-size 50G)

python3 .github/scripts/manage-virtual-bmh-vms.py create \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --network default \
  --disk-dir "${HOME}/virtual-bmh-disks" \
  --redfish-host "${GW_IP}" \
  --count 2 \
  "${DATA_DISK_ARGS[@]}"
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

Optionally pass `--data-disk-size 50G` to `create` to attach a separate sparse,
unbacked qcow2 disk at `vdb`. The default is disabled; the OS disk remains
150G at `vda`. Sizes are positive integers with an optional `k`, `M`, `G`, or
`T` suffix. CI forwards `BMH_DATA_DISK_SIZE` to this option; only CaaS enables
it, with `50G`. BMaaS keeps its single-disk default.

This option is per invocation, not persisted in the inventory. Keep
`DATA_DISK_ARGS` enabled for every later addition to a data-disk inventory:
omitting the option adds OS-only VMs, and a later enabled creation rejects the
mixed inventory. The add-one example reuses the array from step 2 in the same
Bash session.

Creation remains additive: each call adds `--count` fresh VMs, even when an
inventory already exists. Enabled creation first checks every existing host's
owned OS disk, data disk and NVRAM. An old single-disk inventory or incompatible
storage is rejected without repairing, attaching, or deleting anything.
`validate-data-storage --inventory-file FILE --disk-dir ABSOLUTE_DIRECTORY`
runs this read-only preflight separately; CI runs it before starting services.
Enabled creation also rejects name or storage collisions, including dangling
symlinks, rather than overwriting them. Before any external commands, enabled
creation also rejects direct data-file remnants (including dangling links) for
unrecorded hosts of the inventory's exact group. Other groups and sibling clone
directories are not claimed or scanned. Without an inventory group, a remnant
matching the tool's generated VM naming convention in the supplied disk directory
has ambiguous ownership and is rejected without mutation. An empty or nonexistent
directory and unrelated shared files remain valid. Validation never repairs
storage or generates an inventory prefix; resolve ambiguous leftovers explicitly
before retrying. Default-disabled creation keeps its upstream additive behavior.

For direct `delete`, optionally supply `--disk-dir ABSOLUTE_DIRECTORY` to authorize
data-file cleanup. It must be an existing canonical directory without symlink or
`..` components. Only the exact regular, non-symlink direct-child data source is
removed, after domain absence is verified. Warnings about retained outside paths,
aliases, symlinks or data files without this option apply only to matching data
sources known from the domain XML. If XML is unavailable (for example, the domain
is already absent), deletion can remove the inventory entry without identifying
or warning about leftover data files. Before retrying enabled creation, which
rejects orphan data remnants, inspect leftovers manually. Remove a file only
after confirming inventory-group ownership, the owning domain's absence, and
that no live VM references it; do not infer ownership from its basename alone.
Existing OS disk and NVRAM cleanup is unchanged. CI's separate clone-scoped
teardown continues to clean its custom disk directory as before.

```bash
# add one more
python3 .github/scripts/manage-virtual-bmh-vms.py create \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --network default \
  --disk-dir "${HOME}/virtual-bmh-disks" \
  --redfish-host "${GW_IP}" \
  --count 1 \
  "${DATA_DISK_ARGS[@]}"

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
# Add --disk-dir "${HOME}/virtual-bmh-disks" to authorize opted-in data-file cleanup.
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

`.github/scripts/setup-virtual-bmh.sh` — the CI orchestrator — wires
exactly these tools and is the shortest complete reference. The
semantics (naming scheme, verified deletions, reconcile rules) live in
each tool's docstring and in the `raw_inventory.py` library that owns
the inventory format.

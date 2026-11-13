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
GW_IP=$(virsh -c qemu:///system net-dumpxml default | sed -n "s/.*<ip address='\([^']*\)'.*/\1/p")

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

# remove one by name — the full '<name_prefix>-<token>' form, e.g.
# virtual-bmh-9f2b7c31-3f; the inventory's hosts[].name entries list them
python3 .github/scripts/manage-virtual-bmh-vms.py delete \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  "$(jq -r '.hosts[0].name' "${HOME}/virtual-bmh-inventory.json")"
```

## 4. Activate Ironic

Applies the metal3 Provisioning CR, waits for Ironic and its webhook.
Cluster-level configuration. Idempotent:

```bash
KUBECONFIG=/path/to/kubeconfig bash .github/scripts/activate-ironic.sh
```

## 5. Reconcile the inventory on the cluster

Both backends consume the same raw inventory; run the one matching the
cluster's bare-metal fulfillment inventory backend.

### Metal3 — BareMetalHosts

One command applies new and changed hosts and removes the ones the
inventory no longer lists:

```bash
KUBECONFIG=/path/to/kubeconfig python3 .github/scripts/reconcile-bmhs.py \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --namespace host-inventory
```

### NetBox — devices

When the cluster's bare-metal fulfillment runs the NetBox inventory
backend, the same raw inventory drives NetBox devices instead of
BareMetalHosts — the operator creates each BMH itself when it claims a
device.

Prerequisites: the `osac` CLI on PATH and logged in, plus the NetBox
endpoint and token. `pynetbox` comes with this repository's Python
environment:

```bash
python3 .github/scripts/reconcile-netbox-devices.py \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --url https://netbox.example.test \
  --token-file netbox-token.txt
```

Inside a cluster over plain HTTP (NetBox behind a service, token from
the deployment), pass `--insecure`:

```bash
OSAC_NETBOX_URL=http://osac-infra-netbox.osac-infra.svc.cluster.local \
OSAC_NETBOX_TOKEN="${NETBOX_TOKEN}" \
python3 .github/scripts/reconcile-netbox-devices.py \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" --insecure
```

Per run:

- one staged device per host, stamped with the OSAC custom fields
- one BMC Secret per host in the OSAC Secret API, labeled for its device
- the NetBox schema (site, role, manufacturer, device type, custom
  fields) bootstrapped when missing

Idempotent — re-running creates nothing. An emptied inventory removes
the whole group, devices and Secrets: that is the NetBox-mode cleanup
path.

## 6. Clean up

`set -e` stops the block at the first failure, so the `rm -rf` only runs
after the deletions succeeded.

```bash
set -e
# remove every VM — this empties the inventory
python3 .github/scripts/manage-virtual-bmh-vms.py delete \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  $(jq -r '.hosts[].name' "${HOME}/virtual-bmh-inventory.json")

# reconcile the emptied inventory: every BMH of the group goes too.
# NetBox backend: same inventory, same connection settings as step 5 —
# the emptied inventory removes the group's devices and BMC Secrets.
KUBECONFIG=/path/to/kubeconfig python3 .github/scripts/reconcile-bmhs.py \
  --inventory-file "${HOME}/virtual-bmh-inventory.json" \
  --namespace host-inventory
# OSAC_NETBOX_URL=... OSAC_NETBOX_TOKEN=... \
#   python3 .github/scripts/reconcile-netbox-devices.py \
#   --inventory-file "${HOME}/virtual-bmh-inventory.json"

# host side
kill "$(cat "${HOME}/sushy-local/sushy.pid")"
podman rm --force sushy-cache-local && podman volume rm sushy-cache-local
virsh -c qemu:///system pool-destroy bmh-local && virsh -c qemu:///system pool-undefine bmh-local
rm -rf "${HOME}/virtual-bmh-disks" "${HOME}/sushy-local" \
  "${HOME}/sushy-vmedia-tmp" "${HOME}/virtual-bmh-inventory.json"
```

## Going further

`.github/scripts/setup-virtual-bmh.sh` — the CI orchestrator — wires
exactly these tools and is the shortest complete reference. Its NetBox
counterpart, `.github/scripts/setup-virtual-netbox-devices.sh`, differs
only in the last step: `reconcile-netbox-devices.py`, the NetBox token
lookup (the superuser Secret's `api_token` key) and a port-forward to
the in-cluster service. The
semantics (naming scheme, verified deletions, reconcile rules) live in
each tool's docstring and in the `raw_inventory.py` library that owns
the inventory format.

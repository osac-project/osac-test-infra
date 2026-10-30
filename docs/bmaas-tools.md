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

## 6. Reconcile the NetBox inventory instead

When the cluster's bare-metal fulfillment runs the NetBox inventory
backend, the same raw inventory drives NetBox devices instead of
BareMetalHosts — the operator creates each BMH itself when it claims a
device. Needs the `osac` CLI on PATH, logged in, and the NetBox
endpoint and token (`pynetbox` comes with this repository's Python
environment):

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

One staged device per host, stamped with the OSAC custom fields, plus
one BMC Secret per host in the OSAC Secret API, labeled for its device.
The NetBox schema (site, role, manufacturer, device type, custom
fields) is bootstrapped when missing. Idempotent — re-running creates
nothing — and an emptied inventory removes the whole group, devices and
Secrets: that is the NetBox-mode cleanup path.

## 7. Clean up

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

`.github/scripts/setup-virtual-bmh.sh` — the CI orchestrator — wires
exactly these tools and is the shortest complete reference. Its NetBox
counterpart, `.github/scripts/setup-virtual-netbox-devices.sh`, swaps
only the last step for `reconcile-netbox-devices.py` and adds the
NetBox token lookup (the superuser Secret's `api_token` key) plus a
port-forward to the in-cluster service. The
semantics (naming scheme, verified deletions, reconcile rules) live in
each tool's docstring and in the `raw_inventory.py` library that owns
the inventory format.

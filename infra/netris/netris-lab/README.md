# Netris Spectrum-X GPU Cluster Lab

Automated deployment of a Netris Spectrum-X GPU cluster network simulation lab on a single bare-metal server. Creates KVM VMs simulating Cumulus Linux switches, Ubuntu GPU servers, Netris softgates, an ISP server, and a management server — all orchestrated by a Netris controller on K3s.

## Prerequisites

- **OS**: RHEL 9.x or Rocky Linux 9.x
- **Access**: Root
- **Hardware**: 16+ cores, 64GB+ RAM, 100GB+ disk in `/var/lib/libvirt/images`
- **Network**: Internet access for image/package downloads
- **Tools**: Ansible installed (`dnf install -y ansible-core`)
- **License**: Netris license key at `./license.key` (gitignored)

All other dependencies (EPEL, libvirt, qemu-kvm, Go, Pulumi, OpenTofu, openvpn, skopeo, etc.) are installed automatically by the `prerequisites` role.

## Quick Start

```bash
git clone <repository-url> netris-lab && cd netris-lab

# Place your Netris license key (not committed to git)
cp /path/to/license.key ./license.key

# Configure (optional — defaults work out of the box)
vim group_vars/all.yml

# First deploy + cache images for future cycles
make setup

# Or just deploy (without caching)
make deploy

# Verify everything is healthy
make verify
```

Deployment takes ~15-30 minutes depending on image cache state.

## Configuration

All variables are in `group_vars/all.yml`.

### Lab Scale

| Variable | Default | Description |
|----------|---------|-------------|
| `gpu_server_count` | `4` | Number of GPU servers (minimum 1) |
| `gpu_server_hostname` | `"hgx"` | GPU server name prefix |
| `site_name` | `"Datacenter-1"` | Netris site name |
| `ew_fabric_enable` | `0` | `1` to enable East-West fabric, `0` for NS-only |

### Controller

| Variable | Default | Description |
|----------|---------|-------------|
| `netris_controller_chart_version` | `"2.8.1"` | Helm chart version |
| `netris_login` | `"netris"` | Controller username |
| `netris_password` | `"netris"` | Controller password |
| `k3s_api_port` | `6444` | K3s API port (6443 often in use) |
| `controller_ui_port` | `9443` | Fixed port for controller web UI |

### Connectivity

| Variable | Default | Description |
|----------|---------|-------------|
| `connectivity_mode` | `"isolated"` | `"isolated"` or `"public"` (see below) |
| `bgp_password` | `"newNet0ps"` | BGP session password |
| `bgp_link_subnet` | `"198.51.100.8/29"` | BGP peering subnet (isolated mode) |

### North-South Fabric (`ns_fabric.*`)

| Variable | Default | Description |
|----------|---------|-------------|
| `enable` | `1` | Enable NS fabric |
| `leaf_count` | `2` | NS leaf switches |
| `spine_count` | `2` | NS spine switches |
| `softgate_count` | `4` | Softgate VMs |
| `softgate_roles` | `'["general","general","snat","snat"]'` | Softgate role assignment |
| `softgate_node_type` | `"softgate_hs"` | Installer node type (dpdk disabled post-install) |
| `oob_leaf_count` | `1` | OOB leaf switches |
| `oob_gpu_per_switch` | `4` | GPU servers per OOB leaf |

### VM Resources

| Variable | Default | Description |
|----------|---------|-------------|
| `switch_vcpu` | `4` | vCPU per switch VM |
| `switch_memory` | `2048` | Memory (MB) per switch VM |
| `server_vcpu` | `1` | vCPU per GPU server VM |
| `server_memory` | `1024` | Memory (MB) per GPU server VM |
| `softgate_vcpu` | `2` | vCPU per softgate VM |
| `softgate_memory` | `4096` | Memory (MB) per softgate VM |
| `servers_gw` | `"192.168.16.1/20"` | Server management gateway |

### Connectivity Modes

**`isolated` (default)** — No public IPs needed. BGP uses TEST-NET-3 ranges (198.51.100.x). The host masquerades traffic for internet access. Good for CI and environments without public IPs.

**`public`** — Real public IPs. A physical interface is bridged to `br-public` for real BGP peering:

```yaml
connectivity_mode: "public"
public_interface: "eth1"                    # bare NIC or VLAN (bond0.64)
public_bgp_link_subnet: "203.0.113.0/29"
public_nat_cidr: "203.0.113.8/30"
public_l4lb_cidr: "203.0.113.12/30"
public_bgp_subnets_to_advertise:
  - "203.0.113.8/30"
  - "203.0.113.12/30"
```

## Usage

```bash
make setup           # prerequisites + cache (first time)
make deploy          # Full deployment (all roles in sequence)
make destroy         # Tear down everything (K3s, VMs, topology)
make verify          # Health check — asserts all switches, softgates, E-BGP healthy
make connectivity    # Re-run connectivity only (VPN, socat, softgates, ISP)
make cache           # Save K3s container + cloud images to local cache
```

### Image Cache

After the first deploy, `make cache` (or `make setup`) saves all container and cloud images to `/var/cache/netris-lab/`. Subsequent deploy/destroy cycles load from cache — no Docker Hub pulls, no rate limits.

### East-West Fabric

Set `ew_fabric_enable: 1` to deploy the EW GPU fabric (leaf-spine) in addition to the NS fabric. With 4 GPU servers this creates 1 EW leaf + 1 EW spine. With 32+ servers it scales to the full Spectrum-X topology (4+ leaves, 2+ spines).

When disabled (`ew_fabric_enable: 0`), only the North-South fabric is deployed — fewer VMs and resources.

## Accessing the Lab

**Controller UI:**
```
http://<server-ip>:9443
Login: netris / netris
```

**Switches** (Cumulus Linux):
```bash
ssh cumulus@10.253.0.1    # password: newNet0ps!
```

**Management server:**
```bash
ssh root@192.168.122.10
```

**Softgates:**
```bash
ssh root@10.3.3.1        # ns-softgate-0
```

## Project Structure

```
netris-lab/
├── Makefile
├── ansible.cfg
├── license.key                         # your license (gitignored)
├── group_vars/all.yml                  # all configuration
├── inventory/lab.yml
├── playbooks/
│   ├── cache.yml                       # image caching
│   ├── deploy.yml                      # full deploy
│   ├── destroy.yml                     # full teardown
│   ├── prerequisites.yml               # system dependencies
│   ├── verify.yml                      # health check — asserts switches/softgates/E-BGP
│   └── connectivity.yml                # connectivity-only re-run
├── roles/
│   ├── prerequisites/                  # Go, Pulumi, OpenTofu, libvirt, bridges, firewall, packages
│   ├── cache/                          # Pre-download container and cloud images (skopeo)
│   ├── k3s_controller/                 # K3s, container image cache, Netris Helm chart, license
│   ├── topology/                       # OpenTofu — creates switches, servers, links in controller
│   ├── cloudsim/                       # Pulumi — creates KVM VMs simulating the topology
│   ├── connectivity/                   # VPN, socat port forwarding, ISP FRR, softgate agents
│   ├── verify/                         # Health checks via Netris API
│   └── lab_destroy/                    # Teardown: VMs, topology, K3s, bridges, cleanup
├── collections/
│   └── ansible_collections/            # netris.controller, ansible.posix, ansible.utils
├── netris-cloudsim/                    # Pulumi Go project for VM provisioning
└── docs/
    ├── architecture.md                 # full Spectrum-X simulation deep-dive
    └── troubleshooting.md              # all known issues and fixes
```

## Integration with netris-test-infra

This project can be used standalone, or as a submodule of [netris-test-infra](https://github.com/danmanor/netris-test-infra) which adds OCP installation and OSAC deployment on top. When used as a submodule, the parent project includes these roles via `include_role` rather than running them as separate playbooks.

## Further Reading

- [Architecture Deep-Dive](docs/architecture.md) — how the lab simulates a real Spectrum-X deployment
- [Troubleshooting Guide](docs/troubleshooting.md) — all known issues and fixes

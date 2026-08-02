# netris-lab

Standalone project for deploying a configurable Netris Spectrum-X GPU cluster simulation on a RHEL bare-metal server. Deploys ~50 KVM VMs (switches, softgates, GPU servers) with full network fabric using UDP tunnels to simulate physical cables.

This project has no OSAC or OpenShift knowledge — it is purely network infrastructure.

## Project Structure

```
roles/                          # Ansible roles (executed in this order)
  prerequisites/                # OS packages, Go, Pulumi, OpenTofu, libvirt, bridges, firewall
  cache/                        # Pre-download container and cloud images (skopeo)
  k3s_controller/               # Deploy K3s + Netris controller Helm chart
  topology/                     # Terraform/OpenTofu: create ~718 topology resources in Netris API
  cloudsim/                     # Pulumi/Go: provision KVM VMs + UDP tunnel networking
  connectivity/                 # OpenVPN, socat forwarding, ISP simulation (FRR), softgate agents
  verify/                       # Health checks via Netris API
playbooks/                      # Ansible playbooks
group_vars/all.yml              # All configuration variables
inventory/lab.yml               # Inventory (localhost, local connection)
collections/                    # Bundled netris.controller Ansible collection
netris-cloudsim/                # Pulumi Go program for VM + tunnel provisioning
```

## Commands

```
make setup          # prerequisites + cache
make prerequisites  # Install system dependencies
make cache          # Pre-cache container and cloud images
make deploy         # Full deployment (all 5 roles in sequence)
make destroy        # Teardown everything
make verify         # Run health checks
make connectivity   # Re-run connectivity phase only
```

## Configuration

All variables in `group_vars/all.yml`. Key knobs:

- `gpu_server_count: 4` — number of GPU server VMs (1–32)
- `ew_fabric_enable: 0` — enable East-West leaf/spine fabric (0=NS-only, 1=full)
- `connectivity_mode: "isolated"` — `isolated` (TEST-NET ranges, no real IPs) or `public` (real IPs, bridged interface)
- `ns_fabric.*` — North-South fabric sizing (leaf/spine/softgate counts, port layout)
- VM resources: `switch_vcpu/memory`, `server_vcpu/memory`, `softgate_vcpu/memory`
- `netris_controller_chart_version`, `netris_controller_images.*` — controller versions
- `k3s_version`, `k3s_api_port` — K3s configuration

## Architecture

```
RHEL bare-metal host
├── br-mgmt (192.168.16.254/20) — management network
├── br-public — BGP/ISP connectivity
├── K3s cluster
│   └── Netris controller (backend, frontend, gRPC, MariaDB, MongoDB, Redis, HAProxy)
├── ~13 Cumulus Linux switch VMs (EW + NS leaf/spine fabric)
├── 4 softgate VMs (SNAT/L4LB, eBGP peering)
├── 4 GPU server VMs (hgx-00..03)
├── 5 CPU server VMs
├── mgmt-server (DHCP, DNS, OpenVPN)
└── ISP-server (FRR BGP router)
```

## Key Technologies

- **Terraform/OpenTofu** — topology resources in Netris controller API (`roles/topology/files/*.tf`)
- **Pulumi/Go** — KVM VM provisioning with UDP tunnel networking (`netris-cloudsim/`)
- **K3s + Helm** — Netris controller deployment
- **KVM/libvirt** — VM hypervisor (virsh, qemu-img)
- **Cumulus Linux** — switch OS (ZTP provisioned)
- **OpenVPN** — management plane tunnel (switches → mgmt-server → host)
- **socat** — port forwarding (workaround for K3s iptables-legacy vs RHEL nftables)
- **FRR** — ISP BGP simulation (isolated mode)

## Key Design Decisions

- **UDP tunnels** (not veth pairs) simulate cables — more realistic, supports multi-hypervisor
- **socat forwarding** instead of K3s LoadBalancer — bridges iptables-legacy/nftables incompatibility
- **DPDK disabled** immediately after softgate agent install — DPDK would steal management NIC in VMs
- **K3s on port 6444** — avoids conflict with other services on 6443

## Conventions

- Ansible roles: `tasks/main.yml`, `templates/`, `handlers/`, `defaults/`
- Terraform files: static `.tf` in `roles/topology/files/`, templates rendered by Ansible
- Pulumi: Go program in `netris-cloudsim/` — reads topology from Netris API, creates VMs and tunnels
- Connectivity sub-tasks: `roles/connectivity/tasks/{vpn,socat,isp_server,softgates}.yml`
- Netris API interactions use `netris.controller.*` collection (auth, inventory, ebgp, license)

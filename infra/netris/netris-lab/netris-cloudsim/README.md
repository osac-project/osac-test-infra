# netris-cloudsim

Pulumi program that provisions a virtual network simulation lab on bare-metal KVM hypervisors. It reads network topology from a Netris controller and creates libvirt VMs with UDP tunnel-based links to simulate the physical topology.

## Prerequisites

Install on the machine where you'll run Pulumi (can be the hypervisor itself or a separate management host):

```bash
# Go 1.21.x
wget https://go.dev/dl/go1.21.13.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf go1.21.13.linux-amd64.tar.gz && rm -f go1.21.13.linux-amd64.tar.gz
echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.profile
source ~/.profile

# Pulumi
curl -fsSL https://get.pulumi.com | sh
echo 'export PULUMI_CONFIG_PASSPHRASE=""' >> ~/.bashrc
source ~/.bashrc
pulumi login --local

# Build tools
sudo apt install mkisofs xsltproc
```

## Network Requirements

You need the following from your bare-metal cloud provider (PNAP, IBM Cloud Classic, Equinix Metal, Hetzner, etc.):

1. **Bare-metal server(s)** with KVM/libvirt support (nested virtualization on cloud VMs won't perform well)
2. **Management network** (private) — used for inter-VM communication and Netris management plane
3. **Public network** — used for simulated ISP / BGP / external connectivity
4. **Public IP subnets**:
   - One /29 for BGP peering between the simulated ISP and softgates
   - One /29 or /30 for NAT/L4LB services (advertised into the Netris fabric via BGP)

## Hypervisor Preparation

### Generate cloud-init config

Use `gen_cloudinit.sh` to generate a cloud-init config for your hypervisor. The script needs to know which network interfaces to bridge for management and public traffic.

```bash
./gen_cloudinit.sh --mgmt-iface <IFACE> --public-iface <IFACE>
```

The interface can be:
- A **VLAN subinterface** (e.g., `bond0.22`) — the script will create the VLAN interface automatically
- A **direct interface** (e.g., `eth1`) — the script will bridge it as-is

#### Provider Examples

**PNAP** — bonded interface with VLAN trunking. Get VLAN IDs from the PNAP portal (Networks tab):
```bash
./gen_cloudinit.sh --mgmt-iface bond0.22 --public-iface bond0.23
```

**IBM Cloud Classic** — bonded interfaces with VLAN trunking. Get VLAN IDs from the IBM Cloud portal (Classic Infrastructure > Network > VLANs):
```bash
./gen_cloudinit.sh --mgmt-iface bond0.100 --public-iface bond1.200
```

**Equinix Metal** — bonded interface with VLAN trunking. Attach VLANs via the Equinix console or API:
```bash
./gen_cloudinit.sh --mgmt-iface bond0.1000 --public-iface bond0.1001
```

**Hetzner / generic** — separate physical interfaces, no VLAN tagging:
```bash
./gen_cloudinit.sh --mgmt-iface eth1 --public-iface eth0
```

Paste the output into your provider's cloud-init / user-data field when provisioning the bare-metal server.

### Manual hypervisor setup (alternative)

If your server is already running or doesn't support cloud-init, run these steps manually:

```bash
sudo apt-get update && sudo apt-get install virt-manager bridge-utils

# Disable SELinux in libvirt
sudo sed -i '/#security_driver = "selinux"/c\security_driver = "none"' /etc/libvirt/qemu.conf
sudo systemctl restart libvirtd

# Create bridges — adapt interface names to your provider
# For VLAN subinterfaces, create them first:
#   sudo ip link add link bond0 name bond0.22 type vlan id 22
sudo brctl addbr br-mgmt
sudo brctl addif br-mgmt <your-mgmt-interface>
sudo ip link set dev br-mgmt up

sudo brctl addbr br-public
sudo brctl addif br-public <your-public-interface>
sudo ip link set dev br-public up

# Libvirt storage pool
sudo virsh pool-define-as --name default --type dir --target /var/lib/libvirt/images
sudo virsh pool-start default
sudo virsh pool-autostart default

# Download base images
sudo curl http://downloads.netris.ai/cumulus-linux-5.11.3-vx-amd64-qemu.qcow2 \
  -o /var/lib/libvirt/images/cumulus-linux-5.11.3.qcow2
sudo curl https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img \
  -o /var/lib/libvirt/images/ubuntu-24.04-server-cloudimg-amd64.img

# VPN forwarding
sudo iptables -t nat -A PREROUTING -p tcp --dport 1194 -j DNAT --to-destination 192.168.122.10:1194
sudo iptables -I FORWARD -p tcp --dport 1194 -j ACCEPT
```

## Configuration

### Initialize a Pulumi stack

```bash
git clone git@gitlab.netris.ai:infra/netris-cloudsim.git
cd netris-cloudsim
pulumi stack init main --non-interactive
pulumi stack select main
cp Pulumi.dev.yaml Pulumi.main.yaml
```

### Edit `Pulumi.main.yaml`

Key settings to configure:

| Config key | Description |
|---|---|
| `sshAuthKeys` | SSH public keys for VM access (required) |
| `hypers_list` | Hypervisor IP address(es) (required) |
| `controller_url` | Netris controller URL (default: `http://localhost`) |
| `controller_login` | Netris API username (default: `netris`) |
| `controller_password` | Netris API password (default: `newNet0ps`) |
| `controller_site` | Netris site name to simulate (default: `Air`) |
| `hypers_ssh_user` | SSH user for hypervisors (default: `ubuntu`) |
| `servers_gw` | Management subnet CIDR for servers |
| `bgp_link_subnet` | /29 subnet for BGP peering between simulated ISP and softgates |
| `bgp_password` | BGP session password |
| `bgp_subnets_to_advertise` | Public subnets to advertise via BGP (for NAT/L4LB) |
| `switch_vcpu` / `switch_memory` | Resource overrides for switch VMs |
| `server_vcpu` / `server_memory` | Resource overrides for server VMs |
| `apt_repo` | Netris APT repository: `main` or `dev` |

Example:
```yaml
config:
  netris-air:sshAuthKeys:
    - ssh-rsa AAAA... your-key
  netris-air:hypers_list:
    - 10.0.0.5
  netris-air:controller_url: https://controller.example.com
  netris-air:controller_password: your-password
  netris-air:controller_site: Datacenter-1
  netris-air:hypers_ssh_user: ubuntu
  netris-air:servers_gw: 192.168.16.1/20
  netris-air:bgp_link_subnet: 203.0.113.0/29
  netris-air:bgp_password: your-bgp-password
  netris-air:bgp_subnets_to_advertise:
    - 203.0.113.8/30
```

## Deploy

```bash
# Preview changes
pulumi preview

# Deploy
pulumi up

# Destroy
pulumi destroy
```

## Operations

### Rebuild a single VM

```bash
./rebuild.sh <vm-name>
```

This destroys the VM, recreates its disk from the backing image, and starts it. Useful for resetting a switch or server without tearing down the whole topology.

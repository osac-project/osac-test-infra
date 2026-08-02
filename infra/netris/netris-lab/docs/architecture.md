# Architecture Deep-Dive: Netris Spectrum-X GPU Cluster Simulation Lab

This document explains the full architecture of a virtual simulation lab that
recreates an NVIDIA Spectrum-X GPU cluster network on a single bare-metal
server. It is written for someone who has never seen Netris, Spectrum-X, or
this lab before.

---

## 1. What is Spectrum-X

NVIDIA Spectrum-X is an ethernet networking platform purpose-built for AI/GPU
clusters. In a real data center, dozens or hundreds of GPU servers (such as
NVIDIA HGX systems with 8 GPUs each) must communicate at extremely high
bandwidth and low latency for distributed AI training. Spectrum-X provides:

- **Spectrum-4 switches** running Cumulus Linux, deployed in a leaf-spine
  fabric topology.
- **RoCE (RDMA over Converged Ethernet)** for GPU-to-GPU communication,
  eliminating TCP overhead. Adaptive routing and congestion control are built
  into the switch ASIC.
- **Two separate fabrics** in a typical deployment:
  - **East-West (EW) fabric**: Carries GPU-to-GPU RDMA/RoCE traffic. Every
    GPU server connects to multiple EW leaf switches; leaves connect to spines
    in a full mesh. This fabric is optimized for maximum bisection bandwidth.
  - **North-South (NS) fabric**: Provides external connectivity -- internet
    access, storage, management. Uses softgates (software-defined gateways)
    for NAT, L4 load balancing, and BGP peering with upstream ISPs.
- **Netris** is the network controller software. It runs as a centralized
  application and manages all switches and softgates via agents installed on
  each device. It provides a web UI, REST/gRPC APIs, and a Terraform provider
  for infrastructure-as-code.

A typical Spectrum-X deployment for 32 GPU servers uses 4 EW leaf switches,
2 EW spine switches, 2 NS leaf switches, 2 NS spine switches, 1 OOB
(out-of-band) management leaf, 4 softgates, and several CPU servers -- roughly
15 network devices in total.

---

## 2. What the Lab Simulates

This lab recreates the entire Spectrum-X deployment as virtual machines on a
single RHEL 9 bare-metal server. Every physical switch becomes a Cumulus Linux
VX (virtual appliance) VM. Every physical cable becomes a UDP tunnel between
two VMs. The Netris controller runs in K3s (lightweight Kubernetes) on the
host itself.

The result is a fully functional network simulation where:

- Switches boot, get IP addresses via DHCP, auto-provision via ZTP
  (Zero-Touch Provisioning), and register with the Netris controller.
- BGP sessions establish between leaves, spines, and softgates.
- The controller pushes configuration to all switches and softgates.
- NAT and L4LB services work through the softgates.
- An ISP is simulated via FRR (Free Range Routing) for BGP peering.

This allows development, testing, and demonstration of Spectrum-X network
configurations without requiring millions of dollars of physical hardware.

The topology is configurable via `gpu_server_count` (default: 4, max: 32)
and `ew_fabric_enable` (default: 0). The default deployment is NS-only with
4 GPU servers. The maximum scale below assumes `gpu_server_count=32` and
`ew_fabric_enable=1`:

**Maximum scale (32 GPU servers, full EW+NS fabric):**

- 13 switch VMs (Cumulus Linux)
- 32 GPU server VMs (Ubuntu -- simulated, no real GPUs)
- 5 CPU server VMs (Ubuntu)
- 4 softgate VMs (Ubuntu + netris-softgate agent)
- 1 mgmt-server VM (DHCP, DNS, Apache, OpenVPN, NAT gateway)
- 1 isp-server VM (FRR BGP router)
- ~698 Terraform resources in the controller
- ~300 UDP tunnels simulating physical cables

---

## 3. Real-to-Virtual Mapping

| Real Hardware             | Virtual Equivalent             | OS / Software         | VM Resources      |
|---------------------------|--------------------------------|-----------------------|-------------------|
| Spectrum-4 EW leaf switch | libvirt/KVM VM                 | Cumulus Linux VX 5.11.3 | 4 vCPU, 2 GB RAM  |
| Spectrum-4 EW spine       | libvirt/KVM VM                 | Cumulus Linux VX 5.11.3 | 4 vCPU, 2 GB RAM  |
| Spectrum-4 NS leaf switch | libvirt/KVM VM                 | Cumulus Linux VX 5.11.3 | 4 vCPU, 2 GB RAM  |
| Spectrum-4 NS spine       | libvirt/KVM VM                 | Cumulus Linux VX 5.11.3 | 4 vCPU, 2 GB RAM  |
| OOB management leaf       | libvirt/KVM VM                 | Cumulus Linux VX 5.11.3 | 4 vCPU, 2 GB RAM  |
| NVIDIA HGX GPU server     | libvirt/KVM VM (no GPU)        | Ubuntu 24.04          | 1 vCPU, 1 GB RAM  |
| CPU server (compute)      | libvirt/KVM VM                 | Ubuntu 24.04          | 4 vCPU, 8 GB RAM  |
| SoftGate appliance        | libvirt/KVM VM                 | Ubuntu 24.04 + agent  | 2 vCPU, 4 GB RAM  |
| ISP router                | isp-server VM                  | Ubuntu 24.04 + FRR    | 2 vCPU, 4 GB RAM  |
| DHCP/mgmt infrastructure  | mgmt-server VM                 | Ubuntu 24.04          | 2 vCPU, 4 GB RAM  |
| Physical cable (DAC/AOC)  | UDP tunnel between VMs         | libvirt XML config    | N/A               |
| Netris controller (HA)    | K3s pods on host               | Helm chart            | Host resources    |

---

## 4. Component Architecture

### 4.1 Deployment Pipeline

The lab is deployed in five stages, each building on the previous:

```
Stage 1: Prerequisites (Ansible)
  |
  |  OS packages, Go, Pulumi, OpenTofu, libvirt, bridges, firewall
  v
Stage 2: Netris Controller (Ansible + K3s + Helm)
  |
  |  Controller API is now available
  v
Stage 3: Spectrum-X-Init (OpenTofu/Terraform)
  |
  |  Controller now knows the full topology
  v
Stage 4: Cloudsim (Pulumi/Go)
  |
  |  VMs boot, ZTP provisions them, agents connect to controller
  v
Stage 5: Connectivity (Ansible)
       OpenVPN tunnel, socat port forwarding, ISP BGP (via cloud-init)
```

Additional roles (not part of the main deploy pipeline):

- **cache**: Pre-downloads container and cloud images (run via `make setup`).
- **verify**: Post-deploy health checks via Netris API (run via `make verify`).
- **lab_destroy**: Teardown of all infrastructure (run via `make destroy`).

### 4.2 High-Level Architecture Diagram

```
+=========================================================================+
|                        BARE-METAL RHEL 9 HOST                           |
|                                                                         |
|  +-------------------+    +------------------------------------------+  |
|  |    K3s Cluster     |    |              KVM / libvirt               |  |
|  |                    |    |                                          |  |
|  |  +- netris-ctrl -+ |    |  +----------+  +----------+             |  |
|  |  | Web UI/API    | |    |  | EW Leaves |  | EW Spines|             |  |
|  |  | gRPC server   | |    |  | (4 VMs)   |  | (2 VMs)  |             |  |
|  |  | Telescope     | |    |  +-----+-----+  +----+-----+             |  |
|  |  | MariaDB       | |    |        |   UDP tunnels  |                |  |
|  |  | MongoDB       | |    |        +-------+--------+                |  |
|  |  | Redis         | |    |                |                         |  |
|  |  | Graphite      | |    |  +----------+  +----------+             |  |
|  |  | HAProxy       | |    |  | NS Leaves |  | NS Spines|             |  |
|  |  +-------+-------+ |    |  | (2 VMs)   |  | (2 VMs)  |             |  |
|  |          |          |    |  +-----+-----+  +----+-----+             |  |
|  +----------+----------+    |        |              |                  |  |
|      socat  |               |  +-----+-----+  +----+-----+            |  |
|      fwd    |               |  | Softgates |  | OOB Leaf |            |  |
|             |               |  | (4 VMs)   |  | (1 VM)   |            |  |
|  +----------+----------+    |  +-----+-----+  +----------+            |  |
|  |  OpenVPN (tun0)     |    |        |                                |  |
|  |  10.8.0.2 <---------|----|--> 10.8.0.1 (mgmt-server)              |  |
|  +---------------------+    |        |                                |  |
|                             |  +-----+------+  +----------+          |  |
|                             |  | mgmt-server |  |isp-server|          |  |
|                             |  | DHCP/ZTP/   |  | FRR/BGP  |          |  |
|                             |  | Apache/VPN  |  +----------+          |  |
|                             |  +-------------+                        |  |
|                             |                                         |  |
|                             |  +----------------------------------+   |  |
|                             |  | GPU Servers (32 VMs)             |   |  |
|                             |  | CPU Servers (5 VMs)              |   |  |
|                             |  +----------------------------------+   |  |
|                             +------------------------------------------+  |
+=========================================================================+
```

### 4.3 Component Details

**Netris Controller (K3s + Helm)**

The controller is deployed as a Helm chart into a single-node K3s cluster
running directly on the bare-metal host. The chart version is 2.8.1 and
deploys these containers:

| Container   | Version   | Purpose                                       |
|-------------|-----------|-----------------------------------------------|
| Backend     | 4.9.0-012 | Node.js REST API and business logic            |
| Frontend    | 4.9.0-006 | Node.js web UI                                 |
| gRPC        | 4.9.0.005 | Go gRPC server -- switch/softgate agent comms  |
| Telescope   | 4.9.0.002 | Telemetry collection and monitoring            |
| Migration   | 4.9.0     | Database schema migrations                     |
| MariaDB     | (bundled) | Primary relational database                    |
| MongoDB     | (bundled) | Document store for telemetry                   |
| Redis       | (bundled) | Caching and pub/sub                            |
| Graphite    | (bundled) | Time-series metrics                            |
| HAProxy     | (bundled) | TLS termination and load balancing              |

K3s API runs on port 6444 (not 6443) to avoid conflicts. The K3s service
load balancer (svclb) uses iptables-legacy DNAT inside containers, which is
invisible to the host's nftables -- this is why socat forwarding is required
(see Section 5.4).

**Spectrum-X-Init (OpenTofu/Terraform)**

Uses the `netrisai/netris` Terraform provider (>=3.6.3) to populate the
controller with the desired topology via API calls. Counts below assume
`gpu_server_count=32` and `ew_fabric_enable=1`; smaller deployments scale
proportionally.

| Resource Type            | Count | Details                                     |
|--------------------------|-------|---------------------------------------------|
| Site                     | 1     | "Datacenter-1", public ASN 655001           |
| Inventory Profiles       | 2     | East-West and North-South                   |
| IP Allocations           | 3     | Private (10.0.0.0/8), NAT, L4LB             |
| Subnets                  | 6+    | Mgmt, loopback, NAT pool, L4LB pool, etc.  |
| EW Leaf Switches         | 4     | 64 ports, 2x400 breakout, ASN 4200100001+  |
| EW Spine Switches        | 2     | 64 ports, 2x400 breakout, ASN 4200200001   |
| NS Leaf Switches         | 2     | 64 ports, 4x200 breakout, ASN 4200300001+  |
| NS Spine Switches        | 2     | 64 ports, 4x200 breakout, ASN 4200300257+  |
| OOB Leaf Switch          | 1     | 54 ports, ASN 4200300129                    |
| Softgates                | 4     | 2 general-purpose, 2 SNAT                   |
| GPU Servers (HGX)        | 32    | 16 ports each (8 data + 2 bond + 1 aux + 5 reserved) |
| Server Cluster Template  | 1     | Auto-generates VPCs/VNets for GPU servers   |
| EW Leaf-Spine Links      | 256   | Full mesh with /31 on 10.254.x.x            |
| EW Leaf-Server Links     | 256   | /31 on 172.x.x.x (8 data paths per server)  |
| NS Links                 | ~122  | Leaf-spine, leaf-server, leaf-softgate       |
| BGP Sessions             | 4     | Softgate-to-ISP peering                     |
| **Total**                |**~698**| All managed via Terraform state              |

Note: CPU servers (COMPUTE00-04) are **not** Terraform resources. They are
created by the Cloudsim Pulumi stage as VMs only.

**Cloudsim (Pulumi/Go)**

A Pulumi program written in Go that reads the topology from the controller
API and creates libvirt/KVM VMs to simulate it. Key behaviors:

1. Fetches inventory, links, IPAM, and BGP config from the Netris API.
2. Computes link mappings: each link becomes a UDP tunnel with a unique
   port pair (local port / remote port, starting at 1025).
3. Creates copy-on-write qcow2 volumes from base images.
4. Generates cloud-init ISOs with type-specific user-data and network config.
5. Defines libvirt domains with XSLT-injected NIC configurations.

MAC addresses use the deterministic prefix `52:54:09` followed by
counter-derived bytes. VMs with more than 32 NICs get additional PCI bridge
controllers added via XSLT.

---

## 5. Networking Deep-Dive

The networking is the most complex part of the lab. There are three Linux
bridges, a VPN tunnel, UDP tunnels for every simulated cable, and socat
port forwarders to bridge K3s networking gaps.

### 5.1 Linux Bridges

```
+-------------------------------------------------------------------+
|                     BARE-METAL HOST                               |
|                                                                   |
|  virbr0 (192.168.122.0/24)        NAT to internet via iptables   |
|  |         |                                                      |
|  |  mgmt-server          isp-server                              |
|  |  192.168.122.10        192.168.122.15                          |
|  |         |                    |    \                             |
|  |    +----+----+          +----+--+  +--UDP tunnels--+            |
|  |    | br-mgmt |          |br-pub |  |  to NS leaf   |            |
|  |    +---------+          +-------+  |  switch ports  |            |
|  |    |  |  |  |           (ISP gw)   |  (BGP peers)   |            |
|  |    All switch                      +-------+--------+            |
|  |    + server +                              |                    |
|  |    softgate                          NS leaf switches           |
|  |    mgmt NICs                               |                    |
|  |    (ens3/eth0)                       VXLAN L2 tunnels           |
|  |                                            |                    |
|  |                                      Softgate br-2..br-6       |
|  |                                      (BGP over tunnels)        |
+-------------------------------------------------------------------+
```

Note: Softgates do not connect to br-public. They connect to NS leaf
switch ports via UDP tunnels, and the Netris controller creates VXLAN L2
tunnels through the fabric to deliver ISP connectivity into the softgate
as virtual bridge interfaces.

**virbr0 -- libvirt default NAT bridge (192.168.122.0/24)**

- Pre-existing libvirt bridge providing NAT internet access.
- mgmt-server (192.168.122.10) and isp-server (192.168.122.15) attach here.
- Provides the only path to the internet for lab VMs.
- Hypervisor IP on this bridge: 192.168.122.1 (libvirt default).

**br-mgmt -- management bridge**

- Created by the hypervisor Ansible role as a plain Linux bridge (no IP on
  the host side by default; 192.168.16.254/20 is added for direct access).
- Carries three management subnets:

| Subnet              | Purpose                                          |
|----------------------|--------------------------------------------------|
| 10.253.0.0/18        | EW switch management IPs                         |
| 10.3.0.0/16          | NS switch, softgate, OOB switch management IPs  |
| 192.168.16.0/20      | Server management IPs (GPU + CPU servers)        |

- The mgmt-server VM bridges virbr0 and br-mgmt: it is the default gateway
  for all devices on br-mgmt, providing internet access through virbr0 NAT.
- Every switch's first NIC (ens3 in Cumulus VX, virtio) attaches here.

**br-public -- ISP upstream bridge**

- Plain Linux bridge. In isolated mode the host adds 198.51.100.9/29 to
  act as the ISP server's upstream gateway.
- Connects only the isp-server's upstream NIC. Softgates do **not** attach
  to br-public directly -- they connect to NS leaf switch ports via UDP
  tunnels, and the Netris controller creates VXLAN L2 tunnels through the
  NS fabric to bridge ISP switch ports into virtual interfaces (br-2..br-6)
  inside each softgate. This matches production, where ISP routers plug
  into NS leaf ports and Netris tunnels them to softgates automatically.
- The isp-server's other NICs (ens6--ens9) are UDP tunnels to NS leaf
  switch ports, simulating the physical cables between an ISP router and
  the NS fabric. Each tunnel carries one eBGP session (10.10.0.x/30).

### 5.2 UDP Tunnels -- Simulating Physical Cables

In the real data center, switches connect via physical DAC (Direct Attach
Copper) or AOC (Active Optical Cable) cables. In the lab, each cable is
replaced by a UDP tunnel between two VM NICs.

libvirt supports this natively via `<interface type='udp'>`:

```xml
<interface type="udp">
  <source address="127.0.0.1" port="1027">
    <local address="127.0.0.1" port="1025"/>
  </source>
  <model type="virtio"/>
</interface>
```

This means:
- The VM sends packets out this NIC to `127.0.0.1:1027` (the remote end).
- The VM receives packets on this NIC from `127.0.0.1:1025` (the local end).
- The remote VM has the inverse: local=1027, remote=1025.

**Port numbering scheme:**

- Ports start at 1025 and increment by 2 for each link.
- Link N uses local ports `1025 + (2*N)` and `1025 + (2*N) + 1`.
- For VMs on the same hypervisor, both endpoints use 127.0.0.1.
- For VMs on different hypervisors (multi-host labs), endpoints use the
  hypervisors' real IPs.

**Scale:** With 32 GPU servers, the lab creates approximately 300 UDP tunnel
pairs. Each EW leaf has 64 ports (32 to spines + 32 to servers), each NS
leaf has 64 ports, and so on.

**NIC ordering matters.** The order of `<interface>` elements in the libvirt
domain XML determines the device name inside the VM. The first NIC is ens3
(management, on br-mgmt), and subsequent NICs map to swp1s0, swp1s1, etc.
The ZTP script uses udev rules to rename them to match the Netris topology.

### 5.3 VPN Tunnel -- Controller Connectivity

Switches install the Netris agent pointing to `10.8.0.2` as the controller
address. But the controller runs in K3s on the bare-metal host, not on
that IP. The VPN tunnel bridges this gap:

```
Switch VM (on br-mgmt)                    Bare-metal Host
      |                                        |
      | mgmt traffic to 10.8.0.2               |
      v                                        |
mgmt-server VM                                 |
  10.253.63.254 (gw for switches)              |
  10.3.x.254   (gw for NS devices)             |
  192.168.16.1 (gw for servers)                |
      |                                        |
      | OpenVPN server (tun0)                  |
      | 10.8.0.1                               |
      |                                        |
      +-------- VPN tunnel (TCP 1194) -------->+
                                               |
                                        OpenVPN client (tun0)
                                        10.8.0.2
                                               |
                                        socat forwarders
                                               |
                                        K3s pods (HAProxy)
```

The flow:
1. A switch sends gRPC traffic to 10.8.0.2:50051.
2. The packet arrives at mgmt-server (its default gateway).
3. mgmt-server has iptables MASQUERADE rules that NAT traffic from the
   management subnets toward the VPN tunnel (tun0, 10.8.0.1).
4. The packet traverses the OpenVPN tunnel to the host (10.8.0.2).
5. On the host, socat picks up the packet and forwards it to the K3s pod.

The OpenVPN tunnel uses TCP port 1194. The server runs inside mgmt-server;
the client runs on the bare-metal host. A route fix script removes the
conflicting virbr0 route for 192.168.122.0/24 that would otherwise prevent
the VPN from establishing.

### 5.4 Socat Port Forwarding -- Bridging K3s Networking

K3s uses its built-in svclb (service load balancer) which creates iptables
DNAT rules inside a container's network namespace. These rules are invisible
to the host's nftables-based networking stack. Traffic arriving at the host's
10.8.0.2 address cannot be DNAT'd by K3s's mechanism.

The solution: socat binds directly on 10.8.0.2 and the host's primary IP,
forwarding TCP connections to the HAProxy pod IP inside K3s.

```
socat TCP-LISTEN:50051,bind=10.8.0.2,fork,reuseaddr \
      TCP:<haproxy-pod-ip>:50051
```

Ports forwarded:

| Port  | Service        | Protocol | Purpose                          |
|-------|----------------|----------|----------------------------------|
| 50051 | gRPC           | TCP      | Switch/softgate agent control    |
| 3033  | Telescope      | TCP      | Telemetry collection             |
| 3034  | Telescope TLS  | TCP      | Encrypted telemetry              |
| 2003  | Graphite       | TCP      | Time-series metrics ingestion    |

Each port gets a systemd service unit (`socat-forwarder@<port>.service`)
that auto-restarts on failure. The HAProxy pod IP is discovered dynamically
and injected into the socat unit files.

### 5.5 Complete Network Diagram

```
                                  INTERNET
                                     |
                              +------+------+
                              |   virbr0    |
                              | 192.168.122 |
                              |  .0/24 NAT  |
                              +--+-------+--+
                                 |       |
                          .10    |       |    .15
                    +------------+       +------------+
                    |                                  |
              +-----+------+                    +------+-----+
              | mgmt-server|                    | isp-server |
              |            |                    |            |
              | DHCP       |                    | FRR/BGP    |
              | Apache     |                    | ASN 65401  |
              | OpenVPN    |                    +------+-----+
              +-----+------+                          |
                    |                                  |
          +---------+---------+               +--------+--------+
          |      br-mgmt      |               |    br-public    |
          | 10.253.0.0/18     |               | ISP upstream gw |
          | 10.3.0.0/16       |               | (198.51.100.9)  |
          | 192.168.16.0/20   |               +--------+--------+
          +--+--+--+--+--+---+                        |
             |  |  |  |  |                     ISP server ens5
         ens3 of every switch                  (upstream only)
         server + softgate VM
         (mgmt NICs)                     ISP ens6-9: UDP tunnels
                                         to NS leaf switch ports

      .....UDP tunnels between data ports.....

    +------+  +------+  +------+  +------+
    |leaf-0|  |leaf-1|  |leaf-2|  |leaf-3|      EW Leaves
    +--+---+  +--+---+  +--+---+  +--+---+
       |  \      |  \      |  /      |  /
       |   +-----+---+----+--+------+  |        Full mesh
       |         |    \  /   |         |
    +--+---+  +--+---+                             EW Spines
    |spine0|  |spine1|
    +------+  +------+

    +-------+ +-------+ +--------+ +--------+
    |ns-lf-0| |ns-lf-1| |ns-sp-0 | |ns-sp-1 |  NS Fabric
    +---+---+ +---+---+ +----+---+ +----+---+
        |         |           |          |
    +---+---+ +---+---+ +----+---+ +----+---+
    | sg-0  | | sg-1  | |  sg-2  | |  sg-3  |   Softgates
    +-------+ +-------+ +--------+ +--------+

    +--------+--------+--------+--------+
    | hgx-00 | hgx-01 |  ...   | hgx-31 |       GPU Servers
    +--------+--------+--------+--------+

    +--------+--------+--------+--------+--------+
    |COMPUTE | COMPUTE|COMPUTE |COMPUTE |COMPUTE |  CPU Servers
    |  00    |   01   |  02    |  03    |  04    |
    +--------+--------+--------+--------+--------+
```

---

## 6. DHCP and Zero-Touch Provisioning Flow

When a Cumulus Linux switch VM boots, it must discover its identity, configure
its network interfaces, and install the Netris agent -- all without manual
intervention. This is the ZTP (Zero-Touch Provisioning) flow.

### 6.1 DHCP Server Configuration

The mgmt-server VM runs isc-dhcp-server. Its configuration is generated
dynamically by Cloudsim based on the topology fetched from the Netris
controller API.

Key DHCP settings:

```
default-lease-time  172800   (2 days)
max-lease-time      345600   (4 days)
domain-name         "sim.netris.local"
dns-servers         8.8.8.8, 8.8.4.4
```

Custom DHCP options used:

| Option Code | Name                    | Purpose                             |
|-------------|-------------------------|-------------------------------------|
| 72          | www-server              | Points to HTTP server for ZTP files |
| 239         | cumulus-provision-url   | URL of the ZTP script               |
| 125         | VIVSO                   | ONIE vendor encapsulation           |

Each switch gets a `host` declaration with a fixed MAC-to-IP mapping:

```
host leaf-pod00-su0-r0 {
    hardware ethernet 52:54:09:00:01:00;
    fixed-address 10.253.0.1;
    option host-name "leaf-pod00-su0-r0";
    option cumulus-provision-url "http://10.253.63.254/cumulus-ztp";
}
```

The DHCP server listens on the br-mgmt-facing interface of mgmt-server. It
serves separate subnet declarations for each management range (10.253.0.0/18,
10.3.0.0/16, 192.168.16.0/20).

### 6.2 ZTP Script

The ZTP script is served by Apache on mgmt-server at the URL provided in
the DHCP response. When Cumulus Linux boots and receives the
`cumulus-provision-url` option, it downloads and executes the script.

The ZTP script performs these steps in order:

```
1. SET HOSTNAME
   - Queries DHCP lease to determine assigned hostname
   - Writes to /etc/hostname and applies immediately

2. CONFIGURE SSH
   - Installs authorized_keys from http://<mgmt-server>/authorized_keys
   - Enables root SSH access

3. SET DEFAULT PASSWORD
   - Changes cumulus user password to "newNet0ps!"

4. GENERATE UDEV RULES FOR NIC NAMING
   - Cumulus VX NICs come up as virtio devices (ens4, ens5, ens6...)
   - The ZTP script creates udev rules to rename them to match the
     Netris topology: swp1s0, swp1s1, swp2s0, etc.
   - Rule format:
     SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="52:54:09:xx:xx:xx",
     NAME="swp1s0"
   - This is critical: the Netris agent expects interface names matching
     the controller's topology definition

5. INSTALL NETRIS AGENT
   - Downloads from https://get.netris.io
   - Configures the agent to connect to the controller at 10.8.0.2
   - Uses an auth_key generated by the controller for authentication
   - Starts the netris-sw agent service

6. REBOOT
   - Applies udev rules and starts the agent with correct NIC names
```

### 6.3 Boot-to-Managed Timeline

```
t=0s    VM created by Pulumi (libvirt domain start)
        |
t=5s    Cumulus Linux kernel boots, GRUB -> kernel -> systemd
        |
t=30s   DHCP client sends DISCOVER on ens3 (br-mgmt)
        |
t=31s   mgmt-server responds: IP, hostname, cumulus-provision-url
        |
t=35s   ZTP script downloaded from Apache, execution begins
        |
t=40s   SSH configured, hostname set, udev rules written
        |
t=50s   Netris agent downloaded and installed
        |
t=60s   VM reboots to apply udev NIC renaming
        |
t=90s   Second boot: NICs now named swp1s0, swp1s1, etc.
        |
t=95s   Netris agent starts, connects to 10.8.0.2:50051 (gRPC)
        |
t=100s  Agent registers with controller, receives configuration
        |
t=120s  Switch is fully managed -- BGP sessions begin establishing
```

(Times are approximate and depend on host performance.)

---

## 7. BGP Simulation

The lab simulates a complete BGP environment with internal (iBGP/eBGP within
the fabric) and external (eBGP with an ISP) routing.

### 7.1 Internal BGP -- Fabric Underlay

Within each fabric (EW and NS), leaf and spine switches run BGP as the
underlay routing protocol. This is configured automatically by the Netris
controller via the netris-sw agent.

**East-West Fabric:**

| Role  | ASN          | Notes                                     |
|-------|--------------|-------------------------------------------|
| Leaves | 4200100001+ | Sequential ASN per leaf (private 4-byte)  |
| Spines | 4200200001  | Shared ASN across all EW spines           |

- Unnumbered BGP underlay (uses link-local IPv6 for peering).
- Optimized BGP overlay for EVPN.
- RoCE adaptive routing and congestion control enabled.
- Full mesh: every leaf peers with every spine.
- Link addressing: 10.254.x.y/31 (x = spine index, y = sequential).

**North-South Fabric:**

| Role      | ASN          | Notes                                   |
|-----------|--------------|-----------------------------------------|
| NS Leaves | 4200300001+  | Sequential ASN per leaf                 |
| NS Spines | 4200300257+  | Sequential ASN per spine                |
| OOB Leaf  | 4200300129   | Offset by 128 from NS leaf base         |
| Softgates | (managed)    | Netris manages softgate BGP internally  |

- Unnumbered BGP underlay.
- Link addressing: 169.254.x.y/31 (link-local range).
- Leaf-to-spine link count: 4 links per leaf-spine pair.

### 7.2 External BGP -- ISP Peering

The isp-server VM runs FRR (Free Range Routing) to simulate an upstream ISP.

```
                ISP (FRR on isp-server)
                     ASN 65401
                    /    |    \     \
            VLAN 10  VLAN 11  VLAN 12  VLAN 13
                /      |        \        \
          sg-0      sg-1       sg-2      sg-3
       10.10.0.1  10.10.0.5  10.10.0.9  10.10.0.13
       (local)    (local)    (local)    (local)
       10.10.0.2  10.10.0.6  10.10.0.10 10.10.0.14
       (remote)   (remote)   (remote)   (remote)

       All softgates use site public ASN: 655001
```

Each BGP session rides a separate VLAN on br-public. The ISP side uses the
even IP in each /30; the softgate side uses the odd IP.

**ISP routing policy:**

| Direction | Policy                                                        |
|-----------|---------------------------------------------------------------|
| Export    | Advertises TEST-NET-3 subnets: 198.51.100.0/29 (NAT pool),   |
|           | 198.51.100.16/29 (L4LB pool). Also sends default route.      |
| Import    | Accepts only public routes (filters RFC 1918 private space).  |

**Why TEST-NET-3?** RFC 5737 reserves 198.51.100.0/24 for documentation and
testing. Using these addresses avoids conflicts with real public IPs and
makes the simulation self-contained.

**FRR configuration highlights:**

```
router bgp 65401
  neighbor V4 peer-group
  neighbor V4 remote-as 655001
  neighbor V4 password newNet0ps
  neighbor 10.10.0.1 peer-group V4
  neighbor 10.10.0.5 peer-group V4
  neighbor 10.10.0.9 peer-group V4
  neighbor 10.10.0.13 peer-group V4

  address-family ipv4 unicast
    network 198.51.100.0/29
    network 198.51.100.16/29
    neighbor V4 route-map EXPORT out
    neighbor V4 route-map ACCEPT_PUBLIC in
```

### 7.3 Softgate Roles

The four softgates are not identical:

| Softgate     | Role    | Function                                        |
|--------------|---------|------------------------------------------------|
| ns-softgate-0 | general | NAT + L4LB, primary traffic path              |
| ns-softgate-1 | general | NAT + L4LB, redundant pair                    |
| ns-softgate-2 | snat    | Source NAT only, dedicated SNAT path           |
| ns-softgate-3 | snat    | Source NAT only, redundant pair                |

Important: these softgates use the standard `softgate` node type, NOT
`softgate_hs` (high-speed / DPDK). The DPDK variant takes over the virtio
management NIC in VMs, making the softgate unreachable. The non-DPDK variant
uses standard Linux networking, which works correctly in the virtual
environment.

---

## 8. Data Flow Examples

### 8.1 East-West: GPU-to-GPU Traffic (RDMA/RoCE Path)

Scenario: hgx-pod00-su0-h00 sends RDMA data to hgx-pod00-su0-h16 via the
East-West fabric.

```
hgx-su0-h00                                              hgx-su0-h16
    |                                                         ^
    | eth1 (172.16.0.1/31)                                    | eth1 (172.16.0.33/31)
    |                                                         |
    v                                                         |
  [UDP tunnel: ports 1025/1027]                    [UDP tunnel: ports 1089/1091]
    |                                                         ^
    v                                                         |
leaf-pod00-su0-r0                                  leaf-pod00-su0-r2
  swp1s0                                             swp1s0
    |                                                  ^
    | (10.254.0.0/31)                                  | (10.254.0.64/31)
    v                                                  |
  [UDP tunnel]                                    [UDP tunnel]
    |                                                  ^
    v                                                  |
spine-0-pod00 ---------------------------------------->+
  Forwards based on destination IP / ECMP
  across spine-0 and spine-1
```

Step by step:

1. **Application layer**: hgx-su0-h00 initiates an RDMA write to
   hgx-su0-h16. The NIC generates a RoCEv2 packet (UDP-encapsulated
   InfiniBand) with destination 172.16.0.33.

2. **Server to leaf**: The packet exits eth1, which is a virtio NIC backed
   by a UDP tunnel. QEMU sends the raw ethernet frame as a UDP datagram to
   127.0.0.1 on the port assigned to leaf-pod00-su0-r0's swp1s0 interface.

3. **Leaf forwarding**: The Cumulus Linux switch VM receives the frame on
   swp1s0. Its FIB (programmed by Netris via BGP) shows that 172.16.0.33
   is reachable via spine-0 or spine-1. ECMP (Equal-Cost Multi-Path)
   selects one. In the real Spectrum-X, adaptive routing would steer
   around congested paths.

4. **Leaf to spine**: The packet traverses another UDP tunnel to spine-0.
   The spine's FIB shows 172.16.0.33 is reachable via leaf-pod00-su0-r2.

5. **Spine to leaf**: Another UDP tunnel carries the packet to the
   destination leaf switch.

6. **Leaf to server**: The destination leaf forwards the packet out swp1s0
   (connected to hgx-su0-h16 eth1) via the final UDP tunnel.

7. **Server receives**: hgx-su0-h16 receives the RDMA packet on eth1 and
   passes it to the application.

In the real system, this entire path runs at 400 Gbps per link with
sub-microsecond latency. In the simulation, the UDP tunnels add overhead,
but the control plane behavior (BGP, ECMP, Netris management) is identical.

### 8.2 North-South: Server to Internet (NAT Path)

Scenario: hgx-pod00-su0-h00 makes an HTTPS request to an external server.

```
hgx-su0-h00
    |
    | eth9/eth10 (bond0, 192.168.0.1/21)
    |
    v
ns-leaf-0
    |
    | BGP learned route: default via softgate
    | (EVPN/VXLAN L3 VNI tunnel)
    v
ns-softgate-0 (General role)
    |
    | Checks service type -> SNAT -> redirect to SNAT softgate
    v
ns-softgate-2 or -3 (SNAT role)
    |
    | SNAT: src 192.168.0.1 -> 198.51.100.0 (NAT pool)
    | via VXLAN L2 tunnel through NS fabric
    v
isp-server (FRR, on NS leaf switch port)
    |
    | default gw: 198.51.100.9 (host on br-public)
    v
Host: masquerade -> eno3 -> internet
    |
    v
Return: internet -> host -> ISP -> softgate (reverse NAT)
        -> NS fabric -> server
```

Step by step:

1. **Server egress**: hgx-su0-h00 has a default route via 192.168.7.254
   (the NS fabric gateway). The packet exits via the bond0 interface
   (eth9 + eth10 bonded for redundancy) toward the NS leaf switch.

2. **NS leaf routing**: The NS leaf has learned (via BGP from the spine
   layer) that the default route points to a softgate. The packet is
   forwarded to ns-softgate-0.

3. **Softgate routing**: The packet arrives at a General softgate (which
   injects the default route into the VPC). The General softgate checks
   the service type: SNAT traffic is redirected to the appropriate SNAT
   softgate via Maglev consistent hashing.

4. **SNAT**: The SNAT softgate performs source NAT, replacing the server's
   private IP (192.168.0.1) with an IP from the NAT pool (198.51.100.0/29).
   The softgate agent programs nftables NAT rules based on the controller's
   configuration, using packet marks to match SNAT-eligible flows.

5. **BGP uplink via VXLAN tunnel**: The NATted packet exits the softgate
   via a VXLAN L2 tunnel (br-6 / vx-2 interfaces) through the NS fabric
   to the ISP server. Softgates do not connect to br-public directly --
   the Netris controller creates dedicated VXLAN tunnels from NS leaf
   switch ports to virtual bridge interfaces inside each softgate.

6. **ISP routing**: The isp-server (FRR) receives the packet on a tunnel
   interface and routes it via its default gateway (198.51.100.9 on
   br-public). In isolated mode, the host masquerades the traffic to
   its real outgoing interface (eno3) and forwards it to the internet.

7. **Return path**: The response arrives at the host, is un-masqueraded
   back to the SNAT pool IP (198.51.100.0), routed via a specific route
   (198.51.100.0/29 via 198.51.100.10) to the ISP server, which forwards
   it through the BGP tunnels to the SNAT softgate. The softgate reverses
   the NAT and forwards the packet through the NS fabric back to the
   server.

### 8.3 Management Plane: Switch to Controller

This path is used by every switch and softgate to communicate with the
Netris controller (gRPC for configuration, Telescope for telemetry).

```
leaf-pod00-su0-r0
    |
    | ens3 (10.253.0.1, on br-mgmt)
    | dst: 10.8.0.2:50051 (gRPC)
    v
mgmt-server
    |
    | 10.253.63.254 (gateway for 10.253.0.0/18)
    | iptables MASQUERADE on tun0
    v
OpenVPN tunnel (TCP 1194)
    |
    | 10.8.0.1 -> 10.8.0.2
    v
Bare-metal host
    |
    | socat on 10.8.0.2:50051
    | forwards to HAProxy pod IP
    v
K3s pod: HAProxy -> gRPC server container
    |
    | Response follows reverse path
    v
leaf-pod00-su0-r0 receives configuration update
```

This three-hop path (switch -> mgmt-server -> VPN -> socat -> K3s) is the
price of running the controller on the same host as the VMs. In a production
deployment, switches would reach the controller directly over the network.

---

## Appendix A: IP Address Map

| Range               | Purpose                           | Used By                    |
|----------------------|-----------------------------------|----------------------------|
| 10.0.0.0/8           | Private IP allocation (umbrella)  | Netris IPAM                |
| 10.253.0.0/18        | EW switch management              | EW leaves + spines         |
| 10.253.128.0/24      | Switch loopback IPs               | All switches               |
| 10.254.0.0/16        | EW leaf-spine link /31s           | Point-to-point links       |
| 10.2.0.0/16          | NS loopback IPs                   | NS switches + softgates    |
| 10.3.0.0/16          | NS management IPs                 | NS switches + softgates    |
| 10.8.0.0/24          | VPN tunnel                        | mgmt-server <-> host       |
| 10.10.0.0/24         | BGP peering /30s                  | Softgate <-> ISP           |
| 169.254.x.y/31       | NS leaf-spine links               | Point-to-point links       |
| 172.16-30.x.y/31     | EW server data links (8 paths)    | Leaf <-> GPU server        |
| 192.168.0.0/21       | NS GPU server subnet              | GPU server NS interfaces   |
| 192.168.8.0/21       | GPU server IPMI                   | GPU server OOB ports       |
| 192.168.16.0/20      | Server management                 | GPU + CPU server mgmt      |
| 192.168.122.0/24     | libvirt NAT bridge                | mgmt-server, isp-server    |
| 198.51.100.0/29      | NAT pool (TEST-NET-3)             | Softgate SNAT              |
| 198.51.100.16/29     | L4LB pool (TEST-NET-3)            | Softgate L4LB VIPs         |
| 198.51.100.8/29      | BGP link subnet                   | ISP uplink addressing      |

## Appendix B: ASN Map

| ASN          | Device / Role                     |
|--------------|-----------------------------------|
| 4200100001+  | EW leaf switches (sequential)     |
| 4200200001   | EW spine switches (shared)        |
| 4200300001+  | NS leaf switches (sequential)     |
| 4200300129   | NS OOB leaf switch                |
| 4200300257+  | NS spine switches (sequential)    |
| 65401        | ISP (isp-server FRR)              |
| 65500        | Site ROH ASN                      |
| 65501        | Site VM ASN                       |
| 655001       | Site public ASN (softgates)       |

## Appendix C: Port and Protocol Reference

| Port  | Protocol | Service            | Listener               |
|-------|----------|--------------------|------------------------|
| 1194  | TCP      | OpenVPN            | mgmt-server            |
| 2003  | TCP      | Graphite           | socat -> K3s           |
| 3033  | TCP      | Telescope          | socat -> K3s           |
| 3034  | TCP      | Telescope TLS      | socat -> K3s           |
| 6444  | TCP      | K3s API            | K3s on host            |
| 50051 | TCP      | Netris gRPC        | socat -> K3s           |
| 80    | TCP      | Apache (ZTP files) | mgmt-server            |
| 9443  | TCP      | Netris Web UI      | socat -> K3s Traefik   |\n| 80    | TCP      | Controller API     | socat -> K3s Traefik   |
| 67    | UDP      | DHCP server        | mgmt-server            |
| 179   | TCP      | BGP                | Switches, FRR, softgates|
| 1025+ | UDP      | VM-to-VM tunnels   | QEMU/libvirt           |

## 9. Single-Server Deployment Challenges

When consolidating the lab from two servers to one, several architectural
challenges arise. This section documents how each is solved.

### 9.1 Isolated Mode — Internet for VMs Without Public IPs

In the original two-server lab, the ISP server sits on `br-public` with a real
public IP and a real upstream gateway. In isolated mode (single server, no
public IPs), `br-public` has no upstream.

**Solution:** The host acts as the gateway on `br-public`:

```
ISP server (198.51.100.10)
    |
    | ens5 (br-public)
    |
    | default route: via 198.51.100.9
    v
Host (198.51.100.9 on br-public)  ──masquerade──>  eno3 ──> Internet
```

One IP address and one iptables MASQUERADE rule give all br-public VMs
internet access. The same pattern applies to `br-mgmt` (10.3.0.0/16) for
softgate internet access during agent installation.

**Return-path routing for SNAT pools:** The host's br-public interface must
not have a prefix length that covers the NAT/L4LB pool addresses. If the
host has 198.51.100.x/24, the entire 198.51.100.0/24 becomes a connected
route -- reply packets destined for the SNAT pool (198.51.100.0/29) are
delivered directly on br-public instead of being routed back through the
ISP server to the softgates. The fix is to add specific routes:

```
ip route add 198.51.100.0/29 via 198.51.100.10 dev br-public
ip route add 198.51.100.16/29 via 198.51.100.10 dev br-public
```

This ensures SNAT'd reply traffic follows the correct return path:
internet → host → ISP server → softgate (reverse NAT) → NS fabric →
server.

### 9.2 Controller Connectivity — The Socat Bridge

On the original lab, the controller runs on a separate server with its own
IP. Switches connect to it directly. On a single server, the controller
runs in K3s pods. K3s's LoadBalancer implementation (svclb) uses
iptables-legacy DNAT inside containers, which is invisible to RHEL's
nftables networking.

**Solution:** Socat systemd services bind on the VPN IP (10.8.0.2) and the
host IP, forwarding directly to the K3s haproxy pod IP:

```
Switch agent (10.253.0.1)
    |
    | gRPC to 10.8.0.2:50051
    v
mgmt-server (10.253.63.254)  ──NAT via tun0──>  Host tun0 (10.8.0.2)
    |
    | socat on 10.8.0.2:50051
    v
K3s haproxy pod (10.42.0.x:50051)
    |
    v
Netris gRPC server pod
```

A fixed UI port (default: 9443) is also forwarded via socat so the web UI
URL doesn't change between K3s restarts.

### 9.3 Softgate Agent Installation

In the original lab, softgates install via cloud-init:

1. Cloud-init runs `dhclient` → DHCP sets hostname to `ns-softgate-0`
2. The install script greps `/tmp/netris-devices` for `$(hostname)`
3. Match found → agent installs

On a single server with 54 VMs booting simultaneously, DHCP hostname
assignment races with cloud-init. The hostname stays `softgate` (from the
cloud-init config), the grep fails, and the install is silently skipped.

**Solution:** The Ansible connectivity role installs the agent after VMs are
fully booted:

1. Wait for SSH to be accessible
2. Set hostname via `hostnamectl`
3. Run the installer with `--node-type softgate_hs` (the `softgate` type has
   a bug — empty package name variable)
4. Immediately set `dpdk = no` in the config (VMs can't use DPDK; without this
   the agent crashes or takes over the management NIC)
5. Restart the agent

### 9.4 libvirt and K3s Firewall Conflicts

K3s loads `br_netfilter` and sets `bridge-nf-call-iptables=1`, causing all
bridge traffic to pass through iptables/nftables FORWARD chains. libvirt
creates restrictive nftables chains (`guest_input`, `guest_output`) that only
allow established/related traffic to VMs — blocking new connections from the
host.

Additionally, libvirt's default firewall backend (iptables-legacy) conflicts
with K3s's iptables-nft. The solution is:

1. Set `firewall_backend = "nftables"` in `/etc/libvirt/network.conf`
2. Flush the restrictive `libvirt_network` chains after every K3s restart
3. Keep `virbr0`, `br-mgmt`, and `br-public` in firewalld's trusted zone

### 9.5 VPN Route Conflict

The OpenVPN server on the mgmt-server VM pushes a route for
`192.168.122.0/24` to the VPN client (the host). This overrides the
virbr0 connected route, making the mgmt-server and ISP server unreachable
from the host.

**Solution:** A systemd `ExecStartPost` script deletes the pushed route and
re-adds the virbr0 connected route immediately after the VPN starts.

### 9.6 CPU Server Link Underlay Flag

The OpenTofu topology code (`north-south.tf`) creates links between servers
and NS leaf switches. GPU server links explicitly set `underlay = "disabled"`,
which tells the controller these are access ports for VNet traffic. However,
the CPU server link resources (`netris_link.north-south-leaf-to-cpu-group-*`)
were missing this attribute. The Netris Terraform provider defaults `underlay`
to `"enabled"`, causing the controller to treat CPU server ports as fabric
interconnects instead of access ports.

**Symptom:** When a VNet is created with CPU server ports (e.g., eth9@COMPUTE01),
the controller creates the SVI/VXLAN/VRF config on the NS leaf but never
creates a bond or bridge-port for the switch port facing the server. The
anycast gateway IP exists on the leaf but the server's traffic never enters
the bridge -- ARP for the gateway fails.

**Fix:** Add `underlay = "disabled"` to all four CPU server link resources in
`north-south.tf`. This is already set for GPU server links (line 220) and
OOB links (line 312) but was missing for CPU groups (lines 235--281).

### 9.7 CPU Server VNet and SNAT Configuration

GPU servers use Server Cluster Templates that auto-generate VPCs, VNets, and
IPAM subnets. CPU servers (COMPUTE00--04) require manual configuration to
access the internet via the NS fabric:

1. **Create a VPC** (e.g., "test") under Network → VPC.
2. **Create an IPAM allocation and subnet** (e.g., 192.168.100.0/24, purpose
   "common") assigned to the VPC and the site.
3. **Create a VNet** with the VPC, add 192.168.100.1/24 as the gateway, and
   attach the **server ports** (e.g., eth9@COMPUTE01), not the switch ports.
   Using server ports lets the controller resolve the link and create the
   bond + bridge-port on the NS leaf automatically.
4. **Create a NAT rule** (SNAT, source 192.168.100.0/24, destination 0.0.0.0/0)
   assigned to the VPC. The SNAT softgates will apply the rule.

The VNet gateway becomes an anycast IP on every NS leaf that hosts a member
port. Traffic from COMPUTE servers hits the gateway, gets routed to a General
softgate (which injects a default route into the VPC), then redirected to an
SNAT softgate for source NAT, and finally exits via the ISP eBGP tunnels.

### 9.8 Controller API Authentication

The Netris v1 auth endpoint (`POST /api/auth`) uses `user` and `password`
fields, not `login`. The v2 endpoints require a session cookie obtained from
v1 auth. Fresh controller installs have `salt="true"` in the MariaDB users
table which triggers a mandatory password change flag. The Ansible
`password_fix.yml` task detects this and replaces it with a proper bcrypt
hash so the API is usable immediately after deployment.

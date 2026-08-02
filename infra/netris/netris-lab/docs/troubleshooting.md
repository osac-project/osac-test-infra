# Troubleshooting Guide

This document covers every issue encountered during the manual deployment of the Netris lab on RHEL 9.5.

## 1. K3s Won't Start — Port 6443 in Use

**Symptom:** K3s fails to start with error `listen tcp :6443: bind: address already in use`.

**Root Cause:** Another service (kind cluster, OpenShift, etc.) is already using port 6443, which is K3s' default API server port.

**Fix:** Configure K3s to use a different port by creating `/etc/rancher/k3s/config.yaml` with:
```yaml
https-listen-port: 6444
```
Then restart K3s with `systemctl restart k3s`.

## 2. Controller Web UI Returns 502 Bad Gateway

**Symptom:** curl to port 80 or NodePort returns 502 Bad Gateway.

**Root Cause:** Traefik started before the frontend pod was ready, so the endpoints were not registered in the ingress controller.

**Fix:** Restart the Traefik deployment:
```bash
kubectl rollout restart deploy/traefik -n kube-system
```
Alternatively, just wait a few moments and retry — the endpoints eventually register.

## 3. Pod-to-Pod Networking Broken After Firewalld Changes

**Symptom:** Pods can't reach each other, DNS resolution fails in pods with errors like `lookup ... i/o timeout`.

**Root Cause:** Firewalld's nftables rules block flannel VXLAN traffic on the cni0 and flannel.1 interfaces.

**Fix:** Add the CNI interfaces to the firewalld trusted zone:
```bash
firewall-cmd --permanent --zone=trusted --add-interface=cni0
firewall-cmd --permanent --zone=trusted --add-interface=flannel.1
firewall-cmd --reload
```
If DNS is still broken after this, restart K3s:
```bash
systemctl restart k3s
```

## 4. Can't Reach VMs on virbr0 (Ping/SSH to 192.168.122.10 Fails)

**Symptom:** ARP works but ICMP and TCP connections to VMs on 192.168.122.0/24 fail.

**Root Cause:** The VPN pushes a route for 192.168.122.0/24 via tun0, overriding the virbr0 connected route.

**Fix:** Delete the conflicting VPN route:
```bash
ip route del 192.168.122.0/24 via 255.255.255.0 dev tun0
```
The automation handles this with an ExecStartPost script in the OpenVPN systemd service.

## 5. SPICE Graphics Not Supported on RHEL QEMU

**Symptom:** VM creation fails with error `unsupported configuration: spice graphics are not supported with this QEMU`.

**Root Cause:** RHEL 9.5 QEMU doesn't include SPICE support.

**Fix:** Change the graphics type from SPICE to VNC in `main.go`:
```go
Type: pulumi.String("vnc")
```
Instead of:
```go
Type: pulumi.String("spice")
```

## 6. Mandatory Password Change Blocks API Authentication

**Symptom:** API login returns `mandatoryPasswordChange: true` and no token. Terraform provider authentication fails.

**Root Cause:** Fresh Netris install has `salt="true"` in MariaDB which triggers the mandatory password change flag.

**Fix:** Update MariaDB with a proper bcrypt salt and hash. Generate the hash:
```bash
python3 -c "import bcrypt; salt=bcrypt.gensalt(); print(f'Salt: {salt.decode()}'); print(f'Hash: {bcrypt.hashpw(b\"newpassword123\", salt).decode()}')"
```
Then update the database:
```bash
kubectl exec -n netris-controller deploy/mariadb -- mysql -u netris -pnetris netris -e \
  "UPDATE users SET salt='<generated-salt>', password='<generated-hash>' WHERE id=1"
```

## 7. br-public Bridge Doesn't Exist

**Symptom:** `Cannot get interface MTU on 'br-public': No such device` when creating isp-server VM.

**Root Cause:** The br-public bridge wasn't created before Pulumi runs.

**Fix:** Create the bridge manually:
```bash
ip link add name br-public type bridge
ip link set dev br-public up
```

## 8. VPN Service Name Differs on RHEL

**Symptom:** `Unit openvpn@client.service not found` when trying to start the VPN.

**Root Cause:** RHEL uses `openvpn-client@client` not `openvpn@client`. Config files go in `/etc/openvpn/client/` not `/etc/openvpn/`.

**Fix:** Copy configs to the correct location:
```bash
cp client.conf /etc/openvpn/client/
cp client.ovpn /etc/openvpn/client/
```
Then use the correct service name:
```bash
systemctl start openvpn-client@client
systemctl enable openvpn-client@client
```

## 9. K3s svclb DNAT Doesn't Work on RHEL

**Symptom:** LoadBalancer services get external IP but connections timeout. Ports show as listening via `ss` but curl hangs.

**Root Cause:** The svclb container creates iptables-legacy rules, but RHEL uses iptables-nft. The legacy rules are invisible to nftables.

**Fix:** Use socat to forward ports on the VPN IP (10.8.0.2) and host IP to the haproxy pod IP directly:
```bash
socat TCP-LISTEN:80,fork,reuseaddr TCP:10.43.x.x:80 &
socat TCP-LISTEN:443,fork,reuseaddr TCP:10.43.x.x:443 &
```
Or use systemd services to manage the socat processes persistently.

## 10. Softgate DPDK Takes Over Management NIC

**Symptom:** After reboot, softgate VMs have no ARP response and are completely unreachable.

**Root Cause:** `--node-type softgate_hs` enables DPDK which binds ALL virtio interfaces, including the management NIC.

**Fix:** Use `--node-type softgate` (non-DPDK version) when provisioning softgates. The existing lab also uses non-DPDK softgates (`dpdk = no` in netris.conf).

## 11. `virsh net-destroy default` Detaches VM Ports from virbr0

**Symptom:** After restarting the libvirt default network, mgmt-server and isp-server lose virbr0 connectivity.

**Root Cause:** `virsh net-destroy` removes the bridge and all port attachments. `virsh net-start` recreates the bridge but doesn't re-attach VM ports.

**Fix:** Avoid using `virsh net-destroy`. If you must restart the network, manually re-add ports:
```bash
ip link set vnetXX master virbr0
```
where `vnetXX` is the tap device for each VM (check with `virsh domiflist <vm-name>`).

## 12. Firewalld Restart Breaks K3s Networking

**Symptom:** After `systemctl restart firewalld`, cluster DNS fails and pods can't communicate.

**Root Cause:** Firewalld reload flushes and recreates nftables rules, invalidating K3s-managed iptables entries.

**Fix:** Restart K3s after firewalld changes:
```bash
systemctl restart k3s
```
Or avoid restarting firewalld — use `firewall-cmd --permanent` to stage changes, then `firewall-cmd --reload` instead of `systemctl restart firewalld`.

## 13. Softgate Installer Uses Wrong Package Name with --node-type softgate

**Symptom:** `curl -fsSL https://get.netris.io | sh -s -- --node-type softgate` runs but the package is never installed. `apt` says "0 newly installed".

**Root Cause:** The installer script sets `VTEP_AGENT_NAME` only for `acs_hyper` and `evpn_vtep` types. For `softgate`, the variable is empty, so it runs `apt-get install netris--agent` (note the double dash — nonexistent package) which silently does nothing.

**Fix:** Use `--node-type softgate_hs` instead. This maps to the correct package `netris-sg-hs`. Then set `dpdk = no` in `/opt/netris/etc/netris.conf` before starting the agent (VMs can't use DPDK).

## 14. libvirt_network nftables Table Blocks Host-to-VM Traffic

**Symptom:** ARP works to VMs on virbr0 but ICMP/TCP fails. Happens after K3s install or libvirt network restart.

**Root Cause:** libvirt creates a `libvirt_network` nftables table with `guest_input` and `guest_output` chains that only allow established/related traffic to VMs. With `bridge-nf-call-iptables=1` (set by K3s), bridged traffic passes through these chains and gets rejected.

**Fix:** The automation flushes these chains and adds `accept` rules. If connectivity breaks, run:
```bash
nft flush chain ip libvirt_network guest_input
nft flush chain ip libvirt_network guest_output
nft add rule ip libvirt_network guest_input accept
nft add rule ip libvirt_network guest_output accept
```

## 15. libvirt Fails to Start Default Network — iptables Conflict

**Symptom:** `virsh net-start default` fails with "table nat is incompatible, use nft tool".

**Root Cause:** libvirt defaults to iptables-legacy for firewall rules, but K3s uses iptables-nft. The two backends conflict on the nat table.

**Fix:** Set libvirt to use the nftables backend:
```bash
echo 'firewall_backend = "nftables"' >> /etc/libvirt/network.conf
systemctl restart libvirtd
```

## 16. ISP Server Can't Reach Internet in Isolated Mode

**Symptom:** FRR fails to install on ISP server. `apt-get` can't resolve or connect to repositories.

**Root Cause:** The ISP server's default route points to br-public (TEST-NET address) which has no real upstream in isolated mode.

**Fix:** The automation adds the host as the br-public gateway and masquerades traffic:
```bash
ip addr add 198.51.100.9/29 dev br-public
iptables -t nat -A POSTROUTING -s 198.51.100.0/29 -o eno3 -j MASQUERADE
```

## 17. Softgate Cloud-Init Skips Agent Install — Hostname Mismatch

**Symptom:** Softgate VMs boot but the netris agent is never installed by cloud-init.

**Root Cause:** All softgate VMs share one cloud-init ISO with `hostname: softgate`. The install script does `grep "^$(hostname)" /tmp/netris-devices` but the devices file has `ns-softgate-0`, not `softgate`. The grep never matches, so the script exits without installing.

**Fix:** The automation installs the agent from the connectivity role after VMs are up and hostnames are set via DHCP/hostnamectl. This bypasses the cloud-init hostname issue entirely.

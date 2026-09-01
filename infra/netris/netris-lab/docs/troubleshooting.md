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

## 18. Softgate Agent Crash-Loops — Missing plugins.conf

**Symptom:** `netris-sg` never reaches `active`; `systemctl status netris-sg` shows a growing restart counter. `journalctl -u netris-sg` shows `ENOENT: no such file or directory, lstat '/opt/netris/etc/plugins.conf'`.

**Root Cause:** The `netris-sg-hs` package ships two variant plugin configs, `plugins.conf_dpdkno` and `plugins.conf_dpdkyes`, but never symlinks or copies either one to the plain `plugins.conf` path the systemd unit's `--plugins` flag expects. This happens regardless of networking or the `dpdk = no` fix in item 10/13 above — setting `dpdk = no` alone does not create `plugins.conf`.

**Fix:** After forcing `dpdk = no`, also copy the matching variant into place:
```bash
cp -f /opt/netris/etc/plugins.conf_dpdkno /opt/netris/etc/plugins.conf
systemctl restart netris-sg
```
The automation does this in `roles/connectivity/tasks/softgates.yml`'s agent-install step, right after the `dpdk` sed.

## 19. VPN Route Conflict Also Hits br-mgmt and the Softgate Mgmt Subnet

**Symptom:** After a VPN reconnect (or `make deploy-infra` retry), softgates at `10.3.3.x` become unreachable again even though they were previously working — `ping`/`ssh` from the host time out, `ip neigh` shows no ARP replies.

**Root Cause:** Item 4/5 above cover the VPN pushing a conflicting route for `192.168.122.0/24` (virbr0). The same VPN server also pushes conflicting routes for br-mgmt's own subnet (`mgmt_bridge_network`, e.g. `192.168.16.0/20`) and the softgate mgmt subnet (`ns_fabric.mgmt_subnet`, e.g. `10.3.0.0/16`) via `tun0`, with the same malformed gateway (the peer's netmask). This silently breaks host-to-softgate reachability on every VPN (re)connect, independent of item 18 above.

**Fix:** `openvpn-route-fix.sh.j2` (installed as `openvpn-client@client.service`'s `ExecStartPost`) deletes and re-adds the connected routes for all three prefixes, not just virbr0, so this self-heals on every VPN start/restart. If you hit this on a host where the fix predates this update, re-run `make connectivity` (or manually re-run `/usr/local/bin/openvpn-route-fix.sh`) to pick up the regenerated script.

## 20. Softgate `ens4` Loses Its IP After Reboot — `Invalid server_address`

**Symptom:** `netris-sg` crash-loops (or never starts) with `Invalid server_address` even after the `plugins.conf` fix (item 18) is applied. `ip a show ens4` on the softgate shows no IPv4 address. `netris.conf`'s `grpc.address` and `telescope.server_address` are empty.

**Root Cause:** The softgate cloud-init template (`netris-cloudsim/templates.go`'s `prepareCloudInitSG`) only runs `[dhclient, -v]` once, imperatively, in `runcmd` — then reboots at the end of that same `runcmd` (needed for the hostname/agent setup to take effect). `dhclient -v` gets a lease for the current boot, but nothing re-requests one on **subsequent** boots, so `ens4` comes up with no IP after every reboot. Without an IP, `curl get.netris.io | bash` (the installer that normally populates `grpc.address`/`telescope.server_address`) can't reach the controller at `10.8.0.2`, so those fields are left blank and the agent fails with `Invalid server_address`.

**Fix:** `prepareCloudInitSG` now also writes a persistent netplan config in `write_files`:
```yaml
- path: /etc/netplan/90-mgmt-dhcp.yaml
  content: |
    network:
      version: 2
      ethernets:
        ens4:
          dhcp4: true
```
The softgate image (Ubuntu 24.04) runs `systemd-networkd` as its netplan renderer, so this is picked up automatically on every boot — confirmed via `networkctl status ens4` showing `Network File: /run/systemd/network/10-netplan-ens4.network` and an active DHCP4 lease after a reboot. This takes effect for newly-created softgate VMs (next Pulumi rebuild).

**Live workaround for already-deployed softgates:**
1. Request a lease over the qemu guest agent (no SSH needed, since there's no IP yet):
   ```bash
   virsh qemu-agent-command <vm> '{"execute":"guest-exec","arguments":{"path":"/sbin/dhclient","arg":["-v","ens4"],"capture-output":true}}'
   ```
2. Re-run the installer (`roles/connectivity/tasks/softgates.yml`'s install command) now that the softgate can reach the controller. If the agent already crash-looped through a first, network-less install attempt, `/opt/netris/etc/netris.conf` and `/opt/netris/installer.lock` are already populated with a bad/incomplete config, and the installer treats a second run as an "upgrade" that **skips re-initialization** (`- Initialize the Softgate Step was skipped`) — silently keeping the broken config. Delete the lock first to force full re-init:
   ```bash
   ssh root@<softgate> "rm -f /opt/netris/installer.lock"
   # then re-run: curl -fsSL https://get.netris.io | sh -s -- --lo <main_ip> --controller 10.8.0.2 \
   #   --hostname <name> --auth <token> --node-type softgate_hs
   ```
   A full re-init also runs "Setup Main Loopback" (fixes a possible `Cannot determine loopback ip address` telescope error) and prints `*** ATTENTION: You must reboot SoftGate to complete the installation` — reboot once the netplan fix above (or its live-applied equivalent) is in place so the softgate doesn't lose `ens4` again on that reboot.
3. `<token>` is the controller's static `netris_auth_token` setting (not a per-user login token) — fetch it the same way `netris.controller.general`'s `read` role does: `POST /api/auth` with `{"user":"netris","password":"netris","auth_scheme_id":1}` to get a `connect.sid` cookie, then `GET /api/v2/general` with that cookie and read the `netris_auth_token` entry from the response's `data` array.

## 21. Switches Show `Critical` Health / E-BGP Never Establishes — Strict rp_filter Drops Softgate-to-Controller Traffic

**Symptom:** All leaf/spine switches show `health: critical` (`health_monitoring: critical - Monitoring Unavailable`) in the Netris UI/`api/v2/hw?showHealth=true`. `api/v2/ebgp` shows every session's `bgp_state` as empty (never `Established`). On a softgate, `vtysh -c "show bgp summary"` returns `% BGP instance not found` even though `netris.conf` has a real `server_address` (item 20 is already fixed) and `systemctl is-active netris-sg` reports `active`. `journalctl -u netris-sg` shows `offloaderpd`/`telescope` endlessly retrying `API.getSwitchInfo(): rpc error: code = DeadlineExceeded`.

**Root Cause:** The controller's gRPC (`50051`) and telescope (`3033`/`3034`) ports are exposed to the lab VMs via `socat`, bound to `10.8.0.2` — the hypervisor's OpenVPN client's own `tun0` address (forwarded into the k3s cluster's `netris-controller-haproxy` pod). RHEL's `/usr/lib/sysctl.d/50-redhat.conf` sets `net.ipv4.conf.default.rp_filter = 1`. Since `br-mgmt` and `tun0` are both created *after* boot (by the lab automation and by the VPN client, respectively), they inherit this strict default — and the *effective* filter for an interface is `max(all, <iface>)`, so this holds even when `net.ipv4.conf.all.rp_filter` is `0`. With strict RPF on both `br-mgmt` (softgates' ingress) and `tun0` (owns the destination address), the kernel silently drops the softgates' inbound connections to `10.8.0.2`, confirmed by a `tcpdump -i br-mgmt` showing SYN retransmits with no reply, and conclusively by toggling `rp_filter` to `0` and watching the TCP connection immediately succeed. Without connectivity to the controller, `offloaderpd`/`telescope` never pull switch/BGP config, so FRR never gets a BGP instance at all, and health-monitoring data never reaches the controller (hence the switches' `Monitoring Unavailable`).

**Fix:** `roles/prerequisites/tasks/main.yml` now sets `net.ipv4.conf.default.rp_filter = 0` and `net.ipv4.conf.all.rp_filter = 0` (via `/etc/sysctl.d/99-netris-lab-rp-filter.conf`), covering interfaces created after this task runs, plus an explicit fixup loop that zeroes `rp_filter` on `{{ mgmt_bridge_name }}` and `tun0` if they already exist (the re-run case, since the `default` template only applies at interface-creation time and won't retroactively change an already-created interface's value). If you hit this on a host predating the fix, re-run `make connectivity` (or manually apply the same two sysctls and restart `netris-sg` on each softgate).

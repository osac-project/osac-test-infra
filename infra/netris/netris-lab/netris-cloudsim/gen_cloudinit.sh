#!/bin/bash

usage() {
  cat <<USAGE
Usage:
  $0 --mgmt-iface <IFACE> --public-iface <IFACE>
  $0 <mgmt_vlan> <public_vlan>                        (legacy, assumes bond0.<VLAN>)

Examples:
  PNAP:          $0 --mgmt-iface bond0.22 --public-iface bond0.23
  IBM Classic:   $0 --mgmt-iface bond0.100 --public-iface bond1.200
  Equinix Metal: $0 --mgmt-iface bond0.1000 --public-iface bond0.1001
  Hetzner:       $0 --mgmt-iface eth1 --public-iface eth0
  Legacy:        $0 22 23
USAGE
  exit 1
}

MGMT_IFACE=""
PUBLIC_IFACE=""

# Parse arguments
if [ "$#" -eq 2 ] && [[ "$1" != --* ]]; then
  # Legacy mode: positional VLAN IDs on bond0
  MGMT_IFACE="bond0.$1"
  PUBLIC_IFACE="bond0.$2"
elif [ "$#" -ge 4 ]; then
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --mgmt-iface)  MGMT_IFACE="$2"; shift 2 ;;
      --public-iface) PUBLIC_IFACE="$2"; shift 2 ;;
      *) usage ;;
    esac
  done
else
  usage
fi

if [ -z "$MGMT_IFACE" ] || [ -z "$PUBLIC_IFACE" ]; then
  usage
fi

# Generate bridge setup commands for a given interface.
# VLAN subinterfaces (containing a dot) are created before being added to the bridge.
bridge_cmds() {
  local br_name="$1"
  local iface="$2"

  if [[ "$iface" == *.* ]]; then
    local parent="${iface%%.*}"
    local vlan_id="${iface##*.}"
    echo "  - ip link add link ${parent} name ${iface} type vlan id ${vlan_id} 2>/dev/null || true"
  fi
  echo "  - brctl addbr ${br_name} 2>/dev/null || true && brctl addif ${br_name} ${iface} && ip link set dev ${br_name} up"
}

MGMT_BRIDGE_CMDS=$(bridge_cmds "br-mgmt" "$MGMT_IFACE")
PUBLIC_BRIDGE_CMDS=$(bridge_cmds "br-public" "$PUBLIC_IFACE")

cat <<EOF
#cloud-config
packages:
  - virt-manager
  - bridge-utils
bootcmd:
  - [ sh, -c, 'echo \$(date) | sudo tee -a /root/bootcmd.log' ]
  - [ sh, -c, 'iptables -t nat -A PREROUTING -p tcp --dport 1194 -j DNAT --to-destination 192.168.122.10:1194' ]
${MGMT_BRIDGE_CMDS}
${PUBLIC_BRIDGE_CMDS}
runcmd:
  - sudo sed -i '/#security_driver = "selinux"/c\security_driver = "none"' /etc/libvirt/qemu.conf && sudo systemctl restart libvirtd
  - [ sh, -c, 'echo \$(date) | sudo tee -a /root/runcmd.log' ]
  - [ sh, -c, 'echo "net.core.rmem_default=2129920" >> /etc/sysctl.conf' ]
  - [ sh, -c, 'sysctl -p' ]
${MGMT_BRIDGE_CMDS}
${PUBLIC_BRIDGE_CMDS}
  - [ sh, -c, 'sudo virsh pool-define-as --name default --type dir --target /var/lib/libvirt/images' ]
  - [ sh, -c, 'sudo virsh pool-start default' ]
  - [ sh, -c, 'sudo virsh pool-autostart default' ]
  - [ sh, -c, 'iptables -I FORWARD -p tcp --dport 1194 -j ACCEPT' ]
  - [ sh, -c, 'sudo curl http://downloads.netris.ai/cumulus-linux-5.11.3-vx-amd64-qemu.qcow2 -o /var/lib/libvirt/images/cumulus-linux-5.11.3.qcow2' ]
  - [ sh, -c, 'sudo curl https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img -o /var/lib/libvirt/images/ubuntu-24.04-server-cloudimg-amd64.img' ]
EOF

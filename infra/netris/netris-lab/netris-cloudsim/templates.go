package main

import (
	"bytes"
	"fmt"
	"net"
	"text/template"
	"github.com/Masterminds/sprig/v3"
)

type TmplMapEachDevBlock map[string]interface{}

func domainXslt(vmSpecs ToTemplate) string {

	// Create a function map with the 'mod' and 'div' functions
	funcMap := template.FuncMap{
		"mod": func(a, b int) int {
			return a % b
		},
		"div": func(a, b int) int {
			return a / b
		},
		"add": func(a, b int) int {
			return a + b
		},
	}

	tmpl, err := template.New("NICs").Funcs(funcMap).Parse(
		`
{{- $dot := . }}
<?xml version="1.0" ?>
<xsl:stylesheet version="1.0"
xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output omit-xml-declaration="yes" indent="yes"/>
<xsl:template match="node()|@*">
<xsl:copy>
<xsl:apply-templates select="node()|@*"/>
</xsl:copy>
</xsl:template>
<xsl:template match="/domain/devices">
<devices>
<xsl:apply-templates select="@*|node()"/>
<controller type="pci" index="0" model="pci-root"/>
{{- range $key, $value := .Links }}
{{- if eq (mod $key 32) 0 }}
<controller type="pci" index="{{ add (div $key 32) 1 }}" model="pci-bridge">
  <model name="pci-bridge"/>
</controller>
{{- end }}
{{- if and (ne $value.RemoteIP "") (ne $value.LocalIP "") (ne $value.RemoteID 0) (ne $value.LocalID 0) }}
<interface type="udp">
  <source address="{{ $value.RemoteIP }}" port="{{ $value.RemoteID }}">
    <local address="{{ $value.LocalIP }}" port="{{ $value.LocalID }}"/>
  </source>
  <model type="virtio"/>
</interface>
{{- else if and (eq $value.RemoteID 0) (eq $value.LocalID 0) $dot.CreateEmptyPorts }}
<interface type='bridge'>
  <source bridge='virbr0'/>
  <model type='virtio'/>
  <link state="down"/>
</interface>
{{- end }}
{{- end }}
</devices>
</xsl:template>
</xsl:stylesheet>
`)
	if err != nil {
		panic(err)
	}
	var tmplOut bytes.Buffer
	err = tmpl.Execute(&tmplOut, vmSpecs)
	if err != nil {
		panic(err)
	}
	return tmplOut.String()
}

func prepareCloudInit(diskArgs map[string]interface{}, forNetwork bool) string {
	if forNetwork {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
version: 2
ethernets:
  ens4:
    dhcp4: true
    dhcp6: false
    mtu: {{ .mtu }}
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	} else {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
#cloud-config
hostname: {{ .hostname }}
manage_etc_hosts: true
users:
  - name: root
    lock_passwd: false
    hashed_passwd: $6$SX2xaqo0V5Z3duvX$1UXpCr.x.XV7PJkARgqoh9r6LlHXofX99IcX9.NCnfTedHoBVe1CBwsRgcCbvKqkzwo7tiKe2k4Z75uRxnafE/
    ssh_authorized_keys:
    {{- range $item := .sshAuthKey }}
      - {{ $item }}
    {{- end }}
# only cert auth via ssh (console access can still login)
ssh_pwauth: false
disable_root: false
packages:
  - qemu-guest-agent
{{- range $item := .installedPackages }}
  - {{ $item }}
{{- end }}
# every boot
bootcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/bootcmd.log' ]
# run once for setup
runcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/runcmd.log' ]
  - [ sh, -c, 'apt-get update && apt-get install isc-dhcp-client -y' ]
  - [dhclient, -v]
  - |
    #!/bin/bash

    # Function to check if the network interface is up and has an IP address
    check_interface_ip() {
        while true; do
            # Replace 'ens4' with your actual interface name, e.g., 'ens3'
            IP_ADDRESS=$(ip -4 addr show ens4 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
            if [ -n "$IP_ADDRESS" ]; then
                echo "Network interface ens4 is up and has IP: $IP_ADDRESS"
                break
            else
                echo "Waiting for network interface ens4 to get an IP address..."
                sleep 5
            fi
        done
    }

    # Check that the network interface has an IP address
    check_interface_ip

    # Function to check internet and DNS connectivity
    check_connectivity() {
        while true; do
            # Check basic internet connectivity
            if ping -c 1 8.8.8.8 &> /dev/null; then
                echo "Internet is accessible."
                # Check DNS resolution
                if nslookup archive.ubuntu.com &> /dev/null; then
                    echo "DNS resolution is working."
                    break
                else
                    echo "DNS resolution failed. Retrying..."
                fi
            else
                echo "Waiting for internet connectivity..."
            fi
            sleep 5
        done
    }


    bash /etc/network_nics_up.sh
    default_gw=$(ip route show default | awk '{print $3}'| head -1)
    sed -i "/          via: DEFAULT_GW_HERE/c\          via: $default_gw" /tmp/99-static-route.yaml
    cp /tmp/99-static-route.yaml /etc/netplan/
    chmod +x /root/cluster-ping.sh
    gpg --dearmor -o /usr/share/keyrings/netris-public-keyring.gpg /root/netris-public.key
    echo "deb [signed-by=/usr/share/keyrings/netris-public-keyring.gpg] http://repo.netris.ai/repo/ noble {{ .ctlInfo.AptRepo }}" | sudo tee /etc/apt/sources.list.d/netris.list
    # Wait until internet is accessible
    check_connectivity
    apt-get update && apt-get install -y netris-hnp
    sudo sed -i '/preserve_hostname: false/c\preserve_hostname: true' /etc/cloud/cloud.cfg
write_files:
  - path: /root/netris-public.key
    permissions: '0644'
    content: |
      -----BEGIN PGP PUBLIC KEY BLOCK-----

      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
      =/+lL
      -----END PGP PUBLIC KEY BLOCK-----
  - path: /tmp/99-static-route.yaml
    permissions: '0600'
    content: |
      # This file was generated by a script to add a static route
      network:
        version: 2
        ethernets:
          ens4:
            routes:
              - to: 10.8.0.0/30
                via: DEFAULT_GW_HERE

  - path: /etc/network_nics_up.sh
    permissions: '0744'
    content: |
      #!/bin/bash

      # Find all network interfaces
      interfaces=$(ls /sys/class/net)

      # Loop through each interface
      for nic in $interfaces; do
          # Check if the interface is a physical NIC (not a virtual or loopback interface)
          if [[ -d "/sys/class/net/$nic/device" && "$nic" != "lo" ]]; then
              echo "Setting up interface $nic"
              sudo ip link set "$nic" up
              echo "Setting speed of the interface $nic"
              ethtool -s $nic speed 1000 duplex full
          fi
      done
  - path: /etc/dhcp/dhclient-exit-hooks.d/set-hostname
    permissions: '0744'
    content: |
      #!/bin/sh
      if [ "$reason" = "BOUND" ] || [ "$reason" = "REBOOT" ] || [ "$reason" = "RENEW" ]; then
        if [ -n "$new_host_name" ]; then
          echo "Setting hostname to $new_host_name from DHCP"
          hostnamectl set-hostname "$new_host_name"
          echo "$new_host_name" > /etc/hostname
          sed -i "s/127.0.1.1.*/127.0.1.1 $new_host_name/" /etc/hosts
        fi
      fi
  - path: /root/cluster-ping.sh
    permissions: '0744'
    content: |
      #!/bin/bash


      cidrhost() {
          local cidr=$1
          local offset=$2

          # Split CIDR into IP and prefix
          local ip=$(echo "$cidr" | cut -d'/' -f1)
          local prefix=$(echo "$cidr" | cut -d'/' -f2)

          # Convert IP address to a 32-bit integer
          IFS='.' read -r -a octets <<< "$ip"
          local ipnum=$(( (octets[0] << 24) + (octets[1] << 16) + (octets[2] << 8) + octets[3] ))

          # Calculate the network mask based on the prefix
          local mask=$(( 0xFFFFFFFF << (32 - prefix) & 0xFFFFFFFF ))

          # Calculate the network base address
          local network=$(( ipnum & mask ))

          # Add the offset to the base address
          local host_ip=$(( network + offset ))

          # Calculate the broadcast address to ensure IP is in range
          local broadcast=$(( network | ~mask & 0xFFFFFFFF ))

          # Ensure the result is within the valid range
          if [ "$host_ip" -gt "$broadcast" ] || [ "$host_ip" -le "$network" ]; then
              echo "Error: Resulting IP address is out of range."
              return 1
          fi

          # Convert the resulting integer back to a dotted-decimal IP address
          local result_ip=$(( (host_ip >> 24) & 0xFF )).$(( (host_ip >> 16) & 0xFF )).$(( (host_ip >> 8) & 0xFF )).$(( host_ip & 0xFF ))

          echo "$result_ip"
      }

      # Example usage:
      #cidr="192.168.1.0/24"
      #offset=5
      #result=$(cidrhost "$cidr" "$offset")
      #echo "Calculated IP: $result"



      echo 'Usage: ./cluster-ping.sh <SU> <Host>'
      echo
      host=$2
      hostid=$(($2 * 2))
      su=$1
      r1='172.16.'$su'.'$hostid
      r2='172.18.'$su'.'$hostid
      r3='172.20.'$su'.'$hostid
      r4='172.22.'$su'.'$hostid
      r5='172.24.'$su'.'$hostid
      r6='172.26.'$su'.'$hostid
      r7='172.28.'$su'.'$hostid
      r8='172.30.'$su'.'$hostid

      cidr="192.168.0.0/21"
      offset=$(($host+1+($su*32)))
      ns1=$(cidrhost "$cidr" "$offset")

      cidr="192.168.8.0/21"
      ipmi=$(cidrhost "$cidr" "$offset")

      hostname=$(hostname)
      echo "Ping from $hostname to SU:$1 host:$2"
      echo
      echo "------ East-West Fabric ------"
      echo -n "ping rail0 ($r1)    : "
      ping $r1 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail1 ($r2)   : "
      ping $r2 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail2 ($r3)   : "
      ping $r3 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail3 ($r4)   : "
      ping $r4 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail4 ($r5)  : "
      ping $r5 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail5 ($r6)  : "
      ping $r6 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail6 ($r7)  : "
      ping $r7 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo -n "ping rail7 ($r8)  : "
      ping $r8 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo
      echo "------ North-South Fabric ------"
      echo -n "ping bond0  ($ns1)  : "
      ping $ns1 -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo
      echo "------ IPMI/BMC ------"
      echo -n "ping eth11 ($ipmi)  : "
      ping $ipmi -c 1 -q -W 0.5 | awk '/packets/{if ($4) {print "OK";} else {print "Timeout"} }'
      echo
      echo
# written to /var/log/cloud-init-output.log
final_message: "The system is finally up, after $UPTIME seconds"
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	}
}

func prepareCloudInitSG(diskArgs map[string]interface{}, forNetwork bool) string {
	if forNetwork {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
version: 2
ethernets:
  ens4:
    dhcp4: true
    dhcp6: false
    mtu: {{ .mtu }}
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	} else {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
{{- $dot := . }}
#cloud-config
hostname: {{ .hostname }}
manage_etc_hosts: true
users:
  - name: root
    lock_passwd: false
    hashed_passwd: $6$SX2xaqo0V5Z3duvX$1UXpCr.x.XV7PJkARgqoh9r6LlHXofX99IcX9.NCnfTedHoBVe1CBwsRgcCbvKqkzwo7tiKe2k4Z75uRxnafE/
    ssh_authorized_keys:
    {{- range $item := .sshAuthKey }}
      - {{ $item }}
    {{- end }}
# only cert auth via ssh (console access can still login)
ssh_pwauth: false
disable_root: false
packages:
  - qemu-guest-agent
{{- range $item := .installedPackages }}
  - {{ $item }}
{{- end }}
# every boot
bootcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/bootcmd.log' ]
  - [ sh, -c, 'bash /etc/network_nics_up.sh' ]
# run once for setup
runcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/runcmd.log' ]
  - [ sh, -c, 'apt-get update && apt-get install isc-dhcp-client -y' ]
  - [dhclient, -v]
  - |
    #!/bin/bash

    # Function to check if the network interface is up and has an IP address
    check_interface_ip() {
        while true; do
            # Replace 'ens4' with your actual interface name, e.g., 'ens3'
            IP_ADDRESS=$(ip -4 addr show ens4 | grep -oP '(?<=inet\s)\d+(\.\d+){3}')
            if [ -n "$IP_ADDRESS" ]; then
                echo "Network interface ens4 is up and has IP: $IP_ADDRESS"
                break
            else
                echo "Waiting for network interface ens4 to get an IP address..."
                sleep 5
            fi
        done
    }

    # Check that the network interface has an IP address
    check_interface_ip

    # Function to check internet and DNS connectivity
    check_connectivity() {
        while true; do
            # Check basic internet connectivity
            if ping -c 1 8.8.8.8 &> /dev/null; then
                echo "Internet is accessible."
                # Check DNS resolution
                if nslookup archive.ubuntu.com &> /dev/null; then
                    echo "DNS resolution is working."
                    break
                else
                    echo "DNS resolution failed. Retrying..."
                fi
            else
                echo "Waiting for internet connectivity..."
            fi
            sleep 5
        done
    }

    MAINIP=$(grep -w "^$(hostname)" /tmp/netris-devices | awk '{print $2}')

    if [ -z "$MAINIP" ]; then
        exit 0
    fi

    HOSTNAME=$(hostname)
    echo hostname: $HOSTNAME
    AUTHKEY={{ $dot.ctlInfo.AuthKey }}
    VERSION={{ $dot.ctlInfo.Version }}
    APT_REPO={{ $dot.ctlInfo.AptRepo }}

    bash /etc/network_nics_up.sh

    curl -fsSL https://get.netris.io | sh -s -- --lo $MAINIP --controller 10.8.0.2 --ctl-version $VERSION --hostname $HOSTNAME --auth $AUTHKEY --node-type softgate_hs --apt-repo $APT_REPO --debug

    reboot

write_files:
  - path: /etc/network_nics_up.sh
    permissions: '0744'
    content: |
      #!/bin/bash

      # Find all network interfaces
      interfaces=$(ls /sys/class/net)

      # Loop through each interface
      for nic in $interfaces; do
          # Check if the interface is a physical NIC (not a virtual or loopback interface)
          if [[ -d "/sys/class/net/$nic/device" && "$nic" != "lo" ]]; then
              echo "Setting up interface $nic"
              sudo ip link set "$nic" up
              echo "Setting speed of the interface $nic"
              ethtool -s $nic speed 1000 duplex full
          fi
      done
  - path: /tmp/netris-devices
    permissions: '0644'
    content: |
    {{- range $hyper := $dot.allVms }}
      {{- range $eachvm := $hyper }}
        {{- if eq $eachvm.Type "softgate" }}
      {{ $eachvm.Name }} {{ $eachvm.MainAddress }}
        {{- end }}
      {{- end }}
    {{- end }}
  - path: /etc/dhcp/dhclient-exit-hooks.d/set-hostname
    permissions: '0744'
    content: |
      #!/bin/sh
      if [ "$reason" = "BOUND" ] || [ "$reason" = "REBOOT" ] || [ "$reason" = "RENEW" ]; then
        if [ -n "$new_host_name" ]; then
          echo "Setting hostname to $new_host_name from DHCP"
          hostnamectl set-hostname "$new_host_name"
          echo "$new_host_name" > /etc/hostname
          sed -i "s/127.0.1.1.*/127.0.1.1 $new_host_name/" /etc/hosts
        fi
      fi
# written to /var/log/cloud-init-output.log
final_message: "The system is finally up, after $UPTIME seconds"
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	}
}

func prepareCloudInitISP(diskArgs map[string]interface{}, forNetwork bool) string {
	if forNetwork {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
version: 2
ethernets:
  ens4:
    dhcp4: false
    dhcp6: false
    mtu: {{ .mtu }}
    addresses:
      - 192.168.122.15/24
  {{- if .bgpLinkIp }}
  ens5:
    dhcp4: false
    dhcp6: false
    addresses:
      - {{ .bgpLinkIp }}
    {{- if .bgpLinkRemoteIp }}
    routes:
      - to: default
        via: {{ .bgpLinkRemoteIp }}
    nameservers:
      addresses:
        - 1.1.1.1
        - 8.8.8.8
    {{- end }}
  {{- end }}
  {{- range $item := .bgpPorts }}
  {{- $portNameList := split "@" $item.Remote.Name }}
  {{ $portNameList._0 }}:
    dhcp4: false
    dhcp6: false
    addresses:
      - {{ $item.Remote.Ipv4 }}
  {{- end }}
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	} else {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
{{- $dot := . }}
#cloud-config
hostname: {{ .hostname }}
manage_etc_hosts: true
users:
  - name: root
    lock_passwd: false
    hashed_passwd: $6$SX2xaqo0V5Z3duvX$1UXpCr.x.XV7PJkARgqoh9r6LlHXofX99IcX9.NCnfTedHoBVe1CBwsRgcCbvKqkzwo7tiKe2k4Z75uRxnafE/
    ssh_authorized_keys:
    {{- range $item := .sshAuthKey }}
      - {{ $item }}
    {{- end }}
# only cert auth via ssh (console access can still login)
ssh_pwauth: false
disable_root: false
packages:
  - qemu-guest-agent
{{- range $item := .installedPackages }}
  - {{ $item }}
{{- end }}
# every boot
bootcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/bootcmd.log' ]
# run once for setup
runcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/runcmd.log' ]
  - |
    #!/bin/bash

    sed -i '/bgpd=no/c\bgpd=yes' /etc/frr/daemons
    # Wait for the file to exist just in case
    while [ ! -f /tmp/frr.conf ]; do sleep 1; done
    cp /tmp/frr.conf /etc/frr/frr.conf
    systemctl restart frr

    # Disable unattended upgrades and needrestart after setup
    systemctl stop unattended-upgrades || true
    systemctl disable unattended-upgrades || true
    apt-get purge unattended-upgrades needrestart -y || true


write_files:
  - path: /tmp/frr.conf
    permissions: '0644'
    content: |
      frr defaults traditional
      log syslog informational
      ip forwarding
      ipv6 forwarding
      service integrated-vtysh-config
      !
      {{- if .bgpLinkRemoteIp }}
      ip route 169.254.247.0/32 {{ .bgpLinkRemoteIp }}
      ip route 169.254.247.1/32 {{ .bgpLinkRemoteIp }}
      {{- end }}

      !
      router bgp 65401
      bgp ebgp-requires-policy
      neighbor V4 peer-group
      neighbor V4 remote-as 65400
      neighbor V4 ebgp-multihop 7
      {{- if .bgpPassword }}
      neighbor V4 password {{ .bgpPassword }}
      {{- end }}
      neighbor 169.254.247.0 peer-group V4
      neighbor 169.254.247.1 peer-group V4
      {{- range $item := .bgpPorts }}
      {{- $portNameList := split "@" $item.Local.Name }}
      {{- $neighborIPList := split "/" $item.Local.Ipv4 }}
      neighbor {{ $neighborIPList._0 }} remote-as {{ $dot.netrisASN }}
      neighbor {{ $neighborIPList._0 }} description Netris-{{ $portNameList._1 }}
      neighbor {{ $neighborIPList._0 }} soft-reconfiguration inbound
      neighbor {{ $neighborIPList._0 }} default-originate
      {{- end }}
      !
      address-family ipv4 unicast
        {{- range $item := .bgpSubnetsToAdvertise }}
        network {{ $item }}
        aggregate-address {{ $item }}
        {{- end }}
        neighbor V4 route-map IMPORT in
        neighbor V4 route-map EXPORT out
        {{- range $item := .bgpPorts }}
        {{- $neighborIPList := split "/" $item.Local.Ipv4 }}
        neighbor {{ $neighborIPList._0 }} activate
        neighbor {{ $neighborIPList._0 }} route-map DEFAULT_ONLY out
        neighbor {{ $neighborIPList._0 }} route-map ACCEPT_PUBLIC in
        {{- end }}
      exit-address-family
      !
      route-map EXPORT deny 100
      !
      route-map EXPORT permit 1
      match ip address prefix-list EXPORT
      !
      route-map IMPORT deny 1
      !
      route-map DEFAULT_ONLY permit 10
      match ip address prefix-list DEFAULT_ROUTE
      !      !
      route-map ACCEPT_PUBLIC permit 10
      match ip address prefix-list ACCEPT_PUBLIC
      !
      {{- range $item := .bgpSubnetsToAdvertise }}
      ip prefix-list EXPORT permit {{ $item }}
      {{- end }}
      !
      ip prefix-list DEFAULT_ROUTE seq 5 permit 0.0.0.0/0
      !
      ip prefix-list ACCEPT_PUBLIC seq 10 deny 0.0.0.0/8 le 32
      ip prefix-list ACCEPT_PUBLIC seq 20 deny 10.0.0.0/8 le 32
      ip prefix-list ACCEPT_PUBLIC seq 30 deny 100.64.0.0/10 le 32
      ip prefix-list ACCEPT_PUBLIC seq 40 deny 127.0.0.0/8 le 32
      ip prefix-list ACCEPT_PUBLIC seq 50 deny 169.254.0.0/16 le 32
      ip prefix-list ACCEPT_PUBLIC seq 60 deny 172.16.0.0/12 le 32
      ip prefix-list ACCEPT_PUBLIC seq 70 deny 192.168.0.0/16 le 32
      ip prefix-list ACCEPT_PUBLIC seq 80 deny 224.0.0.0/4 le 32
      ip prefix-list ACCEPT_PUBLIC seq 90 deny 240.0.0.0/4 le 32
      ip prefix-list ACCEPT_PUBLIC seq 100 deny 255.255.255.255/32 le 32
      ip prefix-list ACCEPT_PUBLIC seq 1000 permit 0.0.0.0/0 le 32

      line vty
      !
# written to /var/log/cloud-init-output.log
final_message: "The system is finally up, after $UPTIME seconds"
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	}
}

func prepareCloudInitISPInternal(diskArgs map[string]interface{}, forNetwork bool) string {
	if forNetwork {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
version: 2
ethernets:
  ens4:
    dhcp4: false
    dhcp6: false
    mtu: {{ .mtu }}
    addresses:
      - 192.168.122.15/24
  {{- if .bgpLinkIp1 }}
  ens5:
    dhcp4: false
    dhcp6: false
    addresses:
      - {{ .bgpLinkIp1 }}
      - {{ .bgpLinkIp2 }}
    {{- if .bgpLinkRemoteIp1 }}
    routes:
      - to: default
        via: {{ .bgpLinkRemoteIp1 }}
    {{- end }}
    nameservers:
      addresses:
        - 1.1.1.1
        - 8.8.8.8
  {{- end }}
  {{- $interfaces := dict -}}
  {{- range $item := .bgpPorts -}}
    {{- $portNameList := split "@" $item.Remote.Name -}}
    {{- $ifName := $portNameList._0 -}}
    {{- $addr := $item.Remote.Ipv4 -}}
    {{- $addrs := get $interfaces $ifName | default list -}}
    {{- $addrs := append $addrs $addr -}}
    {{- $_ := set $interfaces $ifName $addrs -}}
  {{- end -}}
  {{- range $ifName, $addrs := $interfaces }}
  {{ $ifName }}:
    dhcp4: false
    dhcp6: false
    addresses:
      {{- range $addr := $addrs }}
      - {{ $addr }}
      {{- end }}
  {{- end }}
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	} else {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(sprig.TxtFuncMap()).Parse(
			`
{{- $dot := . }}
#cloud-config
hostname: {{ .hostname }}
manage_etc_hosts: true
users:
  - name: root
    lock_passwd: false
    hashed_passwd: $6$SX2xaqo0V5Z3duvX$1UXpCr.x.XV7PJkARgqoh9r6LlHXofX99IcX9.NCnfTedHoBVe1CBwsRgcCbvKqkzwo7tiKe2k4Z75uRxnafE/
    ssh_authorized_keys:
    {{- range $item := .sshAuthKey }}
      - {{ $item }}
    {{- end }}
# only cert auth via ssh (console access can still login)
ssh_pwauth: false
disable_root: false
packages:
  - qemu-guest-agent
{{- range $item := .installedPackages }}
  - {{ $item }}
{{- end }}
# every boot
bootcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/bootcmd.log' ]
# run once for setup
runcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/runcmd.log' ]
  - |
    #!/bin/bash

    sed -i '/bgpd=no/c\bgpd=yes' /etc/frr/daemons
    # Wait for the file to exist just in case
    while [ ! -f /tmp/frr.conf ]; do sleep 1; done
    cp /tmp/frr.conf /etc/frr/frr.conf
    systemctl restart frr

    # Disable unattended upgrades and needrestart after setup
    systemctl stop unattended-upgrades || true
    systemctl disable unattended-upgrades || true
    apt-get purge unattended-upgrades needrestart -y || true

write_files:
  - path: /tmp/frr.conf
    permissions: '0644'
    content: |
      frr defaults traditional
      log syslog informational
      ip forwarding
      ipv6 forwarding
      service integrated-vtysh-config

      !
      router bgp 65401
      bgp ebgp-requires-policy
      neighbor V4 peer-group
      neighbor V4 remote-as 65007
      neighbor V4 ebgp-multihop 7
      {{- if .bgpPassword }}
      neighbor V4 password {{ .bgpPassword }}
      {{- end }}
      neighbor {{ .bgpLinkRemoteIp1 }} peer-group V4
      neighbor {{ .bgpLinkRemoteIp2 }} peer-group V4
      {{- range $item := .bgpPorts }}
      {{- $portNameList := split "@" $item.Local.Name }}
      {{- $neighborIPList := split "/" $item.Local.Ipv4 }}
      neighbor {{ $neighborIPList._0 }} remote-as {{ $dot.netrisASN }}
      neighbor {{ $neighborIPList._0 }} description Netris-{{ $portNameList._1 }}
      neighbor {{ $neighborIPList._0 }} soft-reconfiguration inbound
      neighbor {{ $neighborIPList._0 }} default-originate
      {{- end }}
      !
      address-family ipv4 unicast
        {{- range $item := .bgpSubnetsToAdvertise }}
        network {{ $item }}
        aggregate-address {{ $item }}
        {{- end }}
        neighbor V4 route-map IMPORT in
        neighbor V4 route-map EXPORT out
        {{- range $item := .bgpPorts }}
        {{- $neighborIPList := split "/" $item.Local.Ipv4 }}
        neighbor {{ $neighborIPList._0 }} activate
        neighbor {{ $neighborIPList._0 }} route-map DEFAULT_ONLY out
        neighbor {{ $neighborIPList._0 }} route-map ACCEPT_PUBLIC in
        {{- end }}
      exit-address-family
      !
      route-map EXPORT deny 100
      !
      route-map EXPORT permit 1
      match ip address prefix-list EXPORT
      !
      route-map IMPORT deny 1
      !
      route-map DEFAULT_ONLY permit 10
      match ip address prefix-list DEFAULT_ROUTE
      !      !
      route-map ACCEPT_PUBLIC permit 10
      match ip address prefix-list ACCEPT_PUBLIC
      !
      {{- range $item := .bgpSubnetsToAdvertise }}
      ip prefix-list EXPORT permit {{ $item }}
      {{- end }}
      !
      ip prefix-list DEFAULT_ROUTE seq 5 permit 0.0.0.0/0
      !
      ip prefix-list ACCEPT_PUBLIC seq 10 deny 0.0.0.0/8 le 32
      ip prefix-list ACCEPT_PUBLIC seq 20 deny 10.0.0.0/8 le 32
      ip prefix-list ACCEPT_PUBLIC seq 30 deny 100.64.0.0/10 le 32
      ip prefix-list ACCEPT_PUBLIC seq 40 deny 127.0.0.0/8 le 32
      ip prefix-list ACCEPT_PUBLIC seq 50 deny 169.254.0.0/16 le 32
      ip prefix-list ACCEPT_PUBLIC seq 60 deny 172.16.0.0/12 le 32
      ip prefix-list ACCEPT_PUBLIC seq 70 deny 192.168.0.0/16 le 32
      ip prefix-list ACCEPT_PUBLIC seq 80 deny 224.0.0.0/4 le 32
      ip prefix-list ACCEPT_PUBLIC seq 90 deny 240.0.0.0/4 le 32
      ip prefix-list ACCEPT_PUBLIC seq 100 deny 255.255.255.255/32 le 32
      ip prefix-list ACCEPT_PUBLIC seq 1000 permit 0.0.0.0/0 le 32

      line vty
      !
# written to /var/log/cloud-init-output.log
final_message: "The system is finally up, after $UPTIME seconds"
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	}
}

func prepareCloudInitForMgmt(diskArgs map[string]interface{}, forNetwork bool) string {
	// Define custom FuncMap (merged with Sprig for both branches)
	funcMap := sprig.TxtFuncMap()
	funcMap["subnetMask"] = func(cidr string) string {
		mask, err := subnetMask(cidr)
		if err != nil {
			return fmt.Sprintf("Error: %v", err)
		}
		return mask
	}
	funcMap["isIPInSubnet"] = isIPInSubnet

	if forNetwork {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(funcMap).Parse(
			`
version: 2
ethernets:
  ens3:
    dhcp4: false
    dhcp6: false
    mtu: 1500
    addresses:
      - 192.168.122.10/24
    routes:
      - to: default
        via: 192.168.122.1
    nameservers:
      addresses:
        - 8.8.8.8
        - 8.8.4.4
  ens4:
    dhcp4: false
    dhcp6: false
    mtu: {{ .mtu }}
    addresses:
    {{- range $item := .ips }}
      - {{ $item.DefaultGateway }}/{{ $item.Subnet.Length }}
    {{- end }}

`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	} else {
		tmpl, err := template.New("cloudInitVolumesUserData").Funcs(funcMap).Parse(
			`
{{- $dot := . }}
#cloud-config
hostname: {{ .hostname }}
fqdn: {{ .hostname }}.localhost
manage_etc_hosts: true
users:
  - name: root
    lock_passwd: false
    hashed_passwd: $6$SX2xaqo0V5Z3duvX$1UXpCr.x.XV7PJkARgqoh9r6LlHXofX99IcX9.NCnfTedHoBVe1CBwsRgcCbvKqkzwo7tiKe2k4Z75uRxnafE/
    ssh_authorized_keys:
    {{- range $item := .sshAuthKey }}
      - {{ $item }}
    {{- end }}
# only cert auth via ssh (console access can still login)
ssh_pwauth: false
disable_root: false
packages:
  - qemu-guest-agent
{{- range $item := .installedPackages }}
  - {{ $item }}
{{- end }}
# every boot
bootcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/bootcmd.log' ]
# run once for setup
runcmd:
  - [ sh, -c, 'echo $(date) | sudo tee -a /root/runcmd.log' ]
  - [ sh, -c, 'cp /tmp/dhcpd.conf /etc/dhcp/dhcpd.conf' ]
  - [ sh, -c, 'systemctl restart isc-dhcp-server.service' ]
  - [ sh, -c, 'cp /root/.ssh/authorized_keys /var/www/html/authorized_keys' ]
  - [ sh, -c, 'cp /tmp/cumulus-ztp /var/www/html/cumulus-ztp' ]
  - [ sh, -c, 'chmod 644 /var/www/html/authorized_keys' ]
  - [ sh, -c, 'cp /tmp/rules.v4 /etc/iptables/rules.v4' ]
  - [ sh, -c, 'echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf' ]
  - [ sh, -c, 'sysctl -p' ]
  - [ sh, -c, 'iptables-restore < /etc/iptables/rules.v4' ]
  - [ sh, -c, 'curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/ca.crt -o /etc/openvpn/ca.crt' ]
  - [ sh, -c, 'curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/dh.pem -o /etc/openvpn/dh.pem' ]
  - [ sh, -c, 'curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/myservername.crt -o /etc/openvpn/myservername.crt' ]
  - [ sh, -c, 'curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/myservername.key -o /etc/openvpn/myservername.key' ]
  - [ sh, -c, 'curl -sS https://raw.githubusercontent.com/rawfilescloud/ovpn-config-examples/main/ta.key -o /etc/openvpn/ta.key' ]
  - [ sh, -c, 'cp /tmp/ovpn-server.conf /etc/openvpn/server.conf' ]
  - [ sh, -c, 'mkdir /etc/openvpn/ccd' ]
  - [ sh, -c, 'echo "ifconfig-push 10.8.0.2 255.255.255.0" > /etc/openvpn/ccd/myclient1' ]
  - [ sh, -c, 'systemctl start openvpn@server' ]
  - |
    #!/bin/bash

    # Disable unattended upgrades and needrestart after setup
    systemctl stop unattended-upgrades || true
    systemctl disable unattended-upgrades || true
    apt-get purge unattended-upgrades needrestart -y || true

write_files:
  - path: /tmp/ovpn-server.conf
    content: |
      port 1194
      proto tcp
      proto tcp6
      dev tun

      ca ca.crt
      cert myservername.crt
      key myservername.key
      dh dh.pem
      server 10.8.0.0 255.255.255.0
      ifconfig-pool-persist /var/log/openvpn/ipp.txt

      {{- range $item := .ips }}
      push "route {{ $item.Subnet.Prefix }} {{ subnetMask $item.Prefix }}"
      {{- end }}
      push "route 192.168.122.0 255.255.255.0"

      client-config-dir ccd
      keepalive 10 120
      tls-auth ta.key 0 # This file is secret
      cipher AES-256-CBC
      persist-key
      persist-tun
      status /var/log/openvpn/openvpn-status.log
      verb 3
      explicit-exit-notify 0
  - path: /tmp/rules.v4
    content: |
      # Generated by iptables-save v1.8.7 on Fri Mar 17 17:29:35 2023
      *nat
      :PREROUTING ACCEPT [0:0]
      :INPUT ACCEPT [0:0]
      :OUTPUT ACCEPT [0:0]
      :POSTROUTING ACCEPT [0:0]
      -A POSTROUTING -o ens3 -j MASQUERADE
      COMMIT
      # Completed on Fri Mar 17 17:29:35 2023
  - path: /tmp/cumulus-ztp
    content: |
      #!/bin/bash
      # Created by Topology-Converter v4.7.1
      #    Template Revision: v4.7.1

      function error() {
        echo -e "e[0;33mERROR: The Zero Touch Provisioning script failed while running the command $BASH_COMMAND at line $BASH_LINENO.e[0m" >&2
      }
      trap error ERR

      {{- if gt (len .ips) 0 }}
        {{- with index .ips 0 }}
      SSH_URL="http://{{ .DefaultGateway }}/authorized_keys"
        {{- end }}
      {{- end }}

      # Uncomment to setup SSH key authentication for Ansible
      mkdir -p /home/cumulus/.ssh
      wget -q -O /home/cumulus/.ssh/authorized_keys $SSH_URL

      # Uncomment to unexpire and change the default cumulus user password
      passwd -x 99999 cumulus
      echo 'cumulus:newNet0ps!' | chpasswd

      # Uncomment to make user cumulus passwordless sudo
      echo "cumulus ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/10_cumulus


      tee /tmp/netris-devices <<EOF
      {{- range $hyper := $dot.allVms }}
        {{- range $eachvm := $hyper }}
          {{- if eq $eachvm.Type "switch" }}
      {{ $eachvm.Name }} {{ $eachvm.MainAddress }} {{ $eachvm.Nos.Tag }}
          {{- end }}
        {{- end }}
      {{- end }}
      EOF

      tee /tmp/nicnames <<EOF
      {{- range $servername, $server := $dot.links }}
        {{- range $serverport := $server }}
      {{ $servername }} {{ $serverport.Local }}
        {{- end }}
      {{- end }}
      EOF

      sudo sed -i '/SETHOSTNAME="no"/c\SETHOSTNAME="yes"' /etc/dhcp/dhclient-exit-hooks.d/dhcp-sethostname
      sudo dhclient -r && sudo dhclient

      echo '50.117.59.233 get.netris.io' >> /etc/hosts

      # Get the hostname of the switch
      hostname=$(hostname)
      # File containing the NIC names and corresponding devices
      nicnames_file="/tmp/nicnames"
      # Output file for udev rules
      udev_rules_file="/etc/udev/rules.d/70-persistent-net.rules"
      # Find physical interfaces excluding eth0 and non-physical interfaces like mgmt
      interfaces=$(ip -o link show | sort -n | awk -F': ' '{print $2}' | grep -v 'lo' | grep -v 'eth0' | grep -v 'mgmt')
      # Initialize udev rules file
      echo "# This file was generated by a script" > $udev_rules_file
      # Get the NIC names from the nicnames_file corresponding to the current switch
      nicnames=$(grep "^${hostname} " $nicnames_file | awk '{print $2}')
      # Convert interfaces and nicnames to arrays
      interfaces_array=($interfaces)
      nicnames_array=($nicnames)
      echo "# Interfaces Array: ${interfaces_array[@]}" >> $udev_rules_file
      echo "# NIC Names Array: ${nicnames_array[@]}" >> $udev_rules_file
      # Ensure the number of interfaces matches the number of names
      if [ ${#interfaces_array[@]} -ne ${#nicnames_array[@]} ]; then
        echo "Error: The number of detected interfaces does not match the number of names in the nicnames file."
        exit 1
      fi
      # Loop through the interfaces and assign names based on the nicnames file
      for i in "${!interfaces_array[@]}"; do
        iface="${interfaces_array[$i]}"
        nicname="${nicnames_array[$i]}"
        # Extract the MAC address of the interface
        mac=$(cat /sys/class/net/${iface}/address)
        # Add the udev rule
        echo "SUBSYSTEM==\"net\", ACTION==\"add\", ATTR{address}==\"${mac}\", NAME=\"${nicname}\", SUBSYSTEMS==\"pci\"" >> $udev_rules_file
      done
      echo "70-persistent-net.rules has been generated at $udev_rules_file"

      sudo update-initramfs -u


      MAINIP=$(grep -w "^$(hostname)" /tmp/netris-devices | awk '{print $2}')

      if [ -z "$MAINIP" ]; then
          exit 0
      fi

      NOS=$(grep -w "^$(hostname)" /tmp/netris-devices | awk {'print $3'})
      HOSTNAME=$(hostname)
      echo hostname: $HOSTNAME
      AUTHKEY={{ $dot.ctlInfo.AuthKey }}
      VERSION={{ $dot.ctlInfo.Version }}
      APT_REPO={{ $dot.ctlInfo.AptRepo }}
      VERSION_OLD=4.1.1-016

      # Check if NOS is cumulus_nvue
      if [ "$NOS" == "cumulus_nvue" ]; then
          curl -fksSL https://get.netris.io | sh -s -- --lo $MAINIP --controller 10.8.0.2 --ctl-version $VERSION --hostname $HOSTNAME --auth $AUTHKEY --hw-nos cumulus_nvue --apt-repo $APT_REPO --debug
      else
          curl -fksSL https://get.netris.io | sh -s -- --lo $MAINIP --controller 10.8.0.2 --ctl-version $VERSION_OLD --hostname $HOSTNAME --auth $AUTHKEY --apt-repo $APT_REPO --debug
      fi

      reboot

      exit 0
      #CUMULUS-AUTOPROVISIONING
  - path: /tmp/dhcpd.conf
    content: |
      ddns-update-style none;
      authoritative;
      log-facility local7;

      default-lease-time 172800;  #2 days
      max-lease-time 345600;      #4 days
      option domain-name-servers 8.8.8.8, 8.8.4.4;
      option domain-name "sim.netris.local";
      option www-server code 72 = ip-address;
      option cumulus-provision-url code 239 = text;

      option space onie code width 1 length width 1;
      # Define the code names and data types within the ONIE namespace
      option onie.installer_url code 1 = text;
      option onie.updater_url   code 2 = text;
      option onie.machine       code 3 = text;
      option onie.arch          code 4 = text;
      option onie.machine_rev   code 5 = text;
      # Package the ONIE namespace into option 125
      option space vivso code width 4 length width 1;
      option vivso.onie code 42623 = encapsulate onie;
      option vivso.iana code 0 = string;
      option op125 code 125 = encapsulate vivso;
      class "onie-vendor-classes" {
        # Limit the matching to a request we know originated from ONIE
        match if substring(option vendor-class-identifier, 0, 11) = "onie_vendor";
        # Required to use VIVSO
        option vivso.iana 01:01:01;

      }
      shared-network LOCAL-NET{
      {{- range $item := .ips }}
      subnet {{ $item.Subnet.Prefix }} netmask {{ subnetMask $item.Prefix }} {
        option www-server {{ $item.DefaultGateway }};
        option default-url = "http://{{ $item.DefaultGateway }}/onie-installer";
        option cumulus-provision-url "http://{{ $item.DefaultGateway }}/cumulus-ztp";
        option routers {{ $item.DefaultGateway }};
      {{- range $hyper := $dot.allVms }}
        {{- range $eachvm := $hyper }}
          {{- if ne $eachvm.Type "mgmt-server" }}
            {{- if (isIPInSubnet $item.Prefix $eachvm.MgmtAddress) }}
        host {{ $eachvm.Name }} {hardware ethernet {{ $eachvm.MacAddress }}; fixed-address {{ $eachvm.MgmtAddress }}; option host-name "{{ $eachvm.Name }}"; option cumulus-provision-url "http://{{ $item.DefaultGateway }}/cumulus-ztp";  }
            {{- end }}
          {{- end }}
         {{- end }}
      {{- end }}
      }
      {{- end }}
      }

    owner: 'root:root'
    permissions: '0644'
# written to /var/log/cloud-init-output.log
final_message: "The system is finally up, after $UPTIME seconds"
`)
		if err != nil {
			panic(err)
		}
		var tmplOut bytes.Buffer
		err = tmpl.Execute(&tmplOut, diskArgs)
		if err != nil {
			panic(err)
		}
		return tmplOut.String()
	}
}

func subnetMask(cidr string) (string, error) {
	_, ipNet, err := net.ParseCIDR(cidr)
	if err != nil {
		return "", err
	}
	mask := ipNet.Mask
	return fmt.Sprintf("%d.%d.%d.%d", mask[0], mask[1], mask[2], mask[3]), nil
}

// Function to check if the IP falls within the subnet
func isIPInSubnet(subnetStr string, ipStr string) bool {
	_, ipNet, err := net.ParseCIDR(subnetStr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(ipStr)
	return ipNet.Contains(ip)
}

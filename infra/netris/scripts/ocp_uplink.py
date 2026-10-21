#!/usr/bin/env python3
"""Resolve the SNO's fabric port and reconcile its Netris VNet.

The two commands intentionally share only read-only discovery and API helpers.
``resolve`` never changes the lab; ``reconcile`` only changes the OCP VNet's
port list after Ansible has created the VNet if necessary.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


class UplinkError(Exception):
    """A discovery or reconciliation failure safe to show in Ansible output."""


MAC_PATTERN = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
IFACE_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]+$")
ALIAS_PATTERN = re.compile(r"^alias\s+(\S+)=(['\"])(.*)\2$")


def mac_address(value: str) -> str:
    value = value.strip().lower()
    if not MAC_PATTERN.fullmatch(value):
        raise UplinkError(f"Invalid network adapter MAC address: {value!r}")
    return value


def run(argv: list[str], *, timeout: int = 30) -> str:
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UplinkError(f"Could not run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise UplinkError(f"{argv[0]} failed: {detail or 'no output'}")
    return result.stdout


def ssh(host: str, user: str, key: str, command: str) -> str:
    return run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=5",
            "-i", key,
            f"{user}@{host}",
            command,
        ],
        timeout=12,
    )


def aliases(path: Path) -> dict[str, str]:
    """Read Cloudsim's generated SSH aliases as data, never by sourcing them."""
    if not path.is_file():
        return {}
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ALIAS_PATTERN.fullmatch(line.strip())
        if not match:
            continue
        try:
            destination = shlex.split(match.group(3))[-1]
            address = destination.rsplit("@", 1)[1]
            found[match.group(1)] = str(ipaddress.ip_address(address))
        except (IndexError, ValueError):
            continue
    return found


def api_request(base_url: str, path: str, *, method: str = "GET", body: dict | None = None) -> object:
    cookie = os.environ.get("NETRIS_SESSION_COOKIE", "")
    if not cookie:
        raise UplinkError("Netris session cookie is unavailable")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Cookie": f"connect.sid={cookie}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    context = ssl._create_unverified_context() if os.environ.get("NETRIS_VALIDATE_CERTS") == "false" else None
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            payload = response.read()
            result = json.loads(payload) if payload else {}
    except (OSError, ValueError) as exc:
        raise UplinkError(f"Netris {method} {path} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise UplinkError(f"Netris {method} {path} returned an unexpected response")
    if result.get("isSuccess") is False:
        raise UplinkError(f"Netris {method} {path} was rejected: {result.get('message', 'unknown error')}")
    if method == "GET" and "data" not in result:
        raise UplinkError(f"Netris {method} {path} returned no data field")
    return result.get("data")


def api_list(base_url: str, path: str) -> list[dict]:
    data = api_request(base_url, path)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise UplinkError(f"Netris {path} did not return a list")
    return data


def vm_names(pattern: str) -> tuple[list[str], str]:
    try:
        name_pattern = re.compile(pattern)
    except re.error as exc:
        raise UplinkError(f"Invalid ocp_vm_name_pattern: {exc}") from exc
    names = [name for name in run(["virsh", "list", "--all", "--name"]).splitlines() if name]
    matches = [name for name in names if name_pattern.search(name)]
    if len(matches) != 1:
        raise UplinkError(f"Expected one SNO VM matching {pattern!r}; found {matches}")
    return names, matches[0]


def udp_interfaces(vm: str) -> list[dict]:
    try:
        root = ET.fromstring(run(["virsh", "dumpxml", "--inactive", vm]))
    except ET.ParseError as exc:
        raise UplinkError(f"Invalid libvirt XML for {vm}: {exc}") from exc
    interfaces = []
    for interface in root.findall("./devices/interface[@type='udp']"):
        mac = interface.find("mac")
        source = interface.find("source")
        local = source.find("local") if source is not None else None
        if mac is None or source is None or local is None:
            raise UplinkError(f"UDP adapter in {vm} lacks MAC or tunnel endpoint")
        remote_endpoint = (source.get("address"), source.get("port"))
        local_endpoint = (local.get("address"), local.get("port"))
        if not all(remote_endpoint + local_endpoint):
            raise UplinkError(f"UDP adapter in {vm} has an incomplete tunnel endpoint")
        interfaces.append(
            {
                "vm": vm,
                "mac": mac_address(mac.get("address", "")),
                "local": local_endpoint,
                "remote": remote_endpoint,
            }
        )
    return interfaces


def leaf_mac(host: str, key: str, interface: str) -> str:
    if not IFACE_PATTERN.fullmatch(interface):
        raise UplinkError(f"Invalid leaf interface name: {interface!r}")
    return mac_address(ssh(host, "cumulus", key, f"cat /sys/class/net/{interface}/address"))


def north_south_links(links: list[dict], vm: str) -> dict[str, dict]:
    """Return Netris server-port links to leaf VMs, including underlay state."""
    found: dict[str, list[dict]] = {}
    for link in links:
        local, remote = link.get("local"), link.get("remote")
        if not isinstance(local, dict) or not isinstance(remote, dict):
            continue
        endpoints = (local.get("name"), remote.get("name"))
        for server_port, leaf_endpoint in (endpoints, endpoints[::-1]):
            if not isinstance(server_port, str) or not isinstance(leaf_endpoint, str):
                continue
            match = re.fullmatch(rf"eth\d+@{re.escape(vm)}", server_port)
            leaf_match = re.fullmatch(r"([^@]+)@(ns-leaf-[^@]+)", leaf_endpoint)
            if not (match and leaf_match):
                continue
            underlay = link.get("underlay")
            if underlay not in {"enabled", "disabled"}:
                raise UplinkError(f"Netris North-South link {server_port} has invalid underlay state {underlay!r}")
            found.setdefault(server_port, []).append(
                {
                    "leaf_endpoint": leaf_endpoint,
                    "leaf_vm": leaf_match.group(2),
                    "leaf_interface": leaf_match.group(1),
                    "underlay": underlay,
                }
            )
    if not found:
        raise UplinkError(f"Netris has no North-South links to an ethN port on SNO {vm}")
    for port, peers in found.items():
        if len(peers) != 1:
            raise UplinkError(f"Netris port {port} has {len(peers)} North-South links; expected one")
    return {port: peers[0] for port, peers in found.items()}

def verified_uplinks(args: argparse.Namespace, names: list[str], vm: str, known_ips: dict[str, str]) -> dict[str, dict]:
    devices = api_list(args.controller_url, "/api/v2/hw")
    links = north_south_links(api_list(args.controller_url, "/api/v2/link"), vm)
    sno_adapters = udp_interfaces(vm)
    leaf_adapters: dict[str, list[dict]] = {}
    leaf_ips: dict[str, str] = {}
    verified = {}
    for server_port, endpoint in links.items():
        leaf_endpoint = endpoint["leaf_endpoint"]
        leaf_vm = endpoint["leaf_vm"]
        interface = endpoint["leaf_interface"]
        if leaf_vm not in names:
            raise UplinkError(f"Netris leaf {leaf_vm} on {server_port} is missing from libvirt")
        if leaf_vm not in leaf_ips:
            inventory = [item for item in devices if item.get("name") == leaf_vm]
            if len(inventory) != 1:
                raise UplinkError(f"Netris inventory does not uniquely identify leaf VM {leaf_vm}")
            address = inventory[0].get("mgmtAddress") or known_ips.get(leaf_vm)
            if not address:
                raise UplinkError(f"Management IP for leaf VM {leaf_vm} is missing")
            try:
                leaf_ips[leaf_vm] = str(ipaddress.ip_address(address.split("/", 1)[0]))
            except ValueError as exc:
                raise UplinkError(f"Invalid management IP for leaf VM {leaf_vm}: {address!r}") from exc
            leaf_adapters[leaf_vm] = udp_interfaces(leaf_vm)
        mac = leaf_mac(leaf_ips[leaf_vm], args.host_key, interface)
        peers = [
            (sno, leaf)
            for sno in sno_adapters
            for leaf in leaf_adapters[leaf_vm]
            if leaf["mac"] == mac and sno["local"] == leaf["remote"] and sno["remote"] == leaf["local"]
        ]
        if len(peers) != 1:
            raise UplinkError(
                f"Netris port {server_port} via {leaf_endpoint} MAC {mac} has {len(peers)} "
                "matching reverse libvirt tunnels; expected one unambiguous SNO MAC"
            )
        verified[server_port] = {
            "guest_mac": peers[0][0]["mac"],
            "leaf_port": leaf_endpoint,
            "underlay": endpoint["underlay"],
        }
    mac_to_ports: dict[str, list[str]] = {}
    for port, uplink in verified.items():
        mac_to_ports.setdefault(uplink["guest_mac"], []).append(port)
    for mac, ports in mac_to_ports.items():
        if len(ports) > 1:
            raise UplinkError(f"SNO MAC {mac} matches multiple North-South ports: {ports}")
    return verified

def resolve(args: argparse.Namespace) -> dict:
    names, vm = vm_names(args.vm_name_pattern)
    known_ips = aliases(Path(args.aliases_path))
    management_ip = args.vm_mgmt_ip or known_ips.get(vm)
    if not management_ip:
        raise UplinkError(f"Management IP for {vm} is missing; set ocp_vm_mgmt_ip or restore Cloudsim aliases")
    try:
        management_ip = str(ipaddress.ip_address(management_ip))
    except ValueError as exc:
        raise UplinkError(f"Invalid management IP for {vm}: {management_ip!r}") from exc

    uplinks = verified_uplinks(args, names, vm, known_ips)
    data_uplinks = {port: item for port, item in uplinks.items() if item["underlay"] == "disabled"}
    if not data_uplinks:
        raise UplinkError(f"Netris has no underlay-disabled North-South data link to SNO {vm}")

    underlay_port = ""
    underlay_uplink = None
    if args.underlay_server_port:
        if not re.fullmatch(r"eth\d+", args.underlay_server_port):
            raise UplinkError(f"Invalid Netris underlay server port: {args.underlay_server_port!r}")
        underlay_port = f"{args.underlay_server_port}@{vm}"
        underlay_uplink = uplinks.get(underlay_port)
        if underlay_uplink is None:
            raise UplinkError(f"Configured Netris underlay port {underlay_port} has no verified leaf/tunnel mapping")
        if underlay_uplink["underlay"] != "enabled":
            raise UplinkError(f"Configured Netris underlay port {underlay_port} is not enabled for underlay")

    vnets = api_list(args.controller_url, "/api/v2/vnet")
    matches = [item for item in vnets if item.get("name") == args.vnet_name]
    if len(matches) > 1:
        raise UplinkError(f"Netris has multiple VNets named {args.vnet_name}")
    current_ports = []
    if matches:
        current_vnet = vnet_detail(args.controller_url, int(matches[0]["id"]))
        if current_vnet.get("name") != args.vnet_name:
            raise UplinkError(f"VNet {args.vnet_name} detail has an unexpected name")
        current_ports = current_vnet.get("ports") or []
        if len(current_ports) > 1:
            raise UplinkError(f"VNet {args.vnet_name} has {len(current_ports)} ports; refusing to replace attachments")
        if current_ports:
            current = full_port_name(current_ports[0])
            if current not in data_uplinks:
                if current in uplinks and uplinks[current]["underlay"] == "enabled":
                    raise UplinkError(f"OCP VNet {args.vnet_name} is attached to underlay port {current}")
                raise UplinkError(f"VNet {args.vnet_name} contains unrelated or unverified port {current}")

    occupied = {}
    for other in vnets:
        if matches and int(other["id"]) == int(matches[0]["id"]):
            continue
        for port in vnet_detail(args.controller_url, int(other["id"])).get("ports") or []:
            occupied[full_port_name(port)] = other.get("name")
    if current_ports:
        server_port = full_port_name(current_ports[0])
        if server_port in occupied:
            raise UplinkError(f"Port {server_port} is also attached to VNet {occupied[server_port]}")
    else:
        available = [port for port in data_uplinks if port not in occupied]
        if not available:
            raise UplinkError(f"All verified data ports on {vm} are attached to other VNets: {occupied}")
        server_port = min(available, key=lambda port: (int(port.split("@", 1)[0][3:]), port))

    chosen = data_uplinks[server_port]
    result = {
        "vm_name": vm,
        "management_ip": management_ip,
        "guest_mac": chosen["guest_mac"],
        "leaf_port": chosen["leaf_port"],
        "server_port": server_port,
    }
    if underlay_uplink is not None:
        result.update(
            {
                "underlay_guest_mac": underlay_uplink["guest_mac"],
                "underlay_leaf_port": underlay_uplink["leaf_port"],
                "underlay_server_port": underlay_port,
            }
        )
    return result

def full_port_name(port: dict) -> str:
    name = port.get("name")
    switch = port.get("switch")
    switch_name = port.get("switchName") or (switch.get("name") if isinstance(switch, dict) else None)
    short_name = port.get("port")
    if isinstance(short_name, str) and isinstance(switch_name, str):
        short_name = re.sub(r"\([^)]*\)$", "", short_name)
        if short_name and switch_name:
            return f"{short_name}@{switch_name}"

    if isinstance(name, str):
        match = re.fullmatch(r"([^()\s@]+)(?:\([^)]*\))?@([^\s()]+)(?:\s+\([^)]*\))?", name.strip())
        if match:
            return f"{match.group(1)}@{match.group(2)}"
    raise UplinkError(f"Unrecognized Netris VNet port record: {port}")


def vnet_detail(base_url: str, vnet_id: int) -> dict:
    detail = api_request(base_url, f"/api/v2/vnet/{vnet_id}")
    if not isinstance(detail, dict):
        raise UplinkError(f"Netris VNet {vnet_id} detail was not an object")
    return detail


def check_vnet_identity(detail: dict, name: str, vpc_id: int) -> None:
    if detail.get("name") != name or int((detail.get("vpc") or {}).get("id", -1)) != vpc_id:
        raise UplinkError(f"VNet {name} has an unexpected name or VPC; refusing to change it")


def server_port_underlay(links: list[dict], server_port: str) -> str:
    statuses = []
    for link in links:
        local, remote = link.get("local"), link.get("remote")
        if not isinstance(local, dict) or not isinstance(remote, dict):
            continue
        if server_port in (local.get("name"), remote.get("name")):
            statuses.append(link.get("underlay"))
    if len(statuses) != 1 or statuses[0] not in {"enabled", "disabled"}:
        raise UplinkError(f"Netris port {server_port} does not have one link with a known underlay state")
    return statuses[0]


def reconcile(args: argparse.Namespace) -> dict:
    status = server_port_underlay(api_list(args.controller_url, "/api/v2/link"), args.server_port)
    if status == "enabled":
        raise UplinkError(f"Refusing to attach underlay-enabled Netris port {args.server_port} to the OCP data VNet")

    vnets = api_list(args.controller_url, "/api/v2/vnet")
    matches = [item for item in vnets if item.get("name") == args.vnet_name]
    if len(matches) != 1:
        raise UplinkError(f"Expected one existing VNet named {args.vnet_name}; found {len(matches)}")
    vnet_id = int(matches[0]["id"])
    detail = vnet_detail(args.controller_url, vnet_id)
    check_vnet_identity(detail, args.vnet_name, args.vpc_id)

    for other in vnets:
        if int(other.get("id", -1)) == vnet_id:
            continue
        other_ports = vnet_detail(args.controller_url, int(other["id"])).get("ports") or []
        if any(full_port_name(port) == args.server_port for port in other_ports):
            raise UplinkError(f"Port {args.server_port} is already attached to VNet {other.get('name')}")

    ports = detail.get("ports") or []
    if len(ports) > 1:
        raise UplinkError(f"VNet {args.vnet_name} has {len(ports)} ports; refusing to remove unrelated attachments")
    if ports:
        current = full_port_name(ports[0])
        if current != args.server_port:
            raise UplinkError(
                f"VNet {args.vnet_name} changed to port {current} after resolution; refusing to replace it"
            )
    changed = not ports or full_port_name(ports[0]) != args.server_port or ports[0].get("accessMode") is not True
    if changed:
        api_request(
            args.controller_url,
            f"/api/v2/vnet/{vnet_id}",
            method="PUT",
            body={"ports": [{"name": args.server_port, "accessMode": True}]},
        )

    actual = vnet_detail(args.controller_url, vnet_id)
    check_vnet_identity(actual, args.vnet_name, args.vpc_id)
    actual_ports = actual.get("ports") or []
    if (
        len(actual_ports) != 1
        or full_port_name(actual_ports[0]) != args.server_port
        or actual_ports[0].get("accessMode") is not True
    ):
        raise UplinkError(f"VNet {args.vnet_name} did not retain port {args.server_port} in access mode")
    return {"changed": changed, "vnet_id": vnet_id, "server_port": args.server_port}

def guest_interfaces(args: argparse.Namespace) -> dict:
    requested = [mac_address(value) for value in args.mac]
    if len(requested) != len(set(requested)):
        raise UplinkError("The requested guest MAC addresses must be unique")

    command = (
        "for path in /sys/class/net/*; do "
        "[ -e ${path}/device ] || continue; "
        "printf '%s %s\\n' \"$(cat ${path}/address)\" \"${path##*/}\"; "
        "done"
    )
    output = ssh(args.vm_mgmt_ip, "core", args.host_key, command)
    observed: dict[str, list[str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        mac, interface = parts
        try:
            mac = mac_address(mac)
        except UplinkError:
            continue
        if not IFACE_PATTERN.fullmatch(interface):
            continue
        observed.setdefault(mac, []).append(interface)

    mapping = {}
    for mac in requested:
        matches = observed.get(mac, [])
        if len(matches) != 1:
            raise UplinkError(
                f"Expected one physical guest interface for MAC {mac}; found {matches or 'none'}"
            )
        mapping[mac] = matches[0]
    if len(set(mapping.values())) != len(mapping):
        raise UplinkError(f"Different Netris adapter MACs resolved to the same guest interface: {mapping}")

    address_output = ssh(args.vm_mgmt_ip, "core", args.host_key, "ip -o -4 addr show")
    management_interfaces = set()
    for line in address_output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        address = parts[3].split("/", 1)[0]
        if address == args.management_address:
            management_interfaces.add(parts[1].split("@", 1)[0])
    if len(management_interfaces) != 1:
        raise UplinkError(
            f"Expected one guest interface holding management address {args.management_address}; "
            f"found {sorted(management_interfaces)}"
        )
    management_interface = management_interfaces.pop()
    if not IFACE_PATTERN.fullmatch(management_interface):
        raise UplinkError(f"Invalid management interface name: {management_interface!r}")
    return {"interfaces": mapping, "management_interface": management_interface}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    resolver = subparsers.add_parser("resolve")
    resolver.add_argument("--controller-url", required=True)
    resolver.add_argument("--vm-name-pattern", required=True)
    resolver.add_argument("--vnet-name", required=True)
    resolver.add_argument("--host-key", required=True)
    resolver.add_argument("--aliases-path", default="/root/.cloudsim_aliases")
    resolver.add_argument("--vm-mgmt-ip", default="")
    resolver.add_argument("--underlay-server-port", default="")
    reconciler = subparsers.add_parser("reconcile")
    reconciler.add_argument("--controller-url", required=True)
    reconciler.add_argument("--vnet-name", required=True)
    reconciler.add_argument("--vpc-id", required=True, type=int)
    reconciler.add_argument("--server-port", required=True)
    interfaces = subparsers.add_parser("interfaces")
    interfaces.add_argument("--vm-mgmt-ip", required=True)
    interfaces.add_argument("--management-address", required=True)
    interfaces.add_argument("--host-key", required=True)
    interfaces.add_argument("--mac", action="append", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "resolve":
            result = resolve(args)
        elif args.operation == "reconcile":
            result = reconcile(args)
        else:
            result = guest_interfaces(args)
    except (UplinkError, KeyError, TypeError, ValueError) as exc:
        print(f"OCP uplink {args.operation} failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

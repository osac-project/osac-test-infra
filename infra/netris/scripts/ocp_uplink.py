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
    """Normalize and validate a hardware address from network metadata."""
    value = value.strip().lower()
    if not MAC_PATTERN.fullmatch(value):
        raise UplinkError("Invalid network adapter MAC address.")
    return value


def run(argv: list[str], *, timeout: int = 30) -> str:
    """Run a local command without a shell and return its standard output."""
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UplinkError("Could not run a required local command.") from exc
    if result.returncode != 0:
        raise UplinkError("A required local command failed.")
    return result.stdout


def ssh(host: str, user: str, key: str, command: str) -> str:
    """Run a remote command over SSH and return its standard output."""
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
    """Send an authenticated Netris request and return its data payload."""
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
        raise UplinkError("Netris API request failed.") from exc
    if not isinstance(result, dict):
        raise UplinkError("Netris API returned an unexpected response.")
    if result.get("isSuccess") is False:
        raise UplinkError("Netris API rejected the request.")
    if method == "GET" and "data" not in result:
        raise UplinkError("Netris API response is missing its data field.")
    return result.get("data")


def api_list(base_url: str, path: str) -> list[dict]:
    """Fetch a Netris collection and verify that it contains objects."""
    data = api_request(base_url, path)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise UplinkError("Netris API response is not a list of objects.")
    return data


def vm_names(pattern: str) -> tuple[list[str], str]:
    """Find the unique local VM matching the configured name pattern."""
    try:
        name_pattern = re.compile(pattern)
    except re.error as exc:
        raise UplinkError("The configured VM name pattern is invalid.") from exc
    names = [name for name in run(["virsh", "list", "--all", "--name"]).splitlines() if name]
    matches = [name for name in names if name_pattern.search(name)]
    if len(matches) != 1:
        raise UplinkError("Expected exactly one SNO VM matching the configured pattern.")
    return names, matches[0]


def udp_interfaces(vm: str) -> list[dict]:
    """Parse one VM's libvirt XML and return its UDP interfaces."""
    try:
        root = ET.fromstring(run(["virsh", "dumpxml", "--inactive", vm]))
    except ET.ParseError as exc:
        raise UplinkError("Could not parse the selected VM's libvirt XML.") from exc
    interfaces = []
    for interface in root.findall("./devices/interface[@type='udp']"):
        mac = interface.find("mac")
        source = interface.find("source")
        local = source.find("local") if source is not None else None
        if mac is None or source is None or local is None:
            raise UplinkError("A libvirt UDP adapter is missing required network metadata.")
        remote_endpoint = (source.get("address"), source.get("port"))
        local_endpoint = (local.get("address"), local.get("port"))
        if not all(remote_endpoint + local_endpoint):
            raise UplinkError("A libvirt UDP adapter has an incomplete tunnel endpoint.")
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
    """Read and validate a leaf interface's hardware address."""
    if not IFACE_PATTERN.fullmatch(interface):
        raise UplinkError("The discovered leaf interface name is invalid.")
    return mac_address(ssh(host, "cumulus", key, f"cat /sys/class/net/{interface}/address"))


def north_south_links(links: list[dict], vm: str) -> dict[str, tuple[str, str, str]]:
    """Map each SNO server port to its matching leaf endpoint and interface."""
    found: dict[str, list[tuple[str, str, str]]] = {}
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
            if match and leaf_match:
                found.setdefault(server_port, []).append((leaf_endpoint, leaf_match.group(2), leaf_match.group(1)))
    if not found:
        raise UplinkError("Netris has no North-South link to an SNO uplink.")
    for peers in found.values():
        if len(peers) != 1:
            raise UplinkError("A Netris uplink has multiple North-South links.")
    return {port: peers[0] for port, peers in found.items()}


def verified_uplinks(args: argparse.Namespace, names: list[str], vm: str, known_ips: dict[str, str]) -> dict[str, dict]:
    """Correlate Netris links and libvirt tunnels to verify uplink ports."""
    devices = api_list(args.controller_url, "/api/v2/hw")
    links = north_south_links(api_list(args.controller_url, "/api/v2/link"), vm)
    sno_adapters = udp_interfaces(vm)
    leaf_adapters: dict[str, list[dict]] = {}
    leaf_ips: dict[str, str] = {}
    verified = {}
    for server_port, (leaf_endpoint, leaf_vm, interface) in links.items():
        if leaf_vm not in names:
            raise UplinkError("A Netris leaf device is missing from libvirt.")
        if leaf_vm not in leaf_ips:
            inventory = [item for item in devices if item.get("name") == leaf_vm]
            if len(inventory) != 1:
                raise UplinkError("Netris inventory does not uniquely identify a leaf device.")
            address = inventory[0].get("mgmtAddress") or known_ips.get(leaf_vm)
            if not isinstance(address, str) or not address:
                raise UplinkError("A leaf device is missing its management address.")
            try:
                leaf_ips[leaf_vm] = str(ipaddress.ip_address(address.split("/", 1)[0]))
            except ValueError as exc:
                raise UplinkError("A leaf device has an invalid management address.") from exc
            leaf_adapters[leaf_vm] = udp_interfaces(leaf_vm)
        mac = leaf_mac(leaf_ips[leaf_vm], args.host_key, interface)
        peers = [
            (sno, leaf)
            for sno in sno_adapters
            for leaf in leaf_adapters[leaf_vm]
            if leaf["mac"] == mac and sno["local"] == leaf["remote"] and sno["remote"] == leaf["local"]
        ]
        if len(peers) != 1:
            raise UplinkError("Could not correlate exactly one leaf tunnel with the SNO uplink.")
        verified[server_port] = {"guest_mac": peers[0][0]["mac"], "leaf_port": leaf_endpoint}
    mac_to_ports: dict[str, list[str]] = {}
    for port, uplink in verified.items():
        mac_to_ports.setdefault(uplink["guest_mac"], []).append(port)
    for ports in mac_to_ports.values():
        if len(ports) > 1:
            raise UplinkError("A SNO uplink hardware address matches multiple Netris ports.")
    return verified


def resolve(args: argparse.Namespace) -> dict:
    """Resolve the SNO uplink and management address without changing Netris."""
    names, vm = vm_names(args.vm_name_pattern)
    known_ips = aliases(Path(args.aliases_path))
    management_ip = args.vm_mgmt_ip or known_ips.get(vm)
    if not management_ip:
        raise UplinkError("The SNO management address is missing from configuration.")
    try:
        management_ip = str(ipaddress.ip_address(management_ip))
    except ValueError as exc:
        raise UplinkError("The configured SNO management address is invalid.") from exc

    uplinks = verified_uplinks(args, names, vm, known_ips)
    vnets = api_list(args.controller_url, "/api/v2/vnet")
    matches = [item for item in vnets if item.get("name") == args.vnet_name]
    if len(matches) > 1:
        raise UplinkError("Netris has multiple matching OCP VNets.")
    current_ports = []
    if matches:
        current_vnet = vnet_detail(args.controller_url, int(matches[0]["id"]))
        if current_vnet.get("name") != args.vnet_name:
            raise UplinkError("The OCP VNet detail did not match its inventory record.")
        current_ports = current_vnet.get("ports") or []
        if len(current_ports) > 1:
            raise UplinkError("The OCP VNet has multiple ports; refusing to replace attachments.")
        if current_ports:
            current = full_port_name(current_ports[0])
            if current not in uplinks:
                raise UplinkError("The OCP VNet contains an unverified port attachment.")

    occupied = {}
    for other in vnets:
        if matches and int(other["id"]) == int(matches[0]["id"]):
            continue
        for port in vnet_detail(args.controller_url, int(other["id"])).get("ports") or []:
            occupied[full_port_name(port)] = other.get("name")
    if current_ports:
        server_port = full_port_name(current_ports[0])
        if server_port in occupied:
            raise UplinkError("The verified OCP port is attached to another VNet.")
    else:
        available = [port for port in uplinks if port not in occupied]
        if not available:
            raise UplinkError("All verified OCP ports are attached to other VNets.")
        server_port = min(available, key=lambda port: (int(port.split("@", 1)[0][3:]), port))

    chosen = uplinks[server_port]
    return {
        "vm_name": vm,
        "management_ip": management_ip,
        "guest_mac": chosen["guest_mac"],
        "leaf_port": chosen["leaf_port"],
        "server_port": server_port,
    }


def full_port_name(port: dict) -> str:
    """Normalize a Netris port record to its switch-qualified name."""
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
    raise UplinkError("Netris returned an unrecognized VNet port record.")


def vnet_detail(base_url: str, vnet_id: int) -> dict:
    """Fetch one Netris VNet record and verify that it is an object."""
    detail = api_request(base_url, f"/api/v2/vnet/{vnet_id}")
    if not isinstance(detail, dict):
        raise UplinkError("Netris VNet detail is not an object.")
    return detail


def check_vnet_identity(detail: dict, name: str, vpc_id: int) -> None:
    """Ensure a VNet belongs to the expected inventory entry and VPC."""
    if detail.get("name") != name or int((detail.get("vpc") or {}).get("id", -1)) != vpc_id:
        raise UplinkError("The OCP VNet identity does not match inventory; refusing to change it.")


def reconcile(args: argparse.Namespace) -> dict:
    """Attach the resolved port to the OCP VNet and verify the final state."""
    vnets = api_list(args.controller_url, "/api/v2/vnet")
    matches = [item for item in vnets if item.get("name") == args.vnet_name]
    if len(matches) != 1:
        raise UplinkError("Expected exactly one existing OCP VNet.")
    vnet_id = int(matches[0]["id"])
    detail = vnet_detail(args.controller_url, vnet_id)
    check_vnet_identity(detail, args.vnet_name, args.vpc_id)

    for other in vnets:
        if int(other.get("id", -1)) == vnet_id:
            continue
        other_ports = vnet_detail(args.controller_url, int(other["id"])).get("ports") or []
        if any(full_port_name(port) == args.server_port for port in other_ports):
            raise UplinkError("The resolved OCP port is already attached to another VNet.")

    ports = detail.get("ports") or []
    if len(ports) > 1:
        raise UplinkError("The OCP VNet has multiple ports; refusing to remove attachments.")
    if ports:
        current = full_port_name(ports[0])
        if current != args.server_port:
            raise UplinkError("The OCP VNet changed after resolution; refusing to replace its port.")
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
        raise UplinkError("The OCP VNet did not retain the resolved port in access mode.")
    return {"changed": changed, "vnet_id": vnet_id, "server_port": args.server_port}


def main() -> int:
    """Parse arguments, run the requested operation, and report its result."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    resolver = subparsers.add_parser("resolve")
    resolver.add_argument("--controller-url", required=True)
    resolver.add_argument("--vm-name-pattern", required=True)
    resolver.add_argument("--vnet-name", required=True)
    resolver.add_argument("--host-key", required=True)
    resolver.add_argument("--aliases-path", default="/root/.cloudsim_aliases")
    resolver.add_argument("--vm-mgmt-ip", default="")
    reconciler = subparsers.add_parser("reconcile")
    reconciler.add_argument("--controller-url", required=True)
    reconciler.add_argument("--vnet-name", required=True)
    reconciler.add_argument("--vpc-id", required=True, type=int)
    reconciler.add_argument("--server-port", required=True)
    args = parser.parse_args()
    try:
        result = resolve(args) if args.operation == "resolve" else reconcile(args)
    except UplinkError as exc:
        print(f"OCP uplink {args.operation} failed: {exc}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError):
        print(f"OCP uplink {args.operation} failed because its input or response was invalid.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

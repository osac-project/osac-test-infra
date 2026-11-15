#!/usr/bin/env python3
"""Pick a free static address block on a cluster-tool clone network.

CaaS guest clusters need one ingress VIP per ClusterOrder, advertised by
MetalLB inside the guest. The address has to sit on the guest workers' L2
segment, which is the clone network the runner and the hub components share.
Nothing in CI reserves such an address today, so this script derives one from
the only authoritative source there is: the libvirt network definition.

cluster-tool gives every clone `192.168.<subnet>.0/24` with the gateway at
`.1`, a DHCP pool at `.128-.254`, and the management-cluster VM pinned at
`.10` (see allocate_subnet() and the network XML it writes). The space below
the DHCP pool is static, so the topmost contiguous block of free addresses is
handed back as the value for AAP's `CI_GUEST_INGRESS_IP_RANGE`:

    CI_GUEST_INGRESS_IP_RANGE=192.168.163.124-192.168.163.127

Usage:
    CLONE_NAME=<clone> python3 guest_ingress_vip_range.py [--block-size N]
    python3 guest_ingress_vip_range.py --network-xml net.xml [--net-leases f]

`--network-xml` and `--net-leases` exist so the selection logic is testable
without libvirt; production runs read the live network with `virsh`.

Exit codes: 0 the block was found (printed on stdout, nothing else), 1 no
usable block or an unreadable network, 2 usage error. Diagnostics go to
stderr, so `$(...)` stays clean.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DEFAULT_BLOCK_SIZE = 4
DEFAULT_VIRSH = ("virsh", "-c", "qemu:///system")
NETWORK_PREFIX = "test-infra-net-"

# Offsets inside a clone network that are never guest VIP material: the
# network address itself, the gateway at .1 and the broadcast address.
FIRST_USABLE_OFFSET = 2


class RangeError(Exception):
    """A range could not be derived from the network definition."""


@dataclass
class CloneNetwork:
    """The parts of a libvirt network definition this script cares about."""

    network: int
    prefix: int
    gateway: int
    dhcp_first: int | None = None
    dhcp_last: int | None = None
    reserved: set[int] = field(default_factory=set)

    @property
    def size(self) -> int:
        return 1 << (32 - self.prefix)

    @property
    def broadcast(self) -> int:
        return self.network + self.size - 1

    def describe(self) -> str:
        pool = (
            f"dhcp {_ip(self.dhcp_first)}-{_ip(self.dhcp_last)}"
            if self.dhcp_first is not None and self.dhcp_last is not None
            else "dhcp none"
        )
        pinned = ", ".join(_ip(address) for address in sorted(self.reserved)) or "none"
        return f"network {_ip(self.network)}/{self.prefix} gateway {_ip(self.gateway)} {pool} pinned {pinned}"


def _parse_ipv4(value: str) -> int:
    octets = value.strip().split(".")
    if len(octets) != 4:
        raise RangeError(f"not an IPv4 address: {value!r}")
    try:
        numbers = [int(octet) for octet in octets]
    except ValueError as error:
        raise RangeError(f"not an IPv4 address: {value!r}") from error
    if any(number < 0 or number > 255 for number in numbers):
        raise RangeError(f"not an IPv4 address: {value!r}")
    return (numbers[0] << 24) | (numbers[1] << 16) | (numbers[2] << 8) | numbers[3]


def _ip(value: int) -> str:
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def parse_network(xml: str) -> CloneNetwork:
    """Extract the IPv4 addressing plan from a libvirt network XML document."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise RangeError(f"network XML is not well formed: {error}") from error

    ip_element = None
    for element in root.findall("ip"):
        if element.get("family", "ipv4") == "ipv4":
            ip_element = element
            break
    if ip_element is None:
        raise RangeError("network XML has no IPv4 <ip> element")

    if ip_element.get("netmask") is not None:
        raise RangeError("network netmask is unsupported; use an explicit IPv4 prefix")

    gateway = _parse_ipv4(ip_element.get("address", ""))
    try:
        prefix = int(ip_element.get("prefix", "24"))
    except ValueError as error:
        raise RangeError(f"network prefix is not a number: {error}") from error
    if not 8 <= prefix <= 30:
        raise RangeError(f"unsupported network prefix /{prefix}")

    network = gateway & (((1 << 32) - 1) ^ ((1 << (32 - prefix)) - 1))
    parsed = CloneNetwork(network=network, prefix=prefix, gateway=gateway)

    dhcp = ip_element.find("dhcp")
    if dhcp is not None:
        dhcp_ranges = dhcp.findall("range")
        if len(dhcp_ranges) > 1:
            raise RangeError("multiple DHCP ranges are unsupported")
        if dhcp_ranges:
            dhcp_range = dhcp_ranges[0]
            parsed.dhcp_first = _parse_ipv4(dhcp_range.get("start", ""))
            parsed.dhcp_last = _parse_ipv4(dhcp_range.get("end", ""))
            if parsed.dhcp_first > parsed.dhcp_last:
                raise RangeError(f"DHCP range is inverted: {_ip(parsed.dhcp_first)}-{_ip(parsed.dhcp_last)}")

    # Static reservations live either inside or outside the DHCP pool; either
    # way a guest VIP must not land on one.
    for host in ip_element.iter("host"):
        address = host.get("ip")
        if address:
            parsed.reserved.add(_parse_ipv4(address))

    return parsed


def parse_leases(text: str) -> set[int]:
    """Extract the IPv4 addresses already leased on a network."""
    leased = set()
    for line in text.splitlines():
        columns = line.split()
        if not columns:
            continue
        # `virsh net-dhcp-leases` prints ipv4 rows as: Expiry Time MAC address Protocol IP-address ...
        candidate = None
        for column in columns:
            if column.count(".") == 3 and "/" in column:
                candidate = column.split("/", 1)[0]
                break
        if candidate is None:
            continue
        try:
            leased.add(_parse_ipv4(candidate))
        except RangeError:
            continue
    return leased


def select_block(
    net: CloneNetwork, block_size: int = DEFAULT_BLOCK_SIZE, leased: set[int] | None = None
) -> tuple[int, int]:
    """Return the topmost contiguous free block below the DHCP pool."""
    if block_size < 1:
        raise RangeError(f"block size must be at least 1, got {block_size}")

    first_usable = net.network + FIRST_USABLE_OFFSET
    last_usable = net.dhcp_first - 1 if net.dhcp_first is not None else net.broadcast - 1
    if last_usable < first_usable:
        raise RangeError(f"{net.describe()}: no static space between {_ip(first_usable)} and {_ip(last_usable)}")

    taken = set(net.reserved) | {net.gateway} | set(leased or ())
    usable = last_usable - first_usable + 1
    if usable < block_size:
        raise RangeError(
            f"{net.describe()}: only {usable} static address(es) below the DHCP "
            f"pool, a block of {block_size} does not fit"
        )

    # Walk down from the top so the block sits next to the DHCP pool: the low
    # addresses stay available for anything a future clone layout pins there.
    for last in range(last_usable, first_usable + block_size - 2, -1):
        first = last - block_size + 1
        if any(address in taken for address in range(first, last + 1)):
            continue
        return first, last

    raise RangeError(f"{net.describe()}: no run of {block_size} free static address(es) below the DHCP pool")


def _virsh(virsh: tuple[str, ...], *arguments: str) -> str:
    command = [*virsh, *arguments]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RangeError(f"{command[0]} is not installed") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise RangeError(f"{' '.join(command)} failed: {detail}")
    return completed.stdout


def _read_network(network_name: str, virsh: tuple[str, ...], explain: bool) -> str:
    if explain:
        print(f"reading network {network_name!r} with {' '.join(virsh)}", file=sys.stderr)
    return _virsh(virsh, "net-dumpxml", network_name)


def _read_leases(network_name: str, virsh: tuple[str, ...], explain: bool) -> set[int]:
    """Best effort: the static window is outside the DHCP pool by construction."""
    try:
        text = _virsh(virsh, "net-dhcp-leases", network_name)
    except RangeError as error:
        if explain:
            print(f"ignoring DHCP leases ({error})", file=sys.stderr)
        return set()
    leased = parse_leases(text)
    if explain:
        print(f"leased addresses: {', '.join(sorted(_ip(address) for address in leased)) or 'none'}", file=sys.stderr)
    return leased


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print a free static address block on a cluster-tool clone network, "
            "formatted for AAP's CI_GUEST_INGRESS_IP_RANGE."
        )
    )
    parser.add_argument(
        "--clone-name",
        default=None,
        help="cluster-tool clone name; the network is test-infra-net-<clone> (default: $CLONE_NAME)",
    )
    parser.add_argument("--network", default=None, help="libvirt network name (default: test-infra-net-<clone>)")
    parser.add_argument(
        "--block-size",
        type=int,
        default=DEFAULT_BLOCK_SIZE,
        help=f"number of consecutive addresses (default: {DEFAULT_BLOCK_SIZE})",
    )
    parser.add_argument("--virsh", default=None, help="virsh command line, for example 'virsh -c qemu:///system'")
    parser.add_argument("--network-xml", default=None, help="read the network from a file instead of virsh (for tests)")
    parser.add_argument(
        "--net-leases",
        default=None,
        help="read `virsh net-dhcp-leases` output from a file instead of virsh (for tests)",
    )
    parser.add_argument("--explain", action="store_true", help="print the addressing plan and the selection to stderr")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    clone_name = args.clone_name or os.environ.get("CLONE_NAME", "")
    network_name = args.network or (f"{NETWORK_PREFIX}{clone_name}" if clone_name else "")
    if args.network_xml is None and not network_name:
        print("error: CLONE_NAME or --network-xml is required", file=sys.stderr)
        return 2

    virsh = tuple(args.virsh.split()) if args.virsh else DEFAULT_VIRSH

    try:
        if args.network_xml is not None:
            with open(args.network_xml, encoding="utf-8") as handle:
                xml = handle.read()
            if args.net_leases is not None:
                with open(args.net_leases, encoding="utf-8") as handle:
                    leased = parse_leases(handle.read())
            else:
                leased = set()
        else:
            xml = _read_network(network_name, virsh, args.explain)
            leased = _read_leases(network_name, virsh, args.explain)

        net = parse_network(xml)
        if args.explain:
            print(net.describe(), file=sys.stderr)
        first, last = select_block(net, args.block_size, leased)
    except RangeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.explain:
        print(f"selected {_ip(first)}-{_ip(last)}", file=sys.stderr)
    print(f"{_ip(first)}-{_ip(last)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Render the AgentlessNet inventory for the virtual BMH fabric ports.

The networking worker reads this document from AGENTLESS_NET_VN_INVENTORY.
Access ports used by a host must not also be listed as trunks.

A BMaaS networking run needs more than one host: the Netris-style suite places
BMIs on several hosts and checks same-VirtualNetwork connectivity and
cross-VirtualNetwork isolation between them. Each host therefore gets its own
fabric access port, and every port must be unique.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

FABRIC_INTERFACE = "eth9"
PROVISIONING_VLAN_ID = 2
# The lab exposes one access port on each leaf. A host maps to exactly one.
SUPPORTED_PORTS = {("leaf-1", "swp2"), ("leaf-2", "swp2"), ("leaf-2", "swp3")}


def inventory_document(*, mgmt_prefix: str, hosts: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Return the inventory for `hosts`, a list of (host_name, switch, port) triples."""
    if not mgmt_prefix:
        raise ValueError("mgmt_prefix is required")
    if not hosts:
        raise ValueError("at least one host is required")
    seen_hosts: set[str] = set()
    seen_ports: set[tuple[str, str]] = set()
    host_ports: dict[str, dict[str, dict[str, str]]] = {}
    for host_name, switch, port in hosts:
        if not host_name:
            raise ValueError("host_name is required")
        if (switch, port) not in SUPPORTED_PORTS:
            raise ValueError(f"unsupported fabric port {switch}/{port}")
        if host_name in seen_hosts:
            raise ValueError(f"duplicate host_name {host_name}")
        if (switch, port) in seen_ports:
            raise ValueError(f"duplicate fabric port {switch}/{port} from {host_name}")
        seen_hosts.add(host_name)
        seen_ports.add((switch, port))
        host_ports[host_name] = {FABRIC_INTERFACE: {"switch": switch, "port": port}}
    prefix = mgmt_prefix.rstrip(".")
    return {
        "all": {
            "vars": {
                "agentless_net_provisioning_network": {
                    "name": "agentless-provisioning",
                    "vlan_id": PROVISIONING_VLAN_ID,
                },
                "agentless_net_host_ports": host_ports,
            },
            "children": {
                "net_nodes": {
                    "hosts": {
                        "network-node": {"ansible_host": f"{prefix}.30", "ansible_user": "root", "ansible_port": 22}
                    }
                },
                "switches": {
                    "hosts": {
                        "leaf-1": {
                            "ansible_host": f"{prefix}.11",
                            "ansible_user": "cumulus",
                            "ansible_network_os": "cumulus",
                            "trunk_ports": ["swp1", "swp3", "swp4"],
                        },
                        "leaf-2": {
                            "ansible_host": f"{prefix}.12",
                            "ansible_user": "cumulus",
                            "ansible_network_os": "cumulus",
                            "trunk_ports": ["swp1"],
                        },
                    }
                },
            },
        }
    }


def render_inventory(*, mgmt_prefix: str, hosts: list[tuple[str, str, str]]) -> str:
    return json.dumps(inventory_document(mgmt_prefix=mgmt_prefix, hosts=hosts))


def _parse_host(value: str) -> tuple[str, str, str]:
    """Parse `name=switch:port`, the CLI form of one host mapping."""
    name, sep, rest = value.partition("=")
    switch, sep2, iface = rest.partition(":")
    if not sep or not sep2 or not name or not switch or not iface:
        raise argparse.ArgumentTypeError(f"expected name=switch:port, got {value!r}")
    return name, switch, iface


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mgmt-prefix", required=True)
    parser.add_argument(
        "--host",
        action="append",
        type=_parse_host,
        required=True,
        help="Host mapping name=switch:port (repeatable, one per fabric access port)",
    )
    args = parser.parse_args(argv)
    sys.stdout.write(render_inventory(mgmt_prefix=args.mgmt_prefix, hosts=args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

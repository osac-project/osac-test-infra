#!/usr/bin/env python3
"""Render the AgentlessNet inventory for one virtual BMH fabric port.

The networking worker reads this document from AGENTLESS_NET_VN_INVENTORY.
Access ports used by a host must not also be listed as trunks.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


FABRIC_INTERFACE = "eth9"
FABRIC_MAC = "52:54:00:bb:dd:01"
PROVISIONING_VLAN_ID = 2


def inventory_document(*, mgmt_prefix: str, host_name: str) -> dict[str, Any]:
    """Return the inventory for host_name's eth9 on leaf-1 swp2."""
    if not mgmt_prefix or not host_name:
        raise ValueError("mgmt_prefix and host_name are required")
    prefix = mgmt_prefix.rstrip(".")
    return {
        "all": {
            "vars": {
                "agentless_net_provisioning_network": {
                    "name": "agentless-provisioning",
                    "vlan_id": PROVISIONING_VLAN_ID,
                },
                "agentless_net_host_ports": {
                    host_name: {
                        FABRIC_INTERFACE: {
                            "switch": "leaf-1",
                            "port": "swp2",
                        }
                    }
                },
            },
            "children": {
                "net_nodes": {
                    "hosts": {
                        "network-node": {
                            "ansible_host": f"{prefix}.30",
                            "ansible_user": "root",
                            "ansible_port": 22,
                        }
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


def render_inventory(*, mgmt_prefix: str, host_name: str) -> str:
    return json.dumps(inventory_document(mgmt_prefix=mgmt_prefix, host_name=host_name))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mgmt-prefix", required=True)
    parser.add_argument("--host-name", required=True)
    args = parser.parse_args(argv)
    sys.stdout.write(render_inventory(mgmt_prefix=args.mgmt_prefix, host_name=args.host_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

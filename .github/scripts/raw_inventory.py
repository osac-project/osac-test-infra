#!/usr/bin/env python3
"""Raw inventory library for the virtual BareMetalHost tools.

One JSON file tracks one group of hosts — the contract between the
writer (manage-virtual-bmh-vms.py) and the consumers (reconcile-bmhs.py,
later other inventory backends):

    {
      "name_prefix": "virtual-bmh-<generated>",
      "hosts": [
        {
          "name": "<VM name>",
          "uuid": "<libvirt domain UUID>",
          "boot_mac": "<boot MAC address>",
          "bmc_address": "redfish-virtualmedia+http://<host>:<port>/redfish/v1/Systems/<uuid>",
          "bmc_username": "admin",
          "bmc_password": "password",
          "cpu": 4,
          "memory_mib": 8192
        }
      ]
    }

The group's name prefix is generated when the inventory is first dumped
and persisted before any host is added, so cleanup can always find the
VMs. Every add/remove is followed by a save, so the file always
describes exactly the hosts the group holds.

Invalid files raise InventoryError; callers decide how to report it.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path


class InventoryError(Exception):
    """The raw inventory file is invalid."""


# Host names are <name_prefix>-<token>, where the token is one hex byte.
GROUP_NAME_TOKEN_RE = re.compile(r"[0-9a-f]{2}")


def is_group_host_name(name: str, name_prefix: str) -> bool:
    """True when name follows the group's naming scheme: <name_prefix>-<token>.

    The complete name is checked, not just the prefix: a group named
    "grp" must not claim a host of the overlapping group "grp-01".
    """
    suffix = name[len(name_prefix) + 1 :]
    return name.startswith(f"{name_prefix}-") and GROUP_NAME_TOKEN_RE.fullmatch(suffix) is not None


@dataclass
class Host:
    """One virtual bare-metal host of a group (one raw-inventory entry).

    The field order is the JSON key order of a raw-inventory entry.
    """

    name: str
    uuid: str
    boot_mac: str
    bmc_address: str
    bmc_username: str
    bmc_password: str
    cpu: int
    memory_mib: int


def _validate_host_types(host: Host, path: Path) -> None:
    """Host(**entry) accepts wrong types; the consumers must not."""
    for field in ("name", "uuid", "boot_mac", "bmc_address", "bmc_username", "bmc_password"):
        if not isinstance(getattr(host, field), str):
            raise InventoryError(f"host entry in '{path}': '{field}' must be a string")
    for field in ("cpu", "memory_mib"):
        if not isinstance(getattr(host, field), int) or isinstance(getattr(host, field), bool):
            raise InventoryError(f"host entry in '{path}': '{field}' must be an integer")


class RawInventory:
    """A group of hosts tracked in a raw inventory file."""
    def __init__(self, name_prefix: str | None = None, hosts: list[Host] | None = None) -> None:
        self.name_prefix = name_prefix
        self.hosts: list[Host] = hosts if hosts is not None else []

    @classmethod
    def load(cls, path: Path) -> RawInventory:
        """Load the raw inventory; a missing file starts a new, empty group."""
        if not path.exists():
            return cls()
        try:
            with open(path) as inventory_file:
                inventory = json.load(inventory_file)
        except json.JSONDecodeError as exc:
            raise InventoryError(f"raw inventory file '{path}' is not valid JSON: {exc}") from exc
        if not isinstance(inventory, dict) or not isinstance(inventory.get("hosts"), list):
            raise InventoryError(f"raw inventory file '{path}' must be a JSON object with a 'hosts' array")
        name_prefix = inventory.get("name_prefix")
        if not isinstance(name_prefix, str) or not name_prefix:
            raise InventoryError(f"raw inventory file '{path}' has no name_prefix")
        hosts = []
        for entry in inventory["hosts"]:
            if not isinstance(entry, dict):
                raise InventoryError(f"host entry in '{path}' must be a JSON object")
            try:
                host = Host(**entry)
            except TypeError as exc:
                raise InventoryError(f"invalid host entry in '{path}': {exc}") from exc
            _validate_host_types(host, path)
            hosts.append(host)
        return cls(name_prefix=name_prefix, hosts=hosts)

    def save(self, path: Path) -> None:
        """Write the raw inventory atomically (temp file + rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.tmp")
        payload = {"name_prefix": self.name_prefix, "hosts": [asdict(host) for host in self.hosts]}
        with open(tmp_path, "w") as inventory_file:
            json.dump(payload, inventory_file, indent=2)
            inventory_file.write("\n")
        os.replace(tmp_path, path)

    def ensure_name_prefix(self, path: Path) -> str:
        """Return the group's name prefix, generating and persisting it first.

        The prefix is written to the inventory before any host is added,
        so a crash mid-add still leaves the prefix discoverable for
        cleanup.
        """
        if not self.name_prefix:
            self.name_prefix = f"virtual-bmh-{secrets.token_hex(4)}"
            self.save(path)
        return self.name_prefix

    def host_names(self) -> set[str]:
        return {host.name for host in self.hosts}

    def add_host(self, host: Host) -> None:
        self.hosts.append(host)

    def remove_hosts(self, names: Iterable[str]) -> None:
        """Drop the hosts with these names; unknown names are ignored."""
        target = set(names)
        self.hosts = [host for host in self.hosts if host.name not in target]

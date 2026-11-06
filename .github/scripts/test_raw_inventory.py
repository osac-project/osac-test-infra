#!/usr/bin/env python3
"""Tests for raw_inventory.py, the library behind the virtual BMH tools.

Run directly: python3 .github/scripts/test_raw_inventory.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Import sibling module without requiring package install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import raw_inventory


def _host(name: str) -> raw_inventory.Host:
    return raw_inventory.Host(
        name=name,
        uuid=f"uuid-of-{name}",
        boot_mac="52:54:00:bb:cc:01",
        bmc_address=f"redfish-virtualmedia+http://127.0.0.1:8000/redfish/v1/Systems/uuid-of-{name}",
        bmc_username="admin",
        bmc_password="password",
        cpu=4,
        memory_mib=8192,
    )


class LoadSaveTests(unittest.TestCase):
    def test_missing_file_starts_an_empty_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            inventory = raw_inventory.RawInventory.load(Path(temp_dir) / "missing.json")
            self.assertIsNone(inventory.name_prefix)
            self.assertEqual(inventory.hosts, [])

    def test_save_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            inventory = raw_inventory.RawInventory("grp", [_host("grp-1"), _host("grp-2")])
            inventory.save(path)
            read_back = raw_inventory.RawInventory.load(path)
            self.assertEqual(read_back.name_prefix, "grp")
            self.assertEqual(read_back.hosts, [_host("grp-1"), _host("grp-2")])

    def test_invalid_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            path.write_text("{not json")
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_missing_name_prefix_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            path.write_text(json.dumps({"hosts": []}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_missing_hosts_array_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            path.write_text(json.dumps({"name_prefix": "grp"}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_missing_host_fields_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            path.write_text(json.dumps({"name_prefix": "grp", "hosts": [{"name": "grp-1"}]}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_unknown_host_fields_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            entry = {**vars(_host("grp-1")), "surprise": True}
            path.write_text(json.dumps({"name_prefix": "grp", "hosts": [entry]}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_non_string_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            entry = {**vars(_host("grp-1")), "name": 123}
            path.write_text(json.dumps({"name_prefix": "grp", "hosts": [entry]}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_non_integer_cpu_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            entry = {**vars(_host("grp-1")), "cpu": "4"}
            path.write_text(json.dumps({"name_prefix": "grp", "hosts": [entry]}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)

    def test_boolean_cpu_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            entry = {**vars(_host("grp-1")), "cpu": True}
            path.write_text(json.dumps({"name_prefix": "grp", "hosts": [entry]}))
            with self.assertRaises(raw_inventory.InventoryError):
                raw_inventory.RawInventory.load(path)


class IsGroupHostNameTests(unittest.TestCase):
    def test_accepts_group_names(self) -> None:
        self.assertTrue(raw_inventory.is_group_host_name("grp-3f", "grp"))
        # An overlapping group's host belongs to that group, not to "grp".
        self.assertTrue(raw_inventory.is_group_host_name("grp-01-7f", "grp-01"))

    def test_rejects_overlapping_and_foreign_names(self) -> None:
        # A group "grp" must not claim a host of the group "grp-01".
        self.assertFalse(raw_inventory.is_group_host_name("grp-01-7f", "grp"))
        self.assertFalse(raw_inventory.is_group_host_name("other-3f", "grp"))

    def test_rejects_malformed_tokens(self) -> None:
        self.assertFalse(raw_inventory.is_group_host_name("grp-3", "grp"))
        self.assertFalse(raw_inventory.is_group_host_name("grp-3f4", "grp"))
        self.assertFalse(raw_inventory.is_group_host_name("grp-xyz", "grp"))
        self.assertFalse(raw_inventory.is_group_host_name("grp", "grp"))


class GroupManagementTests(unittest.TestCase):
    def test_ensure_name_prefix_generates_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "inventory.json"
            inventory = raw_inventory.RawInventory.load(path)
            name_prefix = inventory.ensure_name_prefix(path)
            self.assertRegex(name_prefix, r"^virtual-bmh-[0-9a-f]{8}$")
            on_disk = json.loads(path.read_text())
            self.assertEqual(on_disk["name_prefix"], name_prefix)
            self.assertEqual(on_disk["hosts"], [])

            # A second load of the same inventory reuses the stored prefix.
            self.assertEqual(raw_inventory.RawInventory.load(path).ensure_name_prefix(path), name_prefix)

    def test_add_and_remove_hosts(self) -> None:
        inventory = raw_inventory.RawInventory("grp", [_host("grp-1"), _host("grp-2")])
        inventory.add_host(_host("grp-3"))
        self.assertEqual(inventory.host_names(), {"grp-1", "grp-2", "grp-3"})

        # Unknown names are ignored.
        inventory.remove_hosts(["grp-1", "unknown"])
        self.assertEqual(sorted(inventory.host_names()), ["grp-2", "grp-3"])


if __name__ == "__main__":
    unittest.main()

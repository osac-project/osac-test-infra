#!/usr/bin/env python3
"""Tests for reconcile-bmhs.py.

Unit tests cover the manifest rendering; the inventory library itself
is covered by test_raw_inventory.py. The end-to-end tests run the
script as a subprocess against a fake oc on PATH, verifying the applied
manifests (namespace, BMC secrets, BareMetalHosts) and the idempotent
reconciliation of BareMetalHosts that the inventory no longer lists.
Ironic activation is covered by test_activate_ironic.py.

Run directly: python3 .github/scripts/test_reconcile_bmhs.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Import the sibling modules first: the script under test imports them
# too, and the isolated-mode test run resolves them through sys.modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import raw_inventory
import test_fakes

_SPEC = importlib.util.spec_from_file_location(
    "reconcile_bmhs", Path(__file__).resolve().parent / "reconcile-bmhs.py"
)
reconcile_bmhs = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = reconcile_bmhs
_SPEC.loader.exec_module(reconcile_bmhs)

SCRIPT = Path(__file__).resolve().parent / "reconcile-bmhs.py"

INVENTORY = {
    "name_prefix": "virtual-bmh-test",
    "hosts": [
        {
            "name": "virtual-bmh-test-01",
            "uuid": "11111111-1111-1111-1111-111111111111",
            "boot_mac": "52:54:00:bb:cc:01",
            "bmc_address": "redfish-virtualmedia+http://192.0.2.1:8000/redfish/v1/Systems/11111111-1111-1111-1111-111111111111",
            "bmc_username": "ci-admin",
            "bmc_password": "ci-pass",
            "cpu": 4,
            "memory_mib": 8192,
        },
        {
            "name": "virtual-bmh-test-02",
            "uuid": "22222222-2222-2222-2222-222222222222",
            "boot_mac": "52:54:00:bb:cc:02",
            "bmc_address": "redfish-virtualmedia+http://192.0.2.1:8000/redfish/v1/Systems/22222222-2222-2222-2222-222222222222",
            "bmc_username": "ci-admin",
            "bmc_password": "ci-pass",
            "cpu": 4,
            "memory_mib": 8192,
        },
    ],
}


class RenderManifestTests(unittest.TestCase):
    def test_bmh_manifest_comes_from_the_inventory_entry(self) -> None:
        host = raw_inventory.Host(**INVENTORY["hosts"][0])
        manifest = reconcile_bmhs.render_bmh_manifest(host, "host-inventory")
        for expected in (
            f"name: {host.name}-bmc-secret",
            f"username: {json.dumps(host.bmc_username)}",
            f"password: {json.dumps(host.bmc_password)}",
            f"name: {host.name}",
            f'bootMACAddress: "{host.boot_mac}"',
            f'address: "{host.bmc_address}"',
            f"credentialsName: {host.name}-bmc-secret",
        ):
            self.assertIn(expected, manifest)


class EndToEndTests(unittest.TestCase):
    """Run the script against the fake oc from test_fakes."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)

        self.fakes = test_fakes.FakeBins(self.temp, ["oc"])
        self.calls_file = self.fakes.calls_file
        self.capture_dir = self.fakes.capture_dir
        self.ns_marker = self.temp / "ns-applied"
        self.inventory_file = self.temp / "inventory.json"
        self.inventory_file.write_text(json.dumps(INVENTORY))
        self.bmh_list_file = self.temp / "bmh-list.txt"
        self.bmh_list_file.write_text("virtual-bmh-test-01 virtual-bmh-test-02")

        self.env = self.fakes.env(NS_MARKER=str(self.ns_marker), BMH_LIST_FILE=str(self.bmh_list_file))

    def _run(self, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--inventory-file", str(self.inventory_file)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _applies(self) -> str:
        return "\n".join(
            path.read_text() for path in sorted(self.capture_dir.glob("apply-*.yaml"))
        )

    def test_missing_inventory_file_fails(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--inventory-file", str(self.temp / "missing.json")],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not found", result.stderr)

    def test_full_flow_applies_bmhs_from_the_inventory(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        applies = sorted(self.capture_dir.glob("apply-*.yaml"))
        # The namespace first, then one manifest per host.
        self.assertEqual(len(applies), 3)
        self.assertIn("kind: Namespace", applies[0].read_text())

        all_applies = self._applies()
        for host in INVENTORY["hosts"]:
            for expected in (
                f"name: {host['name']}-bmc-secret",
                f"username: {json.dumps(host['bmc_username'])}",
                f"password: {json.dumps(host['bmc_password'])}",
                f'bootMACAddress: "{host["boot_mac"]}"',
                f'address: "{host["bmc_address"]}"',
            ):
                self.assertIn(expected, all_applies)

        calls = self.calls_file.read_text()
        for host in INVENTORY["hosts"]:
            self.assertIn(f"label bmh {host['name']} ", calls)
        self.assertNotIn("delete bmh", calls)

    def test_deletes_bmhs_absent_from_the_inventory(self) -> None:
        # virtual-bmh-test-ff belongs to the group's naming scheme but is
        # no longer in the inventory; virtual-bmh-test-01-7f belongs to an
        # overlapping group ("virtual-bmh-test-01") and must be left alone.
        self.bmh_list_file.write_text(
            "virtual-bmh-test-01 virtual-bmh-test-02 virtual-bmh-test-ff virtual-bmh-test-01-7f"
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        calls = self.calls_file.read_text()
        call_lines = calls.splitlines()
        # Deleted without waiting, then stripped of the finalizers — the
        # backing VMs are gone, Ironic cannot release them on its own.
        for kind, name in (("bmh", "virtual-bmh-test-ff"), ("secret", "virtual-bmh-test-ff-bmc-secret")):
            delete_at = next(i for i, line in enumerate(call_lines) if line.startswith(f"delete {kind} {name} "))
            patch_at = next(i for i, line in enumerate(call_lines) if line.startswith(f"patch {kind} {name} "))
            self.assertLess(delete_at, patch_at)
        self.assertIn('{"metadata":{"finalizers":null}}', calls)
        # No detached annotation: the operator is bypassed entirely.
        self.assertNotIn("annotate", calls)
        # Hosts still in the inventory, and hosts of overlapping groups,
        # are never deleted.
        self.assertNotIn("delete bmh virtual-bmh-test-01", calls)
        self.assertNotIn("delete bmh virtual-bmh-test-02", calls)
        self.assertNotIn("delete bmh virtual-bmh-test-01-7f", calls)

    def test_rerun_with_steady_state_deletes_nothing(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(sorted(self.capture_dir.glob("apply-*.yaml"))), 3)
        self.assertNotIn("delete", self.calls_file.read_text())

    def test_emptied_inventory_deletes_all_group_bmhs(self) -> None:
        """The cleanup path: VMs removed, inventory emptied, reconcile once more."""
        self.inventory_file.write_text(json.dumps({"name_prefix": "virtual-bmh-test", "hosts": []}))
        self.bmh_list_file.write_text(
            "virtual-bmh-test-01 virtual-bmh-test-02 virtual-bmh-test-01-7f"
        )

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        calls = self.calls_file.read_text()
        call_lines = calls.splitlines()
        for name in ("virtual-bmh-test-01", "virtual-bmh-test-02"):
            delete_at = next(i for i, line in enumerate(call_lines) if line.startswith(f"delete bmh {name} "))
            patch_at = next(i for i, line in enumerate(call_lines) if line.startswith(f"patch bmh {name} "))
            self.assertLess(delete_at, patch_at)
            self.assertIn(f"delete secret {name}-bmc-secret", calls)
            self.assertIn(f"patch secret {name}-bmc-secret", calls)
        # Hosts of overlapping groups are still not ours to delete.
        self.assertNotIn("delete bmh virtual-bmh-test-01-7f", calls)


if __name__ == "__main__":
    unittest.main()

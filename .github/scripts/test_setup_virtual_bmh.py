#!/usr/bin/env python3
"""Contract tests for the setup-virtual-bmh.sh wiring."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fakes

ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = ROOT / ".github/scripts/setup-virtual-bmh.sh"
SUSHY_SCRIPT = ROOT / ".github/scripts/setup-sushy-tools.sh"
TEARDOWN_SCRIPT = ROOT / ".github/scripts/teardown.sh"


class SetupVirtualBmhTests(unittest.TestCase):
    def _run_setup(
        self,
        size: str | None,
        *,
        invalid_inventory: bool = False,
        orphan_inventory: str | None = None,
        dangling: bool = False,
    ) -> tuple:
        """Execute real orchestration, VM CLI and reconciliation without services."""
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fakes = test_fakes.FakeBins(temp, ["virsh", "qemu-img", "oc"])
            child_bash = fakes.bin_dir / "bash"
            child_bash.write_text(
                '#!/bin/bash\ncase "$1" in\n'
                '  */activate-ironic.sh|*/setup-sushy-tools.sh) echo "child $*" >> "$CALLS_FILE" ;;\n'
                '  *) exec /bin/bash "$@" ;;\nesac\n'
            )
            child_bash.chmod(0o755)
            firmware = temp / "OVMF_VARS.fd"
            firmware.write_bytes(b"VARS")
            inventory = temp / "virtual-bmh-inventory-contract.json"
            if invalid_inventory:
                inventory.write_text("invalid inventory")
            disks = temp / "custom" / "virtual-bmh-disks-contract"
            if orphan_inventory:
                disks.mkdir(parents=True)
                orphan = disks / "virtual-bmh-1234abcd-01-data.qcow2"
                if dangling:
                    orphan.symlink_to(temp / "absent")
                else:
                    orphan.write_bytes(b"keep")
                if orphan_inventory == "empty":
                    inventory.write_text(json.dumps({"name_prefix": "virtual-bmh-1234abcd", "hosts": []}))
                inventory_before = inventory.read_bytes() if inventory.exists() else None
            env = fakes.env(
                HOME=str(temp),
                CLONE_NAME="contract",
                BMH_COUNT="2",
                VM_DISK_DIR=str(temp / "custom"),
                VM_EFI_VARS=str(firmware),
            )
            env.pop("BMH_DATA_DISK_SIZE", None)
            if size is not None:
                env["BMH_DATA_DISK_SIZE"] = size
            result = subprocess.run(
                ["/bin/bash", str(SETUP_SCRIPT)], env=env, capture_output=True, text=True, timeout=20, check=False
            )
            calls = fakes.calls_file.read_text() if fakes.calls_file.exists() else ""
            domains = [ET.fromstring(path.read_text()) for path in sorted(fakes.capture_dir.glob("vm-*.xml"))]
            manifests = [path.read_text() for path in fakes.capture_dir.glob("apply-*")]
            if invalid_inventory:
                self.assertEqual(inventory.read_text(), "invalid inventory")
                self.assertFalse((temp / "custom").exists())
            if orphan_inventory and size:
                self.assertEqual(inventory.read_bytes() if inventory.exists() else None, inventory_before)
                self.assertEqual(list(disks.iterdir()), [orphan])
                if dangling:
                    self.assertEqual(orphan.readlink(), temp / "absent")
                else:
                    self.assertEqual(orphan.read_bytes(), b"keep")
            return result, calls, domains, manifests

    def test_workflow_option_reaches_actual_vm_consumer(self) -> None:
        """CaaS supplies 50G; default-off BMaaS retains one OS disk."""
        workflow = (ROOT / ".github/workflows/e2e-caas-full-install.yml").read_text()
        step = workflow.split("- name: Setup virtual BareMetalHosts\n", 1)[1].split("\n      - name:", 1)[0]
        match = re.search(r'BMH_DATA_DISK_SIZE: "([^\"]+)"', step)
        self.assertIsNotNone(match, "CaaS must opt in to a data disk")
        self.assertEqual(match.group(1), "50G")
        self.assertNotIn("BMH_DATA_DISK_SIZE:", (ROOT / ".github/workflows/e2e-bmaas-full-install.yml").read_text())
        for size in (None, "", match.group(1)):
            with self.subTest(size=size):
                result, calls, domains, manifests = self._run_setup(size)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(domains), 2)
                for domain in domains:
                    disks = domain.findall("./devices/disk")
                    self.assertEqual(len(disks), 2 if size else 1)
                    self.assertEqual(disks[0].find("target").get("dev"), "vda")
                    self.assertIn(f"{disks[0].find('source').get('file')} 150G", calls)
                    if size:
                        self.assertEqual(disks[1].find("target").get("dev"), "vdb")
                        self.assertIn(f"{disks[1].find('source').get('file')} 50G", calls)
                hosts = [manifest for manifest in manifests if "kind: BareMetalHost" in manifest]
                self.assertEqual(len(hosts), 2)
                for manifest in hosts:
                    self.assertIn("  rootDeviceHints:\n    deviceName: /dev/vda\n", manifest)

    def test_opt_in_environment_adds_data_disk(self) -> None:
        """The real orchestrator must not silently ignore the optional disk."""
        result, _, domains, _ = self._run_setup("50G")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(domains), 2)
        for domain in domains:
            self.assertEqual(len(domain.findall("./devices/disk")), 2)

    def test_orphans_fail_before_services_and_reconciliation(self) -> None:
        """The thin consumer must reject even dangling remnants before external commands."""
        for state in ("missing", "empty"):
            for dangling in (False, True):
                with self.subTest(state=state, dangling=dangling):
                    result, calls, domains, manifests = self._run_setup(
                        "50G", orphan_inventory=state, dangling=dangling
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(calls, "")
                    self.assertEqual(domains, [])
                    self.assertEqual(manifests, [])

    def test_disabled_data_mode_keeps_upstream_creation_with_remnants(self) -> None:
        result, _, domains, _ = self._run_setup(None, orphan_inventory="empty")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(domains), 2)
        self.assertTrue(all(len(domain.findall("./devices/disk")) == 1 for domain in domains))

    def test_invalid_option_and_inventory_fail_before_services(self) -> None:
        for size in ("0G", "-1G", "1.5G", "50GB", " 50G", "50G\n", "$(false)"):
            with self.subTest(size=size):
                result, calls, domains, _ = self._run_setup(size)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, "")
                self.assertEqual(domains, [])
        result, calls, _, _ = self._run_setup("50G", invalid_inventory=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "")

    def test_custom_parent_cleanup_removes_both_disks_only_for_this_clone(self) -> None:
        teardown = TEARDOWN_SCRIPT.read_text()
        start = teardown.index('\nVIRSH="virsh -c qemu:///system"\n')
        end = teardown.index("\nSUSHY_CONFIG_DIR=", start)
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            disks = temp / "custom" / "virtual-bmh-disks-contract"
            other = temp / "custom" / "virtual-bmh-disks-other"
            disks.mkdir(parents=True)
            other.mkdir()
            for name in ("worker.qcow2", "worker-data.qcow2", "worker-VARS.fd"):
                (disks / name).touch()
            keep = other / "other-data.qcow2"
            keep.write_text("keep")
            inventory = temp / "virtual-bmh-inventory-contract.json"
            inventory.write_text(json.dumps({"name_prefix": "virtual-bmh-random", "hosts": []}))
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            virsh = fake_bin / "virsh"
            virsh.write_text('#!/bin/bash\n[[ "$3" == "pool-info" ]] && exit 1\nexit 0\n')
            virsh.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", "-euc", teardown[start:end]],
                env={
                    **os.environ,
                    "HOME": str(temp),
                    "CLONE_NAME": "contract",
                    "VM_DISK_DIR": str(temp / "custom"),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(disks.exists())
            self.assertEqual(keep.read_text(), "keep")

    def test_host_compatibility_and_retry_guards(self) -> None:
        """Check the disk paths and the cgroup manager."""
        setup = SETUP_SCRIPT.read_text()
        sushy = SUSHY_SCRIPT.read_text()
        teardown = TEARDOWN_SCRIPT.read_text()

        self.assertIn('if [[ "${VM_DISK_ROOT}" != /* ]]', setup)
        self.assertIn('VM_DISK_DIR="${VM_DISK_ROOT%/}/virtual-bmh-disks-${CLONE_NAME}"', setup)
        self.assertIn('if [[ "${BMH_DISK_ROOT}" != /* ]]', teardown)
        self.assertIn('BMH_DISK_DIR="${BMH_DISK_ROOT%/}/virtual-bmh-disks-${CLONE_NAME}"', teardown)
        self.assertIn("--cgroup-manager=cgroupfs", sushy)


if __name__ == "__main__":
    unittest.main()

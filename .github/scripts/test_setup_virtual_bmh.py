#!/usr/bin/env python3
"""Contract tests for the setup-virtual-bmh.sh wiring."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = ROOT / ".github/scripts/setup-virtual-bmh.sh"
SUSHY_SCRIPT = ROOT / ".github/scripts/setup-sushy-tools.sh"
TEARDOWN_SCRIPT = ROOT / ".github/scripts/teardown.sh"


class SetupVirtualBmhTests(unittest.TestCase):
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

    def test_vm_resources_are_configurable(self) -> None:
        setup = SETUP_SCRIPT.read_text()

        self.assertIn('BMH_CPU="${BMH_CPU:-4}"', setup)
        self.assertIn('BMH_MEMORY_MIB="${BMH_MEMORY_MIB:-8192}"', setup)
        self.assertIn('  --cpu "${BMH_CPU}"', setup)
        self.assertIn('  --memory-mib "${BMH_MEMORY_MIB}"', setup)

if __name__ == "__main__":
    unittest.main()

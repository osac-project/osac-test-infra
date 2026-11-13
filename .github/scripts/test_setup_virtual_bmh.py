#!/usr/bin/env python3
"""Contract tests for the setup-virtual-bmh.sh wiring."""

from __future__ import annotations

import unittest
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = ROOT / ".github/scripts/setup-virtual-bmh.sh"
SUSHY_SCRIPT = ROOT / ".github/scripts/setup-sushy-tools.sh"
TEARDOWN_SCRIPT = ROOT / ".github/scripts/teardown.sh"
sys.path.insert(0, str(ROOT / ".github/scripts"))
import bmh_history


def install_serial_reader(binaries: Path) -> None:
    reader = binaries / "sudo"
    reader.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
arguments = sys.argv[1:]
if arguments[:2] != ["-n", "/usr/local/sbin/osac-bmh-history"]:
    sys.exit(1)
command, prefix = arguments[2:4]
directory = pathlib.Path(os.environ["FAKE_BMH_SERIAL"])
if command == "list":
    print(json.dumps(sorted(path.name for path in directory.glob(prefix + "-*.serial.log*"))))
elif command == "read":
    sys.stdout.buffer.write((directory / arguments[4]).read_bytes()[:2097152])
else:
    sys.exit(1)
''')
    reader.chmod(0o755)


class SetupVirtualBmhTests(unittest.TestCase):
    def test_cleanup_retains_live_or_unreachable_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir(mode=0o700)
            for status, listing in ((1, ""), (0, "virtual-bmh-aabbccdd-01\n")):
                with mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], status, listing)):
                    bmh_history.cleanup(inventory)
                self.assertTrue(history.exists())
            with mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "virtual-bmh-foreign-01\n")):
                bmh_history.cleanup(inventory)
            self.assertFalse(history.exists())

    def test_stop_waits_for_owned_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as process:
                (directory / "ready.json").write_text(json.dumps(dict(pid=process.pid, identity=bmh_history.process_identity(process.pid))))
                bmh_history.stop(directory)
                self.assertIsNotNone(process.wait(timeout=2))

    def test_failed_private_release_gate_never_stages_raw_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir(mode=0o700)
            (history / "virtual-bmh-aabbccdd-01.serial.log").write_text("private raw data")
            destination = base / "artifacts/history"
            with mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)), \
                 mock.patch.object(bmh_history, "serial_helper", return_value=b"[]"):
                bmh_history.collect(inventory, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(history.glob("staging-*")), [])

    def test_actual_collection_wrapper_preserves_marker_and_failure_behavior(self) -> None:
        for gather_status in (0, 1, 124):
            with self.subTest(gather_status=gather_status), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                workspace = base / "workspace"
                (workspace / "scripts").mkdir(parents=True)
                (workspace / ".github").mkdir()
                (workspace / ".github/scripts").symlink_to(ROOT / ".github/scripts", target_is_directory=True)
                gather = workspace / "scripts/gather-osac-logs.sh"
                gather.write_text(f'#!/bin/bash\nmkdir -p "$1"\nexit {gather_status}\n')
                gather.chmod(0o755)
                binaries = base / "bin"
                binaries.mkdir()
                oc = binaries / "oc"
                oc.write_text('#!/bin/bash\nprintf \'{"items":[]}\\n\'\n')
                oc.chmod(0o755)
                install_serial_reader(binaries)
                inventory = base / "virtual-bmh-inventory-test.json"
                inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
                history = bmh_history.history_dir(inventory)
                history.mkdir(mode=0o700)
                (history / "virtual-bmh-aabbccdd-01.serial.log").write_text('first boot\n{"password":"synthetic-password-012345"}\n')
                env = os.environ | dict(RUNNER_TEMP=str(base), GITHUB_WORKSPACE=str(workspace), HOME=str(base),
                                        CLONE_NAME="test", KUBECONFIG=str(base / "kubeconfig"), PATH=f"{binaries}:{os.defpath}",
                                        FAKE_BMH_SERIAL=str(history))
                result = subprocess.run(["bash", str(ROOT / ".github/scripts/collect-artifacts.sh")],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((history / "stop").exists())
                artifacts = base / "osac-artifacts"
                self.assertEqual((artifacts / ".redaction-complete").exists(), gather_status == 0)
                if gather_status == 0:
                    output = (artifacts / "bmh-history/virtual-bmh-aabbccdd-01.serial.log").read_text()
                    self.assertNotIn("synthetic-password-012345", output + result.stdout + result.stderr)
                else:
                    self.assertFalse((artifacts / "bmh-history").exists())

    def test_recording_wiring_and_collection_order(self) -> None:
        setup = SETUP_SCRIPT.read_text()
        self.assertIn("--record-history", setup)
        self.assertLess(setup.index('echo "INVENTORY_FILE='), setup.index('python3 "${SCRIPT_DIR}/manage-virtual'))
        collection = (ROOT / ".github/scripts/collect-artifacts.sh").read_text()
        self.assertLess(collection.index("oc get secrets"), collection.index('bmh_history.py" collect'))
        self.assertLess(collection.index('bmh_history.py" collect'), collection.index('touch "${ARTIFACT_DIR}/.redaction-complete"', collection.index('bmh_history.py" collect')))
        self.assertIn('bmh_history.py" cleanup', TEARDOWN_SCRIPT.read_text())

    def test_private_serial_staging_uses_actual_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir(mode=0o700)
            secret = "synthetic-history-token-0123456789"
            (base / "pull-secret.json").write_text(json.dumps({"auths": {"example.invalid": {"auth": secret}}}))
            (history / "virtual-bmh-aabbccdd-01.serial.log").write_text('first boot\n{"password":"' + secret + '"}\nsecond boot\n')
            for suffix in (".0", ".1", ".2"):
                (history / ("virtual-bmh-aabbccdd-01.serial.log" + suffix)).write_text('{"password":"' + secret + '"}\n')
            (history / "virtual-bmh-foreign-01.serial.log").write_text("foreign")
            (history / "virtual-bmh-aabbccdd-02.serial.log").write_text(secret)
            (history / "events.log").write_text("2026-11-13 00:00:00.000+0000: event 'reboot' for domain 'virtual-bmh-aabbccdd-01'\n" + secret + "\n")
            artifact = base / "artifacts/bmh-history"
            binaries = base / "bin"
            binaries.mkdir()
            install_serial_reader(binaries)
            result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/bmh_history.py"), "collect", str(inventory), str(artifact)],
                                    env=os.environ | {"RUNNER_TEMP": str(base), "PATH": f"{binaries}:{os.defpath}",
                                                      "FAKE_BMH_SERIAL": str(history)}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = (artifact / "virtual-bmh-aabbccdd-01.serial.log").read_text()
            self.assertNotIn(secret, output + result.stdout + result.stderr)
            self.assertEqual(len(list(artifact.glob("*.log"))), 5)
            for serial in artifact.glob("*.log"):
                self.assertNotIn(secret, serial.read_text())
            self.assertIn("first boot", output)
            self.assertIn("second boot", output)
            self.assertFalse((artifact / "virtual-bmh-foreign-01.serial.log").exists())
            self.assertFalse((artifact / "virtual-bmh-aabbccdd-02.serial.log").exists())
            self.assertTrue((history / "virtual-bmh-aabbccdd-01.serial.log").exists())
            self.assertEqual(list(history.glob("staging-*")), [])

    def test_missing_invalid_inventory_never_releases_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            artifact = base / "artifacts/history"
            for content in (None, "invalid synthetic-secret-value", '{}', '{"name_prefix":"../foreign","hosts":[]}'):
                with self.subTest(content=content):
                    if content is not None:
                        inventory.write_text(content)
                    history = bmh_history.history_dir(inventory)
                    history.mkdir(exist_ok=True)
                    result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/bmh_history.py"), "collect", str(inventory), str(artifact)],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn("synthetic-secret-value", result.stdout + result.stderr)
                    self.assertFalse(artifact.exists())

    def test_cleanup_keeps_inventory_map_when_serial_reader_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir()
            with mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")), \
                 mock.patch.object(bmh_history, "serial_helper", side_effect=OSError):
                bmh_history.cleanup(inventory)
            self.assertTrue(history.exists())
            teardown = TEARDOWN_SCRIPT.read_text()
            self.assertIn('&& ! -d "${BMH_INVENTORY_FILE%.json}.history"', teardown)

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

#!/usr/bin/env python3
"""Contract tests for the setup-virtual-bmh.sh wiring."""

from __future__ import annotations

import unittest
import json
import os
import select
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


class SetupVirtualBmhTests(unittest.TestCase):
    def test_stop_reaps_actual_listener_and_sigterm_ignoring_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[])))
            directory = bmh_history.history_dir(inventory)
            directory.mkdir()
            fifo = base / "started"
            os.mkfifo(fifo)
            descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            executable = base / "virsh"
            executable.write_text(f"#!{sys.executable}\nimport os,signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwith open({str(fifo)!r}, 'w') as ready: ready.write(str(os.getpid())+'\\n')\ntime.sleep(30)\n")
            executable.chmod(0o755)
            with subprocess.Popen([sys.executable, str(ROOT / ".github/scripts/bmh_history.py"), "listen", str(inventory), "virtual-bmh-aabbccdd"],
                                  env=os.environ | {"PATH": f"{base}:{os.defpath}"},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) as listener:
                try:
                    self.assertTrue(select.select([descriptor], [], [], 5)[0])
                    cli_pid = int(os.read(descriptor, 100).strip())
                    self.assertTrue(bmh_history.stop(directory))
                    self.assertEqual(listener.wait(timeout=5), 0)
                    self.assertFalse(Path(f"/proc/{cli_pid}").exists())
                    self.assertIn("stopped", json.loads((directory / "status.json").read_text()))
                finally:
                    os.close(descriptor)
                    if listener.poll() is None:
                        listener.terminate()
                        listener.wait(timeout=5)

    def test_reused_pid_is_not_signalled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "status.json").write_text(json.dumps(dict(pid=os.getpid(), identity="old process")))
            descriptor = bmh_history.pidfd_call("pidfd_open", os.getpid(), 0)
            with mock.patch.object(bmh_history, "pidfd_call", return_value=descriptor) as call:
                self.assertTrue(bmh_history.stop(directory))
            call.assert_called_once_with("pidfd_open", os.getpid(), 0)

    def test_symlink_history_and_staging_never_follow_external_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            source = base / "source"
            source.mkdir()
            (outside / "events.log").write_text("synthetic private bytes")
            (source / "events.log").symlink_to(outside / "events.log")
            staging = base / "staging"
            staging.mkdir()
            bmh_history.stage(source, staging)
            self.assertEqual(list(staging.iterdir()), [])
            inventory = base / "inventory.json"
            bmh_history.history_dir(inventory).symlink_to(outside, target_is_directory=True)
            self.assertFalse(bmh_history.stop(bmh_history.history_dir(inventory)))
            self.assertFalse((outside / "stop").exists())
            destination = base / "destination"
            destination.symlink_to(outside, target_is_directory=True)
            bmh_history.collect(inventory, destination)
            self.assertFalse(destination.exists())
            self.assertEqual((outside / "events.log").read_text(), "synthetic private bytes")

    def test_unavailable_pidfd_retains_stop_until_delayed_listener_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[])))
            directory = bmh_history.history_dir(inventory)
            directory.mkdir()
            program = (
                "import json,os,sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(ROOT / '.github/scripts')!r}); import bmh_history; "
                "directory=Path(sys.argv[1]); "
                "(directory/'status.json').write_text(json.dumps(dict(pid=os.getpid(),identity=bmh_history.process_identity(os.getpid())))); "
                "print('waiting',flush=True); input(); "
                "assert (directory/'stop').exists(); "
                "(directory/'status.json').write_text(json.dumps(dict(stopped='test completed')))"
            )
            with subprocess.Popen([sys.executable, "-c", program, str(directory)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
                try:
                    self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                    self.assertEqual(child.stdout.readline().strip(), "waiting")
                    with mock.patch.object(bmh_history, "pidfd_call", side_effect=OSError("unavailable")), \
                         mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")):
                        bmh_history.cleanup(inventory)
                        self.assertTrue((directory / "stop").exists())
                        bmh_history.collect(inventory, base / "artifacts/history")
                        self.assertFalse((base / "artifacts/history").exists())
                    child.stdin.write("continue\n")
                    child.stdin.flush()
                    self.assertEqual(child.wait(timeout=5), 0)
                    with mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")):
                        bmh_history.cleanup(inventory)
                    self.assertFalse(directory.exists())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)

    def test_stale_history_destination_is_removed_when_gate_rejects(self) -> None:
        for stale_type in ("directory", "file"):
            with self.subTest(stale_type=stale_type), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                inventory = base / "inventory.json"
                inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[])))
                directory = bmh_history.history_dir(inventory)
                directory.mkdir()
                destination = base / "artifacts/history"
                destination.parent.mkdir(parents=True)
                if stale_type == "directory":
                    destination.mkdir()
                    (destination / "stale.log").write_text("synthetic stale raw data")
                else:
                    destination.write_text("synthetic stale raw data")
                with mock.patch.object(bmh_history, "stop", return_value=True), \
                     mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
                    bmh_history.collect(inventory, destination)
                self.assertFalse(destination.exists())

    def test_cleanup_retains_live_or_unreachable_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir(mode=0o700)
            (history / "status.json").write_text(json.dumps({"stopped": "fixture completed"}))
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
                (directory / "status.json").write_text(json.dumps(dict(pid=process.pid, identity=bmh_history.process_identity(process.pid))))
                bmh_history.stop(directory)
                self.assertIsNotNone(process.wait(timeout=2))

    def test_failed_private_release_gate_never_stages_raw_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir(mode=0o700)
            (history / "status.json").write_text(json.dumps({"stopped": "fixture completed"}))
            (history / "events.log").write_text("private raw data")
            destination = base / "artifacts/history"
            with mock.patch.object(bmh_history.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
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
                inventory = base / "virtual-bmh-inventory-test.json"
                inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
                history = bmh_history.history_dir(inventory)
                history.mkdir(mode=0o700)
                (history / "status.json").write_text(json.dumps({"stopped": "fixture completed"}))
                (history / "events.log").write_text('first boot\n{"password":"synthetic-password-012345"}\n')
                env = os.environ | dict(RUNNER_TEMP=str(base), GITHUB_WORKSPACE=str(workspace), HOME=str(base),
                                        CLONE_NAME="test", KUBECONFIG=str(base / "kubeconfig"), PATH=f"{binaries}:{Path(sys.executable).parent}:{os.defpath}")
                result = subprocess.run(["bash", str(ROOT / ".github/scripts/collect-artifacts.sh")],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((history / "stop").exists())
                artifacts = base / "osac-artifacts"
                self.assertEqual((artifacts / ".redaction-complete").exists(), gather_status == 0)
                if gather_status == 0:
                    output = (artifacts / "bmh-history/events.log").read_text()
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

    def test_private_events_use_actual_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            inventory = base / "inventory.json"
            inventory.write_text(json.dumps(dict(name_prefix="virtual-bmh-aabbccdd", hosts=[], planned_names=["virtual-bmh-aabbccdd-01"])))
            history = bmh_history.history_dir(inventory)
            history.mkdir(mode=0o700)
            (history / "status.json").write_text(json.dumps({"stopped": "fixture completed"}))
            fixture_secret = "synthetic-history-token-0123456789"
            (base / "pull-secret.json").write_text(json.dumps({"auths": {"example.invalid": {"auth": fixture_secret}}}))
            (history / "events.log").write_text('first boot\n{"password":"' + fixture_secret + '"}\nsecond boot\n')
            (history / "unselected.log").write_text(fixture_secret)
            artifact = base / "artifacts/history"
            result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/bmh_history.py"), "collect", str(inventory), str(artifact)],
                                    env=os.environ | {"RUNNER_TEMP": str(base)}, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = (artifact / "events.log").read_text()
            self.assertNotIn(fixture_secret, output + result.stdout + result.stderr)
            self.assertEqual(len(list(artifact.glob("*.log"))), 1)
            for event_file in artifact.glob("*.log"):
                self.assertNotIn(fixture_secret, event_file.read_text())
            self.assertIn("first boot", output)
            self.assertIn("second boot", output)
            self.assertFalse((artifact / "unselected.log").exists())
            self.assertTrue((history / "events.log").exists())
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
                    (history / "status.json").write_text(json.dumps({"stopped": "fixture completed"}))
                    result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/bmh_history.py"), "collect", str(inventory), str(artifact)],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn("synthetic-secret-value", result.stdout + result.stderr)
                    self.assertFalse(artifact.exists())


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

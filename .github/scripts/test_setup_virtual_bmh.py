#!/usr/bin/env python3
"""Contract tests for setup-virtual-bmh.sh."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = ROOT / ".github/scripts/setup-virtual-bmh.sh"


def _top_level_spec(manifest: str) -> dict[str, str]:
    """Parse scalar fields directly below spec in this small manifest."""
    lines = manifest.splitlines()
    spec_start = lines.index("spec:") + 1
    fields: dict[str, str] = {}
    for line in lines[spec_start:]:
        if line and not line.startswith("  "):
            break
        if not line.startswith("  ") or line.startswith("    "):
            continue
        key, separator, value = line.strip().partition(":")
        if separator:
            fields[key] = value.strip().strip('"')
    return fields


class SetupVirtualBmhTests(unittest.TestCase):
    def test_host_compatibility_and_retry_guards(self) -> None:
        """Check the disk paths, cgroup manager, and retry guard ordering."""
        setup = SETUP_SCRIPT.read_text()
        teardown = (ROOT / ".github/scripts/teardown.sh").read_text()

        self.assertIn('if [[ "${VM_DISK_ROOT}" != /* ]]', setup)
        self.assertIn('VM_DISK_DIR="${VM_DISK_ROOT%/}/virtual-bmh-disks-${CLONE_NAME}"', setup)
        self.assertIn('if [[ "${BMH_DISK_ROOT}" != /* ]]', teardown)
        self.assertIn('BMH_DISK_DIR="${BMH_DISK_ROOT%/}/virtual-bmh-disks-${CLONE_NAME}"', teardown)
        self.assertIn("podman --cgroup-manager=cgroupfs run --detach", setup)
        self.assertIn("raise SystemExit(0 if disk ==", setup)
        self.assertLess(setup.index('if ${VIRSH} dominfo "${VM_NAME}"'), setup.index("qemu-img create -f qcow2"))
        self.assertLess(setup.index('if [[ -f "${SUSHY_PID_FILE}" ]]'), setup.index("nohup env"))

    def _run_setup(
        self, data_size: str | None = None, *, reuse: str | None = None, fault: str = ""
    ) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
        """Execute the real consumer with all external state confined to a temporary HOME."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            disk_parent = temp / "disks"
            disk_dir = disk_parent / "virtual-bmh-disks-disk-contract"
            domain_dir = temp / "domains"
            domain_dir.mkdir()
            protected_files = {}
            protected_links = {}
            if reuse or fault.startswith("orphan"):
                disk_dir.mkdir(parents=True)
                for number in range(1, 4):
                    name = f"virtual-bmh-disk-contract-{number}"
                    disk = disk_dir / f"{name}.qcow2"
                    data = disk_dir / f"{name}-data.qcow2"
                    nvram = disk_dir / f"{name}-VARS.fd"
                    files = [disk, nvram] + ([data] if data_size else [])
                    if fault.startswith("orphan"):
                        files = [data] if fault.startswith("orphan_data") else [disk]
                    for path in files:
                        if fault == "orphan_data_symlink":
                            destination = temp / f"missing-{number}.qcow2"
                            path.symlink_to(destination)
                            protected_links[path] = destination
                            continue
                        if (fault, path) in (
                            ("missing_data_file", data),
                            ("missing_os_file", disk),
                            ("missing_nvram_file", nvram),
                        ):
                            continue
                        path.write_text("existing storage")
                        protected_files[path] = path.read_text()
                    if reuse:
                        domain = ET.Element("domain")
                        os_element = ET.SubElement(domain, "os")
                        ET.SubElement(os_element, "nvram").text = (
                            str(nvram) if fault != "wrong_nvram" else str(temp / "wrong.fd")
                        )
                        devices = ET.SubElement(domain, "devices")
                        disks = [("vda", disk)] + ([("vdb", data)] if data_size and fault != "missing_vdb" else [])
                        for target, source in disks:
                            device = ET.SubElement(devices, "disk", type="file", device="disk")
                            ET.SubElement(
                                device,
                                "driver",
                                name="qemu",
                                type="raw" if fault == "wrong_driver" and target == "vdb" else "qcow2",
                            )
                            wrong_source = (fault == "wrong_vdb" and target == "vdb") or (
                                fault == "wrong_os" and target == "vda"
                            )
                            ET.SubElement(
                                device, "source", file=str(temp / "wrong.qcow2") if wrong_source else str(source)
                            )
                            ET.SubElement(
                                device,
                                "target",
                                dev=target,
                                bus="scsi" if fault == "wrong_bus" and target == "vdb" else "virtio",
                            )
                        (domain_dir / f"{name}.xml").write_text(ET.tostring(domain, encoding="unicode"))
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            calls_file = temp / "calls.jsonl"
            bash_env = temp / "bash-env"
            bash_env.write_text("kill() { return 0; }\n")
            shim = fake_bin / "shim"
            shim.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    command = Path(sys.argv[0]).name
                    args = sys.argv[1:]
                    stdin = sys.stdin.read() if (
                        (command == "oc" and args[0] == "apply") or
                        (command == "virsh" and "define" in args)
                    ) else ""
                    with open(os.environ["CALLS_FILE"], "a") as calls:
                        calls.write(json.dumps({"command": command, "args": args, "stdin": stdin}) + "\\n")
                    if command == "mkdir":
                        for directory in args[1:]:
                            path = Path(directory)
                            if path.is_relative_to(Path(os.environ["HOME"])):
                                path.mkdir(parents=True, exist_ok=True)
                            else:
                                sys.exit("unexpected directory: " + directory)
                    elif command in {"qemu-img", "cp"}:
                        path = Path(args[-2] if command == "qemu-img" else args[-1])
                        if not path.is_relative_to(Path(os.environ["HOME"])):
                            sys.exit("write outside test HOME")
                        path.write_text("new storage")
                    elif command == "ps":
                        print("test process identity")
                    elif command == "virsh":
                        operation = args[2]
                        if operation == "net-dumpxml":
                            print('<network><ip address="192.0.2.1"/></network>')
                        elif operation == "domuuid":
                            print("00000000-0000-0000-0000-000000000001")
                        elif operation == "pool-info":
                            print("State: running")
                        elif operation == "pool-dumpxml":
                            target = os.environ["EXPECTED_POOL_TARGET"]
                            print(f"<pool><target><path>{target}</path></target></pool>")
                        elif operation == "dominfo":
                            sys.exit(0 if (Path(os.environ["DOMAIN_DIR"]) / (args[3] + ".xml")).exists() else 1)
                        elif operation == "dumpxml":
                            print((Path(os.environ["DOMAIN_DIR"]) / (args[3] + ".xml")).read_text())
                        elif operation == "domstate":
                            print(os.environ["VM_STATE"])
                        elif operation not in {"net-info", "pool-define-as", "pool-start", "define", "start"}:
                            sys.exit("unexpected virsh operation: " + operation)
                    elif command == "podman" and args[0] == "inspect":
                        print("true")
                    elif command == "oc" and args[0] == "get":
                        print("available" if args[1] == "bmh" else "192.0.2.2")
                    """
                )
            )
            shim.chmod(0o755)
            for command in (
                "oc",
                "virsh",
                "podman",
                "pip",
                "qemu-img",
                "cp",
                "mkdir",
                "chmod",
                "curl",
                "nohup",
                "sleep",
                "ps",
            ):
                (fake_bin / command).symlink_to(shim)
            env = os.environ.copy()
            env.pop("BMH_DATA_DISK_SIZE", None)
            env.update(
                {
                    "BASH_ENV": str(bash_env),
                    "CALLS_FILE": str(calls_file),
                    "CLONE_NAME": "disk-contract",
                    "BMH_COUNT": "3",
                    "VM_DISK_DIR": str(disk_parent),
                    "EXPECTED_POOL_TARGET": str(disk_dir),
                    "DOMAIN_DIR": str(domain_dir),
                    "VM_STATE": reuse or "running",
                    "HOME": str(temp),
                    "KUBECONFIG": str(temp / "kubeconfig"),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )
            if data_size is not None:
                env["BMH_DATA_DISK_SIZE"] = data_size
            result = subprocess.run(
                ["bash", str(SETUP_SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=20, check=False
            )
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
            for path, content in protected_files.items():
                self.assertEqual(path.read_text(), content, f"Existing storage changed: {path.name}")
            for path, destination in protected_links.items():
                self.assertTrue(path.is_symlink())
                self.assertEqual(path.readlink(), destination)
                self.assertFalse(destination.exists(), "Orphan symlink target was written")
            return result, calls

    def _assert_disks(self, data_size: str | None) -> None:
        result, calls = self._run_setup(data_size)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        domains = [
            ET.fromstring(call["stdin"]) for call in calls if call["command"] == "virsh" and "define" in call["args"]
        ]
        self.assertEqual(len(domains), 3)
        creations = [call["args"] for call in calls if call["command"] == "qemu-img"]
        self.assertEqual(len(creations), 6 if data_size else 3)
        sources = []
        expected_disks = [("vda", "150G")]
        if data_size:
            expected_disks.append(("vdb", data_size))
        for domain in domains:
            disks = domain.findall("./devices/disk")
            self.assertEqual(len(disks), 2 if data_size else 1)
            for disk, (target, size) in zip(disks, expected_disks, strict=True):
                self.assertEqual(disk.find("target").attrib, {"dev": target, "bus": "virtio"})
                self.assertEqual(disk.find("driver").attrib, {"name": "qemu", "type": "qcow2"})
                source = disk.find("source").attrib["file"]
                self.assertEqual(Path(source).parent.name, "virtual-bmh-disks-disk-contract")
                self.assertEqual(Path(source).parent.parent.name, "disks")
                self.assertIn(["create", "-f", "qcow2", source, size], creations)
                sources.append(source)
        self.assertEqual(len(sources), len(set(sources)))
        manifests = [call["stdin"] for call in calls if "kind: BareMetalHost" in call["stdin"]]
        self.assertEqual(len(manifests), 3)
        for manifest in manifests:
            self.assertIn("  rootDeviceHints:\n    deviceName: /dev/vda\n", manifest)

    def test_optional_data_disk_is_attached_to_every_host(self) -> None:
        """Every fresh CaaS worker gets a separate blank data image and vdb target."""
        self._assert_disks("50G")

    def test_compatible_domains_are_reused_without_recreating_storage(self) -> None:
        """Default BMaaS and opted-in CaaS retain upstream running/shut-off reuse."""
        for data_size in (None, "50G"):
            for state in ("running", "shut off"):
                with self.subTest(data_size=data_size, state=state):
                    result, calls = self._run_setup(data_size, reuse=state)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(any(call["command"] in {"qemu-img", "cp"} for call in calls))
                    operations = [call["args"][2] for call in calls if call["command"] == "virsh"]
                    self.assertNotIn("define", operations)
                    self.assertEqual(operations.count("start"), 3 if state == "shut off" else 0)

    def test_incompatible_reuse_and_orphans_fail_without_storage_changes(self) -> None:
        """Never repair an incompatible existing domain or overwrite orphan storage."""
        faults = (
            "missing_vdb",
            "wrong_vdb",
            "wrong_bus",
            "wrong_driver",
            "missing_data_file",
            "wrong_os",
            "wrong_nvram",
            "missing_os_file",
            "missing_nvram_file",
            "orphan_data",
            "orphan_data_symlink",
            "orphan_os",
        )
        for fault in faults:
            with self.subTest(fault=fault):
                result, calls = self._run_setup(
                    "50G", reuse=None if fault.startswith("orphan") else "running", fault=fault
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR: ", result.stderr)
                self.assertFalse(any(call["command"] in {"qemu-img", "cp"} for call in calls))
                operations = [call["args"][2] for call in calls if call["command"] == "virsh"]
                self.assertEqual(operations.count("dominfo"), 1)
                self.assertEqual(operations.count("dumpxml"), 0 if fault.startswith("orphan") else 1)
                self.assertNotIn("define", operations)
                self.assertNotIn("start", operations)

    def test_unsupported_reuse_state_still_fails(self) -> None:
        """A compatible disk layout does not permit starting a paused domain."""
        result, calls = self._run_setup("50G", reuse="paused")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported state", result.stderr)
        self.assertFalse(any(call["command"] == "qemu-img" for call in calls))

    def test_default_and_empty_config_preserve_single_disk(self) -> None:
        """BMaaS keeps its single OS disk unless data storage is explicitly enabled."""
        for data_size in (None, ""):
            with self.subTest(data_size=data_size):
                self._assert_disks(data_size)

    def test_invalid_data_disk_size_fails_before_external_commands(self) -> None:
        """Malformed opt-in sizes fail before any external commands or file writes."""
        for data_size in ("0", "0G", "-50G", "1.5G", "50GB", " 50G", "50G\n", "$(false)"):
            with self.subTest(data_size=data_size):
                result, calls = self._run_setup(data_size)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BMH_DATA_DISK_SIZE", result.stderr)
                self.assertEqual(calls, [])

    def test_only_caas_workflow_enables_data_disk(self) -> None:
        """The CaaS workflow supplies 50G to this consumer while BMaaS stays off."""
        workflows = ROOT / ".github/workflows"
        caas = (workflows / "e2e-caas-full-install.yml").read_text()
        step = caas.split("- name: Setup virtual BareMetalHosts\n", 1)[1].split("\n      - name:", 1)[0]
        match = re.search(r'^          BMH_DATA_DISK_SIZE: "([^"]+)"$', step, re.MULTILINE)
        self.assertIsNotNone(match, "CaaS virtual BMH setup must enable a data disk")
        self.assertEqual(match.group(1), "50G")
        self.assertIn("run: bash .github/scripts/setup-virtual-bmh.sh", step)
        self._assert_disks(match.group(1))
        bmaas = (workflows / "e2e-bmaas-full-install.yml").read_text()
        self.assertNotIn("BMH_DATA_DISK_SIZE:", bmaas)

    def test_clone_cleanup_removes_both_disks_without_touching_other_clones(self) -> None:
        """The actual cleanup block honors a custom disk parent and clone isolation."""
        teardown = (ROOT / ".github/scripts/teardown.sh").read_text()
        start = teardown.index('\nVIRSH="virsh -c qemu:///system"\n')
        end = teardown.index("\nSUSHY_CONFIG_DIR=", start)
        with tempfile.TemporaryDirectory() as disk_parent, tempfile.TemporaryDirectory() as fake_bin:
            disks = Path(disk_parent) / "virtual-bmh-disks-cleanup-contract"
            disks.mkdir()
            other_disks = Path(disk_parent) / "virtual-bmh-disks-other-clone"
            other_disks.mkdir()
            other_disk = other_disks / "other-data.qcow2"
            other_disk.write_text("keep")
            for filename in ("worker.qcow2", "worker-data.qcow2", "worker-VARS.fd"):
                (disks / filename).touch()
            fake_virsh = Path(fake_bin) / "virsh"
            fake_virsh.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_virsh.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {"CLONE_NAME": "cleanup-contract", "VM_DISK_DIR": disk_parent, "PATH": f"{fake_bin}:{env['PATH']}"}
            )
            result = subprocess.run(
                ["bash", "-euc", teardown[start:end]], env=env, capture_output=True, text=True, timeout=10, check=False
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(disks.exists())
            self.assertEqual(other_disk.read_text(), "keep")

    def test_virtual_media_downloads_are_published_over_http(self) -> None:
        """The generated Provisioning CR must disable virtual-media TLS."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            captured_manifest = temp / "provisioning.yaml"

            fake_oc = fake_bin / "oc"
            fake_oc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "$1" == "apply" ]]; then
                      cat > "${CAPTURED_PROVISIONING_MANIFEST}"
                      exit 0
                    fi
                    if [[ "$1" == "wait" ]]; then
                      exit 86
                    fi
                    exit 87
                    """
                )
            )
            fake_oc.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "CAPTURED_PROVISIONING_MANIFEST": str(captured_manifest),
                    "CLONE_NAME": "transport-contract",
                    "HOME": str(temp),
                    "KUBECONFIG": str(temp / "kubeconfig"),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )

            result = subprocess.run(
                ["bash", str(SETUP_SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
            spec = _top_level_spec(captured_manifest.read_text())
            self.assertEqual(spec.get("disableVirtualMediaTLS"), "true")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for manage-virtual-bmh-vms.py.

Unit tests cover the pure logic (VM token generation, XML rendering,
virsh timeout); the inventory library itself is covered by
test_raw_inventory.py. The end-to-end tests run the script as a
subprocess against the fake binaries from test_fakes.py, verifying the
contract between the script and libvirt: defined domain XML, started
domains, queried UUIDs, disk cleanup from the domain definition, and
verified deletions.

Run directly: python3 .github/scripts/test_manage_virtual_bmh_vms.py
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

# Import the sibling modules first: the script under test imports them
# too, and the isolated-mode test run resolves them through sys.modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import raw_inventory
import test_fakes

_SPEC = importlib.util.spec_from_file_location(
    "manage_virtual_bmh_vms", Path(__file__).resolve().parent / "manage-virtual-bmh-vms.py"
)
manage_virtual_bmh_vms = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = manage_virtual_bmh_vms
_SPEC.loader.exec_module(manage_virtual_bmh_vms)

SCRIPT = Path(__file__).resolve().parent / "manage-virtual-bmh-vms.py"


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


class RunVirshTests(unittest.TestCase):
    def test_virsh_calls_are_bounded_by_a_timeout(self) -> None:
        """An unresponsive libvirtd must raise, not hang the job."""
        with tempfile.TemporaryDirectory() as temp_dir:
            sleeping_virsh = Path(temp_dir) / "sleeping-virsh"
            sleeping_virsh.write_text("#!/usr/bin/env bash\nsleep 5\n")
            sleeping_virsh.chmod(0o755)

            original = (manage_virtual_bmh_vms.VIRSH, manage_virtual_bmh_vms.COMMAND_TIMEOUT_SECONDS)
            manage_virtual_bmh_vms.VIRSH = (str(sleeping_virsh),)
            manage_virtual_bmh_vms.COMMAND_TIMEOUT_SECONDS = 1
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    manage_virtual_bmh_vms.run_virsh(["domuuid", "whatever"])
            finally:
                (manage_virtual_bmh_vms.VIRSH, manage_virtual_bmh_vms.COMMAND_TIMEOUT_SECONDS) = original


class GenerateVmTokenTests(unittest.TestCase):
    def test_token_is_one_hex_byte(self) -> None:
        token = manage_virtual_bmh_vms.generate_vm_token([], "grp")
        self.assertRegex(token, r"^[0-9a-f]{2}$")

    def test_token_avoids_suffixes_already_in_use(self) -> None:
        hosts = [_host(f"grp-{byte:02x}") for byte in range(240)]
        used_names = {host.name for host in hosts}
        for _ in range(16):
            token = manage_virtual_bmh_vms.generate_vm_token(hosts, "grp")
            self.assertRegex(token, r"^[0-9a-f]{2}$")
            self.assertNotIn(f"grp-{token}", used_names)

    def test_fails_when_the_mac_space_is_exhausted(self) -> None:
        hosts = [_host(f"grp-{byte:02x}") for byte in range(256)]
        with self.assertRaises(SystemExit):
            manage_virtual_bmh_vms.generate_vm_token(hosts, "grp")


class RenderVmXmlTests(unittest.TestCase):
    def test_contains_the_domain_contract(self) -> None:
        xml = manage_virtual_bmh_vms.render_vm_xml(
            name="grp-1",
            mac="52:54:00:bb:cc:01",
            cpu=4,
            memory_mib=8192,
            disk_path="/disks/grp-1.qcow2",
            vars_path="/disks/grp-1-VARS.fd",
            efi_code="/usr/share/OVMF/OVMF_CODE.secboot.fd",
            network="test-infra-net-clone",
        )
        for expected in (
            "<name>grp-1</name>",
            "<memory unit='MiB'>8192</memory>",
            "<vcpu>4</vcpu>",
            "<mac address='52:54:00:bb:cc:01'/>",
            "<source network='test-infra-net-clone'/>",
            "<source file='/disks/grp-1.qcow2'/>",
            "<nvram>/disks/grp-1-VARS.fd</nvram>",
            "/usr/share/OVMF/OVMF_CODE.secboot.fd",
        ):
            self.assertIn(expected, xml)


class CreateDeleteEndToEndTests(unittest.TestCase):
    """Run the script against the fake virsh/qemu-img from test_fakes."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)

        self.fakes = test_fakes.FakeBins(self.temp, ["virsh", "qemu-img"])
        self.calls_file = self.fakes.calls_file
        self.capture_dir = self.fakes.capture_dir
        self.disk_dir = self.temp / "disks"
        self.disk_dir.mkdir()
        self.inventory_file = self.temp / "inventory.json"

        # A real file for the UEFI VARS template copy (VM_EFI_VARS points
        # the script at it); the fake virsh reports the disk paths the
        # domain definitions claim (FAKE_DISK_DIR).
        self.efi_vars = self.temp / "OVMF_VARS.fd"
        self.efi_vars.write_bytes(b"VARS")
        self.env = self.fakes.env(VM_EFI_VARS=str(self.efi_vars), FAKE_DISK_DIR=str(self.disk_dir))

    def _run(self, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
        )

    def _create(self, *extra: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            "create",
            "--inventory-file",
            str(self.inventory_file),
            "--network",
            "test-infra-net-clone",
            "--disk-dir",
            str(self.disk_dir),
            "--redfish-host",
            "127.0.0.1",
            "--redfish-port",
            "8000",
            *extra,
            **env_extra,
        )

    def _delete(self, *names: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        return self._run("delete", "--inventory-file", str(self.inventory_file), *names, **env_extra)

    def _enable_domain_capture(self) -> Path:
        domains = self.temp / "domains"
        domains.mkdir()
        self.env["FAKE_DOMAIN_DIR"] = str(domains)
        return domains

    def _snapshot(self) -> dict[str, bytes | str]:
        paths = list(self.disk_dir.iterdir())
        if self.inventory_file.exists():
            paths.append(self.inventory_file)
        return {str(path): str(path.readlink()) if path.is_symlink() else path.read_bytes() for path in paths}

    def test_data_disk_is_blank_vdb_and_creation_stays_additive(self) -> None:
        """Execute the CLI twice: retain existing VMs and add new blank disks."""
        self._enable_domain_capture()
        for count in (2, 1):
            result = self._create("--count", str(count), "--data-disk-size", "50G")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hosts = json.loads(self.inventory_file.read_text())["hosts"]
        self.assertEqual(len(hosts), 3)
        calls = self.calls_file.read_text().splitlines()
        self.assertEqual(sum(line.startswith("define ") for line in calls), 3)
        for index, host in enumerate(hosts):
            root = ET.fromstring((self.capture_dir / f"vm-{index}.xml").read_text())
            disks = root.findall("./devices/disk")
            self.assertEqual(len(disks), 2)
            for disk, target, suffix, size in zip(
                disks, ("vda", "vdb"), (".qcow2", "-data.qcow2"), ("150G", "50G"), strict=True
            ):
                path = self.disk_dir / f"{host['name']}{suffix}"
                self.assertEqual(disk.find("target").attrib, {"dev": target, "bus": "virtio"})
                self.assertEqual(disk.find("driver").attrib, {"name": "qemu", "type": "qcow2"})
                self.assertEqual(disk.find("source").get("file"), str(path))
                self.assertIn(f"qemu-img create -f qcow2 {path} {size}", calls)
                self.assertTrue(path.is_file())
        self.assertEqual(sum(line.startswith("qemu-img ") for line in calls), 6)

    def test_invalid_data_sizes_fail_before_commands(self) -> None:
        for size in ("", "0", "0G", "-1G", "1.5G", "50GB", " 50G", "50G\n", "$(false)"):
            with self.subTest(size=size):
                result = self._create("--data-disk-size", size)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls_file.exists())
                self.assertFalse(self.inventory_file.exists())
                self.assertEqual(list(self.disk_dir.iterdir()), [])

    def test_incompatible_inventory_fails_without_mutation(self) -> None:
        """Opt-in never repairs old single-disk or incompatible inventory domains."""
        domains = self._enable_domain_capture()
        result = self._create()
        self.assertEqual(result.returncode, 0, result.stderr)
        host = json.loads(self.inventory_file.read_text())["hosts"][0]
        domain = domains / f"{host['name']}.xml"
        original = domain.read_text()
        data = self.disk_dir / f"{host['name']}-data.qcow2"
        for fault in (
            "legacy",
            "wrong_source",
            "wrong_bus",
            "wrong_driver",
            "missing_file",
            "symlink",
            "readonly",
            "duplicate",
        ):
            with self.subTest(fault=fault):
                root = ET.fromstring(original)
                if fault != "legacy":
                    device = ET.SubElement(root.find("devices"), "disk", type="file", device="disk")
                    ET.SubElement(device, "source", file=str(data) if fault != "wrong_source" else "/wrong.qcow2")
                    ET.SubElement(device, "target", dev="vdb", bus="sata" if fault == "wrong_bus" else "virtio")
                    ET.SubElement(device, "driver", type="raw" if fault == "wrong_driver" else "qcow2")
                    if fault == "readonly":
                        ET.SubElement(device, "readonly")
                    if fault == "duplicate":
                        root.find("devices").append(ET.fromstring(ET.tostring(device)))
                    if fault == "symlink":
                        data.symlink_to(self.temp / "absent-target")
                    elif fault != "missing_file":
                        data.write_bytes(b"keep")
                domain.write_text(ET.tostring(root, encoding="unicode"))
                before = self._snapshot()
                self.calls_file.write_text("")
                result = self._create("--data-disk-size", "50G")
                self.assertNotEqual(result.returncode, 0, fault)
                self.assertEqual(before, self._snapshot())
                self.assertEqual(domain.read_text(), ET.tostring(root, encoding="unicode"))
                self.assertNotRegex(self.calls_file.read_text(), r"(?m)^(define|start|qemu-img|net-info) ")
                data.unlink(missing_ok=True)

    def test_enabled_collisions_do_not_write_storage(self) -> None:
        self.inventory_file.write_text(json.dumps({"name_prefix": "grp", "hosts": []}))
        for fault in ("domain", "disk", "nvram", "data", "dangling"):
            with self.subTest(fault=fault):
                for number in range(256):
                    suffix = {"disk": ".qcow2", "nvram": "-VARS.fd"}.get(fault, "-data.qcow2")
                    path = self.disk_dir / f"grp-{number:02x}{suffix}"
                    if fault == "dangling":
                        path.symlink_to(self.temp / "absent-target")
                    elif fault != "domain":
                        path.write_bytes(b"keep")
                before = self._snapshot()
                self.calls_file.write_text("")
                result = self._create("--data-disk-size", "50G", VIRSH_COLLISION="true" if fault == "domain" else "")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(before, self._snapshot())
                self.assertNotRegex(self.calls_file.read_text(), r"(?m)^(define|start|qemu-img) ")
                self.assertFalse((self.temp / "absent-target").exists())
                for path in self.disk_dir.iterdir():
                    path.unlink()

    def test_delete_owns_data_file_but_preserves_shared_storage_and_failed_domains(self) -> None:
        domains = self._enable_domain_capture()
        result = self._create("--data-disk-size", "50G")
        self.assertEqual(result.returncode, 0, result.stderr)
        host = json.loads(self.inventory_file.read_text())["hosts"][0]
        domain = domains / f"{host['name']}.xml"
        root = ET.fromstring(domain.read_text())
        shared = self.disk_dir / "shared-data.qcow2"
        shared.write_bytes(b"keep")
        disk = ET.SubElement(root.find("devices"), "disk", type="file", device="disk")
        ET.SubElement(disk, "source", file=str(shared))
        domain.write_text(ET.tostring(root, encoding="unicode"))
        before = self._snapshot()
        result = self._delete(host["name"], VIRSH_UNDEFINE_BROKEN="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self._snapshot())
        result = self._delete(host["name"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.disk_dir.iterdir()), [shared])
        self.assertEqual(shared.read_bytes(), b"keep")

    def test_create_records_inventory_and_defines_domains(self) -> None:
        result = self._create("--count", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        inventory = json.loads(self.inventory_file.read_text())
        self.assertRegex(inventory["name_prefix"], r"^virtual-bmh-[0-9a-f]{8}$")
        self.assertEqual(len(inventory["hosts"]), 2)

        prefix = inventory["name_prefix"]
        names = []
        for index, host in enumerate(inventory["hosts"]):
            # The name suffix is a random hex byte that also forms the last
            # MAC address byte.
            self.assertRegex(host["name"], rf"^{prefix}-[0-9a-f]{{2}}$")
            token = host["name"][len(prefix) + 1 :]
            names.append(host["name"])
            self.assertEqual(host["boot_mac"], f"52:54:00:bb:cc:{token}")
            self.assertEqual(host["uuid"], f"uuid-of-{host['name']}")
            self.assertEqual(
                host["bmc_address"],
                f"redfish-virtualmedia+http://127.0.0.1:8000/redfish/v1/Systems/uuid-of-{host['name']}",
            )
            self.assertEqual(host["cpu"], 4)
            self.assertEqual(host["memory_mib"], 8192)
            self.assertEqual(host["bmc_username"], "admin")
            self.assertEqual(host["bmc_password"], "password")

            domain_xml = (self.capture_dir / f"vm-{index}.xml").read_text()
            self.assertIn(f"<name>{host['name']}</name>", domain_xml)
            self.assertTrue((self.disk_dir / f"{host['name']}.qcow2").exists())
            self.assertTrue((self.disk_dir / f"{host['name']}-VARS.fd").exists())
        self.assertEqual(len(set(names)), 2)

        # Nothing listens on the redfish endpoint: a warning, not a failure.
        self.assertIn("WARNING", result.stderr)

    def test_delete_removes_vm_and_inventory_entry(self) -> None:
        self._create("--count", "2")
        inventory = json.loads(self.inventory_file.read_text())
        deleted_name = inventory["hosts"][0]["name"]
        kept_name = inventory["hosts"][1]["name"]
        # The domain definition references a shared CD-ROM image; only the
        # storage named after the VM may be deleted.
        shared_iso = self.disk_dir / "shared-install.iso"
        shared_iso.write_bytes(b"ISO")

        result = self._delete(deleted_name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        hosts = json.loads(self.inventory_file.read_text())["hosts"]
        self.assertEqual([host["name"] for host in hosts], [kept_name])
        # The disk paths came from the domain definition.
        self.assertFalse((self.disk_dir / f"{deleted_name}.qcow2").exists())
        self.assertFalse((self.disk_dir / f"{deleted_name}-VARS.fd").exists())
        self.assertTrue((self.disk_dir / f"{kept_name}.qcow2").exists())
        self.assertTrue(shared_iso.exists())

        calls = self.calls_file.read_text()
        self.assertIn(f"destroy {deleted_name}", calls)
        self.assertIn(f"undefine {deleted_name} --nvram", calls)

    def test_delete_retains_entry_when_libvirt_deletion_fails(self) -> None:
        self._create("--count", "2")
        inventory = json.loads(self.inventory_file.read_text())
        deleted_name = inventory["hosts"][0]["name"]
        kept_name = inventory["hosts"][1]["name"]

        result = self._delete(deleted_name, VIRSH_UNDEFINE_BROKEN="true")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("could not delete VM(s) from libvirt", result.stderr)
        # The inventory keeps describing what actually exists, and the
        # disk files of the surviving domain are kept.
        hosts = json.loads(self.inventory_file.read_text())["hosts"]
        self.assertEqual({host["name"] for host in hosts}, {deleted_name, kept_name})
        self.assertTrue((self.disk_dir / f"{deleted_name}.qcow2").exists())

    def test_delete_cleans_up_libvirt_remnants_of_unknown_names(self) -> None:
        self._create()
        inventory = json.loads(self.inventory_file.read_text())
        prefix = inventory["name_prefix"]
        known_name = inventory["hosts"][0]["name"]
        # In the group's namespace, but no longer in the inventory. Pick a
        # token the created VM does not use, so the name is really unknown.
        known_token = known_name[len(prefix) + 1 :]
        leftover_name = f"{prefix}-{'00' if known_token != '00' else '01'}"

        result = self._delete(leftover_name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The inventory is unchanged; the remnants were removed from libvirt.
        hosts = json.loads(self.inventory_file.read_text())["hosts"]
        self.assertEqual([host["name"] for host in hosts], [known_name])
        calls = self.calls_file.read_text()
        self.assertIn(f"destroy {leftover_name}", calls)
        self.assertIn(f"undefine {leftover_name} --nvram", calls)

    def test_delete_rejects_names_from_other_groups(self) -> None:
        self._create()
        inventory = json.loads(self.inventory_file.read_text())
        prefix = inventory["name_prefix"]
        known_name = inventory["hosts"][0]["name"]
        # A host of an overlapping group ("<prefix>-01") shares this
        # group's prefix but is not one of its VMs.
        foreign_name = f"{prefix}-01-7f"

        result = self._delete(foreign_name)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"refusing to delete '{foreign_name}'", result.stderr)
        # Nothing was touched: the inventory is unchanged and no libvirt
        # call was made for the foreign name.
        hosts = json.loads(self.inventory_file.read_text())["hosts"]
        self.assertEqual([host["name"] for host in hosts], [known_name])
        self.assertNotIn(foreign_name, self.calls_file.read_text())

    def test_create_after_delete_adds_a_fresh_vm(self) -> None:
        self._create("--count", "2")
        inventory = json.loads(self.inventory_file.read_text())
        prefix = inventory["name_prefix"]
        deleted_name = inventory["hosts"][0]["name"]
        kept_name = inventory["hosts"][1]["name"]
        self._delete(deleted_name)

        result = self._create()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hosts = json.loads(self.inventory_file.read_text())["hosts"]
        names = [host["name"] for host in hosts]
        # The kept VM is untouched; the new VM gets a fresh random token.
        self.assertEqual(len(hosts), 2)
        self.assertIn(kept_name, names)
        self.assertEqual(len(set(names)), 2)
        for name in names:
            self.assertRegex(name, rf"^{prefix}-[0-9a-f]{{2}}$")


if __name__ == "__main__":
    unittest.main()

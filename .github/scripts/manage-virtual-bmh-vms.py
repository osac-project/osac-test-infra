#!/usr/bin/env python3
"""Manage the libvirt VMs that back virtual BareMetalHosts.

  create    Create VMs and append them to a raw inventory file.
  delete    Remove VMs from libvirt and from the raw inventory file.

The raw inventory (raw_inventory.py) is passed with --inventory-file.
VMs are named <name_prefix>-<token>: a random hex byte that is also the
last boot-MAC byte, so a group holds at most 256 VMs and a VM's MAC is
readable from its name.

Run directly: python3 .github/scripts/manage-virtual-bmh-vms.py --help
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from raw_inventory import Host, InventoryError, RawInventory, is_group_host_name

VIRSH = ("virsh", "-c", "qemu:///system")
# 300s: a hung libvirtd or qemu-img must fail, not hang the job.
COMMAND_TIMEOUT_SECONDS = 300

# The token is also the last MAC byte: at most 256 VMs per group.
MAC_PREFIX = "52:54:00:bb:cc"
MAX_VMS_PER_GROUP = 256

DEFAULT_CPU = 4
DEFAULT_MEMORY_MIB = 8192
# Assisted needs at least 100 GB; 150 GB was verified locally for worker installs.
DEFAULT_DISK_SIZE = "150G"
# Fedora defaults; VM_EFI_VARS overrides the VARS template (tests, other distros).
EFI_CODE = "/usr/share/OVMF/OVMF_CODE.secboot.fd"
EFI_VARS = os.environ.get("VM_EFI_VARS", "/usr/share/OVMF/OVMF_VARS.fd")
# sushy-tools accepts any credentials; these are recorded in the inventory.
BMC_USERNAME = "admin"
BMC_PASSWORD = "password"

BMC_ADDRESS_TEMPLATE = "redfish-virtualmedia+http://{host}:{port}/redfish/v1/Systems/{uuid}"

# Storage files are named after the VM: <name>.qcow2 and <name>-VARS.fd.
DISK_SUFFIX = ".qcow2"
VARS_SUFFIX = "-VARS.fd"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run_virsh(
    virsh_args: list[str], *, stdin: str | None = None, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*VIRSH, *virsh_args],
        input=stdin,
        text=True,
        check=check,
        capture_output=capture,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def generate_vm_token(hosts: list[Host], prefix: str) -> str:
    """Random two-hex-digit token for a new VM, unique within the group.

    The token is both the VM name suffix and the last MAC address byte:
    a VM's MAC is readable from its name, and the fixed MAC prefix caps
    the group at 256 VMs.
    """
    used = {
        host.name[len(prefix) + 1 :]
        for host in hosts
        if host.name.startswith(f"{prefix}-")
    }
    if len(used) >= MAX_VMS_PER_GROUP:
        fail(f"the group already holds {MAX_VMS_PER_GROUP} VMs (boot MAC address space exhausted)")
    while True:
        token = secrets.token_hex(1)
        if token not in used:
            return token


def render_vm_xml(
    *,
    name: str,
    mac: str,
    cpu: int,
    memory_mib: int,
    disk_path: str,
    vars_path: str,
    efi_code: str,
    network: str,
) -> str:
    # libvirt 10.10+: firmware='efi' plus explicit <loader>/<nvram> fails
    # with "Unable to find 'efi' firmware". The pflash paths already
    # select UEFI.
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{memory_mib}</memory>
  <vcpu>{cpu}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>{efi_code}</loader>
    <nvram>{vars_path}</nvram>
    <boot dev='network'/>
    <boot dev='hd'/>
  </os>
  <cpu mode='host-passthrough' check='none' migratable='on'/>
  <features>
    <acpi/>
    <apic/>
  </features>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <mac address='{mac}'/>
      <source network='{network}'/>
      <model type='virtio'/>
    </interface>
    <console type='pty'/>
  </devices>
  <seclabel type='none'/>
</domain>
"""


def check_network(network: str) -> None:
    result = run_virsh(["net-info", network], check=False, capture=True)
    if result.returncode != 0:
        print(f"ERROR: libvirt network '{network}' not found.", file=sys.stderr)
        listing = run_virsh(["net-list", "--all"], check=False, capture=True)
        print("Available networks:", file=sys.stderr)
        print(listing.stdout, file=sys.stderr)
        sys.exit(1)


def create_vm(
    *,
    name: str,
    mac: str,
    cpu: int,
    memory_mib: int,
    disk_size: str,
    disk_dir: Path,
    network: str,
    redfish_host: str,
    redfish_port: int,
) -> Host:
    disk_path = disk_dir / f"{name}{DISK_SUFFIX}"
    vars_path = disk_dir / f"{name}{VARS_SUFFIX}"

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(disk_path), disk_size],
        check=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    shutil.copyfile(EFI_VARS, vars_path)

    xml = render_vm_xml(
        name=name,
        mac=mac,
        cpu=cpu,
        memory_mib=memory_mib,
        disk_path=str(disk_path),
        vars_path=str(vars_path),
        efi_code=EFI_CODE,
        network=network,
    )
    run_virsh(["define", "/dev/stdin"], stdin=xml)
    run_virsh(["start", name])

    uuid = run_virsh(["domuuid", name], capture=True).stdout.strip()

    return Host(
        name=name,
        uuid=uuid,
        boot_mac=mac,
        bmc_address=BMC_ADDRESS_TEMPLATE.format(host=redfish_host, port=redfish_port, uuid=uuid),
        bmc_username=BMC_USERNAME,
        bmc_password=BMC_PASSWORD,
        cpu=cpu,
        memory_mib=memory_mib,
    )


def delete_vm(name: str) -> bool:
    """Remove a VM from libvirt and delete the storage it owns.

    The storage paths come from the domain's own definition (virsh
    dumpxml) — the caller does not know where the disks live — but only
    files named after this VM following the tool's own scheme are
    removed: a group-named domain may not have been created by this
    tool, and its definition can reference shared images that must
    survive the deletion.

    Returns True when the domain is gone from libvirt afterwards. A VM
    that is already gone is fine (mirrors teardown.sh); a deletion that
    fails for a real reason must not be reported as success, and its
    disk files are kept — the domain may still be using them.
    """
    disk_paths = []
    dump = run_virsh(["dumpxml", name], check=False, capture=True)
    if dump.returncode == 0:
        root = ET.fromstring(dump.stdout)
        disk_paths = [source.get("file") for source in root.findall(".//disk/source") if source.get("file")]
        nvram = root.find("./os/nvram")
        if nvram is not None and nvram.text:
            disk_paths.append(nvram.text)

    for virsh_args in (["destroy", name], ["undefine", name, "--nvram"]):
        result = run_virsh(virsh_args, check=False, capture=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            print(f"  {name}: virsh {' '.join(virsh_args)} reported: {detail}")
    if run_virsh(["domuuid", name], check=False, capture=True).returncode == 0:
        # Still alive: keep the files, report failure.
        return False
    # --nvram removes the NVRAM file; the extra unlink covers whatever
    # libvirt left behind.
    owned_storage = {f"{name}{DISK_SUFFIX}", f"{name}{VARS_SUFFIX}"}
    for path in disk_paths:
        if Path(path).name in owned_storage:
            Path(path).unlink(missing_ok=True)
    return True


def warn_if_redfish_unreachable(host: str, port: int) -> None:
    """Best-effort check that the emulator serves the created VMs.

    Warning only: the caller may legitimately create VMs before starting
    sushy-tools. The BareMetalHost flow surfaces a dead emulator anyway.
    """
    url = f"http://{host}:{port}/redfish/v1/Systems/"
    try:
        with urllib.request.urlopen(url, timeout=5):
            pass
    except OSError:
        print(f"WARNING: sushy-emulator not responding at {url} (is setup-sushy-tools.sh running?)", file=sys.stderr)


def cmd_create(args: argparse.Namespace) -> None:
    check_network(args.network)

    inventory = RawInventory.load(args.inventory_file)
    name_prefix = inventory.ensure_name_prefix(args.inventory_file)

    args.disk_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.disk_dir, 0o755)

    print(f"==> Creating {args.count} VM(s) on network {args.network}...")
    created: list[str] = []
    for _ in range(args.count):
        token = generate_vm_token(inventory.hosts, name_prefix)
        name = f"{name_prefix}-{token}"
        mac = f"{MAC_PREFIX}:{token}"
        print(f"  Creating VM: {name} (MAC: {mac}, {args.cpu} CPU, {args.memory_mib} MiB)...")
        inventory.add_host(
            create_vm(
                name=name,
                mac=mac,
                cpu=args.cpu,
                memory_mib=args.memory_mib,
                disk_size=args.disk_size,
                disk_dir=args.disk_dir,
                network=args.network,
                redfish_host=args.redfish_host,
                redfish_port=args.redfish_port,
            )
        )
        # Save after each VM: a crash leaves an accurate inventory.
        inventory.save(args.inventory_file)
        created.append(name)

    print(f"VMs created: {' '.join(created)}")
    print(f"Raw inventory updated: {args.inventory_file}")
    warn_if_redfish_unreachable(args.redfish_host, args.redfish_port)


def cmd_delete(args: argparse.Namespace) -> None:
    if not args.inventory_file.exists():
        fail(f"raw inventory file '{args.inventory_file}' not found")
    inventory = RawInventory.load(args.inventory_file)

    known_names = inventory.host_names()
    unknown_names = [name for name in args.names if name not in known_names]
    for name in unknown_names:
        if not is_group_host_name(name, inventory.name_prefix):
            # Shared libvirt: never touch another group's VMs.
            fail(f"refusing to delete '{name}': not a VM of group '{inventory.name_prefix}'")
        print(f"  {name}: not in the inventory, cleaning up libvirt remnants only...")

    print("==> Deleting VM(s)...")
    deleted_names = []
    failed_names = []
    for host in inventory.hosts:
        if host.name in args.names:
            if delete_vm(host.name):
                deleted_names.append(host.name)
            else:
                # VM still exists: keep the entry.
                failed_names.append(host.name)
    for name in unknown_names:
        if not delete_vm(name):
            failed_names.append(name)

    inventory.remove_hosts(deleted_names)
    inventory.save(args.inventory_file)
    print(f"Raw inventory updated: {args.inventory_file}")
    if failed_names:
        fail(f"could not delete VM(s) from libvirt: {' '.join(failed_names)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create VMs and add them to the raw inventory")
    create.add_argument("--inventory-file", type=Path, required=True, help="raw inventory JSON to update")
    create.add_argument("--network", required=True, help="libvirt network to attach the VMs to")
    create.add_argument("--disk-dir", type=Path, required=True, help="directory for the VM disks")
    create.add_argument(
        "--redfish-host", required=True, help="sushy-emulator host, used to build each VM's BMC address"
    )
    create.add_argument("--redfish-port", type=int, default=8000, help="sushy-emulator port (default: 8000)")
    create.add_argument("--count", type=int, default=1, help="number of VMs to create (default: 1)")
    create.add_argument("--cpu", type=int, default=DEFAULT_CPU, help=f"vCPUs per VM (default: {DEFAULT_CPU})")
    create.add_argument(
        "--memory-mib",
        type=int,
        default=DEFAULT_MEMORY_MIB,
        help=f"memory per VM in MiB (default: {DEFAULT_MEMORY_MIB})",
    )
    create.add_argument(
        "--disk-size",
        default=DEFAULT_DISK_SIZE,
        help=f"qcow2 virtual disk size (default: {DEFAULT_DISK_SIZE})",
    )
    create.set_defaults(func=cmd_create)

    delete = subparsers.add_parser("delete", help="remove VMs from libvirt and the raw inventory")
    delete.add_argument("--inventory-file", type=Path, required=True, help="raw inventory JSON to update")
    delete.add_argument("names", nargs="+", help="VM names to delete")
    delete.set_defaults(func=cmd_delete)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        sys.exit(exc.returncode)
    except subprocess.TimeoutExpired as exc:
        fail(f"command timed out after {exc.timeout} seconds: {' '.join(exc.cmd)}")
    except InventoryError as exc:
        fail(str(exc))
    except FileNotFoundError as exc:
        fail(f"required command not found: {exc}")


if __name__ == "__main__":
    main()

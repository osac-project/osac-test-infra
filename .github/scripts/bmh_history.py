"""Run-owned virtual-BMH event recording and passive serial-log collection."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from raw_inventory import RawInventory, is_group_host_name

MAX_BYTES = 2 * 1024 * 1024
MAX_BACKUPS = 3
MAX_SECONDS = 24 * 60 * 60


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def history_dir(inventory: Path) -> Path:
    return inventory.with_suffix(".history")


def process_identity(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def daemon_config(name: str, config: str) -> dict[str, str]:
    pid = int(Path(f"/run/{name}.pid").read_text())
    command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    if Path(os.fsdecode(command[0])).name != name or any(
        argument in (b"-f", b"--config") or argument.startswith(b"--config=")
        for argument in command[1:]
    ):
        raise ValueError("nonstandard daemon configuration")
    boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                if line.startswith("btime "))
    started = boot + int(process_identity(pid)) / os.sysconf("SC_CLK_TCK")
    path = Path(config)
    if path.stat().st_mtime >= started:
        raise ValueError("daemon predates configuration")
    settings = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if re.match(r"^(stdio_handler|max_size|max_backups)\s*=", line):
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip('"')
    return settings


def serial_capability(connection) -> str:
    try:
        if connection.getLibVersion() < 10010000:
            return "unavailable: libvirt older than verified 10.10 backend"
        daemon = "virtqemud" if Path("/run/virtqemud.pid").exists() else "libvirtd"
        qemu = daemon_config(daemon, "/etc/libvirt/qemu.conf")
        logging = daemon_config("virtlogd", "/etc/libvirt/virtlogd.conf")
        size = int(logging.get("max_size", str(MAX_BYTES)))
        backups = int(logging.get("max_backups", str(MAX_BACKUPS)))
        if qemu.get("stdio_handler", "logd") != "logd" or not 0 < size <= MAX_BYTES:
            return "unavailable: serial backend is not bounded virtlogd"
        if not 0 <= backups <= MAX_BACKUPS:
            return "unavailable: serial backup count exceeds limit"
        return "bounded virtlogd"
    except (OSError, ValueError, StopIteration):
        return "unavailable: could not verify active standard daemon configuration"


class EventWriter:
    def __init__(self, directory: Path, prefix: str):
        self.directory = directory
        self.prefix = prefix
        self.path = directory / "events.jsonl"
        self.size = self.path.stat().st_size if self.path.exists() else 0
        self.truncated = False

    def record(self, domain, kind: str, **detail) -> None:
        if not is_group_host_name(domain.name(), self.prefix):
            return
        entry = dict(timestamp=timestamp(), domain=domain.name(), uuid=domain.UUIDString(), event=kind, **detail)
        data = (json.dumps(entry) + "\n").encode()
        if self.size + len(data) <= MAX_BYTES:
            with self.path.open("ab") as output:
                output.write(data)
            self.size += len(data)
        else:
            self.truncated = True


def listen(directory: Path, prefix: str) -> None:
    import libvirt

    os.umask(0o077)
    libvirt.registerErrorHandler(lambda *_: None, None)
    libvirt.virEventRegisterDefaultImpl()
    connection = libvirt.openReadOnly("qemu:///system")
    writer = EventWriter(directory, prefix)
    callbacks = [
        connection.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                                         lambda conn, dom, event, detail, opaque:
                                         writer.record(dom, "lifecycle", state=event, detail=detail), None),
        connection.domainEventRegisterAny(None, libvirt.VIR_DOMAIN_EVENT_ID_REBOOT,
                                         lambda conn, dom, opaque: writer.record(dom, "reboot"), None),
    ]
    if any(callback < 0 for callback in callbacks):
        raise RuntimeError("event registration failed")
    timer = libvirt.virEventAddTimeout(200, lambda *_: None, None)
    if timer < 0:
        raise RuntimeError("event timer registration failed")
    state = dict(pid=os.getpid(), identity=process_identity(os.getpid()), prefix=prefix,
                 started=timestamp(), serial=serial_capability(connection))
    (directory / "status.json").write_text(json.dumps(state))
    (directory / "ready.json").write_text(json.dumps(state))
    deadline = time.monotonic() + MAX_SECONDS
    try:
        while not (directory / "stop").exists() and time.monotonic() < deadline:
            if libvirt.virEventRunDefaultImpl() < 0:
                break
    finally:
        state.update(stopped=timestamp(), events_truncated=writer.truncated)
        (directory / "status.json").write_text(json.dumps(state))
        for callback in callbacks:
            connection.domainEventDeregisterAny(callback)
        libvirt.virEventRemoveTimeout(timer)
        connection.close()
        (directory / "ready.json").unlink(missing_ok=True)


def start(inventory: Path, prefix: str) -> Path | None:
    directory = history_dir(inventory)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        print("WARNING: BMH history directory already exists; not starting another recorder.", file=sys.stderr)
        return None
    except OSError:
        print("WARNING: BMH history directory unavailable; continuing without history.", file=sys.stderr)
        return None
    os.chmod(directory, 0o700)
    try:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "listen", str(inventory), prefix],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError:
        print("WARNING: BMH recorder could not start; continuing without history.", file=sys.stderr)
        return None
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        try:
            state = json.loads((directory / "ready.json").read_text())
            if state["pid"] == process.pid and state["prefix"] == prefix:
                return directory
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    print("WARNING: BMH event recording unavailable; continuing without history.", file=sys.stderr)
    return None


def pidfd_call(name: str, *arguments: int) -> int:
    library = ctypes.CDLL(None, use_errno=True)
    function = getattr(library, name)
    if name == "pidfd_open":
        function.argtypes = [ctypes.c_int, ctypes.c_uint]
    else:
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(*arguments)
    if result < 0:
        raise OSError(ctypes.get_errno(), name)
    return result


def stop(directory: Path) -> None:
    (directory / "stop").touch()
    try:
        state = json.loads((directory / "ready.json").read_text())
        descriptor = pidfd_call("pidfd_open", state["pid"], 0)
        try:
            if process_identity(state["pid"]) != state["identity"]:
                return
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            if not poller.poll(2000):
                pidfd_call("pidfd_send_signal", descriptor, signal.SIGTERM, 0, 0)
                if not poller.poll(1000):
                    pidfd_call("pidfd_send_signal", descriptor, signal.SIGKILL, 0, 0)
                    poller.poll(1000)
        finally:
            os.close(descriptor)
    except (OSError, ValueError, KeyError, AttributeError):
        pass


def collect(inventory: Path, destination: Path) -> None:
    directory = history_dir(inventory)
    if not directory.is_dir():
        return
    stop(directory)
    prefix = RawInventory.load(inventory).name_prefix
    with tempfile.TemporaryDirectory(prefix="staging-", dir=directory) as temporary:
        staging = Path(temporary)
        stage(directory, prefix, staging)
        runner_temp = Path(os.environ.get("RUNNER_TEMP", str(inventory.parent)))
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("release_gate.py")),
                                 str(staging), "--secret-file", str(runner_temp / "pull-secret.json"),
                                 "--k8s-secrets", str(runner_temp / "osac-secret-inventory/k8s-secrets.json")],
                                capture_output=True, timeout=15, check=False)
        if result.returncode:
            return
        shutil.copytree(staging, destination, dirs_exist_ok=True)


def stage(directory: Path, prefix: str, destination: Path) -> None:
    for path in directory.iterdir():
        serial = re.fullmatch(r"(.+)\.serial\.log(?:\.[0-2])?", path.name)
        if path.name not in ("events.jsonl", "status.json") and not (serial and is_group_host_name(serial[1], prefix)):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as source:
                data = source.read(MAX_BYTES)
        except PermissionError:
            result = subprocess.run(["sudo", "-n", "head", "-c", str(MAX_BYTES), "--", str(path)],
                                    capture_output=True, timeout=2, check=False)
            if result.returncode:
                continue
            data = result.stdout
        name = path.name.replace(".log.", ".part-")
        if serial and path.suffix != ".log":
            name += ".log"
        (destination / name).write_bytes(data)


def cleanup(inventory: Path) -> None:
    directory = history_dir(inventory)
    if not directory.is_dir():
        return
    stop(directory)
    prefix = RawInventory.load(inventory).name_prefix
    result = subprocess.run(["virsh", "-c", "qemu:///system", "list", "--all", "--name"],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode or any(is_group_host_name(name, prefix) for name in result.stdout.splitlines()):
        return
    shutil.rmtree(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("listen", "stop", "collect", "cleanup"))
    parser.add_argument("inventory", type=Path)
    parser.add_argument("argument", nargs="?")
    args = parser.parse_args()
    try:
        if args.command == "listen":
            listen(history_dir(args.inventory), args.argument)
        elif args.command == "stop":
            if history_dir(args.inventory).is_dir():
                stop(history_dir(args.inventory))
        elif args.command == "collect":
            collect(args.inventory, Path(args.argument))
        else:
            cleanup(args.inventory)
    except Exception:
        print("WARNING: BMH history operation unavailable.", file=sys.stderr)


if __name__ == "__main__":
    main()

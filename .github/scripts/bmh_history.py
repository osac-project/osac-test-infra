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
MAX_SECONDS = 24 * 60 * 60


def history_dir(inventory: Path) -> Path:
    return inventory.with_suffix(".history")


def process_identity(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def serial_helper(command: str, prefix: str, *arguments: str) -> bytes:
    if not isinstance(prefix, str) or not re.fullmatch(r"virtual-bmh-[0-9a-f]{8}", prefix):
        raise ValueError("invalid history run prefix")
    result = subprocess.run(["sudo", "-n", "/usr/local/sbin/osac-bmh-history", command, prefix, *arguments],
                            capture_output=True, timeout=12 if command == "prepare" else 6, check=True)
    return result.stdout


def serial_capability(prefix: str) -> dict:
    try:
        capability = json.loads(serial_helper("prepare", prefix))
        return dict(serial="bounded virtlogd", **capability)
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(serial="unavailable: installed helper or bounded standard backend unavailable")


def listen(directory: Path, prefix: str) -> None:
    os.umask(0o077)
    state = dict(pid=os.getpid(), identity=process_identity(os.getpid()), prefix=prefix,
                 started=datetime.now(timezone.utc).isoformat(), **serial_capability(prefix))
    (directory / "status.json").write_text(json.dumps(state))
    process = subprocess.Popen(["virsh", "-c", "qemu:///system", "event", "--all", "--loop",
                                "--timestamp", "--timeout", "86400"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C"))
    previous = signal.signal(signal.SIGTERM, lambda *_: (directory / "stop").touch())
    pattern = re.compile(rb"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\+0000: event '(lifecycle|reboot)' for domain '(virtual-bmh-[0-9a-f]{8}-[0-9a-f]{2})'(?:: [^\r\n]*)?\n")
    deadline = time.monotonic() + MAX_SECONDS
    pending, dropping, size = b"", False, 0
    reason = "deadline"
    try:
        (directory / "ready.json").write_text(json.dumps(state))
        with (directory / "events.log").open("wb") as output:
            while time.monotonic() < deadline:
                if (directory / "stop").exists():
                    reason = "stop requested"
                    break
                if not select.select([process.stdout], [], [], 0.1)[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    reason = "CLI exited"
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    line += b"\n"
                    match = pattern.fullmatch(line) if not dropping and len(line) <= 4096 else None
                    dropping = False
                    if not match or not is_group_host_name(match[2].decode(), prefix):
                        continue
                    if size + len(line) > MAX_BYTES:
                        reason = "byte limit"
                        break
                    output.write(line)
                    output.flush()
                    size += len(line)
                    if match[1] == b"lifecycle" and line.endswith((b": Defined Added\n", b": Defined Updated\n")):
                        (directory / (match[2].decode() + ".defined")).touch()
                if reason == "byte limit":
                    break
                if len(pending) > 4096:
                    pending, dropping = b"", True
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        process.stdout.close()
        signal.signal(signal.SIGTERM, previous)
        state.update(stopped=datetime.now(timezone.utc).isoformat(), event_stop=reason)
        (directory / "status.tmp").write_text(json.dumps(state))
        (directory / "status.tmp").replace(directory / "status.json")
        (directory / "ready.json").unlink(missing_ok=True)


def defined(directory: Path, name: str, timeout: float = 1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (directory / (name + ".defined")).is_file():
            return True
        if (directory / "stop").exists():
            return False
        time.sleep(0.05)
    return False


def start(inventory: Path, prefix: str) -> Path | None:
    directory = history_dir(inventory)
    try:
        directory.mkdir(mode=0o700)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "listen", str(inventory), prefix],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError:
        print("WARNING: BMH recorder unavailable or history already exists; continuing without another recorder.", file=sys.stderr)
        return None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            state = json.loads((directory / "status.json").read_text())
            if state["pid"] == process.pid and state["prefix"] == prefix:
                process.poll()
                return directory
        except (OSError, ValueError):
            pass
        if process.poll() is not None:
            break
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
    function = getattr(ctypes.CDLL(None, use_errno=True), name)
    function.argtypes = ([ctypes.c_int, ctypes.c_uint] if name == "pidfd_open" else
                         [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint])
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
    if not isinstance(prefix, str) or not re.fullmatch(r"virtual-bmh-[0-9a-f]{8}", prefix):
        return
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
        if path.name not in ("events.log", "status.json"):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as source:
                data = source.read(MAX_BYTES)
        except OSError:
            continue
        (destination / path.name).write_bytes(data)
    try:
        names = json.loads(serial_helper("list", prefix))
        if not isinstance(names, list) or len(names) > 1024:
            raise ValueError("invalid serial listing")
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(re.escape(prefix) + r"-[0-9a-f]{2}\.serial\.log(?:\.[012])?", name):
                raise ValueError("invalid serial name")
            data = serial_helper("read", prefix, name)
            artifact_name = name.replace(".log.", ".part-")
            if not artifact_name.endswith(".log"):
                artifact_name += ".log"
            (destination / artifact_name).write_bytes(data[:MAX_BYTES])
    except (OSError, ValueError, subprocess.SubprocessError):
        (destination / "serial-collection-status.txt").write_text("Serial collection incomplete: scoped helper unavailable or read rejected.\n")


def cleanup(inventory: Path) -> None:
    directory = history_dir(inventory)
    if directory.is_dir():
        stop(directory)
    prefix = RawInventory.load(inventory).name_prefix
    result = subprocess.run(["virsh", "-c", "qemu:///system", "list", "--all", "--name"],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode or any(is_group_host_name(name, prefix) for name in result.stdout.splitlines()):
        return
    try:
        serial_helper("cleanup", prefix)
    except (OSError, subprocess.SubprocessError):
        return
    if directory.is_dir():
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

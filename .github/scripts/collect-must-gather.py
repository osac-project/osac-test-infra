#!/usr/bin/env python3
"""Collect a bounded, plaintext-only management-cluster diagnostic baseline."""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 20000
OMISSION_REASONS = {
    "link-or-special",
    "unreadable",
    "oversized",
    "archive-or-opaque",
    "binary",
    "unsupported-name",
    "total-size-limit",
}
DIRECTORY_NAME = re.compile(r"dir-[0-9a-f]{64}")
FILE_NAME = re.compile(r"file-[0-9a-f]{64}\.(log|txt|yaml|json|jsonl)")
PUBLIC_DIRECTORIES = {"host_service_logs", "masters", "workers"}
PUBLIC_FILES = {"kubelet_service.log", "crio_service.log"}
ENCODED_VALUE = re.compile(rb"[A-Za-z0-9+/_-]{32,}={0,2}")
TEXT_SUFFIXES = {".log", ".txt", ".yaml", ".json", ".jsonl", ".yml", ".stderr", ".html", ".logs"}
TEXT_NAMES = {"version", "timestamp"}
MAGICS = (
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"PK",
    b"\x28\xb5\x2f\xfd",
    b"7z\xbc\xaf",
    b"Rar!",
    b"%PDF",
    b"SQLite format",
)
ACTIVE: subprocess.Popen | None = None
CREATION = re.compile(
    rb"\[must-gather      \] OUT ([0-9TZ:.+-]+) "
    rb"(namespace/openshift-must-gather-[a-z0-9]{5}|"
    rb"clusterrolebinding.rbac.authorization.k8s.io/must-gather-[a-z0-9]{5}) created\n"
)
VERSION = re.compile(r"v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}")


class PrivateCapture:
    def __init__(self, records: bool = False) -> None:
        self.records = records
        self.data = bytearray()
        self.created: list[tuple[str, str]] = []
        self.invalid = False

    def drain(self, stream: object) -> None:
        continued = False
        while chunk := stream.readline(8193):
            if self.records:
                if not continued and len(chunk) <= 8192 and (match := CREATION.fullmatch(chunk)):
                    if len(self.created) < 2:
                        self.created.append((match[1].decode("ascii"), match[2].decode("ascii")))
                    else:
                        self.invalid = True
                continued = not chunk.endswith(b"\n")
            elif len(self.data) + len(chunk) <= 1024 * 1024:
                self.data.extend(chunk)
            else:
                self.invalid = True


def remove(path: pathlib.Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def stop_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        time.sleep(0.1)
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def interrupted(_number: int, _frame: object) -> None:
    if ACTIVE is not None:
        stop_group(ACTIVE)
    raise InterruptedError


def run(
    command: list[str],
    seconds: float,
    deadline: float,
    env: dict | None = None,
    stdout: object = subprocess.DEVNULL,
    capture: PrivateCapture | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    global ACTIVE
    duration = min(seconds, deadline - time.monotonic() - 0.3)
    if duration <= 0 or (cancelled is not None and cancelled()):
        return 124
    try:
        ACTIVE = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE if capture is not None else stdout,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        reader = None
        if capture is not None:
            reader = threading.Thread(target=capture.drain, args=(ACTIVE.stdout,), daemon=True)
            reader.start()
        try:
            if cancelled is not None and cancelled():
                return 124
            return ACTIVE.wait(timeout=duration)
        except subprocess.TimeoutExpired:
            return 124
        finally:
            stop_group(ACTIVE)
            if reader is not None:
                reader.join(timeout=0.2)
                if reader.is_alive():
                    capture.invalid = True
                else:
                    ACTIVE.stdout.close()
    except InterruptedError:
        raise
    except OSError:
        return 127
    finally:
        ACTIVE = None


def api(arguments: list[str], deadline: float) -> dict:
    capture = PrivateCapture()
    if run(["oc", "--request-timeout=2s", *arguments, "-o", "json"], 2.5, deadline, capture=capture) != 0:
        return {}
    try:
        document = json.loads(capture.data, object_pairs_hook=unique_mapping)
        return document if isinstance(document, dict) and not capture.invalid else {}
    except (ValueError, UnicodeError, RecursionError):
        return {}


def timestamp(value: str) -> float:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def owned_namespace(records: PrivateCapture, started: float, ended: float, deadline: float) -> tuple[str, str] | None:
    if records.invalid or len(records.created) != 2:
        return None
    namespace, binding = (record[1] for record in records.created)
    if not namespace.startswith("namespace/") or not binding.startswith("clusterrolebinding."):
        return None
    try:
        if not all(started - 1 <= timestamp(record[0]) <= ended + 1 for record in records.created):
            return None
        name = namespace.split("/", 1)[1]
        metadata = api(["get", namespace], deadline).get("metadata", {})
        role = api(["get", binding], deadline)
        role_metadata = role.get("metadata", {})
        uid = metadata.get("uid", "")
        if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", uid):
            return None
        for item, expected, prefix in (
            (metadata, name, "openshift-must-gather-"),
            (role_metadata, binding.split("/", 1)[1], "must-gather-"),
        ):
            if (
                item.get("name") != expected
                or item.get("generateName") != prefix
                or item.get("deletionTimestamp")
                or item.get("annotations", {}).get("oc.openshift.io/command") != "oc adm must-gather"
                or not started - 1 <= timestamp(item.get("creationTimestamp", "")) <= ended + 1
            ):
                return None
        labels = metadata.get("labels", {})
        if labels.get("security.openshift.io/scc.podSecurityLabelSync") != "false":
            return None
        if labels.get("openshift.io/run-level") != "0" or any(
            labels.get(f"pod-security.kubernetes.io/{level}") != "privileged" for level in ("enforce", "audit", "warn")
        ):
            return None
        if metadata.get("annotations", {}).get("openshift.io/node-selector") != "":
            return None
        if role_metadata.get("ownerReferences") != [
            {"apiVersion": "v1", "kind": "Namespace", "name": name, "uid": uid}
        ]:
            return None
        if role.get("subjects") != [{"kind": "ServiceAccount", "name": "default", "namespace": name}] or role.get(
            "roleRef"
        ) != {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"}:
            return None
        return name, uid
    except (ValueError, TypeError, AttributeError):
        return None


def same_namespace(identity: tuple[str, str], deadline: float) -> bool:
    metadata = api(["get", f"namespace/{identity[0]}"], deadline).get("metadata", {})
    return (
        isinstance(metadata, dict)
        and metadata.get("name") == identity[0]
        and metadata.get("uid") == identity[1]
        and not metadata.get("deletionTimestamp")
    )


def recover(root: pathlib.Path, identity: tuple[str, str], deadline: float, cancelled: Callable[[], bool]) -> str:
    if cancelled():
        return "copy-incomplete"
    if not same_namespace(identity, deadline):
        return "ownership-unavailable"
    if cancelled():
        return "copy-incomplete"
    pods = api(["get", "pods", "-n", identity[0]], deadline).get("items", [])
    if not isinstance(pods, list) or not 1 <= len(pods) <= 16:
        return "pods-unavailable"
    result = "copied"
    visited = set()
    for index, pod in enumerate(pods):
        if cancelled():
            return "copy-incomplete"
        if not isinstance(pod, dict):
            result = "copy-incomplete"
            continue
        metadata = pod.get("metadata", {})
        specification = pod.get("spec", {})
        status = pod.get("status", {})
        if not all(isinstance(item, dict) for item in (metadata, specification, status)):
            result = "copy-incomplete"
            continue
        name = metadata.get("name", "")
        labels = metadata.get("labels", {})
        containers = specification.get("containers", [])
        states = status.get("containerStatuses", [])
        if not all(
            isinstance(items, list) and all(isinstance(item, dict) for item in items) for items in (containers, states)
        ):
            result = "copy-incomplete"
            continue
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"must-gather-[a-z0-9]{5}", name)
            or name in visited
            or metadata.get("namespace") != identity[0]
            or not isinstance(labels, dict)
            or labels.get("app") != "must-gather"
            or [container.get("name") for container in containers] not in (["gather", "copy"], ["copy", "gather"])
            or not any(
                item.get("name") == "copy"
                and isinstance(item.get("state"), dict)
                and isinstance(item["state"].get("running"), dict)
                for item in states
            )
        ):
            result = "copy-incomplete"
            continue
        visited.add(name)
        target = root / ("recovery-" + hashlib.sha256(name.encode()).hexdigest())
        target.mkdir(mode=0o700)
        if cancelled():
            return "copy-incomplete"
        copied = run(
            ["oc", "rsync", "-n", identity[0], "--container=copy", f"{name}:/must-gather/", str(target)],
            (deadline - time.monotonic()) / (len(pods) - index),
            deadline,
            cancelled=cancelled,
        )
        if copied != 0:
            result = "copy-incomplete"
    return result


def cleanup_namespace(identity: tuple[str, str], deadline: float) -> str:
    if not same_namespace(identity, deadline):
        return "ownership-unavailable"
    with tempfile.TemporaryDirectory(prefix="osac-must-gather-delete-") as temporary:
        options = pathlib.Path(temporary) / "options.json"
        options.write_text(
            json.dumps({"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": identity[1]}})
        )
        options.chmod(0o600)
        result = run(
            ["oc", "--request-timeout=2s", "delete", f"--raw=/api/v1/namespaces/{identity[0]}", "-f", str(options)],
            deadline - time.monotonic(),
            deadline,
        )
    return "deletion-requested" if result == 0 else "deletion-incomplete"


def standard_gather(root: pathlib.Path, deadline: float) -> tuple[int, dict]:
    remaining = max(0, deadline - time.monotonic())
    cleanup_budget = min(10, remaining * 0.15)
    recovery_budget = remaining * 0.35
    client_deadline = deadline - cleanup_budget - recovery_budget
    copy_deadline = deadline - cleanup_budget
    details = {"client": "unknown", "recovery": "not-needed", "cleanup": "ownership-unavailable"}
    cancelled = False
    stopping = False

    def defer_interrupt(_number: int, _frame: object) -> None:
        nonlocal cancelled, stopping
        cancelled = True
        if ACTIVE is not None and not stopping:
            stopping = True
            try:
                stop_group(ACTIVE)
            finally:
                stopping = False

    previous = {number: signal.signal(number, defer_interrupt) for number in (signal.SIGTERM, signal.SIGINT)}
    records = PrivateCapture(records=True)
    identity = None
    result = 124
    try:
        version = api(["version", "--client"], min(client_deadline, time.monotonic() + 2.5))
        client_version = version.get("releaseClientVersion", "")
        if not client_version and isinstance(version.get("clientVersion"), dict):
            client_version = version["clientVersion"].get("gitVersion", "")
        if isinstance(client_version, str) and VERSION.fullmatch(client_version):
            details["client"] = client_version
        started = time.time()
        native_timeout = max(1, int((client_deadline - time.monotonic() - 0.3) * 800))
        if not cancelled:
            result = run(
                ["oc", "adm", "must-gather", f"--dest-dir={root}", "--keep", f"--timeout={native_timeout}ms"],
                client_deadline - time.monotonic(),
                client_deadline,
                capture=records,
                cancelled=lambda: cancelled,
            )
        identity = owned_namespace(records, started, time.time(), deadline)
        if result != 0 or cancelled:
            if cancelled:
                details["recovery"] = "copy-incomplete"
            elif identity:
                details["recovery"] = recover(root, identity, copy_deadline, lambda: cancelled)
            else:
                details["recovery"] = "ownership-unavailable"
    finally:
        try:
            if identity is not None:
                details["cleanup"] = cleanup_namespace(identity, deadline)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    print(f"Management must-gather recovery: {details['recovery']}; cleanup: {details['cleanup']}", flush=True)
    print(f"Management must-gather host client: {details['client']}", flush=True)
    if details["cleanup"] != "deletion-requested":
        print(
            "::warning::Must-gather resources may remain privileged; existing-cluster jobs require operator cleanup",
            flush=True,
        )
    if cancelled:
        raise InterruptedError
    return result, details


def archive_bytes(data: bytes) -> bool:
    return data.startswith(MAGICS) or data[257:262] == b"ustar"


def encoded_archive(data: bytes, depth: int = 0) -> bool:
    if depth >= 3:
        return ENCODED_VALUE.search(data) is not None
    for match in ENCODED_VALUE.finditer(data):
        value = match.group()
        try:
            decoded = base64.b64decode(value + b"=" * (-len(value) % 4), altchars=b"-_", validate=True)
        except binascii.Error:
            continue
        if archive_bytes(decoded) or encoded_archive(decoded, depth + 1):
            return True
    return False


def text_bytes(path: pathlib.Path) -> tuple[bytes | None, str]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return None, "link-or-special"
    if not metadata.st_mode & 0o444:
        return None, "unreadable"
    if metadata.st_size > MAX_FILE_BYTES:
        return None, "oversized"
    with path.open("rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return None, "oversized"
    if archive_bytes(data):
        return None, "archive-or-opaque"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "binary"
    if any(unicodedata.category(character).startswith("C") and character not in "\t\n\r" for character in text):
        return None, "binary"
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
        return None, "unsupported-name"
    if encoded_archive(data):
        return None, "archive-or-opaque"
    return data, ""


def normalized_path(relative: pathlib.Path) -> pathlib.Path:
    directories = [
        part if part in PUBLIC_DIRECTORIES else "dir-" + hashlib.sha256(os.fsencode(part)).hexdigest()
        for part in relative.parts[:-1]
    ]
    suffix = relative.suffix.lower()
    suffix = {".logs": ".log", ".yml": ".yaml"}.get(suffix, suffix)
    if suffix not in {".log", ".txt", ".yaml", ".json", ".jsonl"}:
        suffix = ".txt"
    name = (
        relative.name
        if relative.name in PUBLIC_FILES
        else "file-" + hashlib.sha256(os.fsencode(relative.name)).hexdigest() + suffix
    )
    return pathlib.Path(*directories, name)


def unique_mapping(pairs: list[tuple[str, object]]) -> dict[str, object]:
    mapping = dict(pairs)
    if len(mapping) != len(pairs):
        raise ValueError("duplicate diagnostic metadata key")
    return mapping


def check_metadata(root: pathlib.Path) -> None:
    documents = []
    for name in ("omissions.json", "collection-status.json"):
        content, _reason = text_bytes(root / name)
        if content is None or len(content) > 4096:
            raise ValueError("invalid diagnostic metadata")
        documents.append(json.loads(content, object_pairs_hook=unique_mapping))
    omissions, status = documents
    if not isinstance(omissions, dict) or any(
        reason not in OMISSION_REASONS or type(count) is not int or not 0 <= count <= MAX_ENTRIES
        for reason, count in omissions.items()
    ):
        raise ValueError("invalid omission metadata")
    if not isinstance(status, dict) or set(status) not in (
        {"status", "reason"},
        {"status", "reason", "client", "recovery", "cleanup"},
    ):
        raise ValueError("invalid collection metadata")
    if "client" in status and (
        not isinstance(status["client"], str)
        or (status["client"] != "unknown" and not VERSION.fullmatch(status["client"]))
        or status["recovery"]
        not in ("not-needed", "ownership-unavailable", "pods-unavailable", "copied", "copy-incomplete")
        or status["cleanup"] not in ("ownership-unavailable", "deletion-requested", "deletion-incomplete")
    ):
        raise ValueError("invalid recovery metadata")
    if status["status"] not in ("complete", "partial", "timed-out", "skipped") or status["reason"] not in (
        "none",
        "kubeconfig-unavailable",
        "cluster-unavailable",
        "collector-did-not-complete",
    ):
        raise ValueError("invalid collection metadata")


def normalize(root: pathlib.Path, check: bool = False) -> None:
    if root.resolve() != root.absolute() or not root.is_dir():
        raise ValueError("invalid diagnostic root")
    if check:
        check_metadata(root)
    counts: collections.Counter = collections.Counter()
    total = 0
    entries = 0
    staging = (
        contextlib.nullcontext(None) if check else tempfile.TemporaryDirectory(prefix="osac-normalized-", dir="/tmp")
    )
    with staging as temporary:
        normalized = pathlib.Path(temporary) if temporary is not None else None

        def unreadable_directory(_error: OSError) -> None:
            if check:
                raise ValueError("unreadable diagnostic directory")
            counts["unreadable"] += 1

        for directory, subdirs, files in os.walk(root, followlinks=False, onerror=unreadable_directory):
            base = pathlib.Path(directory)
            if check and base == root:
                files = [name for name in files if name not in {"omissions.json", "collection-status.json"}]
            if check and any(name not in PUBLIC_DIRECTORIES and not DIRECTORY_NAME.fullmatch(name) for name in subdirs):
                raise ValueError("unsafe diagnostic directory name")
            entries += len(subdirs)
            if entries > MAX_ENTRIES:
                raise ValueError("too many diagnostic entries")
            links = [name for name in subdirs if (base / name).is_symlink()]
            subdirs[:] = [name for name in subdirs if name not in links]
            for name in files + links:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ValueError("too many diagnostic entries")
                path = base / name
                relative = path.relative_to(root)
                if (
                    check
                    and not (base == root and name in {"omissions.json", "collection-status.json"})
                    and name not in PUBLIC_FILES
                    and not FILE_NAME.fullmatch(name)
                ):
                    raise ValueError("unsafe diagnostic file name")
                try:
                    content, reason = text_bytes(path)
                except OSError:
                    content, reason = None, "unreadable"
                if content is not None and total + len(content) > MAX_TOTAL_BYTES:
                    content, reason = None, "total-size-limit"
                if content is None:
                    if check:
                        raise ValueError("unsafe diagnostic entry")
                    counts[reason] += 1
                    continue
                total += len(content)
                if check:
                    continue
                target = normalized / normalized_path(relative)
                if target.exists():
                    raise ValueError("diagnostic name collision")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        if check:
            return
        (normalized / "omissions.json").write_text(json.dumps(dict(counts), sort_keys=True) + "\n")
        remove(root)
        shutil.move(str(normalized), root)


def collect(artifacts: pathlib.Path, seconds: int) -> int:
    deadline = time.monotonic() + seconds
    if artifacts.resolve() != artifacts.absolute():
        raise ValueError("invalid artifact directory")
    artifacts.mkdir(parents=True, exist_ok=True)
    marker = artifacts / ".redaction-complete"
    marker.unlink(missing_ok=True)
    root = artifacts / "mgmt-cluster/must-gather"
    if root.parent.is_symlink():
        raise ValueError("invalid management diagnostic directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    if root.exists() or root.is_symlink():
        remove(root)
    root.mkdir(mode=0o700)
    workspace = pathlib.Path(os.environ["GITHUB_WORKSPACE"])
    gather = str(workspace / "scripts/gather-osac-logs.sh")
    reserve = max(3, seconds * 0.3)
    kubeconfig = pathlib.Path(os.environ.get("KUBECONFIG", "/nonexistent"))
    available = kubeconfig.is_file() and kubeconfig.stat().st_size > 0
    if available:
        run(
            ["bash", gather, str(artifacts)],
            seconds * 0.3,
            deadline,
            os.environ | {"OSAC_GATHER_COLLECT_ONLY": "true", "OSAC_GATHER_REDACT_ONLY": "false"},
        )
    status, reason = "skipped", "kubeconfig-unavailable"
    details = {"client": "unknown", "recovery": "not-needed", "cleanup": "ownership-unavailable"}
    if available:
        ready = run(["oc", "--request-timeout=5s", "get", "--raw=/readyz"], min(5, seconds * 0.05), deadline)
        if ready == 0:
            result, details = standard_gather(root, deadline - reserve)
            status = "complete" if result == 0 else "timed-out" if result == 124 else "partial"
            reason = "none" if result == 0 else "collector-did-not-complete"
        else:
            reason = "cluster-unavailable"
    print(f"Management must-gather: {status} ({reason})", flush=True)
    helper = str(pathlib.Path(__file__).resolve())
    if run([sys.executable, "-I", helper, str(root), "--normalize"], reserve / 3, deadline) != 0:
        return 1
    (root / "collection-status.json").write_text(json.dumps({"status": status, "reason": reason, **details}) + "\n")
    inventory = pathlib.Path(os.environ["RUNNER_TEMP"]) / "osac-secret-inventory/k8s-secrets.json"
    inventory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    inventory.unlink(missing_ok=True)
    if available:
        descriptor = os.open(inventory, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            result = run(
                ["oc", "get", "secrets", "-n", os.environ.get("E2E_NAMESPACE", "osac-e2e-ci"), "-o", "json"],
                min(15, seconds * 0.1),
                deadline,
                stdout=output,
            )
        if result != 0:
            inventory.unlink(missing_ok=True)
    result = run(
        ["bash", gather, str(artifacts)],
        deadline - time.monotonic() - 0.5,
        deadline,
        os.environ | {"OSAC_GATHER_REDACT_ONLY": "true", "OSAC_GATHER_COLLECT_ONLY": "false"},
    )
    if result != 0:
        print("::warning::Artifact sanitization incomplete; upload remains blocked", flush=True)
        return 1
    marker.touch()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        if args.normalize or args.check:
            normalize(args.directory, check=args.check)
            return 0
        if args.timeout_seconds < 3:
            raise ValueError("collection budget must be at least three seconds")
        return collect(args.directory, args.timeout_seconds)
    except InterruptedError:
        return 143
    except (OSError, ValueError):
        print("::warning::Diagnostic preparation failed; upload remains blocked", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())

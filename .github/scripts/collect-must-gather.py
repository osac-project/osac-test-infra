#!/usr/bin/env python3
"""Collect a bounded, plaintext-only management-cluster diagnostic baseline."""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import contextlib
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
import time
import unicodedata

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
    command: list[str], seconds: float, deadline: float, env: dict | None = None, stdout: object = subprocess.DEVNULL
) -> int:
    global ACTIVE
    duration = min(seconds, deadline - time.monotonic() - 0.3)
    if duration <= 0:
        return 124
    try:
        ACTIVE = subprocess.Popen(command, env=env, stdout=stdout, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            return ACTIVE.wait(timeout=duration)
        except subprocess.TimeoutExpired:
            return 124
        finally:
            stop_group(ACTIVE)
    except InterruptedError:
        raise
    except OSError:
        return 127
    finally:
        ACTIVE = None


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
    directories = ["dir-" + hashlib.sha256(os.fsencode(part)).hexdigest() for part in relative.parts[:-1]]
    suffix = relative.suffix.lower()
    suffix = {".logs": ".log", ".yml": ".yaml"}.get(suffix, suffix)
    if suffix not in {".log", ".txt", ".yaml", ".json", ".jsonl"}:
        suffix = ".txt"
    name = "file-" + hashlib.sha256(os.fsencode(relative.name)).hexdigest() + suffix
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
    if not isinstance(status, dict) or set(status) != {"status", "reason"}:
        raise ValueError("invalid collection metadata")
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
            if check and any(not DIRECTORY_NAME.fullmatch(name) for name in subdirs):
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
    if available:
        ready = run(["oc", "--request-timeout=5s", "get", "--raw=/readyz"], min(5, seconds * 0.05), deadline)
        if ready == 0:
            result = run(
                ["oc", "adm", "must-gather", f"--dest-dir={root}"], deadline - time.monotonic() - reserve, deadline
            )
            status = "complete" if result == 0 else "timed-out" if result == 124 else "partial"
            reason = "none" if result == 0 else "collector-did-not-complete"
        else:
            reason = "cluster-unavailable"
    print(f"Management must-gather: {status} ({reason})", flush=True)
    helper = str(pathlib.Path(__file__).resolve())
    if run([sys.executable, "-I", helper, str(root), "--normalize"], reserve / 3, deadline) != 0:
        return 1
    (root / "collection-status.json").write_text(json.dumps({"status": status, "reason": reason}) + "\n")
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

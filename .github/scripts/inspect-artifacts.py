#!/usr/bin/env python3
"""Opt-in structural artifact preparation and fail-closed private pattern scanning."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
ARCHIVE_PREFIXES = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"PK", b"\x28\xb5\x2f\xfd", b"7z\xbc\xaf", b"Rar!")
ARCHIVE_SUFFIXES = (".tar", ".tgz", ".gz", ".bz2", ".xz", ".zip", ".zst", ".7z", ".rar")
CREDENTIAL_NAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "auth.json",
    ".dockerconfigjson",
    "credentials",
    "credentials.json",
    "token",
    "password",
    "kubeadmin-password",
}


def traversal_error(error: OSError) -> None:
    raise error


def prepare(root: pathlib.Path) -> int:
    withheld = 0
    total = 0
    for directory, subdirs, files in os.walk(root, followlinks=False, onerror=traversal_error):
        base = pathlib.Path(directory)
        for name in subdirs[:]:
            path = base / name
            if path.is_symlink():
                path.unlink()
                subdirs.remove(name)
                withheld += 1
            elif name == ".git":
                shutil.rmtree(path)
                subdirs.remove(name)
                withheld += 1
        for name in files:
            path = base / name
            mode = path.lstat()
            if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1 or mode.st_size > MAX_FILE_BYTES:
                path.unlink()
                withheld += 1
                continue
            total += mode.st_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("artifact inspection budget exceeded")
            data = path.read_bytes()
            try:
                data.decode("utf-8")
                text = b"\x00" not in data
            except UnicodeDecodeError:
                text = False
            if (
                not text
                or name.lower().endswith((*ARCHIVE_SUFFIXES, ".key", ".kubeconfig"))
                or "kubeconfig" in name.lower()
                or name.lower() in CREDENTIAL_NAMES
                or (name.lower() == "config.json" and base.name == ".docker")
                or b"PRIVATE KEY-----" in data
                or data.startswith(ARCHIVE_PREFIXES)
                or data[257:262] == b"ustar"
            ):
                path.unlink()
                withheld += 1
    return withheld


def validate_paths(root: pathlib.Path, marker: pathlib.Path | None, prepare_only: bool) -> pathlib.Path:
    temporary = pathlib.Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("invalid artifact directory")
    if prepare_only:
        if root.parent != temporary or not root.name.startswith("artifact-collection."):
            raise ValueError("invalid private collection directory")
        if root.is_symlink() or not root.is_dir():
            raise ValueError("invalid private collection directory")
    else:
        relative = root.relative_to(temporary / "osac-artifacts")
        if len(relative.parts) < 2:
            raise ValueError("artifact scan must target a subtree")
        if marker is None or marker.parent != temporary or not marker.name.endswith("-scan-complete"):
            raise ValueError("invalid scan marker")
    for parent in root.parents:
        if parent == temporary:
            break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ValueError("invalid artifact parent")
    return temporary


def scan(root: pathlib.Path, marker: pathlib.Path, temporary: pathlib.Path) -> None:
    marker.unlink(missing_ok=True)
    if not root.exists() and not root.is_symlink():
        return
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("invalid artifact directory")
        withheld = prepare(root)
        with (root / "scan-status.txt").open("w") as stream:
            stream.write(f"uninspectable_files_withheld={withheld}\n")
        engine = shutil.which("podman") or shutil.which("docker")
        if not engine:
            raise ValueError("pattern scanner runtime unavailable")
        config = pathlib.Path(os.environ["GITHUB_WORKSPACE"]) / ".github/scripts/.gitleaks.toml"
        selinux = pathlib.Path("/sys/fs/selinux/enforce")
        suffix = ",Z" if selinux.is_file() and selinux.read_text().strip() == "1" else ""
        with tempfile.TemporaryDirectory(prefix="artifact-scan-", dir=temporary) as private:
            report = pathlib.Path(private) / "findings.json"
            (pathlib.Path(private) / ".gitleaksignore").touch(mode=0o600)
            subprocess.run(
                [
                    engine,
                    "run",
                    "--rm",
                    "--network=none",
                    *(["--userns=keep-id"] if pathlib.Path(engine).name == "podman" and os.geteuid() != 0 else []),
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "-v",
                    f"{root}:/scan:ro{suffix}",
                    "-v",
                    f"{config}:/gitleaks.toml:ro{suffix}",
                    "-v",
                    f"{private}:/out:rw{suffix}",
                    os.environ["GITLEAKS_IMAGE"],
                    "dir",
                    "/scan",
                    "--config=/gitleaks.toml",
                    "--ignore-gitleaks-allow",
                    "--gitleaks-ignore-path=/out/.gitleaksignore",
                    "--report-format=json",
                    "--report-path=/out/findings.json",
                    "--exit-code=0",
                ],
                check=True,
                timeout=180,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            findings = json.loads(report.read_text())
            if not isinstance(findings, list) or findings:
                raise ValueError("pattern scan not clean")
        marker.touch(mode=0o600)
        print("Artifact pattern scan complete; known-secret release gate still required.")
    except (OSError, ValueError, subprocess.SubprocessError):
        if root.is_symlink() or not root.is_dir():
            root.unlink(missing_ok=True)
        else:
            shutil.rmtree(root)
        notice = root.parent / f"{root.name}-withheld.txt"
        notice.unlink(missing_ok=True)
        notice.write_text("Artifact output withheld: inspection or pattern scan did not complete cleanly.\n")
        print("::warning::Artifact output withheld: inspection or pattern scan did not complete cleanly.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=pathlib.Path, required=True)
    parser.add_argument("--marker", type=pathlib.Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    try:
        temporary = validate_paths(args.directory, args.marker, args.prepare_only)
    except (OSError, ValueError):
        parser.error("invalid artifact directory or scan marker")
    if args.prepare_only:
        removed = prepare(args.directory)
        (args.directory / "preparation-status.txt").write_text(f"uninspectable_files_withheld={removed}\n")
    else:
        scan(args.directory, args.marker, temporary)


if __name__ == "__main__":
    main()

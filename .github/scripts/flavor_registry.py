#!/usr/bin/env python3
"""Job-scoped flavor credentials and allowlisted registry diagnostics."""

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


class RegistryError(Exception):
    pass


def diagnostic(message: str) -> None:
    print(f"flavor-registry: {message}", file=sys.stderr, flush=True)


def auths_from_json(content: str | bytes) -> dict:
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError):
        raise RegistryError("auth-malformed") from None
    auths = document.get("auths") if isinstance(document, dict) else None
    if not isinstance(auths, dict):
        raise RegistryError("auth-malformed")
    for key, entry in auths.items():
        if not key or not isinstance(entry, dict):
            raise RegistryError("auth-malformed")
        if any(not isinstance(entry.get(field, ""), str) for field in ("auth", "identitytoken")):
            raise RegistryError("auth-malformed")
        try:
            base64.b64decode(entry.get("auth", "").replace("\r", "").replace("\n", ""), validate=True)
        except ValueError:
            raise RegistryError("auth-malformed") from None
    return auths


def merge_objects(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            merge_objects(base[key], value)
        else:
            base[key] = value
    return base


def initialize() -> None:
    auths = auths_from_json(os.environ.get("PULL_SECRET", ""))
    merge_objects(auths, auths_from_json(os.environ.get("SNAPSHOT_REGISTRY_AUTH", "")))
    user = os.environ.get("QUAY_USER", "")
    password = os.environ.get("QUAY_PASSWORD", "")
    if not user or not password:
        raise RegistryError("auth-missing")
    merge_objects(auths, {"quay.io/osac-project": {"auth": base64.b64encode(f"{user}:{password}".encode()).decode()}})
    directory = Path(tempfile.mkdtemp(prefix="flavor-registry-", dir=os.environ["RUNNER_TEMP"]))
    authfile = directory / "auth.json"
    try:
        with open(os.environ["GITHUB_ENV"], "a") as environment:
            environment.write(f"FLAVOR_REGISTRY_AUTH_FILE={authfile}\n")
        with open(authfile, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump({"auths": auths}, stream)
    except BaseException:
        authfile.unlink(missing_ok=True)
        directory.rmdir()
        raise
    diagnostic("operation=init source=job-file ready=true directory_mode=0700 file_mode=0600")


def auth_scope(auths: dict, image: str) -> str:
    reference = image.split("@", 1)[0]
    registry, separator, repository = reference.partition("/")
    if not separator:
        return "unresolved-reference"
    key = registry + "/" + repository.rsplit(":", 1)[0]
    candidate = key
    while True:
        if candidate in auths:
            if candidate == registry:
                return "registry"
            return "repository" if candidate == key else "namespace"
        if "/" not in candidate:
            break
        candidate = candidate.rsplit("/", 1)[0]

    def normalize(host: str) -> str:
        return "index.docker.io" if host in ("docker.io", "registry-1.docker.io") else host

    matches = 0
    for key in auths:
        normalized = key
        for scheme in ("http://", "https://"):
            if normalized.startswith(scheme):
                normalized = normalized[len(scheme) :].split("/", 1)[0]
                break
        if normalize(normalized) == normalize(registry):
            matches += 1
    return "legacy-registry" if matches == 1 else "ambiguous-registry" if matches else "unmatched"


def selected_authfile(image: str = "") -> str:
    selected = os.environ.get("FLAVOR_REGISTRY_AUTH_FILE")
    if not selected:
        raise RegistryError("auth-missing")
    authfile = Path(selected)
    try:
        metadata = authfile.lstat()
        parent = authfile.parent.lstat()
        if (
            not authfile.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or metadata.st_uid != parent.st_uid
            or (os.geteuid() != 0 and metadata.st_uid != os.geteuid())
        ):
            raise RegistryError("auth-permissions")
        auths = auths_from_json(authfile.read_bytes())
        if not auths:
            raise RegistryError("auth-missing")
    except FileNotFoundError:
        raise RegistryError("auth-missing") from None
    except PermissionError:
        raise RegistryError("auth-unreadable") from None
    diagnostic(f"source=job-file ready=true entries={len(auths)} uid={os.geteuid()} gid={os.getegid()}")
    diagnostic(f"authfile_scope={auth_scope(auths, image)}")
    return str(authfile)


def failure_category(stderr: str) -> str:
    error = stderr.lower()
    if "sudo:" in error and any(
        signature in error
        for signature in (
            "not allowed to set",
            "not allowed to preserve",
            "not allowed to execute",
            "a password is required",
        )
    ):
        return "execution-policy"
    auth_denied = any(
        signature in error
        for signature in (
            "unauthorized",
            "authentication required",
            "requested access to the resource is denied",
            "status code: 401",
            "status code: 403",
        )
    )
    if re.search(r"\b(?:manifest[ _]unknown|name[ _]unknown|404)\b", error):
        return "image-not-found-or-auth" if auth_denied else "image-not-found"
    if "permission denied" in error:
        return "permission-denied"
    if auth_denied:
        return "auth-denied"
    for category, signatures in (
        ("tls", ("x509", "tls handshake", "certificate")),
        ("network", ("timeout", "timed out", "no such host", "connection refused", "network is unreachable")),
        ("registry-unavailable", ("status code: 500", "status code: 502", "status code: 503", "too many requests")),
    ):
        if any(signature in error for signature in signatures):
            return category
    return "unclassified"


def run_captured(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, errors="replace", check=False, env=env)
    except FileNotFoundError:
        raise RegistryError("tool-missing") from None


def versions() -> None:
    result = run_captured(["skopeo", "--version"])
    version = re.match(r"skopeo version ([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})(?:\s|$)", result.stdout)
    safe_version = version.group(1) if version and result.returncode == 0 else "unknown"
    diagnostic(f"skopeo_version={safe_version} python_version={sys.version_info.major}.{sys.version_info.minor}")


def registry_operation(operation: str, image: str) -> int:
    authfile = selected_authfile(image)
    if not image or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]*", image):
        raise RegistryError("image-invalid")
    versions()
    if operation == "inspect":
        command = ["skopeo", "inspect", "--authfile", authfile, f"docker://{image}", "--format", "{{.Digest}}"]
    else:
        cluster_tool = Path("/usr/local/bin/cluster-tool")
        diagnostic(
            f"target_uid=0 server=local cluster_tool_sha256={hashlib.sha256(cluster_tool.read_bytes()).hexdigest()}"
        )
        command = [
            "sudo",
            "-n",
            "-u",
            "root",
            f"REGISTRY_AUTH_FILE={authfile}",
            "/usr/bin/python3",
            str(cluster_tool),
            "pull",
            image,
            "--server",
            "local",
        ]
    diagnostic(f"operation={operation} context=runner starting=true")
    result = run_captured(command, env={**os.environ, "REGISTRY_AUTH_FILE": authfile})
    category = "ok" if result.returncode == 0 else failure_category(result.stderr)
    diagnostic(
        f"operation={operation} exit={result.returncode} category={category} "
        f"hint={str(bool(result.returncode)).lower()}"
    )
    if result.returncode:
        return 1
    if operation == "inspect":
        digest = result.stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RegistryError("digest-invalid")
        print(digest)
    return 0


def cleanup() -> None:
    selected = os.environ.get("FLAVOR_REGISTRY_AUTH_FILE")
    if not selected:
        return
    authfile = Path(selected)
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve()
    directory = authfile.parent
    if (
        authfile.name != "auth.json"
        or not directory.name.startswith("flavor-registry-")
        or directory.parent.resolve() != temporary
        or directory.is_symlink()
    ):
        raise RegistryError("cleanup-path-invalid")
    authfile.unlink(missing_ok=True)
    if directory.exists():
        directory.rmdir()
    diagnostic("operation=cleanup removed=true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("init", "inspect", "pull", "cleanup"))
    args = parser.parse_args()
    try:
        if args.operation == "init":
            initialize()
        elif args.operation == "cleanup":
            cleanup()
        else:
            return registry_operation(args.operation, os.environ.get("FLAVOR_IMAGE", ""))
    except RegistryError as error:
        diagnostic(f"operation={args.operation} category={error} exit=1")
        return 1
    except (OSError, KeyError, ValueError):
        diagnostic(f"operation={args.operation} category=local-io exit=1")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

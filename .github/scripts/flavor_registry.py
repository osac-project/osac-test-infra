#!/usr/bin/env python3
"""Job-scoped flavor credentials and allowlisted registry diagnostics."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


class RegistryError(Exception):
    pass


def diagnostic(message):
    print(f"flavor-registry: {message}", file=sys.stderr, flush=True)


def auths_from_json(content):
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError):
        raise RegistryError("auth-malformed") from None
    auths = document.get("auths") if isinstance(document, dict) else None
    if not isinstance(auths, dict):
        raise RegistryError("auth-malformed")
    for key, entry in auths.items():
        if not key or not isinstance(entry, dict) or not isinstance(entry.get("auth"), str):
            raise RegistryError("auth-malformed")
        try:
            decoded = base64.b64decode(entry["auth"], validate=True)
        except ValueError:
            raise RegistryError("auth-malformed") from None
        if b":" not in decoded:
            raise RegistryError("auth-malformed")
    return auths


def initialize():
    auths = auths_from_json(os.environ.get("PULL_SECRET", ""))
    auths.update(auths_from_json(os.environ.get("SNAPSHOT_REGISTRY_AUTH", "")))
    user = os.environ.get("QUAY_USER", "")
    password = os.environ.get("QUAY_PASSWORD", "")
    if not user or not password:
        raise RegistryError("auth-missing")
    auths["quay.io/osac-project"] = {"auth": base64.b64encode(f"{user}:{password}".encode()).decode()}
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


def selected_authfile():
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
    return str(authfile)


def failure_category(stderr):
    error = stderr.lower()
    for category, signatures in (
        ("auth-denied", ("unauthorized", "authentication required", "denied", "status code: 401", "status code: 403")),
        ("tls", ("x509", "tls handshake", "certificate")),
        ("network", ("timeout", "timed out", "no such host", "connection refused", "network is unreachable")),
        ("image-not-found", ("manifest unknown", "name unknown", "status code: 404")),
        ("registry-unavailable", ("status code: 500", "status code: 502", "status code: 503", "too many requests")),
    ):
        if any(signature in error for signature in signatures):
            return category
    return "unclassified"


def run_captured(command, *, env=None):
    try:
        return subprocess.run(command, capture_output=True, text=True, errors="replace", check=False, env=env)
    except FileNotFoundError:
        raise RegistryError("tool-missing") from None


def versions():
    result = run_captured(["skopeo", "--version"])
    version = re.match(r"skopeo version ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", result.stdout)
    safe_version = version.group(1) if version and result.returncode == 0 else "unknown"
    diagnostic(f"skopeo_version={safe_version} python_version={sys.version_info.major}.{sys.version_info.minor}")


def registry_operation(operation, image):
    authfile = selected_authfile()
    if not image or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]*", image):
        raise RegistryError("image-invalid")
    versions()
    if operation == "inspect":
        command = ["skopeo", "inspect", "--authfile", authfile, f"docker://{image}", "--format", "{{.Digest}}"]
    else:
        if os.geteuid() != 0:
            raise RegistryError("privileged-context-required")
        cluster_tool = Path("/usr/local/bin/cluster-tool")
        diagnostic(f"context=local-root cluster_tool_sha256={hashlib.sha256(cluster_tool.read_bytes()).hexdigest()}")
        command = ["python3", str(cluster_tool), "pull", image, "--server", "local"]
    diagnostic(f"operation={operation} context={'local-root' if operation == 'pull' else 'runner'} starting=true")
    result = run_captured(command, env={**os.environ, "REGISTRY_AUTH_FILE": authfile})
    category = "ok" if result.returncode == 0 else failure_category(result.stderr)
    diagnostic(f"operation={operation} exit={result.returncode} category={category}")
    if result.returncode:
        return 1
    if operation == "inspect":
        digest = result.stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RegistryError("digest-invalid")
        print(digest)
    return 0


def cleanup():
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


def main():
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

#!/usr/bin/env python3
"""Keep host SOS outside ordinary artifacts until authenticated private egress."""

import json
import os
import pathlib
import re
import resource
import runpy
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid

DESTINATION = "osac-project/osac-ci-artifacts"
STAGING_ROOT = pathlib.Path("/var/tmp/osac-artifacts")
MAX_TRANSPORT = 40 * 1024 * 1024
MAX_TREE = 32 * 1024 * 1024
MAX_MEMBER = 8 * 1024 * 1024
MAX_FILES = 4096
IMAGE = "ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"


def candidate():
    root = pathlib.Path(os.environ["RUNNER_TEMP"])
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid runner temporary directory")
    return root / "osac-hypervisor-sosreport"


def discard():
    path = candidate()
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def binding():
    return {
        key: os.environ.get(key, "") for key in ("PRIVATE_REPO", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")
    }


def matches(saved):
    current = binding()
    return current["PRIVATE_REPO"] == DESTINATION and all(current.values()) and saved == current


def limit_file():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TRANSPORT, MAX_TRANSPORT))


def capture(destination):
    with destination.open("xb") as stream:
        with subprocess.Popen(
            ["sudo", "-n", "/usr/local/sbin/osac-hypervisor-sosreport"],
            stdout=stream,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            preexec_fn=limit_file,
        ) as process:
            try:
                if process.wait(timeout=155):
                    raise ValueError("host collection failed")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
    if destination.stat().st_size == 0 or destination.stat().st_size > MAX_TRANSPORT:
        raise ValueError("empty or oversized report")


def eligible():
    path = candidate()
    if matches(binding()) and not path.is_symlink() and path.is_dir():
        saved = path / "binding.json"
        report = path / "report.tar"
        try:
            if (
                not saved.is_symlink()
                and stat.S_ISREG(saved.stat().st_mode)
                and saved.stat().st_size < 4096
                and matches(json.loads(saved.read_text()))
                and not report.is_symlink()
                and stat.S_ISREG(report.stat().st_mode)
                and 0 < report.stat().st_size <= MAX_TRANSPORT
            ):
                return True
        except (OSError, ValueError):
            pass
    return False


def collect():
    path = candidate()
    if eligible():
        return
    discard()
    if not matches(binding()):
        return
    try:
        os.umask(0o077)
        path.mkdir(mode=0o700)
        capture(path / "report.tar")
        (path / "binding.json").write_text(json.dumps(binding()))
    except BaseException:
        discard()
        raise


def check_metadata(metadata, destination):
    if (
        not isinstance(metadata, dict)
        or metadata.get("private") is not True
        or metadata.get("full_name", "").lower() != destination.lower()
    ):
        raise ValueError("private destination not verified")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("destination redirected")


def verify_destination():
    destination = os.environ.get("PRIVATE_REPO", "")
    token = os.environ.get("GH_DISPATCH_TOKEN", "")
    if not token or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", destination):
        raise ValueError("private authentication unavailable")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{destination}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        metadata = json.loads(response.read(65536))
    check_metadata(metadata, destination)


def unpack(source, destination):
    if source.is_symlink() or not stat.S_ISREG(source.stat().st_mode) or source.stat().st_size > MAX_TRANSPORT:
        raise ValueError("invalid transport")
    names = set()
    total = 0
    with tarfile.open(source, mode="r:") as archive:
        for member in archive:
            path = pathlib.PurePosixPath(member.name)
            total += member.size
            if (
                not member.isreg()
                or path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or member.name in names
                or member.size < 0
                or member.size > MAX_MEMBER
                or total > MAX_TREE
                or len(names) >= MAX_FILES
                or "sos_logs" in path.parts
                or path.name.endswith("private_map")
            ):
                raise ValueError("uninspectable report")
            names.add(member.name)
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.extractfile(member) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    if not names:
        raise ValueError("empty transport")


def scan(tree, work):
    repository = pathlib.Path(__file__).resolve().parents[2]
    gate = repository / ".github/scripts/release_gate.py"
    is_archive = runpy.run_path(str(gate))["_is_archive"]
    for path in tree.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as incoming:
            header = incoming.read(512)
        if is_archive(header):
            raise ValueError("archived report member")
        try:
            tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="surrogateescape")
        except tarfile.HeaderError:
            pass
        else:
            raise ValueError("tar report member")
    command = [
        sys.executable,
        "-I",
        str(gate),
        str(tree),
        "--fail-on-find",
        "--max-file-mbytes",
        "8",
        "--report",
        str(work / "gate.json"),
    ]
    for filename in os.environ.get("KNOWN_SECRET_FILES", "").splitlines() or [
        str(candidate().parent / "pull-secret.json")
    ]:
        if filename:
            command.extend(["--secret-file", filename])
    for variable in os.environ.get("KNOWN_SECRET_ENV", "").splitlines():
        if variable:
            command.extend(["--env", variable])
    command.extend(["--k8s-secrets", str(candidate() / "k8s-secrets.json")])
    subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, preexec_fn=limit_file, check=True
    )
    gate_report = json.loads((work / "gate.json").read_text())
    if gate_report.get("withheld") != [] or gate_report.get("censored") != []:
        raise ValueError("release gate did not inspect the complete report")
    engine = shutil.which("docker") or shutil.which("podman")
    if not engine:
        raise ValueError("scanner unavailable")
    results = work / "results"
    results.mkdir(mode=0o700)
    container = f"osac-sos-scan-{uuid.uuid4().hex}"
    try:
        subprocess.run(
            [
                engine,
                "run",
                "--rm",
                "--name",
                container,
                "--network=none",
                "--log-driver=none",
                "--memory=256m",
                "--pids-limit=64",
                "--ulimit=fsize=41943040:41943040",
                "--user",
                "0:0",
                "-v",
                f"{tree}:/scan:ro,Z",
                "-v",
                f"{repository}/.github/scripts/.gitleaks.toml:/gitleaks.toml:ro,Z",
                "-v",
                f"{results}:/out:rw,Z",
                IMAGE,
                "dir",
                "/scan",
                "--config=/gitleaks.toml",
                "--report-format=json",
                "--report-path=/out/gitleaks.json",
                "--exit-code=1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=True,
        )
    finally:
        subprocess.run(
            [engine, "rm", "--force", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    findings = json.loads((results / "gitleaks.json").read_text())
    if findings != []:
        raise ValueError("scanner findings or missing result")


def publish():
    verify_destination()
    path = candidate()
    state_file = path.parent / "publish-private-artifacts.json"
    if state_file.is_symlink() or not stat.S_ISREG(state_file.stat().st_mode) or state_file.stat().st_size > 4096:
        raise ValueError("invalid private staging state")
    state = json.loads(state_file.read_text())
    staged = pathlib.Path(state["dir"])
    if (
        staged.parent != STAGING_ROOT
        or staged.resolve() != staged
        or not staged.is_dir()
        or staged.stat().st_uid != os.getuid()
        or staged.stat().st_mode & 0o077
    ):
        raise ValueError("invalid private staging directory")
    try:
        if not eligible():
            return any(staged.iterdir())
        with tempfile.TemporaryDirectory(prefix="checked-", dir=path) as temporary:
            work = pathlib.Path(temporary)
            tree = work / "tree"
            tree.mkdir(mode=0o700)
            unpack(path / "report.tar", tree)
            scan(tree, work)
            os.rename(tree, staged / "hypervisor-sosreport")
    except Exception:
        print("::notice::Hypervisor report withheld")
    finally:
        discard()
    return any(staged.iterdir())


def inventory():
    path = candidate()
    source = pathlib.Path(os.environ.get("K8S_SECRETS_FILE") or path.parent / "osac-secret-inventory/k8s-secrets.json")
    if eligible() and source.is_file() and not source.is_symlink():
        if source.stat().st_size > MAX_MEMBER:
            raise ValueError("secret inventory too large")
        with tempfile.TemporaryDirectory(prefix="inventory-", dir=path) as temporary:
            output = pathlib.Path(temporary) / "inventory.json"
            with source.open("rb") as incoming, output.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=65536)
            os.replace(output, path / "k8s-secrets.json")


def interrupted(_signum, _frame):
    raise InterruptedError("collection interrupted")


def main():
    os.umask(0o077)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    operation = sys.argv[1:]
    try:
        if operation == ["collect"]:
            collect()
        elif operation == ["inventory"]:
            inventory()
        elif operation == ["publish"]:
            if not publish():
                return 1
        elif operation == ["eligible"]:
            return 0 if eligible() else 1
        elif operation == ["discard"]:
            discard()
        else:
            return 1
    except Exception:
        try:
            discard()
        except Exception:
            pass
        print("::notice::Hypervisor report withheld")
        return 2 if operation == ["publish"] else int(operation == ["eligible"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

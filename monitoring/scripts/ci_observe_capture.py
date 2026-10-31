"""Run an observed test command without changing its result or signal handling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM)

CAPTURE_SCHEMA = "osac.ci-observe.capture"
CAPTURE_VERSION = 1
CAPTURE_NAMESPACE = "ci-observability-canary"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "ci-observe"
MANIFEST = CONFIG_DIR / "cluster.yaml"
COLLECTOR_CONFIG = CONFIG_DIR / "collector.yaml"
TOKEN_PATTERN = re.compile(r"\$\{(CI_OBSERVE_[A-Z_]+)\}")
SIGNAL_FILES = ("traces.jsonl", "metrics.jsonl", "logs.jsonl")

SCC_MANIFEST = """---
apiVersion: security.openshift.io/v1
kind: SecurityContextConstraints
metadata:
  name: ci-observe-beyla
allowHostDirVolumePlugin: true
allowHostPID: true
allowPrivilegedContainer: false
allowedCapabilities: [BPF, SYS_PTRACE, NET_RAW, CHECKPOINT_RESTORE, DAC_READ_SEARCH, PERFMON, SYS_ADMIN]
readOnlyRootFilesystem: true
runAsUser:
  type: MustRunAs
  uid: 0
seLinuxContext:
  type: RunAsAny
fsGroup:
  type: RunAsAny
supplementalGroups:
  type: RunAsAny
"""


@dataclass(frozen=True, kw_only=True)
class TestResult:
    returncode: int
    signal_number: int | None

    @property
    def exit_code(self) -> int:
        if self.signal_number is not None:
            return 128 + self.signal_number
        if self.returncode < 0:
            return 128 - self.returncode
        return self.returncode


class TestProcess:
    def __init__(self, *, escalation_delay: float) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.escalation_delay = escalation_delay
        self.signal_number: int | None = None
        self.interrupted_at: float | None = None

    def attach(self, *, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        if self.signal_number is not None:
            self.send_signal(number=self.signal_number)

    def forward_signal(self, number: int, frame: FrameType | None) -> None:
        if self.signal_number is None:
            self.signal_number = number
            self.interrupted_at = time.monotonic()
        if self.process is not None:
            self.send_signal(number=number)

    def send_signal(self, *, number: int) -> None:
        if self.process is None:
            return
        try:
            os.killpg(self.process.pid, number)
        except ProcessLookupError:
            return

    def wait(self) -> TestResult:
        while True:
            try:
                returncode = self.process.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if self.interrupted_at is not None and time.monotonic() - self.interrupted_at >= self.escalation_delay:
                    self.send_signal(number=signal.SIGKILL)
                    returncode = self.process.wait()
                    break
        return TestResult(returncode=returncode, signal_number=self.signal_number)


def run_test(
    *, command: Sequence[str], environment: Mapping[str, str], directory: Path, escalation_delay: float = 10.0
) -> TestResult:
    """Run ``command`` in its own session and return its native result.

    SIGINT and SIGTERM are forwarded to the child's whole process group. The
    first forwarded signal decides the reported exit code (128 plus that
    signal), even when the child handles it and exits zero: CI sees the
    cancellation, not a fake native success. A child still running
    ``escalation_delay`` seconds after the first forwarded signal is killed
    with SIGKILL; descendants that ignore a forwarded signal while their
    leader already exited are not reaped. The child's own signal mask and
    dispositions are untouched.
    """
    supervisor = TestProcess(escalation_delay=escalation_delay)
    previous: dict[int, object] = {}
    for number in FORWARDED_SIGNALS:
        previous[number] = signal.signal(number, supervisor.forward_signal)
    try:
        supervisor.attach(process=subprocess.Popen(command, cwd=directory, env=environment, start_new_session=True))
        return supervisor.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)  # type: ignore[arg-type]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kubectl(kubeconfig: Path, *arguments: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    return result.stdout


def _cluster_uid(kubeconfig: Path) -> str:
    return _kubectl(kubeconfig, "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").strip()


def _kubelet_endpoint(kubeconfig: Path) -> str:
    # Select the InternalIP address by type: addresses[0] is the InternalDNS
    # hostname on OpenShift, which need not resolve from the gateway pod, and
    # an unresolvable kubelet endpoint silently starves metrics.jsonl.
    query = 'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}'
    address = _kubectl(kubeconfig, "get", "nodes", "-o", query).strip()
    if not address:
        raise ValueError("node reports no InternalIP address")
    return f"https://{address}:10250"


def render_manifest(
    *, capture_id: str, namespace: str, cluster_uid: str, kubelet_endpoint: str, suite: str = "", pr: str = ""
) -> str:
    """Substitute the plain ``${...}`` tokens the coordinator owns.

    Suite and PR are narrowed to safe charsets (lowercase alphanumeric with
    hyphens, and digits) and coalesce to "none" when empty: the values land
    inside the manifest's double-quoted env values, the CI action renders
    the same tokens through sed, and the gateway's collector config cannot
    expand an empty value.
    """
    suite = re.sub(r"[^a-z0-9-]", "", suite) or "none"
    pr = re.sub(r"[^0-9]", "", pr) or "none"
    tokens = {
        "CI_OBSERVE_CAPTURE_ID": capture_id,
        "CI_OBSERVE_NAMESPACE": namespace,
        "CI_OBSERVE_CLUSTER_UID": cluster_uid,
        "CI_OBSERVE_KUBELET_ENDPOINT": kubelet_endpoint,
        "CI_OBSERVE_SUITE": suite,
        "CI_OBSERVE_PR": pr,
    }

    def replace(match: re.Match[str]) -> str:
        return tokens.get(match.group(1), match.group(0))

    rendered = TOKEN_PATTERN.sub(replace, MANIFEST.read_text())
    unresolved = sorted(set(TOKEN_PATTERN.findall(rendered)))
    if unresolved:
        raise ValueError(f"unresolved manifest tokens: {unresolved}")
    return rendered


def _await_gateway_ready(kubeconfig: Path, *, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "get",
                "deployment/ci-observe-gateway",
                "-n",
                CAPTURE_NAMESPACE,
                "-o",
                "jsonpath={.status.availableReplicas}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return
        time.sleep(3)
    raise TimeoutError("collector gateway did not become ready")


def _start_port_forward(kubeconfig: Path) -> tuple[subprocess.Popen, int]:
    process = subprocess.Popen(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "port-forward",
            "-n",
            CAPTURE_NAMESPACE,
            "svc/ci-observe-gateway",
            ":4317",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("port-forward exited before reporting a local port")
        # This kubectl prints "Forwarding from 127.0.0.1:<port>" on stdout.
        line = process.stdout.readline() if process.stdout else ""
        match = re.search(r"127\.0\.0\.1:(\d+)", line)
        if match:
            return process, int(match.group(1))
        time.sleep(0.5)
    process.terminate()
    process.wait(timeout=10)
    raise TimeoutError("port-forward did not report a local port")


def _stop_port_forward(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


READOUT_IMAGE = "docker.io/library/busybox@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec"


def _readout_overrides(*, claim: str) -> str:
    pod = {
        "spec": {
            "containers": [
                {
                    "name": "readout",
                    "image": READOUT_IMAGE,
                    "command": ["sleep", "600"],
                    "volumeMounts": [{"name": "capture", "mountPath": "/capture", "readOnly": True}],
                }
            ],
            "volumes": [{"name": "capture", "persistentVolumeClaim": {"claimName": claim}}],
            "restartPolicy": "Never",
        }
    }
    return json.dumps(pod)


def _copy_capture_files(*, kubeconfig: Path, pod: str, destination: Path) -> None:
    for name in SIGNAL_FILES:
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "exec",
                "-n",
                CAPTURE_NAMESPACE,
                pod,
                "--",
                "cat",
                f"/capture/{name}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if result.returncode == 0:
            (destination / name).write_text(result.stdout)


def _close_capture(*, kubeconfig: Path, capture_id: str, destination: Path) -> None:
    """Stop the writers, mount a readout, copy the closed files, remove the volume."""
    # Foreground cascade: the delete must not return while the gateway pod is
    # still flushing its last batch, or the readout below can copy a
    # truncated final JSONL line.
    _kubectl(
        kubeconfig,
        "delete",
        "deployment/ci-observe-gateway",
        "-n",
        CAPTURE_NAMESPACE,
        "--cascade=foreground",
        "--wait=true",
        "--timeout=120s",
    )
    _kubectl(
        kubeconfig,
        "delete",
        "daemonset/ci-observe-beyla",
        "-n",
        CAPTURE_NAMESPACE,
        "--cascade=foreground",
        "--wait=true",
        "--timeout=120s",
    )
    pod = f"ci-observe-readout-{capture_id}"
    claim = f"ci-observe-capture-{capture_id}"
    _kubectl(
        kubeconfig,
        "run",
        pod,
        "-n",
        CAPTURE_NAMESPACE,
        "--image",
        READOUT_IMAGE,
        "--restart=Never",
        f"--overrides={_readout_overrides(claim=claim)}",
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            phase = _kubectl(
                kubeconfig, "get", "pod", pod, "-n", CAPTURE_NAMESPACE, "-o", "jsonpath={.status.phase}"
            ).strip()
            if phase == "Running":
                break
            time.sleep(2)
        else:
            raise TimeoutError("readout pod did not start")
        _copy_capture_files(kubeconfig=kubeconfig, pod=pod, destination=destination)
    finally:
        _kubectl(kubeconfig, "delete", f"pod/{pod}", "-n", CAPTURE_NAMESPACE, "--wait=true", "--ignore-not-found")
        _kubectl(kubeconfig, "delete", f"pvc/{claim}", "-n", CAPTURE_NAMESPACE, "--wait=true", "--ignore-not-found")


def _write_capture_metadata(*, directory: Path, capture_id: str, state: str, exit_code: int | None) -> None:
    files = []
    delivered = set()
    for path in sorted(directory.iterdir()):
        if path.name == "metadata.json" or not path.is_file():
            continue
        size = path.stat().st_size
        files.append({"path": path.name, "size_bytes": size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        # An empty signal file carries no records: that source is missing.
        if size > 0:
            delivered.add(path.name)
    metadata = {
        "schema": CAPTURE_SCHEMA,
        "version": CAPTURE_VERSION,
        "id": capture_id,
        "source_kind": "local",
        "state": state,
        "created_at": _now(),
        "finished_at": _now(),
        "child_exit_code": exit_code,
        "sources": {
            "pytest": "delivered" if "traces.jsonl" in delivered else "missing",
            "beyla": "delivered" if "traces.jsonl" in delivered else "missing",
            "cluster-objects": "delivered" if "logs.jsonl" in delivered else "missing",
            "cluster-logs": "delivered" if "logs.jsonl" in delivered else "missing",
            "cluster-metrics": "delivered" if "metrics.jsonl" in delivered else "missing",
        },
        "files": files,
    }
    (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")


def capture(args: object) -> int:
    """Deploy the collectors, run the test command, close and copy the capture.

    A telemetry failure never replaces the test command's own exit code: it is
    reported on stderr and the capture is marked interrupted. A cluster access
    failure is a clean error before anything is deployed.
    """
    capture_id = args.capture_id or uuid.uuid4().hex[:12]
    destination = args.state_dir / "captures" / capture_id
    destination.mkdir(parents=True, exist_ok=True)
    kubeconfig: Path = args.kubeconfig
    try:
        rendered = render_manifest(
            capture_id=capture_id,
            namespace=args.namespace,
            cluster_uid=_cluster_uid(kubeconfig),
            kubelet_endpoint=_kubelet_endpoint(kubeconfig),
            suite=args.suite,
            pr=args.pr,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip().splitlines()
        last = detail[-1] if detail else "unknown kubectl failure"
        print(f"ci-observe: cluster access failed: {last}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"ci-observe: cluster access failed: {error}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("ci-observe: cluster access failed: kubectl timed out", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ci-observe: cluster access failed: kubectl/oc not found on PATH", file=sys.stderr)
        return 2
    configmap = _kubectl(
        kubeconfig,
        "create",
        "configmap",
        "ci-observe-collector",
        "-n",
        CAPTURE_NAMESPACE,
        f"--from-file=config.yaml={COLLECTOR_CONFIG}",
        "--dry-run=client",
        "-o",
        "yaml",
    )
    # Manifest first: it creates the capture namespace the ConfigMap applies
    # into (the same order as the CI composite action). On a cluster that has
    # never run a capture, applying the ConfigMap first fails with
    # "namespace not found".
    _kubectl(kubeconfig, "apply", "-f", "-", stdin=rendered)
    _kubectl(kubeconfig, "apply", "-f", "-", stdin=configmap)
    _kubectl(kubeconfig, "apply", "-f", "-", stdin=SCC_MANIFEST)
    subprocess.run(
        [
            "oc",
            "--kubeconfig",
            str(kubeconfig),
            "adm",
            "policy",
            "add-scc-to-user",
            "ci-observe-beyla",
            "-z",
            "ci-observe-beyla",
            "-n",
            CAPTURE_NAMESPACE,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    subprocess.run(
        [
            "oc",
            "--kubeconfig",
            str(kubeconfig),
            "adm",
            "policy",
            "add-scc-to-user",
            "hostmount-anyuid",
            "-z",
            "ci-observe-gateway",
            "-n",
            CAPTURE_NAMESPACE,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    forward = None
    exit_code: int | None = None
    state = "closed"
    try:
        _await_gateway_ready(kubeconfig)
        forward, local_port = _start_port_forward(kubeconfig)
        environment = os.environ.copy()
        environment["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{local_port}"
        environment["OTEL_EXPORTER_OTLP_TIMEOUT"] = "2"
        environment["PYTEST_ADDOPTS"] = (environment.get("PYTEST_ADDOPTS", "") + " --export-traces").strip()
        result = run_test(command=args.command, environment=environment, directory=Path.cwd())
        exit_code = result.exit_code
        if result.signal_number is not None:
            state = "interrupted"
        return result.exit_code
    finally:
        if forward is not None:
            _stop_port_forward(forward)
        try:
            _close_capture(kubeconfig=kubeconfig, capture_id=capture_id, destination=destination)
        except Exception as error:
            state = "interrupted"
            print(f"ci-observe: capture close failed, evidence may be incomplete: {error}", file=sys.stderr)
        finally:
            _write_capture_metadata(directory=destination, capture_id=capture_id, state=state, exit_code=exit_code)

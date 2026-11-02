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
from typing import Any

FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM)

CAPTURE_SCHEMA = "osac.ci-observe.capture"
CAPTURE_VERSION = 1
CAPTURE_NAMESPACE = "ci-observability-canary"
CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "ci-observe"
MANIFEST = CONFIG_DIR / "cluster.yaml"
COLLECTOR_CONFIG = CONFIG_DIR / "collector.yaml"
TOKEN_PATTERN = re.compile(r"\$\{(CI_OBSERVE_[A-Z_]+)\}")
SIGNAL_FILES = ("traces.jsonl", "metrics.jsonl", "logs.jsonl")
MAX_OSAC_IMAGE_IDS_LENGTH = 4096
IMAGE_IDS_TRUNCATION_MARKER = "...[truncated]"
MAX_TEST_SUITE_LENGTH = 256
MAX_TEST_FILTER_LENGTH = 512
MAX_TEST_MARKER_LENGTH = 512

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


@dataclass(frozen=True, kw_only=True)
class OSACImageIDCapture:
    image_ids: str
    complete: str


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


def _test_infra_sha() -> str:
    """Return HEAD only when the local capture code matches that commit.

    Every file the local supervisor runs must be tracked at HEAD and
    byte-identical to the worktree copy: ``git diff`` alone stays quiet for
    a path HEAD does not contain, which would claim a commit that never
    had the running file. The overlay-style local edits to unrelated paths
    (``tests/``) do not matter; only the harness files below are checked.
    """
    repository = Path(__file__).resolve().parents[2]
    source_files = [
        Path(__file__).resolve().relative_to(repository).as_posix(),
        MANIFEST.relative_to(repository).as_posix(),
        COLLECTOR_CONFIG.relative_to(repository).as_posix(),
    ]
    try:
        for source_file in source_files:
            tracked = subprocess.run(
                ["git", "-C", str(repository), "cat-file", "-e", f"HEAD:{source_file}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if tracked.returncode != 0:
                return "unknown"
        changed = subprocess.run(
            ["git", "-C", str(repository), "diff", "--quiet", "HEAD", "--", *source_files],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if changed.returncode != 0:
            return "unknown"
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    sha = result.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        return "unknown"
    return sha


def _ref_or_unknown(ref: str) -> str:
    """A git ref stamped onto telemetry is either a real ref or "unknown":
    characters outside the SHA/branch/tag set (including anything that
    breaks the manifest's double-quoted env values or the CI action's sed
    rendering) degrade to "unknown" rather than a mangled value that
    matches no real ref. The full-string match also rejects empty strings
    and embedded newlines."""
    return ref if re.fullmatch(r"[a-zA-Z0-9._/+-]+", ref) else "unknown"


def _manifest_safe(value: str) -> str:
    """Drop the characters that break the manifest's double-quoted env values
    or the CI action's sed rendering of the same tokens: newline, backslash,
    double quote, the sed-special ``|`` and ``&``, and ``$`` (the action's
    sed chain substitutes tokens sequentially, so a value embedding a
    ``${CI_OBSERVE_...}`` token would be rewritten by a later substitution)."""
    return re.sub(r"[\n|\\&\"$]", "", value)


def _osac_images(kubeconfig: Path, namespace: str) -> str:
    """The OSAC images deployed in the observed namespace.

    One ``deployment=image1,image2`` line per deployment, collapsed to a
    single "; "-separated line: the value lands inside the manifest's
    double-quoted env values (and the CI action renders the same tokens
    through sed), neither of which can carry a newline. Any failure
    (kubectl error, empty namespace) degrades to "unknown", never an empty
    string: the gateway's collector config cannot expand an empty value.
    """
    query = 'jsonpath={range .items[*]}{.metadata.name}={.spec.template.spec.containers[*].image}{"\\n"}{end}'
    try:
        raw = _kubectl(kubeconfig, "get", "deployments", "-n", namespace, "-o", query)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    return _manifest_safe(raw.replace("\n", ";").strip(";")) or "unknown"


def osac_image_ids(kubeconfig: Path, namespace: str) -> OSACImageIDCapture:
    """Return runtime IDs and whether each expected OSAC replica was observed."""
    try:
        raw = _kubectl(kubeconfig, "get", "deployments,replicasets,pods", "-n", namespace, "-o", "json")
        result = json.loads(raw)
    except (json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return OSACImageIDCapture(image_ids="unknown", complete="unknown")
    if not isinstance(result, dict):
        return OSACImageIDCapture(image_ids="unknown", complete="unknown")
    resources = result.get("items", [])
    if not isinstance(resources, list):
        return OSACImageIDCapture(image_ids="unknown", complete="unknown")

    deployments: dict[str, tuple[int, set[str]]] = {}
    replica_set_deployments: dict[str, str] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        metadata = resource.get("metadata")
        if not isinstance(metadata, dict):
            continue
        name = metadata.get("name")
        if not isinstance(name, str):
            continue
        if resource.get("kind") == "Deployment":
            labels = metadata.get("labels")
            if not isinstance(labels, dict) or labels.get("app.kubernetes.io/part-of") != "osac":
                continue
            spec = resource.get("spec")
            template = spec.get("template") if isinstance(spec, dict) else None
            pod_spec = template.get("spec") if isinstance(template, dict) else None
            containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
            if not isinstance(containers, list):
                return OSACImageIDCapture(image_ids="unknown", complete="unknown")
            replicas = spec.get("replicas", 1)
            if not isinstance(replicas, int) or replicas < 0:
                return OSACImageIDCapture(image_ids="unknown", complete="unknown")
            container_names = set()
            for container in containers:
                if not isinstance(container, dict):
                    continue
                raw_container_name = container.get("name")
                container_name = _safe_image_part(raw_container_name)
                if not container_name or container_name != raw_container_name:
                    return OSACImageIDCapture(image_ids="unknown", complete="unknown")
                container_names.add(container_name)
            if not container_names:
                return OSACImageIDCapture(image_ids="unknown", complete="unknown")
            # Deployment names cross the same rendering as container names
            # and image IDs. A name outside the safe charset is impossible
            # for a real Deployment (DNS-1123) but possible for a hostile
            # API response; it degrades the whole value to "unknown" rather
            # than being rewritten into a name that matches no deployment.
            if _safe_image_part(name) != name:
                return OSACImageIDCapture(image_ids="unknown", complete="unknown")
            # A scaled-to-zero Deployment has no runtime image evidence.
            deployments[name] = (max(replicas, 1), container_names)
        elif resource.get("kind") == "ReplicaSet":
            deployment = _controller_owner_name(resource, "Deployment")
            if deployment:
                replica_set_deployments[name] = deployment

    if not deployments:
        return OSACImageIDCapture(image_ids="unknown", complete="unknown")

    image_entries: set[str] = set()
    observed_counts: dict[tuple[str, str], int] = {}
    complete = True
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("kind") != "Pod":
            continue
        status = resource.get("status")
        if not isinstance(status, dict) or status.get("phase") != "Running":
            continue
        replica_set = _controller_owner_name(resource, "ReplicaSet")
        deployment = replica_set_deployments.get(replica_set, "")
        if deployment not in deployments:
            continue
        container_statuses = status.get("containerStatuses")
        if not isinstance(container_statuses, list):
            complete = False
            continue
        seen_in_pod: set[str] = set()
        for container_status in container_statuses:
            if not isinstance(container_status, dict):
                complete = False
                continue
            raw_container = container_status.get("name")
            container = _safe_image_part(raw_container)
            raw_image_id = container_status.get("imageID")
            image_id = _safe_image_part(raw_image_id)
            if not container or container != raw_container:
                complete = False
                continue
            key = (deployment, container)
            if not image_id or image_id != raw_image_id:
                image_entries.add(f"{deployment}/{container}=unknown")
                complete = False
                continue
            image_entries.add(f"{deployment}/{container}={image_id}")
            if container not in seen_in_pod:
                observed_counts[key] = observed_counts.get(key, 0) + 1
                seen_in_pod.add(container)

    for deployment, (replicas, container_names) in deployments.items():
        for container in container_names:
            key = (deployment, container)
            if observed_counts.get(key, 0) < replicas:
                image_entries.add(f"{deployment}/{container}=unknown")
                complete = False

    all_entries = sorted(image_entries)
    full_value = ";".join(all_entries)
    if len(full_value) <= MAX_OSAC_IMAGE_IDS_LENGTH:
        return OSACImageIDCapture(image_ids=full_value or "unknown", complete=str(complete).lower())

    prefix_limit = MAX_OSAC_IMAGE_IDS_LENGTH - len(IMAGE_IDS_TRUNCATION_MARKER) - 1
    entries = []
    total_length = 0
    for entry in all_entries:
        entry_length = len(entry) + (1 if entries else 0)
        if total_length + entry_length > prefix_limit:
            break
        entries.append(entry)
        total_length += entry_length
    return OSACImageIDCapture(image_ids=";".join([*entries, IMAGE_IDS_TRUNCATION_MARKER]), complete="false")


def _controller_owner_name(resource: dict[str, Any], kind: str) -> str:
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    owners = metadata.get("ownerReferences")
    if not isinstance(owners, list):
        return ""
    for owner in owners:
        if not isinstance(owner, dict) or owner.get("kind") != kind or owner.get("controller") is not True:
            continue
        name = owner.get("name")
        if isinstance(name, str):
            return name
    return ""


def _safe_image_part(value: object) -> str:
    """Keep the characters a container name, deployment name or image ID can
    legitimately carry (underscores included -- container names use them) and
    drop everything else: the value crosses the manifest's double-quoted env
    values and the CI action's sed rendering, which cannot carry newlines,
    quotes, backslashes or the sed/token-special characters."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^A-Za-z0-9._:/@+-]", "", value)


def _bounded_selection(value: str, *, limit: int) -> str:
    if len(value.encode("utf-8")) > limit:
        return "unknown"
    return value


def render_manifest(
    *,
    capture_id: str,
    namespace: str,
    cluster_uid: str,
    kubelet_endpoint: str,
    suite: str = "",
    pr: str = "",
    test_ref: str = "",
    test_sha: str = "",
    test_infra_ref: str = "",
    test_infra_sha: str = "",
    test_infra_workflow_sha: str = "",
    workflow: str = "",
    run_attempt: str = "",
    osac_images: str = "",
    osac_image_ids: str = "",
    osac_image_ids_complete: str = "unknown",
    test_suite: str = "",
    test_filter: str = "",
    test_marker: str = "",
    otlp_endpoint: str = "",
    otlp_auth: str = "",
) -> str:
    """Substitute the plain ``${...}`` tokens the coordinator owns.

    Suite and PR are narrowed to safe charsets (lowercase alphanumeric with
    hyphens, and digits) and coalesce to "none" when empty: the values land
    inside the manifest's double-quoted env values, the CI action renders
    the same tokens through sed, and the gateway's collector config cannot
    expand an empty value.

    The run identity tokens have non-empty fallbacks because an empty value
    makes the collector reject its config at load. Requested refs stay
    separate from resolved SHAs: the resolved osac commit (test_sha) is a
    40/64 lowercase hex SHA or "unknown", never the requested ref.
    Test-selection strings are JSON-quoted YAML scalars so quotes,
    backslashes and newlines remain safe and exact; an empty selection
    means the caller reported nothing and records "unknown" ("none" for
    filter and marker, which the wired workflows only pass when the test
    command really ran without -k/-m).

    ``otlp_endpoint`` and ``otlp_auth`` serve the live-push collector
    config (collector-live.yaml); the local file-capture config never reads
    them, so they default to empty and only keep the shared manifest
    renderable.
    """
    suite = re.sub(r"[^a-z0-9-]", "", suite) or "none"
    pr = re.sub(r"[^0-9]", "", pr) or "none"
    test_ref = _ref_or_unknown(test_ref)
    test_sha = _sha_or_unknown(test_sha)
    test_infra_ref = _ref_or_unknown(test_infra_ref) if test_infra_ref else "unspecified"
    test_infra_sha = _sha_or_unknown(test_infra_sha)
    test_infra_workflow_sha = _sha_or_unknown(test_infra_workflow_sha)
    workflow = _manifest_safe(workflow) or "unknown"
    run_attempt = re.sub(r"[^0-9]", "", run_attempt) or "1"
    osac_images = _manifest_safe(osac_images) or "unknown"
    osac_image_ids = _manifest_safe(osac_image_ids) or "unknown"
    if osac_image_ids_complete not in ("true", "false", "unknown"):
        osac_image_ids_complete = "unknown"
    test_suite = _bounded_selection(test_suite or "unknown", limit=MAX_TEST_SUITE_LENGTH)
    test_filter = _bounded_selection(test_filter or "none", limit=MAX_TEST_FILTER_LENGTH)
    test_marker = _bounded_selection(test_marker or "none", limit=MAX_TEST_MARKER_LENGTH)
    tokens = {
        "CI_OBSERVE_CAPTURE_ID": capture_id,
        "CI_OBSERVE_NAMESPACE": namespace,
        "CI_OBSERVE_CLUSTER_UID": cluster_uid,
        "CI_OBSERVE_KUBELET_ENDPOINT": kubelet_endpoint,
        "CI_OBSERVE_SUITE": suite,
        "CI_OBSERVE_PR": pr,
        "CI_OBSERVE_TEST_REF": test_ref,
        "CI_OBSERVE_TEST_SHA": test_sha,
        "CI_OBSERVE_TEST_INFRA_REF": test_infra_ref,
        "CI_OBSERVE_TEST_INFRA_SHA": test_infra_sha,
        "CI_OBSERVE_TEST_INFRA_WORKFLOW_SHA": test_infra_workflow_sha,
        "CI_OBSERVE_WORKFLOW": workflow,
        "CI_OBSERVE_RUN_ATTEMPT": run_attempt,
        "CI_OBSERVE_OSAC_IMAGES": osac_images,
        "CI_OBSERVE_OSAC_IMAGE_IDS": osac_image_ids,
        "CI_OBSERVE_OSAC_IMAGE_IDS_COMPLETE": osac_image_ids_complete,
        "CI_OBSERVE_TEST_SUITE": json.dumps(test_suite, ensure_ascii=True),
        "CI_OBSERVE_TEST_FILTER": json.dumps(test_filter, ensure_ascii=True),
        "CI_OBSERVE_TEST_MARKER": json.dumps(test_marker, ensure_ascii=True),
        "CI_OBSERVE_OTLP_ENDPOINT": otlp_endpoint,
        "CI_OBSERVE_OTLP_AUTH": otlp_auth,
    }

    def replace(match: re.Match[str]) -> str:
        return tokens.get(match.group(1), match.group(0))

    rendered = TOKEN_PATTERN.sub(replace, MANIFEST.read_text())
    unresolved = sorted(set(TOKEN_PATTERN.findall(rendered)))
    if unresolved:
        raise ValueError(f"unresolved manifest tokens: {unresolved}")
    return rendered


def _sha_or_unknown(sha: str) -> str:
    return sha if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha) else "unknown"


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
        image_capture = osac_image_ids(kubeconfig, args.namespace)
        rendered = render_manifest(
            capture_id=capture_id,
            namespace=args.namespace,
            cluster_uid=_cluster_uid(kubeconfig),
            kubelet_endpoint=_kubelet_endpoint(kubeconfig),
            suite=args.suite,
            pr=args.pr,
            test_ref="unknown",
            # A local capture wraps an arbitrary command in an arbitrary
            # working tree: there is no osac test checkout to resolve, so
            # the resolved commit is honestly "unknown" -- never the
            # requested ref or a guessed value.
            test_sha="unknown",
            test_infra_ref="unspecified",
            test_infra_sha=_test_infra_sha(),
            test_infra_workflow_sha="unknown",
            workflow="local",
            run_attempt="1",
            osac_images=_osac_images(kubeconfig, args.namespace),
            osac_image_ids=image_capture.image_ids,
            osac_image_ids_complete=image_capture.complete,
            test_suite="local",
            test_filter="none",
            test_marker="none",
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

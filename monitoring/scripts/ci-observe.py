#!/usr/bin/env python3
"""ci-observe: local stack for collector-first CI observability (local only).

Commands: up, down, import, report, capture, sync. `sync` downloads ci-observe
telemetry artifacts from GitHub Actions runs (through the authenticated gh CLI)
and imports each one with the `import` rules. `capture` only parses arguments and
delegates to the sibling ci_observe_capture module (supplied by the
capture-backend integration, not this file).

Safety rules:
  - Owns only Podman resources named ci-observe-*<state hash> labeled osac.ci-observe.owned.
    Foreign containers or volumes with those names are refused, never restarted or deleted.
  - Publishes only loopback ports (UI 42160, OTLP 4318 by default; 0 picks an ephemeral
    port) and refuses collisions. Tempo, Loki and Prometheus are additionally publishable
    with --tempo-port/--loki-port/--metrics-port, where 0 (the default) means unpublished,
    and only at container creation. The replay collector uses host networking and is checked
    through the otelcol self-metrics endpoint on loopback.
  - The state directory is mode 0700, never a symlink, never adopted when it holds unknown
    data. `down` stops the owned stack and preserves data: the container is kept (Tempo
    holds recent traces in a container-local live store) and the data volume is never
    deleted. `down --purge` explicitly removes both.
  - Capture content is untrusted data: imports accept only closed captures whose metadata
    matches the on-disk files (1 GiB total bound, regular files only, no symlinks,
    verified sha256, 16 MiB OTLP line bound). Nothing inside a capture is executed.
  - An import is marked imported only when the exporter counters show every accepted item
    sent (otelcol_receiver_accepted_* == otelcol_exporter_sent_* > 0, nothing refused or
    failed). A file tailer starting is not import completion.
  - sync runs locally only (never inside CI, which holds no backend credentials) and treats
    GitHub artifact ZIPs as untrusted data: the ZIP structure is validated before anything
    is extracted (only the capture's file names, flat or under one directory, regular files
    only, no symlinks, 1 GiB uncompressed bound enforced while streaming), each capture
    then goes through the full import validation. Downloaded ZIPs are cached privately
    under the 0700 state tree and never re-downloaded; already-imported captures are
    skipped by digest and uncertain partial imports keep their refusal. The --watch loop
    repeats every 5 minutes and stays bounded by --limit downloads per iteration.

Measured backend limits of the pinned LGTM image (2026-10-29, original timestamps kept):
the stock image dropped Prometheus samples about 30 minutes old (10m out-of-order
window) and discarded Loki logs more than about an hour behind the newest entry of
their stream (max_chunk_age/2), or hid them from queries until a chunk flush. The
local-development overrides mounted by `up` (monitoring/config/ci-observe/
lgtm-overrides/) widen this: a 7d Prometheus out-of-order window, a 168h Loki
max_chunk_age with a 1m chunk idle period, plus a forced Loki flush after each
import. With them, a year-old trace, a day-old metric sample and a day-old log line
all survived import and a down/up cycle with original timestamps. These are local
development stack settings, not fleet production settings.

Replay configuration contract (owned by the monitoring/config/ci-observe lane):
  monitoring/config/ci-observe/replay.yaml reads the signal files as
  "${env:CI_OBSERVE_CAPTURE_DIR}/<traces|metrics|logs>.jsonl" with the stock otlpjsonfile
  receiver and exports to "${env:CI_OBSERVE_OTLP_ENDPOINT}". It must contain no other
  receivers or exporters; acknowledgement is checked from collector self-metric totals,
  which cannot attribute mixed pipelines.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TOOL = "ci-observe"

# Image pins resolved from docker.io on 2026-10-29 (registry manifest digests).
LGTM_IMAGE = (
    "docker.io/grafana/otel-lgtm:0.38.1@sha256:0df9bc14142000b161344bd24caf6ddd319b7319055aedaf9b0692b125730af7"
)
COLLECTOR_IMAGE = (
    "docker.io/otel/opentelemetry-collector-contrib:0.164.0"
    "@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b"
)
# The collector pin may be replaced by the config lane; --collector-image overrides it.

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "ci-observe"
DEFAULT_UI_PORT = 42160  # loopback Grafana UI; 0 picks a free ephemeral port
DEFAULT_OTLP_PORT = 4318  # loopback OTLP/HTTP ingest; 0 picks a free ephemeral port
REPLAY_METRICS_PORT = 8888  # otelcol self-metrics, reachable on loopback via host networking
# The backend's query APIs, publishable on loopback on demand: name ->
# (container port, display name). Unlike the UI/OTLP ports, 0 -- the default --
# leaves them unpublished (there 0 picks an ephemeral port instead), and they
# are only assigned at container creation, so an existing container without a
# requested port needs 'down --purge' before it can gain one.
OPTIONAL_PORTS = {"tempo": (3200, "Tempo"), "loki": (3100, "Loki"), "metrics": (9090, "Prometheus")}

# The capture PVC in cluster.yaml is 1 Gi, which is the real ceiling: a full
# E2E suite under Beyla produces close to it (the first real capture was
# 965 MB). The sync's bound matches the PVC so real captures are not refused.
CAPTURE_MAX_BYTES = 1024 * 1024 * 1024
OTLP_LINE_MAX_BYTES = 16 * 1024 * 1024
METADATA_MAX_BYTES = 1024 * 1024
CAPTURE_SCHEMA = "osac.ci-observe.capture"
CAPTURE_VERSION = 1
SIGNALS = {"traces.jsonl": "spans", "metrics.jsonl": "metric_points", "logs.jsonl": "log_records"}
EXPECTED_SOURCES = ("pytest", "beyla", "cluster-objects", "cluster-logs", "cluster-metrics")
# sync: CI uploads each capture as a GitHub Actions artifact. The CI action
# (.github/actions/ci-observe-capture) names them
# ci-observe-capture-<run_id>-attempt-<attempt>; the older plan name
# ci-observe-telemetry-<run_id>-<attempt> stays accepted. The ZIP holds the
# capture's files, either flat or under one ci-observe/<capture_id>/ directory
# (actions/upload-artifact zips the staged directory tree), and signal files
# are optional: the capture metadata lists what is really inside.
ARTIFACT_NAME_PREFIX = "ci-observe-"
ARTIFACT_NAME_PATTERNS = (
    re.compile(r"^ci-observe-capture-(\d+)-attempt-(\d+)$"),
    re.compile(r"^ci-observe-telemetry-(\d+)-(\d+)$"),
)
ARTIFACT_MEMBERS = frozenset({"metadata.json"} | set(SIGNALS))
CAPTURE_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
SYNC_SINCE_HOURS = {"6h": 6, "12h": 12, "24h": 24, "2d": 48, "7d": 168}
SYNC_RUN_LIST_LIMIT = 1000  # runs fetched per sync iteration; busy repos exceed 200/day
SYNC_WATCH_INTERVAL_S = 300
SYNC_DEFAULT_LIMIT = 10
REPLAY_CONFIG = Path(__file__).resolve().parents[1] / "config" / "ci-observe" / "replay.yaml"
# Backend config overrides mounted into the LGTM container; these widen timestamp
# acceptance for replayed captures (local development stack only, see the file headers).
LGTM_OVERRIDES_DIR = Path(__file__).resolve().parents[1] / "config" / "ci-observe" / "lgtm-overrides"
PROMETHEUS_OVERRIDE = LGTM_OVERRIDES_DIR / "prometheus.yaml"
LOKI_OVERRIDE = LGTM_OVERRIDES_DIR / "loki-config.yaml"
TEMPO_OVERRIDE = LGTM_OVERRIDES_DIR / "tempo-config.yaml"
PYROSCOPE_STUB = LGTM_OVERRIDES_DIR / "pyroscope-stub.sh"
STACK_VERSION = 2  # bump when the container layout changes; up refuses mismatched stacks
OWNED_LABEL = "osac.ci-observe.owned"
BACKEND_LIMITS = [
    "Local development stack settings, not fleet production settings.",
    "Prometheus override widens the out-of-order window to 7d: day-old replayed samples "
    "are accepted with original timestamps; samples older than 7d are still rejected.",
    "Loki override sets max_chunk_age 168h (out-of-order acceptance is half that) and "
    "chunk_idle_period 1m, and import forces a chunk flush: day-old replayed logs are "
    "stored and queryable; the stock window silently discarded them.",
    "Tempo retained year-old traces (queried by trace id after import); recent traces "
    "survive restarts only after Tempo cuts them into a block (about 30s).",
    "Imports never shift timestamps; records outside these limits stay dropped and remain visible as this gap.",
]


class ToolError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class BackendUnavailable(ToolError):
    """The local backend is down, not the capture's fault.

    A sync that hits this must retry the import on a later pass (the backend
    comes back with `up`), so unlike a capture refusal it is never recorded
    as permanent.
    """


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def podman(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(["podman", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise ToolError("podman is not available on PATH") from None
    if check and proc.returncode != 0:
        raise ToolError(f"podman {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def gh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run the authenticated gh CLI; sync never talks to GitHub over raw HTTP."""
    try:
        proc = subprocess.run(["gh", *args], capture_output=True)
    except FileNotFoundError:
        raise ToolError("gh is not available on PATH; sync needs an authenticated GitHub CLI") from None
    if check and proc.returncode != 0:
        raise ToolError(f"gh {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc


def gh_json(args: list[str]) -> object:
    try:
        return json.loads(gh(args).stdout)
    except json.JSONDecodeError:
        raise ToolError(f"gh {' '.join(args)} did not return JSON") from None


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str] | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()
    except (urllib.error.URLError, OSError):
        return None


def resolve_state_dir(raw: str | None) -> Path:
    """Return an absolute, safe state directory, creating it when absent."""
    path = Path(os.path.abspath(Path(raw or os.environ.get("CI_OBSERVE_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()))
    marker = path / ".ci-observe-owner"
    if path.is_symlink():
        raise ToolError(f"state directory {path} is a symlink; refusing")
    if path.exists():
        if not path.is_dir():
            raise ToolError(f"state path {path} is not a directory")
        if str(path) != str(path.resolve()):
            raise ToolError(f"state directory {path} contains a symlinked component; refusing")
        st = path.stat()
        if st.st_uid != os.geteuid():
            raise ToolError(f"state directory {path} is not owned by the current user; refusing")
        if st.st_mode & 0o077:
            raise ToolError(f"state directory {path} has group/other permissions; run: chmod 700 {path}")
        if marker.exists():
            try:
                owner = json.loads(marker.read_text())
            except (json.JSONDecodeError, OSError):
                raise ToolError(f"ownership marker {marker} is unreadable; refusing") from None
            if not isinstance(owner, dict) or owner.get("tool") != TOOL:
                raise ToolError(f"state directory {path} belongs to another tool; refusing")
        elif any(path.iterdir()):
            raise ToolError(f"state directory {path} is not empty and has no ownership marker; refusing to adopt")
    else:
        path.mkdir(parents=True)
        os.chmod(path, 0o700)
    if not marker.exists():
        marker.write_text(json.dumps({"tool": TOOL, "version": 1}) + "\n")
        os.chmod(marker, 0o600)
    return path


def instance_id(state_dir: Path) -> str:
    return hashlib.sha256(str(state_dir).encode()).hexdigest()[:8]


def parse_utc(raw: object) -> datetime | None:
    """Parse an untrusted ISO timestamp into an aware UTC datetime, or None."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_since(raw: str) -> tuple[datetime, datetime]:
    """Turn --since (6h/12h/24h/2d/7d or an ISO date) into a UTC (since, until) window."""
    until = utc_now()
    hours = SYNC_SINCE_HOURS.get(raw)
    if hours is not None:
        return until - timedelta(hours=hours), until
    since = parse_utc(raw)
    if since is None:
        raise ToolError(f"invalid --since {raw!r}: use 6h, 12h, 24h, 2d, 7d or an ISO date")
    if since > until:
        raise ToolError(f"--since {raw!r} is in the future")
    return since, until


def inspect_container(name: str) -> dict[str, Any] | None:
    fmt = "{{json .Config.Labels}}|{{.State.Running}}|{{.ImageName}}"
    proc = podman(["inspect", "--format", fmt, name], check=False)
    if proc.returncode != 0:
        return None
    labels_raw, running, image = proc.stdout.strip().split("|")
    return {"labels": json.loads(labels_raw) or {}, "running": running == "true", "image": image}


def require_owned(info: dict[str, Any], name: str) -> None:
    if info["labels"].get(OWNED_LABEL) != "true":
        raise ToolError(f"{name} exists but is not labeled as owned by {TOOL}; refusing to touch it")


def image_matches(image_name: str, ref: str) -> bool:
    """Compare by registry digest when both carry one; fall back to exact reference."""

    def digest(text: str) -> str | None:
        match = re.search(r"sha256:[0-9a-f]{64}", text)
        return match.group(0) if match else None

    left, right = digest(image_name), digest(ref)
    return left == right if left and right else image_name == ref


def check_port_free(port: int, what: str) -> None:
    if port == 0:
        return
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        raise ToolError(f"{what} port {port} is already in use on loopback; refusing") from None
    finally:
        sock.close()


def published_port(container: str, inner: int) -> int:
    out = podman(["port", container, str(inner)]).stdout.strip().splitlines()
    if not out:
        raise ToolError(f"no published port found for {container}:{inner}")
    return int(out[0].rsplit(":", 1)[1])


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as err:
        raise ToolError(f"{path} is unreadable: {err}") from None
    if not isinstance(data, dict):
        raise ToolError(f"{path} does not contain a JSON object")
    return data


def stack_path(state_dir: Path) -> Path:
    return state_dir / "stack.json"


def index_path(state_dir: Path) -> Path:
    return state_dir / "imports" / "index.json"


def load_index(state_dir: Path) -> dict[str, Any]:
    data = load_json(index_path(state_dir))
    if data is None:
        return {"version": 1, "imports": {}}
    if not isinstance(data.get("imports"), dict):
        raise ToolError(f"{index_path(state_dir)} has an unexpected shape; refusing")
    return data


def save_index(state_dir: Path, index: dict[str, Any]) -> None:
    index_path(state_dir).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(index_path(state_dir), json.dumps(index, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_capture(capture_dir: Path, *, tolerate_modified: bool = False) -> dict[str, Any]:
    """Validate a closed capture directory; return its metadata and content digest.

    tolerate_modified: the CI upload path redacts capture files in place after
    the close step hashed them (gitleaks and the release gate both rewrite
    findings), so a size or sha256 mismatch on an artifact that came through
    that path is the scanners doing their job, not corruption. With the flag
    set, a mismatching file is imported as it actually is and recorded in the
    result's "modified" list instead of refusing the capture. The sync sets
    this; local imports stay strict.
    """
    if capture_dir.is_symlink() or not capture_dir.is_dir():
        raise ToolError(f"capture path {capture_dir} is not a plain directory")
    meta_path = capture_dir / "metadata.json"
    if meta_path.is_symlink() or not meta_path.is_file():
        raise ToolError("metadata.json is missing or is not a regular file")
    if meta_path.stat().st_size > METADATA_MAX_BYTES:
        raise ToolError("metadata.json exceeds the 1 MiB bound")
    meta = load_json(meta_path)
    if meta is None:
        raise ToolError("metadata.json is empty")
    if meta.get("schema") != CAPTURE_SCHEMA:
        raise ToolError(f"capture schema is {meta.get('schema')!r}, expected {CAPTURE_SCHEMA!r}")
    if meta.get("version") != CAPTURE_VERSION:
        raise ToolError(f"capture version is {meta.get('version')!r}, expected {CAPTURE_VERSION}")
    if meta.get("state") != "closed":
        raise ToolError(f"capture state is {meta.get('state')!r}; only closed captures can be imported")
    if meta.get("id") != capture_dir.name:
        raise ToolError("metadata id does not match the capture directory name")
    files = meta.get("files")
    if not isinstance(files, list) or not files:
        raise ToolError("metadata files list is missing or empty")
    entries: list[tuple[Path, int, str]] = []
    total = 0
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ToolError("metadata files contains a non-object entry")
        rel, size, file_hash = entry.get("path"), entry.get("size_bytes"), entry.get("sha256")
        if not isinstance(rel, str) or rel in seen:
            raise ToolError(f"invalid or duplicate file path in metadata: {rel!r}")
        seen.add(rel)
        rel_path = Path(rel)
        if rel_path.is_absolute() or not rel_path.parts or ".." in rel_path.parts:
            raise ToolError(f"unsafe file path in metadata: {rel!r}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ToolError(f"invalid size_bytes for {rel!r}")
        if not isinstance(file_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", file_hash):
            raise ToolError(f"invalid sha256 for {rel!r}")
        total += size
        entries.append((rel_path, size, file_hash))
    if total > CAPTURE_MAX_BYTES:
        raise ToolError(f"declared capture size {total} bytes exceeds the 1 GiB bound")
    actual: set[str] = set()
    for cur, dirnames, filenames in os.walk(capture_dir, followlinks=False):
        for name in dirnames + filenames:
            if (Path(cur) / name).is_symlink():
                raise ToolError(f"symlink inside capture: {Path(cur) / name}")
        actual.update((Path(cur) / n).relative_to(capture_dir).as_posix() for n in filenames)
    expected = {"metadata.json"} | {rel.as_posix() for rel, _, _ in entries}
    if actual != expected:
        raise ToolError(
            f"capture contents do not match metadata; unexpected: {sorted(actual - expected)}, "
            f"missing: {sorted(expected - actual)}"
        )
    lines_for_digest = []
    modified: list[str] = []
    for rel, size, file_hash in entries:
        target = capture_dir / rel
        st = target.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise ToolError(f"{rel} is not a regular file")
        actual_hash = sha256_file(target)
        if st.st_size != size or actual_hash != file_hash:
            if not tolerate_modified:
                if st.st_size != size:
                    raise ToolError(f"{rel}: actual size {st.st_size} does not match metadata {size}")
                raise ToolError(f"{rel}: sha256 does not match metadata")
            modified.append(rel.as_posix())
        if rel.name in SIGNALS:
            with target.open("rb") as handle:
                for line in handle:
                    if len(line) > OTLP_LINE_MAX_BYTES:
                        raise ToolError(f"{rel}: OTLP line exceeds the 16 MiB bound; refusing to split")
        lines_for_digest.append(f"{rel.as_posix()} {actual_hash}\n")
    capture_digest = hashlib.sha256("".join(sorted(lines_for_digest)).encode()).hexdigest()
    return {"metadata": meta, "capture_digest": capture_digest, "modified": modified}


METRIC_RE = re.compile(r"^(otelcol_[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$")


def scrape_totals(url: str) -> dict[str, float] | None:
    got = http_get(url)
    if got is None or got[0] != 200:
        return None
    totals: dict[str, float] = {}
    for line in got[1].splitlines():
        match = METRIC_RE.match(line.strip())
        if match:
            totals[match.group(1)] = totals.get(match.group(1), 0.0) + float(match.group(2))
    return totals


def exporter_ack_ok(totals: dict[str, float], needed: dict[str, bool]) -> bool:
    for signal, required in needed.items():
        if not required:
            continue
        accepted = totals.get(f"otelcol_receiver_accepted_{signal}", 0)
        sent = totals.get(f"otelcol_exporter_sent_{signal}", 0)
        failed = totals.get(f"otelcol_exporter_send_failed_{signal}", 0)
        refused = totals.get(f"otelcol_receiver_refused_{signal}", 0)
        # Metrics: the exporter can legitimately send more points than the
        # file receiver accepted (the collector generates points of its
        # own), so require the sent count to cover the accepted count
        # rather than match it exactly. Spans and log records still match.
        if sent <= 0 or sent < accepted or failed or refused:
            return False
    return True


def wait_for_exporter_ack(
    metrics_url: str, needed: dict[str, bool], timeout_s: int, container: str
) -> dict[str, float]:
    """Poll collector self-metrics until every required signal is fully sent."""
    deadline = time.monotonic() + timeout_s
    stable = 0
    reached = False
    last: dict[str, float] = {}
    while time.monotonic() < deadline:
        info = inspect_container(container)
        if info is not None and not info["running"]:
            raise ToolError(f"replay collector {container} exited before confirming delivery")
        totals = scrape_totals(metrics_url)
        if totals is not None:
            reached = True
            last = totals
            stable = stable + 1 if exporter_ack_ok(totals, needed) else 0
            if stable >= 2:
                return totals
        else:
            stable = 0
        time.sleep(2)
    if not reached:
        raise ToolError("replay collector self-metrics never became reachable on loopback")
    raise ToolError(f"exporter acknowledgement not confirmed within {timeout_s}s; last counters: {last}")


def wait_healthy(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        got = http_get(url, timeout=2.0)
        if got is not None and got[0] == 200:
            return
        time.sleep(2)
    raise ToolError(f"{url} did not become healthy within {int(timeout_s)}s")


QUADLET_NAME = "ci-observe-backend"
QUADLET_CONTAINER = "ci-observe-lgtm"
QUADLET_VOLUME = "ci-observe-lgtm-quadlet-data"
# The ports the rendered quadlet publishes; they must match the Grafana
# datasource URLs provisioned by monitoring/config/grafana/datasources.yml.
QUADLET_PORTS = {"ui": 3300, "otlp": 4318, "tempo": 3200, "loki": 3100, "metrics": 9092}


def systemctl_user(*args: str) -> None:
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise ToolError(f"systemctl --user {' '.join(args)} failed: {result.stderr.strip() or result.returncode}")


def render_quadlet(state_dir: Path) -> str:
    """The systemd quadlet for the CI backend container.

    A workflow job cannot own a long-lived rootless container: the runner
    reaps the job's process group when it ends, which kills pasta (rootless
    podman's port forwarder) and network-isolates the container. Running the
    backend as a user quadlet hands it to systemd, exactly like every other
    long-lived rootless container on the central machine. The config mounts
    point at copies inside the state dir, which is stable across jobs.
    """
    config = state_dir / "config"
    lines = [
        "[Unit]",
        "Description=ci-observe telemetry backend (Grafana LGTM stack)",
        "",
        "[Container]",
        f"ContainerName={QUADLET_CONTAINER}",
        f"Image={LGTM_IMAGE}",
        f"Label={OWNED_LABEL}=true",
        "Label=osac.ci-observe.state=quadlet",
        f"PublishPort=127.0.0.1:{QUADLET_PORTS['ui']}:3000",
        f"PublishPort=127.0.0.1:{QUADLET_PORTS['otlp']}:4318",
        f"PublishPort=127.0.0.1:{QUADLET_PORTS['tempo']}:3200",
        f"PublishPort=127.0.0.1:{QUADLET_PORTS['loki']}:3100",
        f"PublishPort=127.0.0.1:{QUADLET_PORTS['metrics']}:9090",
        f"Volume={QUADLET_VOLUME}:/data",
        f"Volume={config / PROMETHEUS_OVERRIDE.name}:/otel-lgtm/prometheus.yaml:ro,Z",
        f"Volume={config / LOKI_OVERRIDE.name}:/otel-lgtm/loki-config.yaml:ro,Z",
        f"Volume={config / TEMPO_OVERRIDE.name}:/otel-lgtm/tempo-config.yaml:ro,Z",
        f"Volume={config / PYROSCOPE_STUB.name}:/otel-lgtm/run-pyroscope.sh:ro,Z",
        "",
        "[Service]",
        "Restart=on-failure",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def up_quadlet(state_dir: Path) -> int:
    for override in (PROMETHEUS_OVERRIDE, LOKI_OVERRIDE, TEMPO_OVERRIDE, PYROSCOPE_STUB):
        if not override.is_file():
            raise ToolError(f"required LGTM override {override} is missing from this checkout")
    config = state_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    os.chmod(config, 0o700)
    for override in (PROMETHEUS_OVERRIDE, LOKI_OVERRIDE, TEMPO_OVERRIDE, PYROSCOPE_STUB):
        target = config / override.name
        shutil.copyfile(override, target)
        # The stub is executed by the image's entrypoint; the image's own
        # component scripts are 0755, and a non-executable copy aborts the
        # whole stack with "run-pyroscope.sh: Permission denied".
        os.chmod(target, 0o755 if override is PYROSCOPE_STUB else 0o600)
    quadlet_dir = Path.home() / ".config" / "containers" / "systemd"
    quadlet_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(quadlet_dir / f"{QUADLET_NAME}.container", render_quadlet(state_dir))
    systemctl_user("daemon-reload")
    # restart (not start): a config or image change must reach an already
    # running backend, and a first install is a restart of nothing.
    systemctl_user("restart", f"{QUADLET_NAME}.service")
    wait_healthy(f"http://127.0.0.1:{QUADLET_PORTS['ui']}/api/health", 120)
    atomic_write(
        stack_path(state_dir),
        json.dumps(
            {
                "version": STACK_VERSION,
                "managed": "quadlet",
                "container": QUADLET_CONTAINER,
                "volume": QUADLET_VOLUME,
                "image": LGTM_IMAGE,
                "ui_port": QUADLET_PORTS["ui"],
                "otlp_port": QUADLET_PORTS["otlp"],
                "tempo_port": QUADLET_PORTS["tempo"],
                "loki_port": QUADLET_PORTS["loki"],
                "metrics_port": QUADLET_PORTS["metrics"],
                "lgtm_overrides": str(config),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    emit(
        {
            "status": "started",
            "url": f"http://127.0.0.1:{QUADLET_PORTS['ui']}",
            "state_dir": str(state_dir),
            "container": QUADLET_CONTAINER,
            "unit": f"{QUADLET_NAME}.service",
            "image": LGTM_IMAGE,
            "otlp_endpoint": f"http://127.0.0.1:{QUADLET_PORTS['otlp']}",
        }
    )
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    if getattr(args, "quadlet", False):
        return up_quadlet(args.state_dir)
    state_dir = args.state_dir
    iid = instance_id(state_dir)
    container, volume = f"ci-observe-lgtm-{iid}", f"ci-observe-lgtm-data-{iid}"
    for override in (PROMETHEUS_OVERRIDE, LOKI_OVERRIDE, TEMPO_OVERRIDE, PYROSCOPE_STUB):
        if not override.is_file():
            raise ToolError(f"required LGTM override {override} is missing from this checkout")
    requested = {name: getattr(args, f"{name}_port") for name in OPTIONAL_PORTS}
    for name, port in requested.items():
        if port < 0:
            raise ToolError(f"--{name}-port must be 0 (not published) or a positive port number")
    info = inspect_container(container)
    if info is not None:
        require_owned(info, container)
        if not image_matches(info["image"], LGTM_IMAGE):
            raise ToolError(f"{container} was created from a different image; remove it before starting a new stack")
        record = load_json(stack_path(state_dir))
        if record is None or record.get("version") != STACK_VERSION:
            raise ToolError(f"{container} was created with an older stack layout; run 'down --purge' and 'up' again")
        # Ports are only assigned at container creation; keep whatever the
        # existing container was created with and refuse a change of plan.
        published_optional: dict[str, int | None] = {}
        for name, port in requested.items():
            display = OPTIONAL_PORTS[name][1]
            recorded = record.get(f"{name}_port")
            if port != 0 and port != recorded:
                where = f"on loopback port {recorded}" if recorded else "unpublished"
                raise ToolError(
                    f"{container} was created with {display} {where}, not on port {port}; ports are only set at "
                    "container creation -- run 'down --purge' and 'up' again"
                )
            published_optional[name] = recorded
        if not info["running"]:
            podman(["start", container])
            wait_healthy(f"http://127.0.0.1:{published_port(container, 3000)}/api/health", 120)
        ui_port, otlp_port = published_port(container, 3000), published_port(container, 4318)
        status = "already-up" if info["running"] else "restarted"
    else:
        if args.ui_port != 0 and args.ui_port == args.otlp_port:
            raise ToolError("UI and OTLP ports must differ")
        taken: dict[int, str] = {}
        for what, port in (("UI", args.ui_port), ("OTLP", args.otlp_port)):
            if port != 0:
                taken[port] = what
        for name, port in requested.items():
            if port == 0:
                continue
            display = OPTIONAL_PORTS[name][1]
            if port in taken:
                raise ToolError(f"--{name}-port must differ from the {taken[port]} port")
            taken[port] = display
        check_port_free(args.ui_port, "UI")
        check_port_free(args.otlp_port, "OTLP")
        for name, port in requested.items():
            if port != 0:
                check_port_free(port, OPTIONAL_PORTS[name][1])
        vol = podman(["volume", "inspect", "--format", "{{json .Labels}}", volume], check=False)
        if vol.returncode == 0:
            labels = json.loads(vol.stdout.strip()) or {}
            if labels.get(OWNED_LABEL) != "true":
                raise ToolError(f"volume {volume} exists but is not labeled as owned; refusing")
        else:
            podman(["volume", "create", "--label", f"{OWNED_LABEL}=true", volume])
        ui_map = "127.0.0.1::3000" if args.ui_port == 0 else f"127.0.0.1:{args.ui_port}:3000"
        otlp_map = "127.0.0.1::4318" if args.otlp_port == 0 else f"127.0.0.1:{args.otlp_port}:4318"
        port_maps = ["-p", ui_map, "-p", otlp_map]
        for name, port in requested.items():
            if port != 0:
                port_maps += ["-p", f"127.0.0.1:{port}:{OPTIONAL_PORTS[name][0]}"]
        podman(
            [
                "run",
                "-d",
                "--name",
                container,
                "--label",
                f"{OWNED_LABEL}=true",
                "--label",
                f"osac.ci-observe.state={iid}",
                *port_maps,
                "-v",
                f"{volume}:/data",
                "-v",
                f"{PROMETHEUS_OVERRIDE.resolve()}:/otel-lgtm/prometheus.yaml:ro,Z",
                "-v",
                f"{LOKI_OVERRIDE.resolve()}:/otel-lgtm/loki-config.yaml:ro,Z",
                # Pyroscope cannot start under rootless podman's pasta
                # networking (its ring lifecyclers insist on an address on
                # eth0/en0, which pasta does not create when it mirrors the
                # host's interface names). The capture stack uses traces,
                # metrics and logs only, so a stub keeps the component slot
                # alive and the rest of the stack serves normally.
                "-v",
                f"{PYROSCOPE_STUB.resolve()}:/otel-lgtm/run-pyroscope.sh:ro,Z",
                LGTM_IMAGE,
            ]
        )
        ui_port, otlp_port = published_port(container, 3000), published_port(container, 4318)
        published_optional = {name: None for name in OPTIONAL_PORTS}
        for name, port in requested.items():
            if port != 0:
                published_optional[name] = published_port(container, OPTIONAL_PORTS[name][0])
        try:
            wait_healthy(f"http://127.0.0.1:{ui_port}/api/health", 120)
        except ToolError:
            podman(["rm", "-f", container], check=False)
            raise
        status = "started"
    stack = {
        "version": STACK_VERSION,
        "container": container,
        "volume": volume,
        "image": LGTM_IMAGE,
        "ui_port": ui_port,
        "otlp_port": otlp_port,
        **{f"{name}_port": published_optional[name] for name in OPTIONAL_PORTS},
        "lgtm_overrides": str(LGTM_OVERRIDES_DIR),
    }
    atomic_write(stack_path(state_dir), json.dumps(stack, indent=2, sort_keys=True) + "\n")
    emit(
        {
            "status": status,
            "url": f"http://127.0.0.1:{ui_port}",
            "state_dir": str(state_dir),
            "container": container,
            "volume": volume,
            "image": LGTM_IMAGE,
            "otlp_endpoint": f"http://127.0.0.1:{otlp_port}",
            **{f"{name}_port": published_optional[name] for name in OPTIONAL_PORTS},
        }
    )
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    record = load_json(stack_path(args.state_dir))
    if record is not None and record.get("managed") == "quadlet":
        systemctl_user("stop", f"{QUADLET_NAME}.service")
        emit(
            {
                "status": "down",
                "state_dir": str(args.state_dir),
                "container": record.get("container"),
                "unit": f"{QUADLET_NAME}.service",
                "volume": record.get("volume"),
                "volume_preserved": True,
            }
        )
        return 0
    iid = instance_id(args.state_dir)
    container, volume = f"ci-observe-lgtm-{iid}", f"ci-observe-lgtm-data-{iid}"
    info = inspect_container(container)
    if info is None:
        emit({"status": "not-running", "state_dir": str(args.state_dir), "volume": volume, "volume_preserved": True})
        return 0
    require_owned(info, container)
    if info["running"]:
        podman(["stop", "-t", "60", container])
    volume_preserved = True
    # The container is kept, only stopped: Tempo in this image keeps recent traces in a
    # container-local live store (/var/tempo) until they are cut into blocks on the data
    # volume, so removing the container would lose them. --purge removes everything.
    if args.purge:
        podman(["rm", container])
        vol = podman(["volume", "inspect", "--format", "{{json .Labels}}", volume], check=False)
        if vol.returncode == 0:
            labels = json.loads(vol.stdout.strip()) or {}
            if labels.get(OWNED_LABEL) != "true":
                raise ToolError(f"volume {volume} is not labeled as owned; refusing to delete it")
            podman(["volume", "rm", volume])
            volume_preserved = False
    emit(
        {
            "status": "down",
            "state_dir": str(args.state_dir),
            "container": container,
            "container_preserved": not args.purge,
            "volume": volume,
            "volume_preserved": volume_preserved,
        }
    )
    return 0


def resolve_capture_dir(args: argparse.Namespace, state_dir: Path) -> Path:
    if args.capture_dir and args.capture_id:
        raise ToolError("pass either --capture-dir or --capture-id, not both")
    if args.capture_dir:
        return Path(args.capture_dir).expanduser()
    if args.capture_id:
        return state_dir / "captures" / args.capture_id
    captures_dir = state_dir / "captures"
    known = sorted(p.name for p in captures_dir.glob("*") if p.is_dir()) if captures_dir.is_dir() else []
    raise ToolError(f"no capture specified; known capture ids in {state_dir}: {known or 'none'}")


def record_import(state_dir: Path, digest: str, entry: dict[str, Any]) -> None:
    index = load_index(state_dir)
    index["imports"][digest] = entry
    save_index(state_dir, index)


def import_capture(
    state_dir: Path,
    capture_dir: Path,
    *,
    replay_config: Path | None = None,
    collector_image: str = COLLECTOR_IMAGE,
    timeout: int = 300,
    tolerate_modified: bool = False,
) -> dict[str, Any]:
    """Validate a closed capture and replay it into the local stack.

    Returns the result dict (status "imported" or "already-imported") for the caller
    to emit or tally. Raises ToolError for every refusal, including the refusal to
    retry an import whose delivery state is uncertain. tolerate_modified is passed
    through to validate_capture (the sync sets it: the CI upload path redacts
    capture files after the close step hashed them).
    """
    validated = validate_capture(capture_dir, tolerate_modified=tolerate_modified)
    meta, digest = validated["metadata"], validated["capture_digest"]
    entry = load_index(state_dir)["imports"].get(digest)
    if entry and entry.get("status") == "imported":
        return {
            "status": "already-imported",
            "capture_id": meta.get("id"),
            "capture_digest": digest,
            "imported_at": entry.get("imported_at"),
        }
    if entry and entry.get("status") == "incomplete":
        raise ToolError(
            f"a previous import of capture digest {digest} is incomplete ({entry.get('reason')}); "
            "delivery state is uncertain, so this tool refuses to import it again into the same "
            "backends. Use a fresh --state-dir for a new attempt."
        )
    sizes = {f["path"]: f["size_bytes"] for f in meta["files"]}
    needed = {signal: sizes.get(fname, 0) > 0 for fname, signal in SIGNALS.items()}
    if not any(needed.values()):
        raise ToolError("capture contains no non-empty signal files; nothing to import")
    stack = load_json(stack_path(state_dir))
    if stack is None:
        raise BackendUnavailable("no local stack record; run 'up' first")
    info = inspect_container(stack["container"])
    if info is None or not info["running"]:
        raise BackendUnavailable(f"local stack container {stack['container']} is not running; run 'up' first")
    require_owned(info, stack["container"])
    config = replay_config if replay_config is not None else REPLAY_CONFIG
    if not config.is_file():
        raise ToolError(
            f"replay configuration {config} not found; the monitoring/config/ci-observe lane "
            "supplies it, or pass --replay-config",
            code=2,
        )
    endpoint = f"http://127.0.0.1:{stack['otlp_port']}"
    iid = instance_id(state_dir)
    replay = f"ci-observe-replay-{iid}"
    # The replay config persists per-capture file offsets there so a re-import
    # resumes instead of duplicating; the capture digest is stable even when
    # --capture-dir was used instead of --capture-id. The directory sits inside
    # the private 0700 state tree; it is world-writable inside because the
    # contrib image runs as UID 10001, which rootless podman maps to an
    # unrelated host UID.
    replay_state = state_dir / "replay-state" / digest[:16]
    replay_state.mkdir(parents=True, exist_ok=True)
    replay_state.chmod(0o777)
    old = inspect_container(replay)
    if old is not None:
        require_owned(old, replay)
        podman(["rm", "-f", replay])
    podman(
        [
            "run",
            "-d",
            "--name",
            replay,
            "--label",
            f"{OWNED_LABEL}=true",
            "--label",
            f"osac.ci-observe.state={iid}",
            "--network",
            "host",
            "-v",
            f"{capture_dir.resolve()}:/capture:ro,Z",
            "-v",
            f"{replay_state.resolve()}:/replay-state:Z",
            "-v",
            f"{config.resolve()}:/replay.yaml:ro,Z",
            "-e",
            "CI_OBSERVE_CAPTURE_DIR=/capture",
            "-e",
            "CI_OBSERVE_REPLAY_STATE=/replay-state",
            "-e",
            f"CI_OBSERVE_OTLP_ENDPOINT={endpoint}",
            collector_image,
            "--config=/replay.yaml",
        ]
    )
    base_entry = {
        "capture_id": meta.get("id"),
        "capture_dir": str(capture_dir.resolve()),
        "collector_image": collector_image,
        "otlp_endpoint": endpoint,
        "replay_config": str(config.resolve()),
    }
    try:
        totals = wait_for_exporter_ack(f"http://127.0.0.1:{REPLAY_METRICS_PORT}/metrics", needed, timeout, replay)
    except KeyboardInterrupt:
        podman(["rm", "-f", replay], check=False)
        record_import(state_dir, digest, {**base_entry, "status": "incomplete", "reason": "interrupted"})
        raise
    except ToolError as err:
        podman(["rm", "-f", replay], check=False)
        record_import(state_dir, digest, {**base_entry, "status": "incomplete", "reason": str(err)})
        raise
    podman(["rm", "-f", replay])
    counts = {
        signal: {
            "accepted": totals.get(f"otelcol_receiver_accepted_{signal}", 0),
            "sent": totals.get(f"otelcol_exporter_sent_{signal}", 0),
        }
        for signal in needed
    }
    # Force a Loki chunk flush so logs replayed with old timestamps are queryable
    # immediately; the stock image hides unflushed old logs from queries.
    flush = podman(["exec", stack["container"], "curl", "-s", "-X", "POST", "http://localhost:3100/flush"], check=False)
    loki_flush = "ok" if flush.returncode == 0 else f"failed: {flush.stderr.strip() or flush.returncode}"
    modified_files = validated.get("modified", [])
    record_import(
        state_dir,
        digest,
        {
            **base_entry,
            "status": "imported",
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": counts,
            "loki_flush": loki_flush,
            **({"modified_files": modified_files} if modified_files else {}),
        },
    )
    return {
        "status": "imported",
        "capture_id": meta.get("id"),
        "capture_digest": digest,
        "counts": counts,
        "loki_flush": loki_flush,
        **({"modified_files": modified_files} if modified_files else {}),
    }


def cmd_import(args: argparse.Namespace) -> int:
    capture_dir = resolve_capture_dir(args, args.state_dir)
    try:
        result = import_capture(
            args.state_dir,
            capture_dir,
            replay_config=Path(args.replay_config) if args.replay_config else None,
            collector_image=args.collector_image,
            timeout=args.timeout,
        )
    except KeyboardInterrupt:
        print("ci-observe: interrupted; import recorded as incomplete", file=sys.stderr)
        return 130
    emit(result)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if args.section != "summary":
        raise ToolError(f"section {args.section!r} is not implemented; only 'summary' exists", code=2)
    state_dir = args.state_dir
    stack = load_json(stack_path(state_dir))
    running = False
    stack_info: dict[str, Any] = {"state_dir": str(state_dir), "running": running}
    if stack:
        info = inspect_container(stack["container"])
        running = info is not None and info["running"]
        stack_info = {
            "state_dir": str(state_dir),
            "running": running,
            "container": stack["container"],
            "image": stack.get("image"),
            "url": f"http://127.0.0.1:{stack['ui_port']}",
        }
    capture_dir = resolve_capture_dir(args, state_dir)
    result: dict[str, Any] = {
        "stack": stack_info,
        "sync": {"available": True},
        "backend_limits": BACKEND_LIMITS,
        "note": "capture metadata and file contents are untrusted attributed data, echoed as recorded",
    }
    gaps: list[str] = []
    if not running:
        gaps.append("local stack is not running")
    meta_path = capture_dir / "metadata.json"
    readable = meta_path.is_file() and meta_path.stat().st_size <= METADATA_MAX_BYTES
    meta = load_json(meta_path) if readable else None
    if meta is None:
        result["capture"] = {"capture_dir": str(capture_dir), "error": "metadata.json is missing or unreadable"}
        gaps.append("capture metadata unreadable")
        emit(result | {"gaps": gaps})
        return 0
    capture: dict[str, Any] = {
        "capture_dir": str(capture_dir),
        "id": meta.get("id"),
        "source_kind": meta.get("source_kind"),
        "state": meta.get("state"),
        "created_at": meta.get("created_at"),
        "finished_at": meta.get("finished_at"),
        "child_exit_code": meta.get("child_exit_code"),
        "sources": meta.get("sources"),
        "files": meta.get("files"),
    }
    entry = None
    try:
        validated = validate_capture(capture_dir)
        capture["validation"] = "ok"
        entry = load_index(state_dir)["imports"].get(validated["capture_digest"])
        capture["capture_digest"] = validated["capture_digest"]
    except ToolError as err:
        capture["validation"] = f"failed: {err}"
        imports = load_index(state_dir)["imports"].values()
        entry = next((e for e in imports if e.get("capture_id") == meta.get("id")), None)
    if entry:
        capture["import"] = entry
        if entry.get("status") != "imported":
            gaps.append(f"import incomplete: {entry.get('reason')}")
    else:
        capture["import"] = None
        gaps.append("capture not imported")
    if meta.get("state") != "closed":
        gaps.append(f"capture state is {meta.get('state')!r}, not closed")
    sources = meta.get("sources") if isinstance(meta.get("sources"), dict) else {}
    for name in EXPECTED_SOURCES:
        status = sources.get(name)
        if status is None:
            gaps.append(f"source {name}: no status reported")
        elif status not in ("collected", "delivered"):
            # The capture writers report "delivered" or "missing"; accept
            # both spellings so a fully delivered capture is not a gap.
            gaps.append(f"source {name}: status {status!r}")
    files = meta.get("files") if isinstance(meta.get("files"), list) else []
    for fname in SIGNALS:
        match = next((f for f in files if isinstance(f, dict) and f.get("path") == fname), None)
        if match is None:
            gaps.append(f"no {fname} in capture")
        elif match.get("size_bytes") == 0:
            gaps.append(f"{fname} is empty")
    if meta.get("child_exit_code") is None:
        gaps.append("no child exit code recorded")
    result["capture"] = capture
    emit(result | {"gaps": gaps})
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    sibling = Path(__file__).resolve().parent / "ci_observe_capture.py"
    if not sibling.is_file():
        raise ToolError(
            "capture backend is not available yet: monitoring/scripts/ci_observe_capture.py "
            "does not exist (it is supplied by the capture-backend integration, not this tool)",
            code=2,
        )
    spec = importlib.util.spec_from_file_location("ci_observe_capture", sibling)
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution looks the module up in sys.modules by __module__;
    # without registration it crashes with 'NoneType' has no '__dict__'.
    sys.modules[spec.name] = module
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    except Exception as err:
        raise ToolError(f"failed to load capture backend {sibling}: {err}", code=2) from err
    handler = getattr(module, "capture", None)
    if not callable(handler):
        raise ToolError(f"capture backend {sibling} does not define capture(args)", code=2)
    return handler(args)


def list_workflow_runs(repository: str, since: datetime) -> list[dict[str, Any]]:
    """Workflow runs created inside the window, from `gh run list`.

    gh has no --since flag for `run list`; --created ><date> filters by whole days on
    the server (strictly after that day's end), so the day before the window start is
    requested and createdAt is filtered here to the exact window.
    """
    day = (since - timedelta(days=1)).strftime("%Y-%m-%d")
    data = gh_json(
        [
            "run",
            "list",
            "--repo",
            repository,
            "--created",
            f">{day}",
            "--limit",
            str(SYNC_RUN_LIST_LIMIT),
            "--json",
            "databaseId,attempt,status,conclusion,createdAt",
        ]
    )
    if not isinstance(data, list):
        raise ToolError(f"unexpected run listing response for {repository}")
    runs = []
    for run in data:
        if not isinstance(run, dict):
            continue
        created = parse_utc(run.get("createdAt"))
        if created is not None and created >= since:
            runs.append(run)
    return runs


def list_run_artifacts(repository: str, run_id: int) -> list[dict[str, Any]]:
    """Artifacts of one run, via the per-run listing endpoint, with retries.

    The repository-wide listing (repos/{repo}/actions/artifacts) is unstable
    for repositories with tens of thousands of artifacts: GitHub
    intermittently answers it with a bare 500 and an empty body, which gh
    surfaces as unparseable JSON. The per-run listing returns a handful of
    records and is stable; transient failures still get a short retry. The
    per-run response carries no workflow_run, so the run id is injected for
    the artifact matching to cross-check.
    """

    def fetch_page(page: int) -> list[dict[str, Any]]:
        args = ["api", f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"]
        last_error = "no attempt made"
        for remaining in range(3, 0, -1):
            try:
                data = gh_json(args)
                if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
                    raise ToolError(f"unexpected artifact listing response for run {run_id}")
                return [artifact for artifact in data["artifacts"] if isinstance(artifact, dict)]
            except ToolError as err:
                last_error = str(err)
                if remaining > 1:
                    time.sleep(5)
        raise ToolError(f"gh {' '.join(args)} failed after retries: {last_error}")

    # Every page, not just the first: a run with more than 100 artifacts
    # could carry its telemetry artifact on a later page, and a miss here
    # would mark the run as having no telemetry permanently.
    collected: list[dict[str, Any]] = []
    page = 1
    while True:
        page_artifacts = fetch_page(page)
        collected.extend(page_artifacts)
        if len(page_artifacts) < 100:
            return [artifact | {"workflow_run": {"id": run_id}} for artifact in collected]
        page += 1


def find_telemetry_artifacts(
    runs: list[dict[str, Any]], artifacts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match telemetry artifacts to in-window runs.

    Returns the matched artifacts, each extended with its parsed run_id and attempt and
    sorted by run, attempt and artifact id, plus detail entries for artifacts that carry
    the telemetry prefix but cannot be used. Artifacts of runs outside the window and
    artifacts without the prefix are none of this tool's business and are skipped
    silently.
    """
    run_ids = {run.get("databaseId") for run in runs}
    matched: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for artifact in artifacts:
        name = artifact.get("name")
        if not isinstance(name, str) or not name.startswith(ARTIFACT_NAME_PREFIX):
            continue
        base = {"artifact_name": name, "artifact_id": artifact.get("id")}
        match = None
        for pattern in ARTIFACT_NAME_PATTERNS:
            match = pattern.fullmatch(name)
            if match is not None:
                break
        if match is None:
            reason = "name does not encode a run id and attempt"
            ignored.append(base | {"status": "ignored", "reason": reason})
            continue
        run_id, attempt = int(match.group(1)), int(match.group(2))
        workflow_run = artifact.get("workflow_run") if isinstance(artifact.get("workflow_run"), dict) else {}
        workflow_run_id = workflow_run.get("id")
        if run_id not in run_ids and workflow_run_id not in run_ids:
            continue
        if workflow_run_id is not None and workflow_run_id != run_id:
            reason = f"name run id {run_id} does not match workflow run {workflow_run_id}"
            ignored.append(base | {"status": "ignored", "reason": reason})
            continue
        if artifact.get("expired"):
            ignored.append(base | {"status": "ignored", "reason": "artifact has expired on GitHub"})
            continue
        if (run_id, attempt) in seen:
            ignored.append(base | {"status": "ignored", "reason": "duplicate artifact for the same run attempt"})
            continue
        seen.add((run_id, attempt))
        matched.append(artifact | {"run_id": run_id, "attempt": attempt})
    matched.sort(key=lambda artifact: (artifact["run_id"], artifact["attempt"], artifact.get("id") or 0))
    return matched, ignored


def download_artifact(repository: str, artifact_id: object, dest: Path) -> None:
    """Download one artifact ZIP through `gh api` into the private cache."""
    if not isinstance(artifact_id, int) or isinstance(artifact_id, bool):
        raise ToolError(f"artifact listing has no usable id for {dest}")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    proc = gh(["api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"])
    if len(proc.stdout) > CAPTURE_MAX_BYTES:
        raise ToolError(f"artifact {artifact_id} exceeds the 1 GiB bound")
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(proc.stdout)
    os.chmod(tmp, 0o600)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink()
        raise ToolError(f"artifact {artifact_id} did not download as a ZIP file")
    os.replace(tmp, dest)


def extract_artifact_zip(zip_path: Path, staging: Path) -> dict[str, Any]:
    """Validate an artifact ZIP, extract it into a fresh staging directory, return metadata.

    The ZIP is untrusted data. Members must be the capture's files, either flat or all
    under one directory (actions/upload-artifact zips the staged ci-observe/<capture_id>
    tree); anything else is unexpected structure. Members must be plain regular files
    (symlinks rejected, directory entries skipped as contentless), and the uncompressed
    total is bounded to the capture size while streaming, not by the declared header
    sizes. Extraction starts only after the whole structure passed validation.
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            # The upload path's release gate writes its report next to the
            # staged capture (osac-artifacts/redaction-report.json), and the
            # upload zips the whole directory, so a real capture ZIP carries
            # the gate's own metadata beside the ci-observe/<capture_id> tree.
            # It is scan output, not capture data: skip it (and the hidden
            # redaction marker, should hidden files ever be included) instead
            # of rejecting the artifact.
            members = [
                info for info in members if info.filename not in ("redaction-report.json", ".redaction-complete")
            ]
            names = [info.filename for info in members]
            if len(set(names)) != len(names):
                raise ToolError("artifact ZIP contains duplicate member names")
            parents = {posixpath.dirname(name) for name in names}
            if parents == {""}:
                prefix = ""
            elif len(parents) == 1:
                prefix = f"{next(iter(parents))}/"
            else:
                raise ToolError(f"unexpected members in artifact ZIP: {sorted(names)}")
            entries: list[tuple[zipfile.ZipInfo, str]] = []
            total = 0
            for info in members:
                rel = info.filename[len(prefix) :] if prefix else info.filename
                if rel not in ARTIFACT_MEMBERS:
                    raise ToolError(f"unexpected member in artifact ZIP: {info.filename!r}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ToolError(f"artifact ZIP member {info.filename!r} is a symlink")
                entries.append((info, rel))
                total += info.file_size
            metadata_size = next((info.file_size for info, rel in entries if rel == "metadata.json"), None)
            if metadata_size is None:
                raise ToolError("artifact ZIP has no metadata.json")
            if metadata_size > METADATA_MAX_BYTES:
                raise ToolError("artifact metadata.json exceeds the 1 MiB bound")
            if total > CAPTURE_MAX_BYTES:
                raise ToolError(f"artifact ZIP expands to {total} bytes, exceeding the 1 GiB bound")
            staging.mkdir(mode=0o700, parents=True)
            written = 0
            for info, rel in entries:
                target = staging / rel
                with archive.open(info) as source, target.open("wb") as sink:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > CAPTURE_MAX_BYTES:
                            raise ToolError("artifact ZIP exceeded the 1 GiB bound while extracting")
                        sink.write(chunk)
                # The replay collector reads the capture as an unrelated mapped UID;
                # the 0700 state tree above it keeps the files private on the host.
                os.chmod(target, 0o644)
    except zipfile.BadZipFile:
        raise ToolError(f"artifact {zip_path} is not a readable ZIP file") from None
    meta = load_json(staging / "metadata.json")
    if meta is None:
        raise ToolError("artifact metadata.json is empty")
    return meta


def sync_artifact(
    state_dir: Path, *, repository: str, artifact: dict[str, Any], can_download: bool, limit: int
) -> dict[str, Any]:
    """Download (or reuse the cached) ZIP of one artifact and import its capture.

    Returns the report detail entry; its status is one of imported, skipped, failed
    or deferred. The ZIP is cached per run and attempt and never re-downloaded.
    """
    run_id, attempt = artifact["run_id"], artifact["attempt"]
    base = {"run_id": run_id, "attempt": attempt, "artifact_id": artifact.get("id"), "artifact_name": artifact["name"]}
    cache_dir = state_dir / "artifacts" / str(run_id) / str(attempt)
    zip_path = cache_dir / "artifact.zip"
    if not zip_path.is_file():
        if not can_download:
            return base | {"status": "deferred", "reason": f"download limit of {limit} artifacts reached"}
        current = state_dir / "artifacts"
        for part in (current, current / str(run_id), cache_dir):
            part.mkdir(mode=0o700, exist_ok=True)
            os.chmod(part, 0o700)  # mkdir alone applies the umask
        download_artifact(repository, artifact.get("id"), zip_path)
        base["downloaded"] = True
    record = load_json(cache_dir / "record.json")
    if record is not None:
        if record.get("status") == "refused":
            # A previous pass already downloaded and extracted this artifact
            # and the import refused it (an interrupted capture, an empty
            # capture). The refusal is deterministic, so retrying every
            # 15-minute pass would only re-fail; delete record.json to retry.
            return base | {
                "status": "skipped",
                "capture_id": record.get("capture_id"),
                "reason": f"refused earlier: {record.get('reason')}",
            }
        entry = load_index(state_dir)["imports"].get(record.get("capture_digest"))
        if entry is not None and entry.get("status") == "imported":
            return base | {"status": "skipped", "capture_id": record.get("capture_id"), "reason": "already imported"}
        # An incomplete import falls through and is refused by the import path below.
    staging = cache_dir / "extracted"
    shutil.rmtree(staging, ignore_errors=True)
    capture_id: str | None = None
    try:
        meta = extract_artifact_zip(zip_path, staging)
        capture_id = meta.get("id")
        if not isinstance(capture_id, str) or not CAPTURE_ID_RE.fullmatch(capture_id):
            raise ToolError(f"capture id {capture_id!r} is not a safe capture directory name")
        target = state_dir / "captures" / capture_id
        if target.exists():
            # A previous sync already extracted this capture; keep the existing directory.
            shutil.rmtree(staging, ignore_errors=True)
        else:
            (state_dir / "captures").mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(staging, target)
            os.chmod(target, 0o755)
        # The upload path's scanners may have redacted the capture's files
        # after the close step hashed them; import what is actually there
        # and record the difference instead of refusing (local imports of
        # untouched captures stay strict).
        result = import_capture(state_dir, target, tolerate_modified=True)
        atomic_write(
            cache_dir / "record.json",
            json.dumps(
                {
                    "capture_id": capture_id,
                    "capture_digest": result["capture_digest"],
                    "artifact_name": artifact["name"],
                    "processed_at": iso_z(utc_now()),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        detail = base | {
            "capture_id": capture_id,
            "capture_digest": result["capture_digest"],
            "status": "imported" if result["status"] == "imported" else "skipped",
        }
        if result["status"] == "already-imported":
            detail["reason"] = "already imported"
        if result.get("modified_files"):
            detail["modified_files"] = result["modified_files"]
        return detail
    except BackendUnavailable:
        # The backend being down is recoverable: the next pass retries once
        # `up` has brought it back, so nothing is recorded and the artifact
        # is reported as deferred, not failed.
        shutil.rmtree(staging, ignore_errors=True)
        detail = base | {"status": "deferred", "reason": "backend unavailable; will retry on the next pass"}
        if capture_id is not None:
            detail["capture_id"] = capture_id
        return detail
    except ToolError as err:
        shutil.rmtree(staging, ignore_errors=True)
        detail = base | {"status": "failed", "reason": str(err)}
        if capture_id is not None:
            detail["capture_id"] = capture_id
        # Record the refusal -- with or without a capture id -- so later
        # passes do not download, extract and fail the same artifact every
        # 15 minutes for the rest of the window. A failure before the
        # capture id is known (a malformed ZIP, a missing metadata.json) is
        # just as deterministic as one after it. The ZIP stays cached;
        # deleting record.json retries.
        atomic_write(
            cache_dir / "record.json",
            json.dumps(
                {"capture_id": capture_id, "status": "refused", "reason": str(err), "processed_at": iso_z(utc_now())},
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return detail


def sync_iteration(state_dir: Path, *, repository: str, since: str, limit: int) -> dict[str, Any]:
    """One sync pass: list runs and artifacts, download and import what is new."""
    since_at, until_at = resolve_since(since)
    runs = list_workflow_runs(repository, since_at)
    # Artifacts are listed per run (the repository-wide listing is unstable;
    # see list_run_artifacts). Completed runs only: an in-progress run has
    # no artifacts yet. Runs already processed on an earlier pass are
    # skipped -- a record for the run's latest attempt means its artifact
    # was imported or refused, a no-telemetry marker means it had none --
    # so the steady state only lists runs that are new or re-run.
    artifacts: list[dict[str, Any]] = []
    for run in runs:
        run_id, attempt_no, status = run.get("databaseId"), run.get("attempt") or 1, run.get("status")
        if not isinstance(run_id, int) or status != "completed":
            continue
        run_cache = state_dir / "artifacts" / str(run_id) / str(attempt_no)
        if (run_cache / "record.json").is_file() or (run_cache / ".no-telemetry").is_file():
            continue
        run_artifacts = list_run_artifacts(repository, run_id)
        if not any(
            isinstance(a.get("name"), str) and a["name"].startswith(ARTIFACT_NAME_PREFIX) for a in run_artifacts
        ):
            run_cache.mkdir(parents=True, exist_ok=True)
            (run_cache / ".no-telemetry").write_text("")
            continue
        artifacts.extend(run_artifacts)
    matched, ignored = find_telemetry_artifacts(runs, artifacts)
    details: list[dict[str, Any]] = list(ignored)
    downloaded = imported = skipped = failed = 0
    for artifact in matched:
        detail = sync_artifact(
            state_dir, repository=repository, artifact=artifact, can_download=downloaded < limit, limit=limit
        )
        details.append(detail)
        if detail.get("downloaded"):
            downloaded += 1
        if detail["status"] == "imported":
            imported += 1
        elif detail["status"] == "skipped":
            skipped += 1
        elif detail["status"] == "failed":
            failed += 1
    return {
        "repository": repository,
        "window": {"since": iso_z(since_at), "until": iso_z(until_at)},
        "runs_found": len(runs),
        "artifacts_found": len(matched),
        "artifacts_downloaded": downloaded,
        "captures_imported": imported,
        "captures_skipped": skipped,
        "captures_failed": failed,
        "details": details,
    }


def cmd_sync(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repository):
        raise ToolError(f"invalid --repository {args.repository!r}: expected OWNER/NAME")
    if args.limit < 1:
        raise ToolError("--limit must be at least 1")
    while True:
        report = sync_iteration(args.state_dir, repository=args.repository, since=args.since, limit=args.limit)
        emit(report)
        if not args.watch:
            return 1 if report["captures_failed"] else 0
        time.sleep(SYNC_WATCH_INTERVAL_S)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci-observe.py", description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", help=f"state directory (default {DEFAULT_STATE_DIR})")
    sub = parser.add_subparsers(dest="action", required=True)

    up = sub.add_parser("up", help="start the pinned local Grafana/LGTM stack on loopback ports")
    up.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT, help="loopback Grafana UI port (0 = ephemeral)")
    up.add_argument("--otlp-port", type=int, default=DEFAULT_OTLP_PORT, help="loopback OTLP/HTTP port (0 = ephemeral)")
    up.add_argument(
        "--quadlet",
        action="store_true",
        help="manage the backend as a systemd user quadlet instead of an ad-hoc container: required on CI runners, "
        "where the job's process-tree cleanup kills an ad-hoc container's port forwarding; fixed ports",
    )
    up.add_argument(
        "--tempo-port",
        type=int,
        default=0,
        help="also publish Tempo on 127.0.0.1:<port> (container port 3200; 0 = not published, unlike --ui-port)",
    )
    up.add_argument(
        "--loki-port",
        type=int,
        default=0,
        help="also publish Loki on 127.0.0.1:<port> (container port 3100; 0 = not published, unlike --ui-port)",
    )
    up.add_argument(
        "--metrics-port",
        type=int,
        default=0,
        help="also publish Prometheus on 127.0.0.1:<port> (container port 9090; 0 = not published, unlike --ui-port)",
    )

    down = sub.add_parser("down", help="stop the owned stack, preserving captured data")
    down.add_argument("--purge", action="store_true", help="also remove the owned container and data volume")

    imp = sub.add_parser("import", help="validate a closed capture and replay it into the local stack")
    imp.add_argument("--capture-dir", help="capture directory path")
    imp.add_argument("--capture-id", help="capture id under <state-dir>/captures/")
    imp.add_argument("--replay-config", help=f"replay collector config (default {REPLAY_CONFIG})")
    imp.add_argument("--collector-image", default=COLLECTOR_IMAGE, help="pinned otelcol-contrib image reference")
    imp.add_argument("--timeout", type=int, default=300, help="seconds to wait for exporter acknowledgement")

    rep = sub.add_parser("report", help="print JSON facts and gaps for a capture")
    rep.add_argument("--capture-dir", help="capture directory path")
    rep.add_argument("--capture-id", help="capture id under <state-dir>/captures/")
    rep.add_argument("--section", default="summary", help="report section (only 'summary' exists)")

    cap = sub.add_parser("capture", help="run a test command under cluster capture (delegates to ci_observe_capture)")
    cap.add_argument("--kubeconfig", required=True, type=Path)
    cap.add_argument("--namespace", required=True)
    cap.add_argument("--capture-id")
    cap.add_argument("--suite", default="none", help="test suite (vmaas/caas/bmaas); stamped as osac.ci.suite")
    cap.add_argument("--pr", default="none", help="pull request number; stamped as osac.ci.pr")
    cap.add_argument("command", nargs=argparse.REMAINDER, metavar="-- CMD", help="test command after --")

    sync = sub.add_parser("sync", help="download ci-observe telemetry artifacts from GitHub and import them")
    sync.add_argument("--repository", required=True, help="GitHub repository to fetch runs from (OWNER/NAME)")
    sync.add_argument("--since", default="24h", help="time window: 6h, 12h, 24h, 2d, 7d or an ISO date")
    sync.add_argument("--watch", action="store_true", help="keep syncing every 5 minutes until interrupted")
    sync.add_argument("--limit", type=int, default=SYNC_DEFAULT_LIMIT, help="max artifact downloads per invocation")
    return parser


HANDLERS = {
    "up": cmd_up,
    "down": cmd_down,
    "import": cmd_import,
    "report": cmd_report,
    "capture": cmd_capture,
    "sync": cmd_sync,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.state_dir = resolve_state_dir(args.state_dir)
        if args.action == "capture":
            if not args.command or args.command[0] != "--":
                parser.error("capture requires the test command after --")
            args.command = args.command[1:]
            if not args.command:
                parser.error("capture requires a non-empty test command after --")
        return HANDLERS[args.action](args)
    except ToolError as err:
        print(f"ci-observe: error: {err}", file=sys.stderr)
        return err.code
    except KeyboardInterrupt:
        print("ci-observe: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

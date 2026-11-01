"""Tests for monitoring/scripts/ci-observe.py (Milestone 1, local tool).

Runs the real CLI: in-process main() calls for most paths, real subprocesses for the
entry point and capture delegation, and owned scratch Podman containers (pinned
images) for the up/import/down lifecycle. No function fields are injected.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from pytest import CaptureFixture, MonkeyPatch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS_DIR / "ci-observe.py"

_spec = importlib.util.spec_from_file_location("ci_observe", SCRIPT)
ci_observe = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ci_observe)

Tool = ModuleType
Capsys = CaptureFixture[str]

PODMAN = shutil.which("podman") is not None


def _image_present(ref: str) -> bool:
    return subprocess.run(["podman", "image", "inspect", ref], capture_output=True).returncode == 0


HAS_IMAGES = PODMAN and _image_present(ci_observe.LGTM_IMAGE) and _image_present(ci_observe.COLLECTOR_IMAGE)
requires_podman = pytest.mark.skipif(not PODMAN, reason="podman is unavailable")
requires_images = pytest.mark.skipif(not HAS_IMAGES, reason="pinned stack images are unavailable")

SOURCES = {
    "pytest": "collected",
    "beyla": "not-collected",
    "cluster-objects": "collected",
    "cluster-logs": "partial",
    "cluster-metrics": "not-collected",
}


def trace_id_for(capture_id: str) -> str:
    return hashlib.md5(capture_id.encode()).hexdigest()


# Fixed once per test session: identical across regenerations of the same capture id
# so the content digest stays stable. Ages are subtracted from it.
SESSION_START = int(time.time())


def signal_lines(capture_id: str, trace_age_s: int = 120, data_age_s: int = 120) -> dict[str, str]:
    """One valid OTLP JSON record per signal, unique per capture id.

    trace_age_s and data_age_s set how far in the past the trace and the metric/log
    timestamps are; the defaults stay within the stock backend limits.
    """
    trace_ts = str((SESSION_START - trace_age_s) * 1_000_000_000)
    ts = str((SESSION_START - data_age_s) * 1_000_000_000)
    service = {"attributes": [{"key": "service.name", "value": {"stringValue": "ci-observe-test"}}]}
    trace = {
        "resourceSpans": [
            {
                "resource": service,
                "scopeSpans": [
                    {
                        "scope": {"name": "t"},
                        "spans": [
                            {
                                "traceId": trace_id_for(capture_id),
                                "spanId": "0102030405060708",
                                "name": f"ci-observe-test-{capture_id}",
                                "kind": 1,
                                "startTimeUnixNano": trace_ts,
                                "endTimeUnixNano": str(int(trace_ts) + 1_000_000_000),
                                "status": {},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    metric = {
        "resourceMetrics": [
            {
                "resource": service,
                "scopeMetrics": [
                    {
                        "scope": {"name": "m"},
                        "metrics": [
                            {
                                "name": f"ci_observe_test_{capture_id}",
                                "gauge": {"dataPoints": [{"timeUnixNano": ts, "asDouble": 1.0}]},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    log = {
        "resourceLogs": [
            {
                "resource": service,
                "scopeLogs": [
                    {
                        "scope": {"name": "l"},
                        "logRecords": [
                            {
                                "timeUnixNano": ts,
                                "severityText": "INFO",
                                "body": {"stringValue": f"ci-observe test log {capture_id}"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return {
        "traces.jsonl": json.dumps(trace) + "\n",
        "metrics.jsonl": json.dumps(metric) + "\n",
        "logs.jsonl": json.dumps(log) + "\n",
    }


REPLAY_YAML = """receivers:
  otlpjsonfile/traces:
    include:
      - "${env:CI_OBSERVE_CAPTURE_DIR}/traces.jsonl"
    start_at: beginning
  otlpjsonfile/metrics:
    include:
      - "${env:CI_OBSERVE_CAPTURE_DIR}/metrics.jsonl"
    start_at: beginning
  otlpjsonfile/logs:
    include:
      - "${env:CI_OBSERVE_CAPTURE_DIR}/logs.jsonl"
    start_at: beginning
exporters:
  otlphttp:
    endpoint: ${env:CI_OBSERVE_OTLP_ENDPOINT}
service:
  pipelines:
    traces:
      receivers: [otlpjsonfile/traces]
      exporters: [otlphttp]
    metrics:
      receivers: [otlpjsonfile/metrics]
      exporters: [otlphttp]
    logs:
      receivers: [otlpjsonfile/logs]
      exporters: [otlphttp]
"""

DEAD_ENDPOINT_YAML = REPLAY_YAML.replace("endpoint: ${env:CI_OBSERVE_OTLP_ENDPOINT}", "endpoint: http://127.0.0.1:1")


def write_capture(
    root: Path,
    capture_id: str,
    files: tuple[str, ...] = ("traces.jsonl", "metrics.jsonl", "logs.jsonl"),
    state: str = "closed",
    child_exit_code: int = 0,
    trace_age_s: int = 120,
    data_age_s: int = 120,
) -> Path:
    """Write a real capture directory with correct hashes and metadata."""
    cap_dir = root / "captures" / capture_id
    cap_dir.mkdir(parents=True, exist_ok=True)
    lines = signal_lines(capture_id, trace_age_s=trace_age_s, data_age_s=data_age_s)
    entries = []
    for name in files:
        data = lines[name].encode()
        (cap_dir / name).write_bytes(data)
        entries.append({"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    metadata = {
        "schema": "osac.ci-observe.capture",
        "version": 1,
        "id": capture_id,
        "source_kind": "local",
        "state": state,
        "created_at": "2026-10-29T12:00:00Z",
        "finished_at": "2026-10-29T12:01:00Z",
        "child_exit_code": child_exit_code,
        "sources": dict(SOURCES),
        "files": entries,
    }
    (cap_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return cap_dir


def rewrite_metadata(cap_dir: Path, **changes: object) -> None:
    metadata = json.loads((cap_dir / "metadata.json").read_text())
    metadata.update(changes)
    (cap_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))


def tamper_entry(cap_dir: Path, path: str, **changes: object) -> None:
    metadata = json.loads((cap_dir / "metadata.json").read_text())
    for entry in metadata["files"]:
        if entry["path"] == path:
            entry.update(changes)
    (cap_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))


def adopted_state(tool: Tool, tmp_path: Path, capsys: Capsys) -> Path:
    """A state directory adopted by a first real CLI call, which writes the ownership marker."""
    state = tmp_path / "state"
    main_err(tool, cli(state, "report", "--capture-id", "adopt"), capsys)
    return state


def cli(state: Path, *args: str) -> list[str]:
    return ["--state-dir", str(state), *args]


def import_argv(state: Path, cap_dir: Path, config: Path | None = None, timeout: int | None = None) -> list[str]:
    argv = cli(state, "import", "--capture-dir", str(cap_dir))
    if config is not None:
        argv += ["--replay-config", str(config)]
    if timeout is not None:
        argv += ["--timeout", str(timeout)]
    return argv


def sync_argv(state: Path, *args: str) -> list[str]:
    """A sync invocation; --since bogus fails validation before gh is ever called."""
    return cli(state, "sync", "--repository", "example/repo", *args)


def run_cli(argv: list[str], script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True, check=False)


def main_json(tool: Tool, argv: list[str], capsys: Capsys) -> tuple[int, dict]:
    code = tool.main(argv)
    return code, json.loads(capsys.readouterr().out)


def main_err(tool: Tool, argv: list[str], capsys: Capsys) -> tuple[int, str]:
    code = tool.main(argv)
    return code, capsys.readouterr().err


@pytest.fixture
def tool() -> Tool:
    return ci_observe


def test_sync_requires_repository(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tool.main(cli(tmp_path / "state", "sync"))
    assert exc.value.code == 2
    assert "--repository" in capsys.readouterr().err


def test_sync_entrypoint_requires_repository(tmp_path: Path) -> None:
    proc = run_cli(cli(tmp_path / "state", "sync"))
    assert proc.returncode == 2
    assert "required" in proc.stderr


@pytest.mark.parametrize("raw", ["6h", "12h", "24h", "2d", "7d", "2026-10-28", "2026-10-28T10:00:00Z"])
def test_sync_since_accepts_windows(tool: Tool, raw: str) -> None:
    since, until = tool.resolve_since(raw)
    assert since <= until
    hours = {"6h": 6, "12h": 12, "24h": 24, "2d": 48, "7d": 168}.get(raw)
    if hours is not None:
        assert (until - since).total_seconds() == hours * 3600


@pytest.mark.parametrize("raw", ["3h", "1d", "yesterday", "2026-13-40", "2066-01-01T00:00:00Z"])
def test_sync_since_rejects_unknown_values(tool: Tool, raw: str) -> None:
    with pytest.raises(ci_observe.ToolError, match="--since"):
        tool.resolve_since(raw)


def test_sync_rejects_invalid_repository(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    code, err = main_err(tool, cli(tmp_path / "state", "sync", "--repository", "no-slash"), capsys)
    assert code == 1
    assert "OWNER/NAME" in err


def test_sync_rejects_non_positive_limit(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    code, err = main_err(tool, cli(tmp_path / "state", "sync", "--repository", "a/b", "--limit", "0"), capsys)
    assert code == 1
    assert "limit" in err


# A fake gh binary first on PATH: the CLI runs it as a real subprocess, so the
# sync code paths exercise real process execution with canned GitHub data.
FAKE_GH = """\
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["run", "list"]:
    sys.stdout.write(os.environ["FAKE_GH_RUNS"])
elif args[0] == "api" and "/actions/runs/" in args[1] and "/artifacts?per_page=100" in args[1]:
    run_id = args[1].split("/actions/runs/")[1].split("/")[0]
    by_run = json.loads(os.environ["FAKE_GH_ARTIFACTS_BY_RUN"])
    listing = by_run.get(run_id, [])
    sys.stdout.write(json.dumps({"total_count": len(listing), "artifacts": listing}))
elif args[0] == "api" and args[1].endswith("/zip"):
    artifact_id = args[1].rsplit("/", 2)[1]
    zips = json.loads(os.environ["FAKE_GH_ZIPS"])
    sys.stdout.buffer.write(Path(zips[artifact_id]).read_bytes())
else:
    print(f"fake gh: unexpected arguments {args}", file=sys.stderr)
    sys.exit(1)
"""


@pytest.fixture
def fake_gh(monkeypatch: MonkeyPatch, tmp_path: Path) -> Callable[..., None]:
    """Put a fake gh on PATH and return its configurator.

    The configurator takes the canned run list, artifact listing and a mapping of
    artifact id to the ZIP file served for its download."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH)
    (bindir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def configure(
        *, runs: list[dict] | None = None, artifacts: list[dict] | None = None, zips: dict[int, Path] | None = None
    ) -> None:
        monkeypatch.setenv("FAKE_GH_RUNS", json.dumps(runs or []))
        by_run: dict[str, list[dict]] = {}
        for artifact in artifacts or []:
            workflow_run = artifact.get("workflow_run") if isinstance(artifact.get("workflow_run"), dict) else {}
            by_run.setdefault(str(workflow_run.get("id")), []).append(artifact)
        monkeypatch.setenv("FAKE_GH_ARTIFACTS_BY_RUN", json.dumps(by_run))
        monkeypatch.setenv("FAKE_GH_ZIPS", json.dumps({str(k): str(v) for k, v in (zips or {}).items()}))

    return configure


def gh_run(run_id: int, *, age_s: int = 60) -> dict:
    return {
        "databaseId": run_id,
        "attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_s)),
    }


def gh_artifact(
    artifact_id: int, run_id: int, attempt: int = 1, *, name: str | None = None, expired: bool = False
) -> dict:
    return {
        "id": artifact_id,
        "name": name or f"ci-observe-telemetry-{run_id}-{attempt}",
        "size_in_bytes": 1024,
        "expired": expired,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workflow_run": {"id": run_id},
    }


def zip_capture(
    cap_dir: Path,
    dest: Path,
    members: tuple[str, ...] = ("traces.jsonl", "metrics.jsonl", "logs.jsonl", "metadata.json"),
    extra: dict[str, bytes] | None = None,
    prefix: str = "",
) -> Path:
    """Zip a capture's files, optionally nested under a directory (prefix ends with /)."""
    with zipfile.ZipFile(dest, "w") as archive:
        for member in members:
            archive.write(cap_dir / member, f"{prefix}{member}")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return dest


def test_sync_with_no_artifacts_reports_zeros(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    fake_gh(runs=[], artifacts=[])
    code, out = main_json(tool, sync_argv(tmp_path / "state"), capsys)
    assert code == 0
    assert out["repository"] == "example/repo"
    assert out["window"]["since"] < out["window"]["until"]
    for key in (
        "runs_found",
        "artifacts_found",
        "artifacts_downloaded",
        "captures_imported",
        "captures_skipped",
        "captures_failed",
    ):
        assert out[key] == 0
    assert out["details"] == []


def test_sync_downloads_extracts_and_fails_without_a_stack(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    cap_dir = write_capture(tmp_path, "cap-sync-1")
    # An interrupted capture is refused deterministically: the refusal is
    # recorded so the scheduled sync does not re-fail it every 15 minutes.
    rewrite_metadata(cap_dir, state="interrupted")
    zip_path = zip_capture(cap_dir, tmp_path / "cap-sync-1.zip")
    fake_gh(
        runs=[gh_run(101), gh_run(102), gh_run(900, age_s=3 * 86400)],  # run 900 is outside the 24h window
        artifacts=[
            gh_artifact(555, 101),
            gh_artifact(556, 900),  # run outside the window: none of this tool's business
            gh_artifact(557, 102, expired=True),
            {"id": 558, "name": "usage", "workflow_run": {"id": 101}},  # a foreign artifact
        ],
        zips={555: zip_path},
    )
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 1  # the capture itself is not importable: an honest failure
    assert out["runs_found"] == 2  # run 900 was created outside the window
    assert out["artifacts_found"] == 1
    assert out["artifacts_downloaded"] == 1
    assert out["captures_imported"] == 0
    assert out["captures_skipped"] == 0
    assert out["captures_failed"] == 1
    by_id = {detail["artifact_id"]: detail for detail in out["details"]}
    assert by_id[557]["status"] == "ignored"
    assert "expired" in by_id[557]["reason"]
    assert by_id[555]["status"] == "failed"
    assert "interrupted" in by_id[555]["reason"]
    assert by_id[555]["capture_id"] == "cap-sync-1"
    assert 556 not in by_id and 558 not in by_id
    cached = state / "artifacts" / "101" / "1" / "artifact.zip"
    assert cached.is_file()
    assert cached.stat().st_mode & 0o777 == 0o600
    assert (state / "artifacts" / "101").stat().st_mode & 0o777 == 0o700
    extracted = state / "captures" / "cap-sync-1"
    assert (extracted / "metadata.json").is_file()
    assert not (state / "artifacts" / "101" / "1" / "extracted").exists()
    # A second sync does not even list the run again: the refusal record is
    # the cache, so the scheduled run stays green over an artifact whose
    # import is deterministically refused.
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    assert out["artifacts_downloaded"] == 0
    assert out["captures_failed"] == 0
    assert out["captures_skipped"] == 0
    record = json.loads((state / "artifacts" / "101" / "1" / "record.json").read_text())
    assert record["status"] == "refused"
    assert record["capture_id"] == "cap-sync-1"


def test_validate_capture_tolerates_redacted_files(tool: Tool, tmp_path: Path) -> None:
    """The CI upload path redacts capture files in place after the close step
    hashed them (gitleaks, the release gate); the sync must import the actual
    bytes and record the difference instead of refusing the capture."""
    cap_dir = write_capture(tmp_path, "cap-redacted")
    (cap_dir / "logs.jsonl").write_text((cap_dir / "logs.jsonl").read_text() + "redacted\n")
    with pytest.raises(ci_observe.ToolError, match="does not match metadata"):
        ci_observe.validate_capture(cap_dir)
    tolerant = ci_observe.validate_capture(cap_dir, tolerate_modified=True)
    assert tolerant["modified"] == ["logs.jsonl"]


def test_sync_imports_a_redacted_capture(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    """End to end: a capture whose files were modified after close passes the
    sync's validation (deferred here only because no backend is running)."""
    cap_dir = write_capture(tmp_path, "cap-redact-sync")
    (cap_dir / "logs.jsonl").write_text((cap_dir / "logs.jsonl").read_text() + "redacted\n")
    zip_path = zip_capture(cap_dir, tmp_path / "cap-redact-sync.zip")
    fake_gh(runs=[gh_run(101)], artifacts=[gh_artifact(555, 101)], zips={555: zip_path})
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    detail = next(d for d in out["details"] if d["artifact_id"] == 555)
    assert detail["status"] == "deferred"


def test_sync_records_a_refusal_even_without_a_capture_id(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    """A malformed artifact (no metadata.json) is refused deterministically;
    the refusal is recorded even though no capture id was ever extracted, so
    later passes skip it instead of re-failing every 15 minutes."""
    zip_path = write_bad_zip(tmp_path / "nometa.zip", [("traces.jsonl", b"{}\n")])
    fake_gh(runs=[gh_run(101)], artifacts=[gh_artifact(555, 101)], zips={555: zip_path})
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 1
    assert out["captures_failed"] == 1
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    assert out["captures_skipped"] == 0
    assert out["details"] == []
    record = json.loads((state / "artifacts" / "101" / "1" / "record.json").read_text())
    assert record["status"] == "refused"
    assert record["capture_id"] is None


def test_sync_defers_a_valid_capture_while_the_backend_is_down(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    """A missing backend is recoverable: no refusal is recorded, later passes retry."""
    cap_dir = write_capture(tmp_path, "cap-sync-down")
    zip_path = zip_capture(cap_dir, tmp_path / "cap-sync-down.zip")
    fake_gh(runs=[gh_run(101)], artifacts=[gh_artifact(555, 101)], zips={555: zip_path})
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0  # deferred, not failed: the backend comes back with `up`
    detail = next(d for d in out["details"] if d["artifact_id"] == 555)
    assert detail["status"] == "deferred"
    assert "backend unavailable" in detail["reason"]
    assert not (state / "artifacts" / "101" / "1" / "record.json").exists()
    # The next pass retries the import (still deferred while the stack is down).
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    detail = next(d for d in out["details"] if d["artifact_id"] == 555)
    assert detail["status"] == "deferred"


def test_sync_skips_already_imported_capture(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    cap_dir = write_capture(tmp_path, "cap-sync-2")
    digest = ci_observe.validate_capture(cap_dir)["capture_digest"]
    state = adopted_state(tool, tmp_path, capsys)
    (state / "imports").mkdir(parents=True)
    (state / "imports" / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "imports": {
                    digest: {"status": "imported", "imported_at": "2026-10-29T00:00:00Z", "capture_id": "cap-sync-2"}
                },
            }
        )
    )
    fake_gh(
        runs=[gh_run(101)],
        artifacts=[gh_artifact(555, 101)],
        zips={555: zip_capture(cap_dir, tmp_path / "cap-sync-2.zip")},
    )
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    assert out["artifacts_downloaded"] == 1
    assert out["captures_skipped"] == 1
    assert out["captures_imported"] == 0
    detail = out["details"][0]
    assert detail["status"] == "skipped"
    assert detail["capture_id"] == "cap-sync-2"
    assert (state / "artifacts" / "101" / "1" / "record.json").is_file()
    # The next sync takes the cached-record fast path: no download, no
    # extraction, and not even an artifact listing -- the run is cached.
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    assert out["artifacts_downloaded"] == 0
    assert out["captures_skipped"] == 0
    assert out["details"] == []


def test_sync_refuses_uncertain_partial_import(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    cap_dir = write_capture(tmp_path, "cap-sync-4")
    digest = ci_observe.validate_capture(cap_dir)["capture_digest"]
    state = adopted_state(tool, tmp_path, capsys)
    (state / "imports").mkdir(parents=True)
    (state / "imports" / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "imports": {digest: {"status": "incomplete", "reason": "interrupted", "capture_id": "cap-sync-4"}},
            }
        )
    )
    fake_gh(
        runs=[gh_run(101)],
        artifacts=[gh_artifact(555, 101)],
        zips={555: zip_capture(cap_dir, tmp_path / "cap-sync-4.zip")},
    )
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 1
    assert out["captures_failed"] == 1
    assert "uncertain" in out["details"][0]["reason"]


def test_sync_limit_bounds_downloads_per_invocation(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    cap_a = write_capture(tmp_path, "cap-sync-a")
    cap_b = write_capture(tmp_path, "cap-sync-b")
    fake_gh(
        runs=[gh_run(101), gh_run(102)],
        artifacts=[gh_artifact(555, 101), gh_artifact(556, 102)],
        zips={555: zip_capture(cap_a, tmp_path / "a.zip"), 556: zip_capture(cap_b, tmp_path / "b.zip")},
    )
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state, "--limit", "1"), capsys)
    assert code == 0  # the processed capture is deferred without a stack, not failed
    assert out["artifacts_downloaded"] == 1
    statuses = sorted(detail["status"] for detail in out["details"])
    assert statuses == ["deferred", "deferred"]
    assert not (state / "artifacts" / "102" / "1" / "artifact.zip").exists()


def write_bad_zip(dest: Path, entries: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(dest, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return dest


def test_sync_rejects_artifact_zip_with_unexpected_member(tool: Tool, tmp_path: Path) -> None:
    zip_path = write_bad_zip(tmp_path / "bad.zip", [("traces.jsonl", b"{}\n"), ("evil.sh", b"boom")])
    with pytest.raises(ci_observe.ToolError, match="unexpected member"):
        ci_observe.extract_artifact_zip(zip_path, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()  # nothing is extracted before validation passes


def test_sync_rejects_artifact_zip_without_metadata(tool: Tool, tmp_path: Path) -> None:
    zip_path = write_bad_zip(tmp_path / "n.zip", [("traces.jsonl", b"{}\n")])
    with pytest.raises(ci_observe.ToolError, match=r"metadata\.json"):
        ci_observe.extract_artifact_zip(zip_path, tmp_path / "staging")


def test_sync_rejects_artifact_zip_with_duplicate_members(tool: Tool, tmp_path: Path) -> None:
    zip_path = write_bad_zip(tmp_path / "d.zip", [("traces.jsonl", b"{}\n"), ("traces.jsonl", b"{}\n")])
    with pytest.raises(ci_observe.ToolError, match="duplicate"):
        ci_observe.extract_artifact_zip(zip_path, tmp_path / "staging")


def test_sync_rejects_artifact_zip_with_symlink_member(tool: Tool, tmp_path: Path) -> None:
    info = zipfile.ZipInfo("traces.jsonl")
    info.create_system = 3  # unix, so external_attr carries a mode
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    zip_path = tmp_path / "lnk.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ci_observe.ToolError, match="symlink"):
        ci_observe.extract_artifact_zip(zip_path, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_sync_extracts_valid_artifact_zip(tool: Tool, tmp_path: Path) -> None:
    cap_dir = write_capture(tmp_path, "cap-zip")
    zip_path = zip_capture(cap_dir, tmp_path / "good.zip")
    staging = tmp_path / "staging"
    meta = ci_observe.extract_artifact_zip(zip_path, staging)
    assert meta["id"] == "cap-zip"
    assert sorted(p.name for p in staging.iterdir()) == ["logs.jsonl", "metadata.json", "metrics.jsonl", "traces.jsonl"]
    assert (staging / "traces.jsonl").stat().st_mode & 0o777 == 0o644


def test_sync_extracts_nested_artifact_zip_layout(tool: Tool, tmp_path: Path) -> None:
    """actions/upload-artifact zips the staged directory tree, so members sit under
    ci-observe/<capture_id>/; the nested layout extracts like the flat one."""
    cap_dir = write_capture(tmp_path, "cap-nested")
    zip_path = zip_capture(cap_dir, tmp_path / "nested.zip", prefix="ci-observe/cap-nested/")
    staging = tmp_path / "staging"
    meta = ci_observe.extract_artifact_zip(zip_path, staging)
    assert meta["id"] == "cap-nested"
    assert sorted(p.name for p in staging.iterdir()) == ["logs.jsonl", "metadata.json", "metrics.jsonl", "traces.jsonl"]


def test_sync_rejects_artifact_zip_with_mixed_directories(tool: Tool, tmp_path: Path) -> None:
    entries = [("ci-observe/cap/metadata.json", b"{}"), ("traces.jsonl", b"{}\n")]
    zip_path = write_bad_zip(tmp_path / "mixed.zip", entries)
    with pytest.raises(ci_observe.ToolError, match="unexpected members"):
        ci_observe.extract_artifact_zip(zip_path, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()


def test_sync_rejects_artifact_zip_path_traversal(tool: Tool, tmp_path: Path) -> None:
    zip_path = write_bad_zip(tmp_path / "t.zip", [("ci-observe/c/../../evil.txt", b"x")])
    with pytest.raises(ci_observe.ToolError, match="unexpected member"):
        ci_observe.extract_artifact_zip(zip_path, tmp_path / "staging")
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_sync_fails_artifact_whose_zip_fails_validation(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    cap_dir = write_capture(tmp_path, "cap-sync-3")
    zip_path = zip_capture(cap_dir, tmp_path / "cap-sync-3.zip", extra={"evil.txt": b"boom"})
    fake_gh(runs=[gh_run(101)], artifacts=[gh_artifact(555, 101)], zips={555: zip_path})
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 1
    assert out["captures_failed"] == 1
    assert "unexpected member" in out["details"][0]["reason"]
    assert not (state / "captures" / "cap-sync-3").exists()


def test_sync_watch_repeats_until_interrupted(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None], monkeypatch: MonkeyPatch
) -> None:
    fake_gh(runs=[], artifacts=[])
    sleeps: list[int] = []

    def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(tool.time, "sleep", fake_sleep)
    code, err = main_err(tool, sync_argv(tmp_path / "state", "--watch"), capsys)
    assert code == 130
    assert "interrupted" in err
    assert sleeps == [ci_observe.SYNC_WATCH_INTERVAL_S]


def test_sync_tolerates_the_release_gate_report_in_the_artifact_zip(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    """The upload path's release gate writes redaction-report.json next to the
    staged capture, and the upload zips the whole directory; the sync must not
    reject the artifact for carrying the gate's own scan output."""
    cap_dir = write_capture(tmp_path, "run-102-attempt-1")
    zip_path = zip_capture(cap_dir, tmp_path / "gate-report.zip", prefix="ci-observe/run-102-attempt-1/")
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("redaction-report.json", '{"findings": []}')
        archive.writestr(".redaction-complete", "")
    fake_gh(
        runs=[gh_run(102)],
        artifacts=[gh_artifact(555, 102, name="ci-observe-capture-102-attempt-1")],
        zips={555: zip_path},
    )
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0  # deferred only because no backend is running
    detail = next(d for d in out["details"] if d["artifact_id"] == 555)
    assert detail["status"] == "deferred"
    assert detail["capture_id"] == "run-102-attempt-1"
    assert (state / "captures" / "run-102-attempt-1" / "metadata.json").is_file()


def test_sync_accepts_the_ci_capture_artifact_name_and_nested_zip(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_gh: Callable[..., None]
) -> None:
    """The CI action (.github/actions/ci-observe-capture) uploads artifacts named
    ci-observe-capture-<run>-attempt-<attempt> whose members sit under
    ci-observe/<capture_id>/."""
    cap_dir = write_capture(tmp_path, "run-101-attempt-1")
    zip_path = zip_capture(cap_dir, tmp_path / "nested.zip", prefix="ci-observe/run-101-attempt-1/")
    fake_gh(
        runs=[gh_run(101)],
        artifacts=[gh_artifact(555, 101, name="ci-observe-capture-101-attempt-1")],
        zips={555: zip_path},
    )
    state = tmp_path / "state"
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0  # the import is deferred only because no local stack is running
    assert out["artifacts_found"] == 1
    detail = out["details"][0]
    assert detail["status"] == "deferred"
    assert "backend unavailable" in detail["reason"]
    assert detail["capture_id"] == "run-101-attempt-1"
    assert (state / "captures" / "run-101-attempt-1" / "metadata.json").is_file()
    assert (state / "artifacts" / "101" / "1" / "artifact.zip").is_file()


def test_capture_suspended_fails_cleanly(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    """Local capture is suspended (live push replaced it): a clean refusal,
    never a traceback."""
    argv = cli(tmp_path / "state", "capture", "--kubeconfig", "/tmp/k", "--namespace", "ns", "--", "true")
    code, err = main_err(tool, argv, capsys)
    assert code == 2
    assert "suspended" in err
    assert "Traceback" not in err


def test_capture_requires_command_after_double_dash(tool: Tool, tmp_path: Path) -> None:
    argv = cli(tmp_path / "state", "capture", "--kubeconfig", "/tmp/k", "--namespace", "ns", "true")
    with pytest.raises(SystemExit) as exc:
        tool.main(argv)
    assert exc.value.code == 2


STUB = """\
import json
from pathlib import Path

def capture(args):
    Path(__file__).with_name("stub-out.json").write_text(json.dumps({
        "state_dir": str(args.state_dir),
        "state_dir_is_path": isinstance(args.state_dir, Path),
        "kubeconfig": str(args.kubeconfig),
        "namespace": args.namespace,
        "command": args.command,
        "capture_id": args.capture_id,
    }))
    return 7
"""


def test_capture_is_suspended_for_live_push(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    """Local file capture is suspended: the pipeline pushes telemetry live now."""
    argv = cli(tmp_path, "capture", "--kubeconfig", "/tmp/k", "--namespace", "ns", "--", "true")
    code, err = main_err(tool, argv, capsys)
    assert code == 2
    assert "suspended" in err
    assert "live" in err


def test_state_dir_refuses_symlink(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    code, err = main_err(tool, sync_argv(link, "--since", "bogus"), capsys)
    assert code == 1
    assert "symlink" in err


def test_state_dir_refuses_foreign_data(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    (state / "foreign.txt").write_text("data")
    code, err = main_err(tool, sync_argv(state, "--since", "bogus"), capsys)
    assert code == 1
    assert "no ownership marker" in err
    marked = tmp_path / "marked"
    marked.mkdir(mode=0o700)
    (marked / ".ci-observe-owner").write_text(json.dumps({"tool": "other"}))
    code, err = main_err(tool, sync_argv(marked, "--since", "bogus"), capsys)
    assert code == 1
    assert "another tool" in err


def test_state_dir_refuses_loose_permissions(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    code, err = main_err(tool, sync_argv(state, "--since", "bogus"), capsys)
    assert code == 1
    assert "chmod 700" in err


def test_state_dir_created_with_safe_mode(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    state = tmp_path / "state"
    code, err = main_err(tool, sync_argv(state, "--since", "bogus"), capsys)
    assert code == 1
    assert "since" in err
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / ".ci-observe-owner").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda cap: rewrite_metadata(cap, state="capturing"), "only closed captures"),
        (lambda cap: rewrite_metadata(cap, schema="other.schema"), "schema"),
        (lambda cap: rewrite_metadata(cap, version=2), "version"),
        (lambda cap: rewrite_metadata(cap, id="other"), "directory name"),
        (lambda cap: rewrite_metadata(cap, files=[]), "files list"),
        (
            lambda cap: (cap / "traces.jsonl").write_text(
                signal_lines("cap-bad")["traces.jsonl"].replace("ci-observe-test", "ci-observe-tesu")
            ),
            "sha256",
        ),
        (
            lambda cap: rewrite_metadata(cap, files=[{"path": "../escape.jsonl", "size_bytes": 1, "sha256": "0" * 64}]),
            "unsafe file path",
        ),
        (lambda cap: tamper_entry(cap, "traces.jsonl", size_bytes=5), "does not match metadata"),
        (lambda cap: (cap / "extra.txt").write_text("x"), "do not match metadata"),
        (lambda cap: cap.joinpath("traces.jsonl").unlink(), "do not match metadata"),
    ],
)
def test_import_rejects_invalid_captures(
    tool: Tool, tmp_path: Path, capsys: Capsys, mutate: Callable[[Path], None], reason: str
) -> None:
    cap_dir = write_capture(tmp_path, "cap-bad")
    mutate(cap_dir)
    code, err = main_err(tool, import_argv(tmp_path / "state", cap_dir), capsys)
    assert code == 1
    assert reason in err


def test_import_rejects_symlinked_signal_file(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    cap_dir = write_capture(tmp_path, "cap-link")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(signal_lines("cap-link")["traces.jsonl"])
    (cap_dir / "traces.jsonl").unlink()
    (cap_dir / "traces.jsonl").symlink_to(outside)
    code, err = main_err(tool, import_argv(tmp_path / "state", cap_dir), capsys)
    assert code == 1
    assert "symlink" in err


def test_import_rejects_capture_exceeding_total_bound(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    cap_dir = write_capture(tmp_path, "cap-big")
    big = 900 * 1024 * 1024
    rewrite_metadata(
        cap_dir,
        files=[
            {"path": "traces.jsonl", "size_bytes": big, "sha256": "0" * 64},
            {"path": "metrics.jsonl", "size_bytes": big, "sha256": "0" * 64},
        ],
    )
    code, err = main_err(tool, import_argv(tmp_path / "state", cap_dir), capsys)
    assert code == 1
    assert "1 GiB bound" in err


def test_import_rejects_oversized_otlp_line(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    cap_dir = write_capture(tmp_path, "cap-line", files=("traces.jsonl",))
    data = b'{"pad":"' + b"a" * (17 * 1024 * 1024) + b'"}\n'
    (cap_dir / "traces.jsonl").write_bytes(data)
    rewrite_metadata(
        cap_dir, files=[{"path": "traces.jsonl", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
    )
    code, err = main_err(tool, import_argv(tmp_path / "state", cap_dir), capsys)
    assert code == 1
    assert "16 MiB bound" in err


def test_import_rejects_capture_with_no_signal_data(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    cap_dir = write_capture(tmp_path, "cap-empty", files=("traces.jsonl",))
    (cap_dir / "traces.jsonl").write_bytes(b"")
    empty_hash = hashlib.sha256(b"").hexdigest()
    rewrite_metadata(cap_dir, files=[{"path": "traces.jsonl", "size_bytes": 0, "sha256": empty_hash}])
    code, err = main_err(tool, import_argv(tmp_path / "state", cap_dir), capsys)
    assert code == 1
    assert "no non-empty signal files" in err


def test_import_requires_stack_record(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    cap_dir = write_capture(tmp_path, "cap-nostack")
    code, err = main_err(tool, import_argv(tmp_path / "state", cap_dir), capsys)
    assert code == 1
    assert "run 'up' first" in err


def test_report_shows_facts_and_gaps_without_inventing_results(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    state = adopted_state(tool, tmp_path, capsys)
    write_capture(state, "cap-report", child_exit_code=3)
    code, out = main_json(tool, cli(state, "report", "--capture-id", "cap-report"), capsys)
    assert code == 0
    capture = out["capture"]
    assert capture["child_exit_code"] == 3
    assert capture["state"] == "closed"
    assert capture["import"] is None
    assert capture["sources"]["cluster-logs"] == "partial"
    assert capture["validation"] == "ok"
    gaps = out["gaps"]
    assert "capture not imported" in gaps
    assert "local stack is not running" in gaps
    assert out["sync"] == {"available": True}
    assert "source beyla: status 'not-collected'" in gaps
    assert "source cluster-logs: status 'partial'" in gaps
    assert "no child exit code recorded" not in gaps


def test_report_missing_metadata_is_an_honest_gap(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    state = adopted_state(tool, tmp_path, capsys)
    (state / "captures" / "cap-none").mkdir(parents=True)
    code, out = main_json(tool, cli(state, "report", "--capture-id", "cap-none"), capsys)
    assert code == 0
    assert "unreadable" in out["capture"]["error"]
    assert "capture metadata unreadable" in out["gaps"]


def test_report_requires_known_section(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    cap_dir = write_capture(tmp_path, "cap-sec")
    code, err = main_err(
        tool, cli(tmp_path / "state", "report", "--capture-dir", str(cap_dir), "--section", "tests"), capsys
    )
    assert code == 2
    assert "only 'summary'" in err


@requires_podman
def test_up_refuses_port_collision(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        code, err = main_err(tool, cli(tmp_path / "state", "up", "--ui-port", str(port), "--otlp-port", "0"), capsys)
    finally:
        sock.close()
    assert code == 1
    assert "already in use" in err


@requires_podman
def test_up_refuses_optional_port_collision(tool: Tool, tmp_path: Path, capsys: Capsys) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        argv = cli(tmp_path / "state", "up", "--ui-port", "0", "--otlp-port", "0", "--loki-port", str(port))
        code, err = main_err(tool, argv, capsys)
    finally:
        sock.close()
    assert code == 1
    assert "already in use" in err


# A fake podman binary first on PATH: the CLI runs it as a real subprocess, so
# the up code paths exercise real process execution while the recorded calls
# show exactly which ports the container was created with.
FAKE_PODMAN = """\
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state_file = Path(os.environ["FAKE_PODMAN_STATE"])
state = json.loads(state_file.read_text()) if state_file.exists() else {"calls": [], "containers": {}, "volumes": []}
state["calls"].append(list(args))


def save() -> None:
    state_file.write_text(json.dumps(state))


# Persist the call itself before any handler below exits early.
save()


if args[:2] == ["inspect", "--format"]:
    info = state["containers"].get(args[-1])
    if info is None:
        sys.exit(1)
    print(f'{json.dumps(info["labels"])}|{"true" if info["running"] else "false"}|{info["image"]}')
elif args[:2] == ["volume", "inspect"]:
    if args[-1] not in state["volumes"]:
        sys.exit(1)
    print(json.dumps({"osac.ci-observe.owned": "true"}))
elif args[:2] == ["volume", "create"]:
    state["volumes"].append(args[-1])
elif args[0] == "run":
    name = args[args.index("--name") + 1]
    ports = {}
    while "-p" in args:
        pos = args.index("-p")
        mapping = args[pos + 1]
        del args[pos : pos + 2]
        _loopback, host, inner = mapping.split(":")
        ports[int(inner)] = int(host) if host else 40000 + int(inner)
    state["containers"][name] = {
        "labels": {"osac.ci-observe.owned": "true"},
        "running": True,
        "image": args[-1],
        "ports": ports,
    }
elif args[0] == "start":
    state["containers"][args[-1]]["running"] = True
elif args[0] == "port":
    info = state["containers"].get(args[1])
    published = (info or {}).get("ports", {}).get(args[2])
    if published is None:
        sys.exit(1)
    print(f"127.0.0.1:{published}")
save()
"""


@pytest.fixture
def fake_podman(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    """Put a fake podman on PATH; returns the file holding its recorded calls.

    The container never really starts, so the health check is satisfied by
    patching http_get instead of a real endpoint."""
    bindir = tmp_path / "podman-bin"
    bindir.mkdir()
    (bindir / "podman").write_text(FAKE_PODMAN)
    (bindir / "podman").chmod(0o755)
    state_file = tmp_path / "podman-state.json"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PODMAN_STATE", str(state_file))
    monkeypatch.setattr(ci_observe, "http_get", lambda url, timeout=5.0: (200, "ok"))
    return state_file


def fake_podman_calls(state_file: Path) -> list[list[str]]:
    return json.loads(state_file.read_text())["calls"]


def run_call(state_file: Path) -> list[str]:
    return next(call for call in fake_podman_calls(state_file) if call[0] == "run")


def port_maps(call: list[str]) -> list[str]:
    return [call[i + 1] for i, arg in enumerate(call) if arg == "-p"]


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_up_leaves_optional_ports_unpublished_by_default(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_podman: Path
) -> None:
    state = tmp_path / "state"
    code, out = main_json(tool, cli(state, "up", "--ui-port", "0", "--otlp-port", "0"), capsys)
    assert code == 0
    assert port_maps(run_call(fake_podman)) == ["127.0.0.1::3000", "127.0.0.1::4318"]
    record = json.loads((state / "stack.json").read_text())
    assert record["tempo_port"] is None
    assert record["loki_port"] is None
    assert record["metrics_port"] is None
    assert out["tempo_port"] is None


def test_up_publishes_optional_ports_when_asked(tool: Tool, tmp_path: Path, capsys: Capsys, fake_podman: Path) -> None:
    tempo, loki, metrics = free_port(), free_port(), free_port()
    state = tmp_path / "state"
    argv = cli(
        state,
        "up",
        "--ui-port",
        "0",
        "--otlp-port",
        "0",
        "--tempo-port",
        str(tempo),
        "--loki-port",
        str(loki),
        "--metrics-port",
        str(metrics),
    )
    code, out = main_json(tool, argv, capsys)
    assert code == 0
    assert port_maps(run_call(fake_podman)) == [
        "127.0.0.1::3000",
        "127.0.0.1::4318",
        f"127.0.0.1:{tempo}:3200",
        f"127.0.0.1:{loki}:3100",
        f"127.0.0.1:{metrics}:9090",
    ]
    record = json.loads((state / "stack.json").read_text())
    assert record["tempo_port"] == tempo
    assert record["loki_port"] == loki
    assert record["metrics_port"] == metrics
    assert out["tempo_port"] == tempo
    assert out["metrics_port"] == metrics


def test_up_rejects_optional_port_equal_to_another_requested_port(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_podman: Path
) -> None:
    port = free_port()
    argv = cli(tmp_path / "state", "up", "--ui-port", str(port), "--otlp-port", "0", "--tempo-port", str(port))
    code, err = main_err(tool, argv, capsys)
    assert code == 1
    assert "must differ" in err
    # The distinctness refusal happens before anything is created.
    assert not any(call[0] == "run" for call in fake_podman_calls(fake_podman))


def test_up_refuses_existing_container_without_a_requested_optional_port(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_podman: Path
) -> None:
    state = tmp_path / "state"
    code, _ = main_json(tool, cli(state, "up", "--ui-port", "0", "--otlp-port", "0"), capsys)
    assert code == 0
    tempo = free_port()
    argv = cli(state, "up", "--ui-port", "0", "--otlp-port", "0", "--tempo-port", str(tempo))
    code, err = main_err(tool, argv, capsys)
    assert code == 1
    assert "Tempo" in err
    assert "down --purge" in err


def test_up_is_idempotent_with_matching_optional_ports(
    tool: Tool, tmp_path: Path, capsys: Capsys, fake_podman: Path
) -> None:
    tempo = free_port()
    state = tmp_path / "state"
    argv = cli(state, "up", "--ui-port", "0", "--otlp-port", "0", "--tempo-port", str(tempo))
    code, out = main_json(tool, argv, capsys)
    assert code == 0
    assert out["status"] == "started"
    assert out["tempo_port"] == tempo
    code, out = main_json(tool, argv, capsys)
    assert code == 0
    assert out["status"] == "already-up"
    assert out["tempo_port"] == tempo
    assert json.loads((state / "stack.json").read_text())["tempo_port"] == tempo
    # A later up without the flag keeps what the container was created with.
    code, out = main_json(tool, cli(state, "up", "--ui-port", "0", "--otlp-port", "0"), capsys)
    assert code == 0
    assert out["tempo_port"] == tempo
    assert json.loads((state / "stack.json").read_text())["tempo_port"] == tempo


def tempo_trace_code(container: str, trace_id: str) -> str:
    """Query Tempo inside the LGTM container by trace id; 200 means the trace is stored."""
    proc = subprocess.run(
        [
            "podman",
            "exec",
            container,
            "curl",
            "-s",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            f"http://localhost:3200/api/traces/{trace_id}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def tempo_block_count(container: str) -> int:
    proc = subprocess.run(
        ["podman", "exec", container, "sh", "-c", "ls /data/tempo/blocks/single-tenant 2>/dev/null | wc -l"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def wait_for_tempo_block(container: str, timeout_s: int = 240, baseline: int = 0) -> None:
    """Wait until Tempo cut more blocks than the baseline count onto the data volume.

    Tempo in this image serves recent traces from a container-local live store that is
    discarded on restart; only cut blocks on the volume survive a restart.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if tempo_block_count(container) > baseline:
            return
        time.sleep(10)
    raise AssertionError("Tempo did not cut a block onto the data volume in time")


def prometheus_has(container: str, metric: str, start_s: int, end_s: int) -> bool:
    """Query Prometheus inside the LGTM container for a metric in a time range."""
    url = f"http://localhost:9090/api/v1/query_range?query={metric}&start={start_s}&end={end_s}&step=300"
    proc = subprocess.run(["podman", "exec", container, "curl", "-s", url], capture_output=True, text=True, check=True)
    return '"values"' in proc.stdout


def loki_has(container: str, needle: str, start_ns: int, end_ns: int) -> bool:
    """Query Loki inside the LGTM container for a log body in a time range."""
    selector = "%7Bservice_name%3D%22ci-observe-test%22%7D"
    url = f"http://localhost:3100/loki/api/v1/query_range?query={selector}&start={start_ns}&end={end_ns}&limit=20"
    proc = subprocess.run(["podman", "exec", container, "curl", "-s", url], capture_output=True, text=True, check=True)
    return needle in proc.stdout


def poll_until(check: Callable[..., bool], args: tuple, timeout_s: int, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check(*args):
            return
        time.sleep(5)
    raise AssertionError(f"{what} not observed within {timeout_s}s")


def trace_stored(container: str, trace_id: str) -> bool:
    return tempo_trace_code(container, trace_id) == "200"


def assert_aged_evidence(container: str, trace_id: str, day_s: int, stage: str) -> None:
    """Tempo serves the year-old trace by id; Prometheus and Loki hold the day-old
    sample and log at their original timestamps. The Prometheus metric name is the
    sanitized form of ci_observe_test_cap-aged (dashes become underscores)."""
    poll_until(trace_stored, (container, trace_id), 120, f"year-old trace in Tempo {stage}")
    poll_until(
        prometheus_has,
        (container, "ci_observe_test_cap_aged", day_s - 600, day_s + 600),
        120,
        f"day-old metric sample in Prometheus {stage}",
    )
    poll_until(
        loki_has,
        (container, "ci-observe test log cap-aged", (day_s - 600) * 10**9, (day_s + 600) * 10**9),
        120,
        f"day-old log line in Loki {stage}",
    )


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict, Path]:
    """One owned LGTM stack on ephemeral loopback ports, shared by the container tests."""
    state = tmp_path_factory.mktemp("ci-observe-state")
    # The replay config stays outside the state directory: the tool refuses to adopt a
    # state directory that already contains unknown files.
    replay_config = tmp_path_factory.mktemp("ci-observe-cfg") / "replay.yaml"
    replay_config.write_text(REPLAY_YAML)
    assert ci_observe.main(cli(state, "up", "--ui-port", "0", "--otlp-port", "0")) == 0
    stack_record = json.loads((state / "stack.json").read_text())
    yield state, stack_record, replay_config
    assert ci_observe.main(cli(state, "down", "--purge")) == 0, "stack fixture teardown failed"


def test_exporter_ack_allows_generated_metric_points() -> None:
    """The replay collector sends more metric points than the file receiver
    accepted (it generates points of its own); the ack must accept that and
    still require every accepted point to be sent."""
    ok = {
        "otelcol_receiver_accepted_metric_points": 71056,
        "otelcol_exporter_sent_metric_points": 75303,
        "otelcol_exporter_send_failed_metric_points": 0,
        "otelcol_receiver_refused_metric_points": 0,
    }
    assert ci_observe.exporter_ack_ok(ok, {"metric_points": True}) is True
    dropped = dict(ok, **{"otelcol_exporter_sent_metric_points": 70000})
    assert ci_observe.exporter_ack_ok(dropped, {"metric_points": True}) is False
    refused = dict(ok, **{"otelcol_receiver_refused_metric_points": 1})
    assert ci_observe.exporter_ack_ok(refused, {"metric_points": True}) is False


@requires_images
def test_import_replays_into_stack_and_confirms_delivery(
    tool: Tool, stack: tuple[Path, dict, Path], capsys: Capsys
) -> None:
    state, _, replay_config = stack
    cap_dir = write_capture(state, "cap-import")
    code, out = main_json(tool, import_argv(state, cap_dir, config=replay_config), capsys)
    assert code == 0
    assert out["status"] == "imported"
    assert out["counts"]["spans"] == {"accepted": 1, "sent": 1}
    assert out["counts"]["metric_points"] == {"accepted": 1, "sent": 1}
    assert out["counts"]["log_records"] == {"accepted": 1, "sent": 1}
    entry = json.loads((state / "imports" / "index.json").read_text())["imports"][out["capture_digest"]]
    assert entry["status"] == "imported"


@requires_images
def test_import_skips_already_imported_digest(tool: Tool, stack: tuple[Path, dict, Path], capsys: Capsys) -> None:
    state, _, replay_config = stack
    cap_dir = write_capture(state, "cap-import")
    code, out = main_json(tool, import_argv(state, cap_dir, config=replay_config), capsys)
    assert code == 0
    assert out["status"] == "already-imported"


@requires_images
def test_sync_imports_artifact_capture_into_the_stack(
    tool: Tool, stack: tuple[Path, dict, Path], capsys: Capsys, tmp_path: Path, fake_gh: Callable[..., None]
) -> None:
    """The full sync path against a live stack: fake gh serves the artifact ZIP, sync
    downloads it, extracts it under captures/, and replays it through the default
    replay config with exporter acknowledgement."""
    state, _, _ = stack
    cap_dir = write_capture(tmp_path, "cap-sync-stack")
    fake_gh(
        runs=[gh_run(101)],
        artifacts=[gh_artifact(555, 101)],
        zips={555: zip_capture(cap_dir, tmp_path / "cap-sync-stack.zip")},
    )
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    assert out["artifacts_downloaded"] == 1
    assert out["captures_imported"] == 1
    detail = out["details"][0]
    assert detail["status"] == "imported"
    assert detail["capture_id"] == "cap-sync-stack"
    assert (state / "captures" / "cap-sync-stack" / "metadata.json").is_file()
    # The next sync does not even list the run again: the import record is
    # the cache, and nothing is downloaded or re-extracted.
    code, out = main_json(tool, sync_argv(state), capsys)
    assert code == 0
    assert out["artifacts_downloaded"] == 0
    assert out["captures_skipped"] == 0
    # The default replay config persists file offsets as a container-mapped UID that
    # the test user cannot delete directly; remove them from inside the user namespace
    # so pytest's tmp cleanup stays quiet.
    subprocess.run(["podman", "unshare", "rm", "-rf", str(state / "replay-state")], check=False)


@requires_images
def test_import_requires_replay_config(stack: tuple[Path, dict, Path]) -> None:
    state, _, _ = stack
    cap_dir = write_capture(state, "cap-nocfg")
    proc = run_cli(import_argv(state, cap_dir, config=state / "missing.yaml"))
    assert proc.returncode == 2
    assert "replay configuration" in proc.stderr


@requires_images
def test_import_failure_records_incomplete_and_refuses_retry(stack: tuple[Path, dict, Path]) -> None:
    state, _, _ = stack
    dead_config = state / "dead.yaml"
    dead_config.write_text(DEAD_ENDPOINT_YAML)
    cap_dir = write_capture(state, "cap-dead")
    proc = run_cli(import_argv(state, cap_dir, config=dead_config, timeout=12))
    assert proc.returncode == 1
    assert "not confirmed" in proc.stderr
    entries = json.loads((state / "imports" / "index.json").read_text())["imports"]
    entry = next(e for e in entries.values() if e["capture_id"] == "cap-dead")
    assert entry["status"] == "incomplete"
    retry = run_cli(import_argv(state, cap_dir, config=dead_config, timeout=12))
    assert retry.returncode == 1
    assert "uncertain" in retry.stderr


@requires_images
def test_import_records_incomplete_when_collector_exits(stack: tuple[Path, dict, Path]) -> None:
    state, _, _ = stack
    bad_config = state / "bad.yaml"
    bad_config.write_text("not: [valid: yaml\n")
    cap_dir = write_capture(state, "cap-exit")
    proc = run_cli(import_argv(state, cap_dir, config=bad_config, timeout=60))
    assert proc.returncode == 1
    assert "exited before confirming delivery" in proc.stderr
    entries = json.loads((state / "imports" / "index.json").read_text())["imports"]
    entry = next(e for e in entries.values() if e["capture_id"] == "cap-exit")
    assert entry["status"] == "incomplete"


@requires_images
def test_down_preserves_imported_evidence_across_restart(
    tool: Tool, stack: tuple[Path, dict, Path], capsys: Capsys
) -> None:
    state, stack_record, replay_config = stack
    cap_dir = write_capture(state, "cap-import")
    code, _ = main_json(tool, import_argv(state, cap_dir, config=replay_config), capsys)
    assert code == 0
    container, trace_id = stack_record["container"], trace_id_for("cap-import")
    assert tempo_trace_code(container, trace_id) == "200"
    wait_for_tempo_block(container)
    code, out = main_json(tool, cli(state, "down"), capsys)
    assert code == 0
    assert out["container_preserved"] is True
    assert out["volume_preserved"] is True
    assert subprocess.run(["podman", "volume", "inspect", stack_record["volume"]], capture_output=True).returncode == 0
    code, out = main_json(tool, cli(state, "up", "--ui-port", "0", "--otlp-port", "0"), capsys)
    assert code == 0
    assert out["status"] == "restarted"
    assert tempo_trace_code(container, trace_id) == "200"


@requires_images
def test_up_is_idempotent(tool: Tool, stack: tuple[Path, dict, Path], capsys: Capsys) -> None:
    state, _, _ = stack
    code, out = main_json(tool, cli(state, "up", "--ui-port", "0", "--otlp-port", "0"), capsys)
    assert code == 0
    assert out["status"] == "already-up"


@requires_images
def test_import_preserves_old_original_timestamps_across_restart(
    tool: Tool, stack: tuple[Path, dict, Path], capsys: Capsys
) -> None:
    """The blocking Milestone 2 gap stays fixed: a year-old trace, a day-old metric
    sample and a day-old log line survive import and a down/up cycle with their
    original timestamps, using the local-development LGTM overrides mounted by up."""
    state, stack_record, replay_config = stack
    container = stack_record["container"]
    cap_dir = write_capture(state, "cap-aged", trace_age_s=365 * 86400, data_age_s=86400)
    day_s = SESSION_START - 86400
    trace_id = trace_id_for("cap-aged")
    baseline = tempo_block_count(container)
    code, out = main_json(tool, import_argv(state, cap_dir, config=replay_config), capsys)
    assert code == 0
    assert out["loki_flush"] == "ok"
    assert_aged_evidence(container, trace_id, day_s, "after import")
    # Prometheus and Loki are durable on the volume; Tempo needs the trace cut into a
    # block. The settle window covers one more periodic cut in case the first cut
    # raced with ingestion and left the trace in the discarded-on-restart live store.
    wait_for_tempo_block(container, baseline=baseline)
    time.sleep(45)
    code, _ = main_json(tool, cli(state, "down"), capsys)
    assert code == 0
    code, _ = main_json(tool, cli(state, "up", "--ui-port", "0", "--otlp-port", "0"), capsys)
    assert code == 0
    assert_aged_evidence(container, trace_id, day_s, "after down/up")

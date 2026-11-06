#!/usr/bin/env python3
"""Bounded, redacted Laya shadow route for a failed E2E job.

The result is diagnostic metadata only. This module never decides whether to
call Vertex or whether to publish a PR comment/status.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import FrameType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "analysis/laya/schema.json").read_text())
EXTRACTOR_VERSION = "osac-laya-evidence-v1"
DEFAULT_ENDPOINT = "https://laya-server-laya.apps.cnv2.engineering.redhat.com/predict"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_FILES = 300
FIELD_LIMITS = {"failed_step": 80, "job_error": 350, "pod_error": 500, "traceback": 250}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s*")
POD_LOG = re.compile(r"pod-(?:osac-aap-bootstrap|fulfillment-grpc-server|fulfillment-controller)-[a-z0-9-]+\.log")
AAP_FAILED = re.compile(r"project-update-\d+-failed\.txt")
BUILD_SIGNALS = (
    (re.compile(r"module declares its path|but was required as", re.I), 90, "Go module path mismatch"),
    (re.compile(r"go: module ", re.I), 90, "Go module dependency resolution failed"),
    (re.compile(r"error TS\d+", re.I), 90, "TypeScript compiler error"),
    (re.compile(r"undefined: ", re.I), 90, "Go compiler undefined symbol"),
    (re.compile(r"ERROR: Cannot install|ResolutionImpossible", re.I), 90, "Python dependency resolution failed"),
    (re.compile(r"not exported under the conditions", re.I), 90, "Package export resolution failed"),
    (re.compile(r"context must be a directory|could not be found on container", re.I), 90, "Build context missing"),
    (re.compile(r"fatal: couldn.t find remote ref", re.I), 90, "Git ref unavailable"),
)
TEST_SIGNAL = re.compile(
    r"\bE\s+(?:AssertionError|TimeoutError|ValueError|ExceptionGroup)|"
    r"stderr\s*=.*(?:ERROR:|Error:|failed to|FailedPrecondition)",
    re.I,
)
INSTALL_TIMEOUT = re.compile(
    r"Error: context deadline exceeded|Error: failed post-install|timed out waiting for the condition", re.I
)


def classify_stage(step: str) -> str:
    if "Run E2E" in step:
        return "e2e"
    if "Authorize" in step:
        return "ci"
    if "Build" in step:
        return "build"
    return "install"


def artifact_member_relevant(name: str) -> bool:
    """Allow only files whose paths and names are known to this extractor."""
    parts = Path(name).parts
    if len(parts) == 1:
        return parts[0] in {"junit.xml", "events.txt", "pods-describe.txt"} or POD_LOG.fullmatch(parts[0]) is not None
    return len(parts) == 2 and parts[0] == "aap-jobs" and AAP_FAILED.fullmatch(parts[1]) is not None


def _safe_file(path: Path, root: Path | None = None) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            return False
        if root is not None:
            path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _read_lines(path: Path, root: Path | None = None) -> list[str]:
    if not _safe_file(path, root):
        return []
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _add(candidates: list[tuple[int, str, str]], priority: int, source: str, line_number: int, message: str) -> None:
    # Callers pass fixed descriptions only. Never retain raw log text here.
    candidates.append((priority, f"{source}:{line_number}", message))


def _format(candidates: list[tuple[int, str, str]], limit: int, count: int = 2) -> tuple[str, list[str], bool]:
    selected: list[str] = []
    refs: list[str] = []
    keys: set[str] = set()
    direct = False
    for priority, ref, message in sorted(candidates, key=lambda row: -row[0]):
        key = re.sub(r"\d+", "#", message)
        if key in keys:
            continue
        keys.add(key)
        line = f"{ref}: {message}"
        if selected and len("\n".join(selected)) + len(line) + 1 > limit:
            continue
        selected.append(line[:limit])
        refs.append(ref)
        direct |= priority >= 70
        if len(selected) >= count:
            break
    return "\n".join(selected)[:limit], refs, direct


def _job_candidates(path: Path, stage: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    for number, line in enumerate(_read_lines(path), 1):
        line = TIMESTAMP.sub("", ANSI.sub("", line))
        if stage == "build":
            for pattern, priority, description in BUILD_SIGNALS:
                if pattern.search(line):
                    _add(candidates, priority, "job.log", number, description)
                    break
        elif stage == "ci" and line.startswith("##[error]") and ("Fork PR blocked:" in line or "/ok-to-test" in line):
            _add(candidates, 95, "job.log", number, "Fork PR blocked; /ok-to-test required")
        elif stage == "e2e" and TEST_SIGNAL.search(line):
            _add(candidates, 90, "job.log", number, "E2E test error")
        elif stage == "install" and INSTALL_TIMEOUT.search(line):
            _add(candidates, 20, "job.log", number, "Install timed out")
    return candidates


def _junit_candidates(path: Path, root: Path) -> list[tuple[int, str, str]]:
    if not _safe_file(path, root):
        return []
    try:
        document = ET.parse(path)
    except (OSError, ET.ParseError):
        return []
    candidates: list[tuple[int, str, str]] = []
    for testcase in document.getroot().iter("testcase"):
        failure = testcase.find("failure")
        if failure is None:
            failure = testcase.find("error")
        if failure is None:
            continue
        _add(candidates, 80, "junit.xml", 1, "JUnit test failure")
        if len(candidates) >= 5:
            break
    return candidates


def _artifact_candidates(root: Path) -> list[tuple[int, str, str]]:
    if not root.is_dir() or root.is_symlink():
        return []
    try:
        paths = sorted(root.iterdir())
    except OSError:
        return []
    candidates: list[tuple[int, str, str]] = []
    consumed = 0
    relevant = [path for path in paths if path.name != "junit.xml" and artifact_member_relevant(path.name)]
    for path in relevant[:MAX_ARTIFACT_FILES]:
        name = path.name
        if not _safe_file(path, root) or consumed + path.stat().st_size > MAX_ARTIFACT_BYTES:
            continue
        consumed += path.stat().st_size
        for number, line in enumerate(_read_lines(path, root), 1):
            if "Unable to create instance_group" in line and "pod_spec_override" in line:
                _add(candidates, 100, name, number, "AAP instance_group rejected pod_spec_override policy")
            elif "duplicate migration file" in line:
                _add(candidates, 100, name, number, "gRPC startup failed: duplicate migration file")
            elif "Failed to send token form" in line and "unsupported protocol scheme" in line:
                _add(candidates, 95, name, number, "Controller token form failed: unsupported protocol scheme")
            elif name == "events.txt" and (
                ("FailedCreate" in line and "job/osac-copy-fulfillment-kafka" in line)
                or ("FailedMount" in line and "references non-existent secret key" in line)
            ):
                description = (
                    "Kubernetes FailedCreate for Kafka copy job"
                    if "FailedCreate" in line
                    else "Kubernetes FailedMount: non-existent secret key"
                )
                _add(candidates, 95, name, number, description)
    aap_dir = root / "aap-jobs"
    if aap_dir.is_dir() and not aap_dir.is_symlink():
        try:
            aap_paths = sorted(aap_dir.iterdir())
        except OSError:
            aap_paths = []
        aap_paths = [path for path in aap_paths if artifact_member_relevant(f"aap-jobs/{path.name}")]
        for path in aap_paths[:MAX_ARTIFACT_FILES]:
            if not _safe_file(path, root) or consumed + path.stat().st_size > MAX_ARTIFACT_BYTES:
                continue
            consumed += path.stat().st_size
            for number, line in enumerate(_read_lines(path, root), 1):
                if "Failed to checkout" not in line and "unable to read tree" not in line:
                    continue
                _add(
                    candidates, 95, f"aap-jobs/{path.name}", number, "AAP project update could not checkout Git source"
                )
    return candidates


def extract_state(artifact_dir: Path, junit_path: Path, job_log_path: Path, failed_step: str) -> dict[str, Any]:
    """Select fixed diagnostic descriptions; never retain raw log content."""
    stage = classify_stage(failed_step)
    step = {
        "e2e": "Run E2E tests",
        "ci": "Authorize fork PR",
        "build": "Build and load component images",
        "install": "Install OSAC",
    }[stage]
    job, job_refs, job_direct = _format(_job_candidates(job_log_path, stage), FIELD_LIMITS["job_error"])
    pod_candidates = _artifact_candidates(artifact_dir) if stage == "install" else []
    pod, pod_refs, pod_direct = _format(pod_candidates, FIELD_LIMITS["pod_error"])
    trace_candidates = _junit_candidates(junit_path, artifact_dir) if stage == "e2e" else []
    traceback, trace_refs, trace_direct = _format(trace_candidates, FIELD_LIMITS["traceback"])
    state = {"failed_step": step, "job_error": job, "pod_error": pod, "traceback": traceback}
    assert sum(len(value) for value in state.values()) <= sum(FIELD_LIMITS.values())
    return {
        "state": state,
        "stage": stage,
        "evidence_available": job_direct or pod_direct or trace_direct,
        "evidence_refs": list(dict.fromkeys(job_refs + pod_refs + trace_refs))[:6],
    }


def _parse_answer(response: dict[str, Any], stage: str) -> tuple[str, float, float, str, int]:
    answer = response["answers"]["failure_domain"]
    criteria = SCHEMA["questions"][stage]["failure_domain"]["criteria"]
    choice = answer["choice"]
    probabilities = answer["probabilities"]
    if answer.get("type") != "choice" or choice not in criteria or set(probabilities) != set(criteria):
        raise ValueError("invalid choice response")
    values = [float(probabilities[label]) for label in criteria]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        raise ValueError("invalid probabilities")
    if abs(sum(values) - 1.0) > 0.02:
        raise ValueError("probabilities do not sum to one")
    score = float(answer["answer_confidence"])
    if not math.isfinite(score) or abs(score - float(probabilities[choice])) > 0.01:
        raise ValueError("choice score mismatch")
    runner_up = max(float(value) for label, value in probabilities.items() if label != choice)
    model = response["routing"]["model"]
    tokens = response["usage"]["input_tokens"]
    if not isinstance(model, str) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError("invalid metadata")
    return choice, score, runner_up, model, tokens


def _deadline_handler(_signal: int, _frame: FrameType | None) -> None:
    raise TimeoutError("Laya request exceeded total deadline")


@contextmanager
def _total_deadline(seconds: float) -> Iterator[None]:
    old_handler = signal.signal(signal.SIGALRM, _deadline_handler)
    old_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *old_timer)


def shadow_classify(evidence: dict[str, Any], endpoint: str = DEFAULT_ENDPOINT, timeout: float = 5.0) -> dict[str, Any]:
    """Return metadata only; every service failure becomes an abstention."""
    stage = evidence["stage"]
    result = {
        "schema_version": SCHEMA["version"],
        "extractor_version": EXTRACTOR_VERSION,
        "mode": "shadow",
        "method": "failed_step" if stage in SCHEMA["direct_step_routes"] else "laya",
        "route": "unknown",
        "model": None,
        "score": None,
        "runner_up_score": None,
        "latency_ms": None,
        "input_tokens": None,
        "evidence_available": evidence["evidence_available"],
        "evidence_refs": evidence["evidence_refs"],
        "state": evidence["state"],
    }
    if stage in SCHEMA["direct_step_routes"]:
        result["route"] = SCHEMA["direct_step_routes"][stage]
        return result
    payload = {"state": evidence["state"], "questions": SCHEMA["questions"][stage], "model": SCHEMA["model"]}
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    started = time.monotonic()
    try:
        with _total_deadline(timeout), urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("oversized Laya response")
        body = json.loads(data)
        route, score, runner_up, model, tokens = _parse_answer(body, stage)
        result.update(route=route, score=score, runner_up_score=runner_up, model=model, input_tokens=tokens)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        result.update(method="error", error=type(exc).__name__)
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
    return result


def main() -> None:
    requested = os.environ.get("LAYA_SHADOW_ENABLED", "false").lower() in {"true", "1", "yes"}
    endpoint = os.environ.get("LAYA_ENDPOINT", "").strip()
    enabled = requested and bool(endpoint)
    if enabled:
        artifact_dir = Path(os.environ.get("ARTIFACT_DIR") or "/nonexistent")
        evidence = extract_state(
            artifact_dir,
            Path(os.environ.get("JUNIT_PATH") or "/nonexistent"),
            Path(os.environ.get("JOB_LOG_PATH") or "/nonexistent"),
            os.environ.get("FAILED_STEP_NAME", ""),
        )
        result = shadow_classify(evidence, endpoint)
    else:
        result = {
            "schema_version": SCHEMA["version"],
            "extractor_version": EXTRACTOR_VERSION,
            "mode": "disabled",
            "method": "disabled",
            "route": "unknown",
            "reason": "feature_disabled" if not requested else "endpoint_not_configured",
            "evidence_available": False,
        }
    result["run"] = {
        "id": os.environ.get("FAILED_RUN_ID", ""),
        "url": os.environ.get("FAILED_RUN_URL", ""),
        "pr": os.environ.get("PR_NUMBER", ""),
        "workflow": os.environ.get("WORKFLOW_NAME", ""),
        "head_sha": os.environ.get("HEAD_SHA", ""),
    }
    output = os.environ.get("LAYA_SHADOW_FILE", "")
    if output:
        temp = f"{output}.tmp-{os.getpid()}"
        try:
            with open(temp, "w") as stream:
                json.dump(result, stream, indent=2)
                stream.write("\n")
            os.replace(temp, output)
        except OSError:
            with suppress(OSError):
                os.remove(temp)
            raise
    print(f"Laya shadow: {result['method']} route={result['route']} evidence={result['evidence_available']}")


if __name__ == "__main__":
    main()

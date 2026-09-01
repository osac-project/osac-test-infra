#!/usr/bin/env python3
"""Diagnose an E2E test failure with Gemini via Vertex AI.

Reads already-redacted, already-gathered failure data (JUnit XML + cluster
logs/events from gather-osac-logs.sh) and writes a short root-cause
diagnosis to $GITHUB_STEP_SUMMARY. Never executes or reads anything from a
PR's own source checkout -- only the artifact directory this same job's
Gather artifacts step already produced.

Bounded by design: sends a fixed-size extract, not the full multi-file
dump, to keep the prompt (and cost) predictable regardless of how much a
given failure happened to log.
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

MAX_FAILURES = 5
MAX_FAILURE_TEXT = 2000
MAX_LOG_MATCHES = 60
MAX_LOG_LINE_LEN = 400

ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "")
JUNIT_PATH = os.environ.get("JUNIT_PATH", "")
GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
GOOGLE_CLOUD_LOCATION = os.environ["GOOGLE_CLOUD_LOCATION"]
SUMMARY_PATH = os.environ.get("GITHUB_STEP_SUMMARY", "")
WORKFLOW_NAME = os.environ.get("WORKFLOW_NAME", "E2E job")
RUN_URL = os.environ.get("RUN_URL", "")

LOG_PATTERN = re.compile(r"error|traceback|panic|failed|exception", re.IGNORECASE)


def extract_junit_failures(path):
    if not path or not os.path.isfile(path):
        return "(no junit.xml found)"
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        return f"(junit.xml present but failed to parse: {exc})"

    chunks = []
    for testcase in tree.getroot().iter("testcase"):
        for tag in ("failure", "error"):
            node = testcase.find(tag)
            if node is None:
                continue
            name = testcase.get("name", "unknown")
            message = (node.get("message") or "").strip()
            text = (node.text or "").strip()[:MAX_FAILURE_TEXT]
            chunks.append(f"### {name}\n**{tag}**: {message}\n```\n{text}\n```")
            if len(chunks) >= MAX_FAILURES:
                return "\n\n".join(chunks)
    return "\n\n".join(chunks) if chunks else "(no failed/errored testcases in junit.xml)"


def extract_log_signal(artifact_dir):
    if not artifact_dir or not os.path.isdir(artifact_dir):
        return "(no artifact directory found)"

    matches = []
    # Flat, top-level text files first (events.txt, pods-describe.txt, pod-*.log)
    # -- these are what gather-osac-logs.sh writes for the main E2E namespace.
    for path in sorted(glob.glob(os.path.join(artifact_dir, "*.txt"))) + sorted(
        glob.glob(os.path.join(artifact_dir, "*.log"))
    ):
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    if LOG_PATTERN.search(line):
                        matches.append(f"[{os.path.basename(path)}] {line.strip()[:MAX_LOG_LINE_LEN]}")
                        if len(matches) >= MAX_LOG_MATCHES:
                            break
        except OSError:
            continue
        if len(matches) >= MAX_LOG_MATCHES:
            break

    return "\n".join(matches) if matches else "(no error/warning lines matched)"


def call_gemini(prompt):
    from google import genai

    client = genai.Client(
        vertexai=True, project=GOOGLE_CLOUD_PROJECT, location=GOOGLE_CLOUD_LOCATION
    )
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return resp.text or "(empty response from Gemini)"


def main():
    junit_section = extract_junit_failures(JUNIT_PATH)
    log_section = extract_log_signal(ARTIFACT_DIR)

    prompt = f"""You are diagnosing a failed CI run for an OpenShift-based
platform (OSAC). Given the test failures and matching log/event lines
below, write a SHORT (under 200 words) root-cause diagnosis for a
developer who has not looked at the logs yet: what likely broke, which
component is implicated, and one concrete next step to investigate. If the
evidence is insufficient to say anything confident, say so plainly rather
than guessing.

## JUnit failures
{junit_section}

## Matching log/event lines (grepped for error/traceback/panic/failed/exception)
{log_section}
"""

    try:
        diagnosis = call_gemini(prompt)
    except Exception as exc:  # noqa: BLE001 -- must never crash the job
        diagnosis = f"_AI diagnosis unavailable: {exc}_"

    if SUMMARY_PATH:
        with open(SUMMARY_PATH, "a") as f:
            f.write(f"## AI Failure Diagnosis: {WORKFLOW_NAME}\n\n")
            f.write(diagnosis.strip() + "\n\n")
            if RUN_URL:
                f.write(f"[Full run]({RUN_URL})\n")
    else:
        print(diagnosis)


if __name__ == "__main__":
    sys.exit(main())

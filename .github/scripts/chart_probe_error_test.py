#!/usr/bin/env python3
"""Exercise the chart-version workflow step with local command stubs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/e2e-vmaas-full-install.yml"
MANIFEST = '{"schemaVersion":2}'
SECRET = "synthetic-credential-never-log"


class ChartProbeTests(unittest.TestCase):
    def test_sanitizer_preserves_recognized_errors_and_bounds_output(self) -> None:
        for error, expected in (
            ("manifest unknown", "manifest unknown"),
            ("MANIFEST NOT FOUND", "manifest not found"),
            ("unauthorized: authentication required", "unauthorized; authentication required"),
            ("requested access to the resource is denied", "denied"),
            ("Forbidden", "forbidden"),
            ("too many requests", "too many requests"),
            ("toomanyrequests: rate exceeded", "toomanyrequests"),
            ("dial tcp: lookup registry: no such host", "no such host"),
            ("read: i/o timeout", "i/o timeout"),
            ("dial tcp: connection refused", "connection refused"),
            ("read: connection reset by peer", "connection reset by peer"),
            ("context deadline exceeded", "context deadline exceeded"),
            ("net/http: TLS handshake timeout", "tls handshake timeout"),
            ("x509: certificate signed by unknown authority", "certificate signed by unknown authority"),
            ("x509: certificate has expired or is not yet valid", "certificate has expired or is not yet valid"),
            ("StatusCode: 403", "HTTP 403"),
            ("status code: 429", "HTTP 429"),
            ("invalid status code from registry 500 (Internal Server Error)", "internal server error; HTTP 500"),
            ("received unexpected HTTP status: 502 Bad Gateway", "bad gateway; HTTP 502"),
            ("HTTP/2 503 Service Unavailable", "service unavailable; HTTP 503"),
            ("HTTP 504 Gateway Timeout", "gateway timeout; HTTP 504"),
        ):
            with self.subTest(error=error):
                result = subprocess.run(
                    ["python3", str(ROOT / ".github/scripts/chart_probe_error.py")],
                    input=f"{SECRET}\n\x1b[31m::warning::{error}\r::set-output name=leak::{SECRET}\n{SECRET}",
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, f"Last chart manifest probe error (allowlisted): {expected}\n")
                self.assertEqual(result.stderr, "")
        result, _, _ = self.run_step(99, " ".join(f"StatusCode: {status}" for status in range(400, 600)))
        diagnostic = next(line for line in result.stdout.splitlines() if line.startswith("Last chart"))
        self.assertLess(len(diagnostic), 512)

    def run_step(self, failures: int, error: str) -> tuple[subprocess.CompletedProcess, list[str], str]:
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step = next(step for step in workflow["jobs"]["e2e"]["steps"] if step.get("id") == "chart-version")
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            (temporary / "chart-registry-auth.json").write_text(json.dumps({"auths": {"registry": {"auth": SECRET}}}))
            stub = temporary / "stub.py"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "root = pathlib.Path(os.environ['STUB_DIR'])\n"
                "name = pathlib.Path(sys.argv[0]).name\n"
                "with (root / 'calls').open('a') as log:\n"
                "    log.write(json.dumps([name, *sys.argv[1:]]) + '\\n')\n"
                "if name == 'sleep':\n"
                "    sys.exit(0)\n"
                "state = root / 'count'\n"
                "count = int(state.read_text()) + 1 if state.exists() else 1\n"
                "state.write_text(str(count))\n"
                "if count <= int(os.environ['FAILURES']):\n"
                "    error = os.environ['PROBE_STDERR'] if count > 1 else 'first error: connection refused'\n"
                "    print(error, file=sys.stderr)\n"
                "    sys.exit(42)\n"
                f"print({MANIFEST!r}, end='')\n"
            )
            stub.chmod(0o700)
            for command in ("skopeo", "sleep"):
                (temporary / command).symlink_to(stub)
            output = temporary / "output"
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{temporary}:{os.environ['PATH']}",
                    "STUB_DIR": directory,
                    "FAILURES": str(failures),
                    "PROBE_STDERR": error,
                    "RUNNER_TEMP": directory,
                    "GITHUB_OUTPUT": str(output),
                    "CHART_OCI_REPO": "quay.io/example/chart",
                    "CHART_OCI_TAG": "test-sha",
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            calls = (temporary / "calls").read_text().splitlines()
            return result, calls, output.read_text() if output.exists() else ""

    def test_success_and_retry_preserve_digest(self) -> None:
        for failures in (0, 2):
            with self.subTest(failures=failures):
                result, calls, output = self.run_step(failures, SECRET)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum('"skopeo"' in call for call in calls), failures + 2)
                self.assertEqual(sum('"sleep", "15"' in call for call in calls), failures)
                self.assertIn(f"digest={hashlib.sha256(MANIFEST.encode()).hexdigest()}", output)
                self.assertNotIn(SECRET, result.stdout + result.stderr)
                self.assertNotIn(MANIFEST, result.stdout + result.stderr)
                self.assertNotIn("Last chart manifest probe error", result.stdout)
                probes = [json.loads(call) for call in calls if '"skopeo"' in call]
                for probe in probes:
                    self.assertEqual(probe[:4], ["skopeo", "inspect", "--raw", "--authfile"])
                    self.assertEqual(Path(probe[4]).name, "chart-registry-auth.json")
                    self.assertEqual(probe[5:], ["docker://quay.io/example/chart:test-sha"])

    def test_final_error_is_safe_and_only_from_last_probe(self) -> None:
        error = (
            f"Authorization: Bearer {SECRET}\nBasic {SECRET}\n"
            f"https://user:{SECRET}@registry.invalid/?token={SECRET}\n"
            f'{{"auth":"{SECRET}","password":"{SECRET}"}}\n'
            f"\x1b[31m::error::injected {SECRET}\r::add-mask::{SECRET}\n" + "manifest unknown\n" * 1000
        )
        result, calls, output = self.run_step(99, error)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sum('"skopeo"' in call for call in calls), 21)
        self.assertEqual(sum('"sleep", "15"' in call for call in calls), 20)
        self.assertTrue(
            all(
                "--raw" in call and "--authfile" in call and "docker://quay.io/example/chart:test-sha" in call
                for call in calls
                if '"skopeo"' in call
            )
        )
        self.assertEqual(output, "")
        logs = result.stdout + result.stderr
        self.assertIn("not available after 300s", logs)
        self.assertIn("Last chart manifest probe error", logs)
        diagnostic = next(line for line in logs.splitlines() if line.startswith("Last chart manifest probe error"))
        self.assertEqual(diagnostic, "Last chart manifest probe error (allowlisted): manifest unknown")
        for forbidden in (SECRET, "injected", "::add-mask::", "\x1b", "connection refused", MANIFEST):
            self.assertNotIn(forbidden, logs)
        self.assertEqual(logs.count("manifest unknown"), 1)

    def test_unknown_or_empty_error_fails_closed(self) -> None:
        for error in (SECRET, ""):
            with self.subTest(error=bool(error)):
                result, _, _ = self.run_step(99, error)
                self.assertEqual(result.returncode, 1)
                self.assertIn("no recognized diagnostic (raw stderr withheld)", result.stdout)
                self.assertNotIn(SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

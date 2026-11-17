#!/usr/bin/env python3
"""Exercise the chart-version workflow step with local command stubs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/e2e-vmaas-full-install.yml"
MANIFEST = '{"schemaVersion":2}'
FIXTURE_CREDENTIAL = "synthetic-credential-never-log"


class ChartProbeTests(unittest.TestCase):
    def run_step(
        self, failures: int, error: str, *, login_password: str | None = None
    ) -> tuple[subprocess.CompletedProcess, list[str], str]:
        workflow = yaml.safe_load(WORKFLOW.read_text())
        step = next(step for step in workflow["jobs"]["e2e"]["steps"] if step.get("id") == "chart-version")
        script = step["run"]
        if login_password is not None:
            login = next(
                step
                for step in workflow["jobs"]["e2e"]["steps"]
                if step.get("name") == "Log in to the chart's OCI registry"
            )
            script = login["run"] + "\n" + script
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            fixture_password = login_password if login_password is not None else FIXTURE_CREDENTIAL
            fixture_auth = base64.b64encode(f"fixture-user:{fixture_password}".encode()).decode()
            (temporary / "chart-registry-auth.json").write_text(
                json.dumps({"auths": {"quay.io": {"auth": fixture_auth}}})
            )
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
                "if name == 'helm':\n"
                "    expected = ['registry', 'login', 'quay.io', '--username', 'fixture-user', '--password-stdin']\n"
                "    if sys.argv[1:] != expected or sys.stdin.read() != os.environ['FIXTURE_PASSWORD']:\n"
                "        sys.exit(43)\n"
                "    print('FIXTURE_HELM_LOGIN')\n"
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
            for command in ("skopeo", "sleep", "helm"):
                (temporary / command).symlink_to(stub)
            output = temporary / "output"
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{temporary}:{os.environ['PATH']}",
                    "STUB_DIR": directory,
                    "FAILURES": str(failures),
                    "PROBE_STDERR": error,
                    "FIXTURE_PASSWORD": fixture_password,
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

    def test_login_registers_exact_masks_before_login_and_probe(self) -> None:
        for fixture_password in (FIXTURE_CREDENTIAL, "fixture-%0A-password\r\n::warning::fixture\nend"):
            with self.subTest(escaped=fixture_password != FIXTURE_CREDENTIAL):
                result, calls, _ = self.run_step(0, "unused", login_password=fixture_password)
                self.assertEqual(result.returncode, 0, result.stderr)
                fixture_auth = base64.b64encode(f"fixture-user:{fixture_password}".encode()).decode()
                escaped_password = fixture_password.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                lines = result.stdout.splitlines()
                masks = [line for line in lines if line.startswith("::add-mask::")]
                self.assertEqual(masks, [f"::add-mask::{fixture_auth}", f"::add-mask::{escaped_password}"])
                self.assertLess(lines.index(masks[-1]), lines.index("FIXTURE_HELM_LOGIN"))
                self.assertLess(
                    lines.index("FIXTURE_HELM_LOGIN"), lines.index("::group::Resolve chart version to install")
                )
                ordinary_output = "\n".join(line for line in lines if not line.startswith("::add-mask::"))
                for forbidden in (fixture_auth, fixture_password, "FIXTURE_PASSWORD=", '"auths"'):
                    self.assertNotIn(forbidden, ordinary_output + result.stderr)
                self.assertEqual(json.loads(calls[0])[0], "helm")
                self.assertEqual(sum('"skopeo"' in call for call in calls), 2)

    def test_success_and_retry_preserve_digest(self) -> None:
        for failures in (0, 2):
            with self.subTest(failures=failures):
                result, calls, output = self.run_step(failures, FIXTURE_CREDENTIAL)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum('"skopeo"' in call for call in calls), failures + 2)
                self.assertEqual(sum('"sleep", "15"' in call for call in calls), failures)
                self.assertIn(f"digest={hashlib.sha256(MANIFEST.encode()).hexdigest()}", output)
                self.assertNotIn(FIXTURE_CREDENTIAL, result.stdout + result.stderr)
                self.assertNotIn(MANIFEST, result.stdout + result.stderr)
                self.assertNotIn("Last chart manifest probe error", result.stdout)
                probes = [json.loads(call) for call in calls if '"skopeo"' in call]
                for probe in probes:
                    self.assertEqual(probe[:4], ["skopeo", "inspect", "--raw", "--authfile"])
                    self.assertEqual(Path(probe[4]).name, "chart-registry-auth.json")
                    self.assertEqual(probe[5:], ["docker://quay.io/example/chart:test-sha"])

    def test_final_error_is_preserved_with_workflow_commands_disabled(self) -> None:
        error = (
            "unrecognized registry response: custom failure 27%\n"
            "::error::injected\n::stop-commands::attacker\n::attacker::\n"
            "\x1b[31m::warning::injected\r::add-mask::injected\n"
            "last diagnostic line"
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
        prefix, diagnostic = result.stdout.split("Last chart manifest probe error:\n", 1)
        stop, diagnostic = diagnostic.split("\n", 1)
        token = stop.removeprefix("::stop-commands::")
        self.assertEqual(str(uuid.UUID(token)), token)
        self.assertNotIn(token, error)
        self.assertEqual(diagnostic, f"{error.replace(chr(13), chr(10))}\n::{token}::\n")
        self.assertNotIn("injected", prefix)
        self.assertEqual(result.stderr, "")
        for forbidden in (FIXTURE_CREDENTIAL, "connection refused", MANIFEST):
            self.assertNotIn(forbidden, logs)

    def test_empty_error_still_exits_one(self) -> None:
        result, _, output = self.run_step(99, "")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Last chart manifest probe error:\n::stop-commands::", result.stdout)
        self.assertEqual(output, "")
        self.assertNotIn(FIXTURE_CREDENTIAL, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

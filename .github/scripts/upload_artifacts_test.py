#!/usr/bin/env python3
"""Exercise the composite action's real YAML, Bash and release gate locally."""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ACTIONS = REPO / ".github/actions"


def load_action(name: str) -> dict:
    return yaml.safe_load((ACTIONS / name / "action.yml").read_text())


class UploadArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.action = load_action("upload-artifacts")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temp = pathlib.Path(self.temporary.name)
        self.artifacts = self.temp / "osac-artifacts"
        self.artifacts.mkdir()
        (self.artifacts / ".redaction-complete").touch()
        (self.artifacts / "log.txt").write_text("ordinary log output\n")
        self.inventory = self.temp / "osac-secret-inventory/k8s-secrets.json"
        self.inventory.parent.mkdir()
        self.inventory.write_text('{"items": []}')
        self.arguments = self.temp / "gate-arguments"
        binaries = self.temp / "bin"
        binaries.mkdir()
        wrapper = binaries / "python3"
        wrapper.write_text(
            f'#!/bin/bash\nprintf "%s\\n" "$@" > "$GATE_ARGUMENTS"\nexec {shlex.quote(sys.executable)} "$@"\n'
        )
        wrapper.chmod(0o755)
        self.environment = {
            "PATH": f"{binaries}:{os.defpath}",
            "RUNNER_TEMP": str(self.temp),
            "GATE_ARGUMENTS": str(self.arguments),
        }

    def step(self, name: str) -> dict:
        return next(step for step in self.action["runs"]["steps"] if step["name"] == name)

    def run_step(self, name: str, inputs: dict | None = None) -> subprocess.CompletedProcess:
        values = {key: item.get("default", "") for key, item in self.action["inputs"].items()}
        values.update(inputs or {})
        contexts = {"runner.temp": str(self.temp), "github.workspace": str(REPO)}
        contexts.update({f"inputs.{key}": str(value) for key, value in values.items()})

        def resolve(value: str) -> str:
            return re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", lambda match: contexts[match[1]], value)

        step = self.step(name)
        environment = self.environment | {key: resolve(value) for key, value in step.get("env", {}).items()}
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", resolve(step["run"])],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_size(self, expected: str, inputs: dict | None = None) -> None:
        result = self.run_step("Release gate - censor secrets", inputs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.arguments.read_text().splitlines()
        self.assertEqual(arguments[arguments.index("--max-file-mbytes") + 1], expected)
        self.assertTrue((self.artifacts / "log.txt").is_file())
        self.assertTrue((self.artifacts / "redaction-report.json").is_file())
        self.assertFalse(self.inventory.exists())

    def test_omitted_input_defaults_to_512(self) -> None:
        self.assert_size("512")

    def test_explicit_override_is_forwarded(self) -> None:
        self.assert_size("2048", {"max-file-mbytes": "2048"})

    def test_capture_yaml_passes_size_override(self) -> None:
        capture = load_action("ci-observe-capture")
        upload = next(step for step in capture["runs"]["steps"] if step.get("id") == "upload")
        self.assertEqual(upload["uses"], "./.github/actions/upload-artifacts")
        self.assertEqual(upload["with"]["max-file-mbytes"], "2048")
        self.assertEqual(upload["with"]["overwrite"], "true")
        self.assert_size("2048", {"max-file-mbytes": upload["with"]["max-file-mbytes"]})

    def test_invalid_size_fails_closed(self) -> None:
        result = self.run_step("Release gate - censor secrets", {"max-file-mbytes": "invalid"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("invalid int value", result.stderr)
        self.assertIn("removing the artifacts instead of uploading", result.stdout)
        self.assertFalse(self.artifacts.exists())
        self.assertFalse(self.inventory.exists())

    def test_gate_runtime_error_fails_closed(self) -> None:
        self.inventory.write_text("not valid JSON")
        result = self.run_step("Release gate - censor secrets")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("removing the artifacts instead of uploading", result.stdout)
        self.assertFalse(self.artifacts.exists())
        self.assertFalse(self.inventory.exists())

    def test_missing_redaction_marker_removes_artifacts(self) -> None:
        (self.artifacts / ".redaction-complete").unlink()
        result = self.run_step("Verify redaction complete")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.artifacts.exists())
        result = self.run_step("Release gate - censor secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.arguments.exists())

    def test_upload_contract_is_preserved(self) -> None:
        upload = self.step("Upload artifacts")
        self.assertEqual(upload["if"], "always()")
        self.assertEqual(upload["with"]["overwrite"], "${{ inputs.overwrite }}")
        self.assertEqual(upload["with"]["retention-days"], 7)
        self.assertEqual(upload["with"]["if-no-files-found"], "warn")
        self.assertEqual(
            upload["with"]["path"].splitlines(),
            ["${{ runner.temp }}/osac-artifacts/", "!${{ runner.temp }}/osac-artifacts/.redaction-complete"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

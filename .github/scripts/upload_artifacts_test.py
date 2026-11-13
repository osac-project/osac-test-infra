#!/usr/bin/env python3
"""Exercise the composite action's real YAML, Bash and release gate locally."""

from __future__ import annotations

import json
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

    def test_gitleaks_scan_runs_by_default(self) -> None:
        # Default true, so every caller that does not pass the input keeps
        # the full scan.
        self.assertEqual(self.action["inputs"]["gitleaks"]["default"], "true")
        scan = self.step("Scan artifacts with gitleaks")
        self.assertEqual(scan["if"], "always() && inputs.gitleaks == 'true'")
        # The gate and the upload must always run, whatever the input says.
        self.assertEqual(self.step("Release gate - censor secrets")["if"], "always()")
        self.assertEqual(self.step("Upload artifacts")["if"], "always() && inputs.private-repo == ''")

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
        self.assertEqual(upload["if"], "always() && inputs.private-repo == ''")
        self.assertEqual(upload["with"]["overwrite"], "${{ inputs.overwrite }}")
        self.assertEqual(upload["with"]["retention-days"], 30)
        self.assertEqual(upload["with"]["if-no-files-found"], "warn")
        self.assertEqual(
            upload["with"]["path"].splitlines(),
            ["${{ runner.temp }}/osac-artifacts/", "!${{ runner.temp }}/osac-artifacts/.redaction-complete"],
        )

    def test_public_upload_is_the_default(self) -> None:
        # Opt-in: callers that do not pass private-repo keep uploading to the run.
        self.assertEqual(self.action["inputs"]["private-repo"]["default"], "")

    def test_private_publish_replaces_the_public_upload(self) -> None:
        publish = self.step("Publish artifacts to the private repository")
        upload = self.step("Upload artifacts")
        # Exactly one of the two runs, and both still run after a failure.
        self.assertEqual(publish["if"], "always() && inputs.private-repo != ''")
        self.assertEqual(upload["if"], "always() && inputs.private-repo == ''")
        self.assertEqual(publish["uses"], "./.github/actions/publish-private-artifacts")
        # The release gate, which censors secrets, comes before either.
        names = [step["name"] for step in self.action["runs"]["steps"]]
        gate = names.index("Release gate - censor secrets")
        self.assertLess(gate, names.index("Publish artifacts to the private repository"))
        self.assertLess(gate, names.index("Upload artifacts"))


class PublishPrivateArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.action = load_action("publish-private-artifacts")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temp = pathlib.Path(self.temporary.name)
        self.source = self.temp / "osac-artifacts"
        (self.source / "logs").mkdir(parents=True)
        (self.source / ".redaction-complete").touch()
        (self.source / "logs/e2e.log").write_text("log\n")
        (self.source / "must-gather.tar.gz").write_text("data")
        self.staging = self.temp / "staging"
        self.output = self.temp / "github-output"
        self.summary = self.temp / "github-summary"
        self.request = self.temp / "request-body"
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.contexts = {
            "inputs.artifact-name": "osac-logs",
            "inputs.source-dir": "",
            "inputs.host-label": "osac-7",
            "inputs.private-repo": "osac-project/osac-ci-artifacts",
            "inputs.workflow": "collect.yml",
            "steps.token.outputs.token": "app-token",
            "github.workflow": "e2e vmaas",
            "github.triggering_actor": "someone",
        }
        self.environment = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.temp),
            "RUNNER_NAME": "osac-7-runner-02",
            "OSAC_ARTIFACT_STAGING_ROOT": str(self.staging),
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_RUN_ID": "4242",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_JOB": "e2e",
            "GITHUB_REPOSITORY": "osac-project/osac",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REF": "refs/pull/9/merge",
            "GITHUB_EVENT_NAME": "pull_request",
        }

    def run_step(self, name: str, overrides: dict | None = None) -> subprocess.CompletedProcess:
        contexts = self.contexts | (overrides or {})
        step = next(step for step in self.action["runs"]["steps"] if step["name"] == name)

        def resolve(value: str) -> str:
            return re.sub(r"\$\{\{\s*([\w.-]+)\s*\}\}", lambda match: contexts.get(match[1], ""), value)

        environment = self.environment | {key: resolve(value) for key, value in step.get("env", {}).items()}
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", resolve(step["run"])],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def stub_curl(self, body: str, status: int = 0) -> None:
        stub = self.bin / "curl"
        stub.write_text(
            "#!/bin/bash\n"
            'while [[ $# -gt 0 ]]; do if [[ $1 == -d ]]; then printf "%s" "$2" > "$REQUEST_BODY"; fi; '
            'if [[ $1 == https://* ]]; then printf "%s\\n" "$1" > "$REQUEST_BODY.url"; fi; shift; done\n'
            f"printf '%s' {shlex.quote(body)}\nexit {status}\n"
        )
        stub.chmod(0o755)
        self.environment["REQUEST_BODY"] = str(self.request)

    def test_staging_copies_files_without_the_marker(self) -> None:
        result = self.run_step("Stage files on the runner host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        outputs = self.outputs()
        self.assertEqual(outputs["staged"], "true")
        self.assertEqual(outputs["host"], "osac-7")
        destination = pathlib.Path(outputs["dir"])
        self.assertEqual(destination.parent, self.staging)
        self.assertTrue(destination.name.startswith("4242-2-e2e-osac-logs-"))
        self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o700")
        self.assertEqual((destination / "logs/e2e.log").read_text(), "log\n")
        self.assertTrue((destination / "must-gather.tar.gz").is_file())
        self.assertFalse((destination / ".redaction-complete").exists())
        # The source is left alone for the other steps.
        self.assertTrue((self.source / "logs/e2e.log").is_file())

    def test_staging_prunes_directories_a_day_old(self) -> None:
        stale = self.staging / "1-1-old-job"
        stale.mkdir(parents=True)
        old = stale.stat().st_mtime - 2 * 86400
        os.utime(stale, (old, old))
        fresh = self.staging / "2-1-recent-job"
        fresh.mkdir()
        result = self.run_step("Stage files on the runner host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_staging_skips_a_missing_or_empty_source(self) -> None:
        for path in (self.temp / "nope", self.temp / "empty"):
            (self.temp / "empty").mkdir(exist_ok=True)
            self.output.write_text("")
            result = self.run_step("Stage files on the runner host", {"inputs.source-dir": str(path)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.outputs(), {"staged": "false"})
            self.assertIn("::warning::", result.stdout)
        self.assertEqual(list(self.staging.iterdir()) if self.staging.exists() else [], [])

    def test_repository_name_is_validated(self) -> None:
        result = self.run_step("Split the repository name")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.outputs(), {"owner": "osac-project", "name": "osac-ci-artifacts"})
        for bad in ("noslash", "a/b/c", "a/b;rm -rf", ""):
            result = self.run_step("Split the repository name", {"inputs.private-repo": bad})
            self.assertEqual(result.returncode, 1, bad)

    def test_dispatch_sends_the_source_context_and_links_the_run(self) -> None:
        self.stub_curl('{"html_url": "https://github.com/osac-project/osac-ci-artifacts/actions/runs/77"}')
        self.contexts["steps.stage.outputs.dir"] = "/var/tmp/osac-artifacts/4242-2-e2e-osac-logs-1"
        self.contexts["steps.stage.outputs.host"] = "osac-7"
        result = self.run_step("Dispatch the collect workflow")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = json.loads(self.request.read_text())
        self.assertEqual(body["ref"], "main")
        self.assertTrue(body["return_run_details"])
        self.assertEqual(
            body["inputs"],
            {
                "host_label": "osac-7",
                "src_dir": "/var/tmp/osac-artifacts/4242-2-e2e-osac-logs-1",
                "artifact_name": "osac-logs",
                "source_repo": "osac-project/osac",
                "source_run_id": "4242",
                "source_run_attempt": "2",
                "source_run_url": "https://github.com/osac-project/osac/actions/runs/4242/attempts/2",
                "source_workflow": "e2e vmaas",
                "source_job": "e2e",
                "source_sha": "a" * 40,
                "source_ref": "refs/pull/9/merge",
                "source_event": "pull_request",
                "source_actor": "someone",
                "source_runner": "osac-7-runner-02",
            },
        )
        self.assertIn(
            "https://api.github.com/repos/osac-project/osac-ci-artifacts/actions/workflows/collect.yml/dispatches",
            pathlib.Path(f"{self.request}.url").read_text(),
        )
        self.assertIn("/osac-ci-artifacts/actions/runs/77", self.summary.read_text())

    def test_dispatch_without_run_details_links_the_workflow(self) -> None:
        self.stub_curl("")
        self.contexts["steps.stage.outputs.dir"] = "/var/tmp/osac-artifacts/x"
        result = self.run_step("Dispatch the collect workflow")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("/osac-ci-artifacts/actions/workflows/collect.yml", self.summary.read_text())

    def test_failed_dispatch_fails_and_keeps_the_files(self) -> None:
        self.stub_curl("", status=22)
        self.contexts["steps.stage.outputs.dir"] = "/var/tmp/osac-artifacts/x"
        result = self.run_step("Dispatch the collect workflow")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::Could not dispatch collect.yml", result.stdout)
        self.assertFalse(self.summary.exists())

    def test_token_is_limited_to_dispatching_in_the_private_repo(self) -> None:
        mint = next(step for step in self.action["runs"]["steps"] if step["name"].startswith("Mint a token"))
        self.assertEqual(mint["with"]["permissions"], '{"actions":"write"}')
        self.assertEqual(mint["with"]["repositories"], "${{ steps.repo.outputs.name }}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Exercise the composite action's real YAML, Bash and release gate locally."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
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
        self.assertEqual(scan["if"], "always() && (inputs.gitleaks == 'true' || inputs.private-repo != '')")
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

    def without_container_runtime(self) -> None:
        # Only bash and rm: the step finds neither docker nor podman.
        lean = self.temp / "lean-bin"
        lean.mkdir()
        for tool in ("bash", "rm"):
            (lean / tool).symlink_to(shutil.which(tool))
        self.environment["PATH"] = str(lean)

    def test_scan_that_cannot_run_fails_closed_for_a_private_repo(self) -> None:
        self.without_container_runtime()
        result = self.run_step("Scan artifacts with gitleaks", {"private-repo": "osac-project/osac-ci-artifacts"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::No container runtime available to scan artifacts bound for osac-project/osac-ci-artifacts", result.stdout)
        self.assertFalse(self.artifacts.exists())

    def test_scan_that_cannot_run_still_uploads_to_the_public_run(self) -> None:
        self.without_container_runtime()
        result = self.run_step("Scan artifacts with gitleaks")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("post-upload scanner will cover", result.stdout)
        self.assertTrue((self.artifacts / "log.txt").is_file())

    def test_private_publication_is_scanned_even_if_gitleaks_is_off(self) -> None:
        # Evaluate the step condition for the four input combinations.
        condition = self.step("Scan artifacts with gitleaks")["if"]
        expression = condition.replace("always() && ", "")
        for gitleaks, private, scanned in (
            ("true", "", True),
            ("false", "", False),
            ("false", "osac-project/osac-ci-artifacts", True),
            ("true", "osac-project/osac-ci-artifacts", True),
        ):
            value = expression.replace("inputs.gitleaks", repr(gitleaks)).replace("inputs.private-repo", repr(private))
            value = value.replace("||", " or ").replace("&&", " and ")
            self.assertEqual(eval(value, {}, {}), scanned, (gitleaks, private))  # noqa: S307
        # Fails closed rather than skipping when the combination is set.
        self.without_container_runtime()
        result = self.run_step(
            "Scan artifacts with gitleaks",
            {"private-repo": "osac-project/osac-ci-artifacts", "gitleaks": "false"},
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

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


    def test_stage_only_withholds_the_dispatch_and_reports_the_host(self) -> None:
        publish = self.step("Publish artifacts to the private repository")
        self.assertEqual(self.action["inputs"]["private-repo-stage-only"]["default"], "false")
        self.assertEqual(publish["with"]["dispatch"], "${{ inputs.private-repo-stage-only != 'true' }}")
        self.assertEqual(self.action["outputs"]["private-host"]["value"], "${{ steps.publish.outputs.host }}")
        self.assertEqual(self.action["outputs"]["private-runner"]["value"], "${{ steps.publish.outputs.runner }}")
        self.assertEqual(publish["id"], "publish")


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
            "inputs.source-job": "",
            "inputs.source-runner": "",
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

    @property
    def state_file(self) -> pathlib.Path:
        return self.temp / "publish-private-artifacts.json"

    def state(self) -> dict[str, str]:
        return json.loads(self.state_file.read_text())

    def write_state(self, directory: str, host: str = "osac-7") -> None:
        self.state_file.write_text(json.dumps({"dir": directory, "host": host}))

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

    def stage_directory(self, name: str = "4242-2-e2e-osac-logs-AbC123") -> pathlib.Path:
        directory = self.staging / name
        directory.mkdir(parents=True)
        (directory / "log.txt").write_text("x")
        return directory

    def locate(self, overrides: dict | None = None) -> subprocess.CompletedProcess:
        values = {"inputs.host-label": "osac-7"} | (overrides or {})
        return self.run_step("Find the files an earlier job staged", values)

    def test_every_step_follows_the_stage_and_dispatch_switches(self) -> None:
        steps = {step["name"]: step for step in self.action["runs"]["steps"]}
        self.assertEqual(steps["Stage files on the runner host"]["if"], "inputs.stage == 'true'")
        self.assertEqual(
            steps["Find the files an earlier job staged"]["if"],
            "inputs.stage != 'true' && inputs.dispatch == 'true'",
        )
        for name in ("Split the repository name", "Dispatch the collect workflow"):
            self.assertTrue(steps[name]["if"].startswith("inputs.dispatch == 'true' && "), name)
        mint = next(step for key, step in steps.items() if key.startswith("Mint a token"))
        self.assertTrue(mint["if"].startswith("inputs.dispatch == 'true' && "))
        # A job that only stages never needs the key.
        self.assertFalse(self.action["inputs"]["app-key"]["required"])
        self.assertEqual(self.action["inputs"]["stage"]["default"], "true")
        self.assertEqual(self.action["inputs"]["dispatch"]["default"], "true")

    def test_locate_finds_the_directory_an_earlier_job_staged(self) -> None:
        directory = self.stage_directory()
        # Other runs' and other artifacts' directories are not this one's.
        self.stage_directory("4242-2-e2e-other-name-Zzz999")
        self.stage_directory("9999-1-e2e-osac-logs-Zzz999")
        self.stage_directory("4242-1-e2e-osac-logs-Zzz999")
        result = self.locate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.outputs(), {"staged": "true"})
        self.assertEqual(self.state(), {"dir": str(directory), "host": "osac-7"})
        self.assertEqual(oct(self.state_file.stat().st_mode & 0o777), "0o600")

    def test_locate_needs_the_host_and_refuses_to_guess(self) -> None:
        self.stage_directory()
        result = self.locate({"inputs.host-label": ""})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("host-label is required", result.stdout)
        self.stage_directory("4242-2-bmaas-osac-logs-Other1")
        result = self.locate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("refusing to guess", result.stdout)
        self.assertFalse(self.state_file.exists())

    def test_locate_with_nothing_staged_publishes_nothing(self) -> None:
        self.staging.mkdir()
        result = self.locate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.outputs(), {"staged": "false"})
        self.assertIn("::warning::", result.stdout)
        self.assertFalse(self.state_file.exists())

    def test_locate_ignores_a_link_to_somewhere_else(self) -> None:
        # Whatever the staging job's code left under the staging root, only a
        # plain directory is handed to the collect workflow.
        self.staging.mkdir()
        elsewhere = self.temp / "elsewhere"
        elsewhere.mkdir()
        (self.staging / "4242-2-e2e-osac-logs-Link00").symlink_to(elsewhere)
        result = self.locate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.outputs(), {"staged": "false"})
        self.assertFalse(self.state_file.exists())

    def test_the_dispatch_can_name_the_staging_job_as_the_source(self) -> None:
        self.stub_curl("")
        self.write_state("/var/tmp/osac-artifacts/x")
        result = self.run_step(
            "Dispatch the collect workflow",
            {"inputs.source-job": "e2e", "inputs.source-runner": "osac-7-runner-05"} | {},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        body = json.loads(self.request.read_text())["inputs"]
        self.assertEqual((body["source_job"], body["source_runner"]), ("e2e", "osac-7-runner-05"))
        # Without them the dispatching job is the source, as before.
        self.environment["GITHUB_JOB"] = "publish-private"
        self.write_state("/var/tmp/osac-artifacts/x")
        result = self.run_step("Dispatch the collect workflow")
        body = json.loads(self.request.read_text())["inputs"]
        self.assertEqual((body["source_job"], body["source_runner"]), ("publish-private", "osac-7-runner-02"))

    def test_stage_then_locate_finds_what_stage_wrote(self) -> None:
        # The two halves run in different jobs; their naming must agree.
        self.assertEqual(self.run_step("Stage files on the runner host").returncode, 0)
        staged = self.state()["dir"]
        self.state_file.unlink()
        self.output.write_text("")
        self.environment["GITHUB_JOB"] = "publish-private"
        result = self.locate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state()["dir"], staged)


    def test_staging_copies_files_without_the_marker(self) -> None:
        result = self.run_step("Stage files on the runner host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The path is not a step output: it would be printed in the public log
        # with the next step's env. The host is (the dispatching job runs on it).
        self.assertEqual(self.outputs(), {"staged": "true", "host": "osac-7", "runner": "osac-7-runner-02"})
        self.assertEqual(self.state()["host"], "osac-7")
        self.assertEqual(oct(self.state_file.stat().st_mode & 0o777), "0o600")
        destination = pathlib.Path(self.state()["dir"])
        self.assertEqual(destination.parent, self.staging)
        self.assertTrue(destination.name.startswith("4242-2-e2e-osac-logs-"))
        self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o700")
        self.assertEqual((destination / "logs/e2e.log").read_text(), "log\n")
        self.assertTrue((destination / "must-gather.tar.gz").is_file())
        self.assertFalse((destination / ".redaction-complete").exists())
        # The source is left alone for the other steps.
        self.assertTrue((self.source / "logs/e2e.log").is_file())

    def test_staged_directory_starts_its_day_at_publication(self) -> None:
        # cp -a copies the source directory's mtime; an old one must not make
        # the next job's prune delete the files before they are collected.
        long_ago = self.source.stat().st_mtime - 3 * 86400
        os.utime(self.source, (long_ago, long_ago))
        result = self.run_step("Stage files on the runner host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        destination = pathlib.Path(self.state()["dir"])
        self.assertGreater(destination.stat().st_mtime, long_ago + 86400)
        # And a second job's prune leaves it alone.
        result = self.run_step("Stage files on the runner host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(destination.exists())

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
            self.assertFalse(self.state_file.exists())
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
        self.write_state("/var/tmp/osac-artifacts/4242-2-e2e-osac-logs-1")
        result = self.run_step("Dispatch the collect workflow")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.state_file.exists())
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
        self.write_state("/var/tmp/osac-artifacts/x")
        result = self.run_step("Dispatch the collect workflow")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("/osac-ci-artifacts/actions/workflows/collect.yml", self.summary.read_text())

    def test_failed_dispatch_fails_and_keeps_the_files(self) -> None:
        self.stub_curl("", status=22)
        self.write_state("/var/tmp/osac-artifacts/secret-dir", host="osac-7")
        result = self.run_step("Dispatch the collect workflow")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::Could not dispatch collect.yml", result.stdout)
        # The log is public: no host and no staging path in it.
        for value in ("osac-7", "secret-dir", "/var/tmp"):
            self.assertNotIn(value, result.stdout + result.stderr)
        self.assertFalse(self.summary.exists())

    def test_no_step_prints_the_host_or_staging_path_in_its_env(self) -> None:
        # A step's env block is printed in the public run log.
        for step in self.action["runs"]["steps"]:
            for value in (step.get("env") or {}).values():
                self.assertNotIn("steps.stage.outputs", str(value), step["name"])
        stage = next(step for step in self.action["runs"]["steps"] if step["name"].startswith("Stage"))
        self.assertNotIn("dir", self.outputs_written_by(stage))

    @staticmethod
    def outputs_written_by(step: dict) -> list[str]:
        return re.findall(r'echo "(\w+)=.*>> "\$\{GITHUB_OUTPUT\}"', step["run"])

    def test_token_is_limited_to_dispatching_in_the_private_repo(self) -> None:
        mint = next(step for step in self.action["runs"]["steps"] if step["name"].startswith("Mint a token"))
        self.assertEqual(mint["with"]["permissions"], '{"actions":"write"}')
        self.assertEqual(mint["with"]["repositories"], "${{ steps.repo.outputs.name }}")


SHARED_WORKFLOWS = {
    "e2e-vmaas": "osac-logs-${{ github.run_id }}",
    "e2e-vmaas-full-install": "osac-logs-full-install-${{ github.run_id }}",
    "e2e-bmaas-full-install": "osac-logs-bmaas-full-install-${{ github.run_id }}",
    "e2e-caas-full-install": "osac-logs-caas-full-install-${{ github.run_id }}",
    "e2e-full-regression": "osac-logs-full-regression-${{ github.run_id }}",
}


class SharedWorkflowsPublishFromATrustedJobTest(unittest.TestCase):
    """The suite job stages; only a job of trusted code holds the app key."""

    def workflow(self, name: str) -> dict:
        return yaml.safe_load((REPO / f".github/workflows/{name}.yml").read_text())

    def test_the_suite_job_never_receives_the_app_key(self) -> None:
        for name in SHARED_WORKFLOWS:
            text = json.dumps(self.workflow(name)["jobs"]["e2e"])
            self.assertNotIn("private-repo-app-key", text, name)
            self.assertNotIn("private-repo-app-id", text, name)
            # The secrets may only be compared with '' (is the publication on?),
            # never handed to a step, an env block or an output.
            for secret in ("osac-ci-app-private-key", "osac-ci-app-id"):
                uses = re.findall(rf"secrets\.{secret}(?! (?:!=|==) '')", text)
                self.assertEqual(uses, [], f"{name}: {secret} is used by the suite job")

    def test_both_upload_steps_only_stage(self) -> None:
        for name, artifact in SHARED_WORKFLOWS.items():
            steps = [s for s in self.workflow(name)["jobs"]["e2e"]["steps"] if s.get("uses") == "./.github/actions/upload-artifacts"]
            self.assertEqual(len(steps), 2, name)
            for step in steps:
                self.assertEqual(step["with"]["private-repo-stage-only"], "true", name)
                self.assertEqual(step["with"]["artifact-name"], artifact, name)
            self.assertEqual({s["id"] for s in steps}, {"upload", "upload-cancelled"}, name)

    def test_the_host_reaches_the_publish_job(self) -> None:
        for name in SHARED_WORKFLOWS:
            outputs = self.workflow(name)["jobs"]["e2e"]["outputs"]
            for key in ("private-host", "private-runner"):
                self.assertIn(f"steps.upload.outputs.{key}", outputs[key], name)
                self.assertIn(f"steps.upload-cancelled.outputs.{key}", outputs[key], name)

    def test_the_publish_job_runs_trusted_actions_on_the_same_host(self) -> None:
        for name, artifact in SHARED_WORKFLOWS.items():
            job = self.workflow(name)["jobs"]["publish-private"]
            self.assertEqual(job["needs"], ["e2e"], name)
            self.assertEqual(job["runs-on"], "${{ needs.e2e.outputs.private-host }}", name)
            # A failed or cancelled run's logs matter most; nothing staged, nothing to do.
            self.assertEqual(job["if"], "${{ always() && needs.e2e.outputs.private-host != '' }}", name)
            self.assertEqual(job["permissions"], {"contents": "read"}, name)
            checkout, publish = job["steps"]
            # The default branch's actions, never the commit under test.
            self.assertEqual(checkout["with"]["ref"], "main", name)
            self.assertEqual(checkout["with"]["repository"], "${{ job.workflow_repository }}", name)
            self.assertEqual(checkout["with"]["sparse-checkout"], ".github/actions", name)
            self.assertNotIn("inputs.test-infra", json.dumps(checkout), name)
            self.assertEqual(publish["uses"], "./.github/actions/publish-private-artifacts", name)
            self.assertEqual(publish["with"]["artifact-name"], artifact, name)
            self.assertEqual(publish["with"]["stage"], "false", name)
            self.assertEqual(publish["with"]["host-label"], "${{ needs.e2e.outputs.private-host }}", name)
            self.assertEqual(publish["with"]["app-key"], "${{ secrets.osac-ci-app-private-key }}", name)

    def test_pull_request_and_queue_callers_pass_the_app_secrets(self) -> None:
        # Only reachable now that the key is used by the trusted job alone.
        for name in ("e2e-pr-vmaas", "e2e-pr-caas", "e2e-pr-bmaas"):
            (job,) = [j for j in self.workflow(name)["jobs"].values() if "uses" in j and j["uses"].startswith("./")]
            self.assertEqual(job["secrets"]["osac-ci-app-id"], "${{ secrets.OSAC_CI_APP_ID }}", name)
            self.assertEqual(job["secrets"]["osac-ci-app-private-key"], "${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}", name)
        suites = [j for j in self.workflow("e2e-queue")["jobs"].values() if str(j.get("uses", "")).startswith("./")]
        self.assertEqual(len(suites), 3)
        for job in suites:
            self.assertEqual(job["secrets"]["osac-ci-app-private-key"], "${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

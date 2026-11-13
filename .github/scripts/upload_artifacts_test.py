#!/usr/bin/env python3
"""Exercise the composite action's real YAML, Bash and release gate locally."""

from __future__ import annotations

import base64
import hashlib
import itertools
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
            "GITHUB_OUTPUT": str(self.temp / "step-outputs"),
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
        self.assertIn("steps.release.outcome == 'success'", self.step("Upload artifacts")["if"])

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
        self.assertIn("steps.verify.outcome == 'success'", upload["if"])
        self.assertIn("steps.scan.outcome == 'success'", upload["if"])
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
        self.assertIn(
            "::error::No container runtime available to scan artifacts bound for osac-project/osac-ci-artifacts",
            result.stdout,
        )
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
            self.assertEqual(eval(value, {}, {}), scanned, (gitleaks, private))
        # Fails closed rather than skipping when the combination is set.
        self.without_container_runtime()
        result = self.run_step(
            "Scan artifacts with gitleaks", {"private-repo": "osac-project/osac-ci-artifacts", "gitleaks": "false"}
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_public_upload_is_the_default(self) -> None:
        # Opt-in: callers that do not pass private-repo keep uploading to the run.
        self.assertEqual(self.action["inputs"]["private-repo"]["default"], "")

    def test_private_publish_replaces_the_public_upload(self) -> None:
        publish = self.step("Publish artifacts to the private repository")
        upload = self.step("Upload artifacts")
        self.assertIn("inputs.private-repo != ''", publish["if"])
        self.assertIn("inputs.private-repo == ''", upload["if"])
        self.assertEqual(publish["uses"], "./.github/actions/publish-private-artifacts")
        # The release gate, which censors secrets, comes before either.
        names = [step["name"] for step in self.action["runs"]["steps"]]
        gate = names.index("Release gate - censor secrets")
        self.assertLess(gate, names.index("Publish artifacts to the private repository"))
        self.assertLess(gate, names.index("Upload artifacts"))

    def must_gather(self) -> pathlib.Path:
        tree = self.artifacts / "mgmt-cluster/must-gather"
        tree.mkdir(parents=True)
        (tree / "kubelet_service.log").write_text("service history\n")
        self.normalize(tree)
        return tree

    def normalize(self, tree: pathlib.Path) -> None:
        result = subprocess.run(
            [sys.executable, str(REPO / ".github/scripts/collect-must-gather.py"), str(tree), "--normalize"],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        (tree / "collection-status.json").write_text('{"status":"complete","reason":"none"}\n')

    def scanner(self, mode: str) -> None:
        docker = self.temp / "bin/docker"
        docker.write_text(f'''#!/bin/bash
set -eu
output=""
for argument in "$@"; do
  case "$argument" in
    *:/out:rw*) output="${{argument%%:/out:rw*}}" ;;
  esac
done
if [[ "{mode}" == "failed" ]]; then exit 2; fi
if [[ "{mode}" == "missing" ]]; then exit 0; fi
report="findings-pass-1.json"
for argument in "$@"; do
  case "$argument" in --report-path=*) report="${{argument##*/}}" ;; esac
done
if [[ "{mode}" == "remaining" ]]; then
  printf '%s\\n' '[{{"RuleID":"synthetic","File":"/scan/log.txt","StartLine":1}}]' > "$output/$report"
elif [[ "{mode}" == "invalid" ]]; then
  printf '{{}}\\n' > "$output/$report"
else
  printf '[]\\n' > "$output/$report"
fi
''')
        docker.chmod(0o755)

    def test_must_gather_guard_refuses_missing_normalization_and_scanner_opt_out(self) -> None:
        for mode in (
            "missing-normalization",
            "scan-disabled",
            "binary",
            "encoded-archive",
            "symlink",
            "unsafe-name",
            "lookalike-service-name",
            "lookalike-directory-name",
            "metadata",
            "duplicate-key",
        ):
            with self.subTest(mode=mode):
                self.artifacts.mkdir(exist_ok=True)
                tree = self.must_gather()
                if mode == "missing-normalization":
                    (tree / "omissions.json").unlink()
                if mode == "binary":
                    next(tree.glob("*.log")).write_bytes(b"\x00opaque")
                if mode == "encoded-archive":
                    next(tree.glob("*.log")).write_bytes(base64.b64encode(b"\x1f\x8b" + b"opaque" * 12))
                if mode == "symlink":
                    path = next(tree.glob("*.log"))
                    path.unlink()
                    path.symlink_to(self.arguments)
                if mode == "unsafe-name":
                    (tree / "synthetic-sentinel-credential.yaml").write_text("kind: List\n")
                if mode == "lookalike-service-name":
                    (tree / "synthetic-sentinel-credential-kubelet_service.log").write_text("service history\n")
                if mode == "lookalike-directory-name":
                    (tree / "host_service_logs-synthetic-sentinel-credential").mkdir()
                if mode == "metadata":
                    (tree / "omissions.json").write_text('{"synthetic-sentinel-credential": 1}')
                if mode == "duplicate-key":
                    (tree / "collection-status.json").write_text(
                        '{"status":"synthetic-sentinel-credential","status":"complete","reason":"none"}'
                    )
                result = self.run_step(
                    "Verify redaction complete", {"gitleaks": "false" if mode == "scan-disabled" else "true"}
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(self.artifacts.exists())

    def test_artifact_ancestor_link_is_refused_without_touching_its_target(self) -> None:
        self.artifacts.rename(self.temp / "external-diagnostics")
        self.artifacts.symlink_to(self.temp / "external-diagnostics", target_is_directory=True)
        result = self.run_step("Verify redaction complete")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.artifacts.exists())
        self.assertTrue((self.temp / "external-diagnostics/log.txt").exists())

    def test_must_gather_scan_fails_closed_on_engine_failure_missing_report_or_remaining_findings(self) -> None:
        for mode in ("failed", "missing", "remaining", "invalid"):
            with self.subTest(mode=mode):
                self.artifacts.mkdir(exist_ok=True)
                self.must_gather()
                self.scanner(mode)
                reports = self.temp / "gitleaks-pre-upload"
                reports.mkdir(exist_ok=True)
                (reports / "findings-pass-1.json").write_text("[]")
                result = self.run_step("Scan artifacts with gitleaks")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertFalse(self.artifacts.exists())

    def test_must_gather_real_gate_censors_known_secret_in_plaintext_yaml_and_json(self) -> None:
        tree = self.must_gather()
        sentinel = "synthetic-upload-secret-950147-testing-only"
        self.inventory.write_text(
            json.dumps({"items": [{"data": {"token": base64.b64encode(sentinel.encode()).decode()}}]})
        )
        (tree / "resource.yaml").write_text(f"credential: {sentinel}\n")
        (tree / "resource.json").write_text(json.dumps({"credential": sentinel}))
        (tree / f"{sentinel}.yaml").write_text(f"credential: {sentinel}\n")
        self.normalize(tree)
        self.assertEqual(self.run_step("Verify redaction complete").returncode, 0)
        self.scanner("clean")
        self.assertEqual(self.run_step("Scan artifacts with gitleaks").returncode, 0)
        result = self.run_step("Release gate - censor secrets")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in tree.rglob("*"):
            self.assertNotIn(sentinel, str(path.relative_to(tree)))
            if path.is_file():
                self.assertNotIn(sentinel, path.read_text())
        self.assertNotIn(sentinel, result.stdout + result.stderr)
        self.assertTrue(any("service history" in path.read_text() for path in tree.glob("*.log")))

    def test_standard_service_paths_survive_real_redaction_and_upload_gate(self) -> None:
        tree = self.artifacts / "mgmt-cluster/must-gather"
        sentinel = "synthetic-service-secret-950147-testing-only"
        image = f"quay-io-openshift-release-dev-ocp-v4-0-art-dev-sha256-{sentinel}"
        node = f"node-{sentinel}"
        source = tree / image / "host_service_logs"
        expected = []
        wrapper = "dir-" + hashlib.sha256(os.fsencode(image)).hexdigest()
        for role in ("masters", "workers"):
            for service in ("kubelet_service.log", "crio_service.log"):
                path = source / role / service
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'service history\n{{"password": "{sentinel}"}}\n')
                expected.append(pathlib.Path(wrapper) / "host_service_logs" / role / service)
        dynamic = source / "masters" / node
        dynamic.mkdir()
        (dynamic / "kubelet_service.log").write_text(f'node history\n{{"token": "{sentinel}"}}\n')
        expected.append(
            pathlib.Path(wrapper)
            / "host_service_logs/masters"
            / ("dir-" + hashlib.sha256(os.fsencode(node)).hexdigest())
            / "kubelet_service.log"
        )
        (dynamic / f"{sentinel}-crio_service.log").write_text(f'{{"password": "{sentinel}"}}\n')
        oversized = source / "workers" / node / "crio_service.log"
        oversized.parent.mkdir()
        with oversized.open("wb") as handle:
            handle.truncate(33 * 1024 * 1024)
        (self.artifacts / ".redaction-complete").unlink()
        self.normalize(tree)
        self.assertEqual(json.loads((tree / "omissions.json").read_text()), {"oversized": 1})
        self.assertEqual(len(list(tree.rglob("crio_service.log"))), 2)
        redaction = subprocess.run(
            ["bash", str(REPO / "scripts/gather-osac-logs.sh"), str(self.artifacts)],
            env=self.environment | {"OSAC_GATHER_REDACT_ONLY": "true", "OSAC_GATHER_COLLECT_ONLY": "false"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(redaction.returncode, 0, redaction.stdout + redaction.stderr)
        for relative in expected:
            self.assertIn("history", (tree / relative).read_text())
            self.assertIn("REDACTED", (tree / relative).read_text())
        self.assertFalse((self.artifacts / ".redaction-complete").exists())
        (self.artifacts / ".redaction-complete").touch()
        self.scanner("clean")
        outputs = redaction.stdout + redaction.stderr
        for step in ("Verify redaction complete", "Scan artifacts with gitleaks", "Release gate - censor secrets"):
            result = self.run_step(step)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outputs += result.stdout + result.stderr
        for relative in expected:
            self.assertTrue((tree / relative).is_file(), relative)
        self.assertNotIn(sentinel, outputs)
        for path in self.artifacts.rglob("*"):
            self.assertNotIn(sentinel, str(path.relative_to(self.artifacts)))
            if path.is_file():
                self.assertNotIn(sentinel, path.read_text())

    def test_final_gate_rechecks_the_actual_subset_after_initial_verification(self) -> None:
        tree = self.must_gather()
        self.assertEqual(self.run_step("Verify redaction complete").returncode, 0)
        self.scanner("clean")
        self.assertEqual(self.run_step("Scan artifacts with gitleaks").returncode, 0)
        (tree / "unvouched-name.txt").write_text("diagnostic text\n")
        result = self.run_step("Release gate - censor secrets")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.artifacts.exists())
        self.assertIn("steps.release.outcome == 'success'", self.step("Upload artifacts")["if"])

    def test_must_gather_scanner_unavailable_fails_closed(self) -> None:
        self.must_gather()
        binaries = self.temp / "no-runtime"
        binaries.mkdir()
        for command in ("bash", "rm"):
            (binaries / command).symlink_to(pathlib.Path("/bin") / command)
        self.environment["PATH"] = str(binaries)
        result = self.run_step("Scan artifacts with gitleaks")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.artifacts.exists())

    def test_actual_upload_predicate_blocks_every_failed_skipped_or_interrupted_gate(self) -> None:
        outcomes = ("success", "failure", "skipped", "cancelled")
        combinations = itertools.product(
            outcomes, outcomes, outcomes, ("true", ""), ("", "osac-project/osac-ci-artifacts")
        )
        for verify, scan, release, must_gather, private in combinations:
            for destination in ("Upload artifacts", "Publish artifacts to the private repository"):
                expression = self.step(destination)["if"]
                expression = expression.replace("always()", "True")
                expression = expression.replace("inputs.private-repo", repr(private))
                expression = expression.replace("steps.verify.outputs.must-gather", repr(must_gather))
                for name, outcome in (("verify", verify), ("scan", scan), ("release", release)):
                    expression = expression.replace(f"steps.{name}.outcome", repr(outcome))
                expression = expression.replace("&&", " and ").replace("||", " or ")
                allowed = eval(expression, {"__builtins__": {}})
                expected = (
                    verify == "success"
                    and release == "success"
                    and ((not private and must_gather != "true") or scan == "success")
                    and bool(private) == (destination == "Publish artifacts to the private repository")
                )
                self.assertEqual(allowed, expected, (destination, verify, scan, release, must_gather, private))


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

    def test_staging_copies_files_without_the_marker(self) -> None:
        result = self.run_step("Stage files on the runner host")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The host and the path are not step outputs: they would be printed
        # in the public log with the next step's env.
        self.assertEqual(self.outputs(), {"staged": "true"})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

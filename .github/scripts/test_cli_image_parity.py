#!/usr/bin/env python3
"""Exercise workflow CLI extraction and test-image forwarding without a cluster.

Run: uv run python .github/scripts/test_cli_image_parity.py
These are infrastructure unit tests, not the E2E suites owned by the monorepo.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
PIPELINES = (
    "e2e-caas-full-install.yml",
    "e2e-bmaas-full-install.yml",
    "e2e-vmaas-full-install.yml",
    "e2e-full-regression.yml",
)
PODMAN_STUB = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PODMAN_LOG"
case "$1" in
  create) printf 'test-container\\n' ;;
  cp)
    if [[ "$CP_EXIT_CODE" != 0 ]]; then exit "$CP_EXIT_CODE"; fi
    printf 'PR CLI binary\\n' > "$3"
    ;;
  rm) ;;
  build)
    [[ " $* " == *" --build-arg OSAC_CLI_BIN=osac-cli-bin "* ]]
    cp ./osac-cli-bin "$TEST_IMAGE_CLI"
    ;;
  *) exit 99 ;;
esac
"""


class CLIImageParityTests(unittest.TestCase):
    def _run_workflow(
        self, workflow: str, image_key: str, *, build_test_image: bool = False, cp_exit_code: int = 0
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
        document = yaml.safe_load((WORKFLOWS / workflow).read_text())
        steps = document["jobs"]["e2e"]["steps"]
        build = next(step["run"] for step in steps if step.get("name") == "Build and load component images")
        # Execute the real extraction block, without the unrelated cluster boot,
        # source clones, image build, or node load that surrounds it.
        section = build.split("# Extract the osac CLI baked", 1)[1]
        extraction = re.search(r"^([ \t]*)if \[\[.*?^\1fi$", section, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(extraction, f"Missing CLI extraction block in {workflow}")
        script = "set -euo pipefail\n" + extraction.group(0) + "\n"
        if build_test_image:
            image_build = next(step["run"] for step in steps if step.get("name") == "Build test image")
            script += 'while IFS= read -r setting; do export "$setting"; done < "$GITHUB_ENV"\n' + image_build

        directory = tempfile.TemporaryDirectory(prefix="cli parity ")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        runner_temp = root / "runner temp"
        runner_temp.mkdir()
        podman = root / "podman"
        podman.write_text(PODMAN_STUB)
        podman.chmod(0o755)
        github_env = root / "github-env"
        github_env.touch()
        log = root / "podman-log"
        image_cli = root / "image-cli"
        env = {
            **os.environ,
            "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_ENV": str(github_env),
            "PODMAN_LOG": str(log),
            "TEST_IMAGE_CLI": str(image_cli),
            "CP_EXIT_CODE": str(cp_exit_code),
            "E2E_IMAGE": "osac-e2e-tests:123",
            "OSAC_VERSION": "",
            "image_key": image_key,
            "tag": "localhost/component-0-pr:123",
        }
        # Do not let a caller's already-extracted binary conceal a skipped block.
        env.pop("OSAC_CLI_BIN", None)
        result = subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, check=False)
        return result, github_env, log, image_cli

    def test_repository_key_extracts_cli_and_forwards_it_to_test_image(self) -> None:
        self._assert_cli_forwarded("service.images.service.repository")

    def test_legacy_key_still_extracts_cli_and_forwards_it_to_test_image(self) -> None:
        self._assert_cli_forwarded("service.images.service")

    def _assert_cli_forwarded(self, image_key: str) -> None:
        for workflow in PIPELINES:
            with self.subTest(workflow=workflow, image_key=image_key):
                result, github_env, log, image_cli = self._run_workflow(workflow, image_key, build_test_image=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                binary = github_env.parent / "runner temp" / "osac-cli-bin"
                self.assertEqual(github_env.read_text(), f"OSAC_CLI_BIN={binary}\n")
                self.assertTrue(os.access(binary, os.X_OK))
                self.assertEqual(image_cli.read_bytes(), binary.read_bytes())
                commands = log.read_text().splitlines()
                self.assertEqual(commands[0], "create localhost/component-0-pr:123")
                self.assertIn(f"cp test-container:/usr/local/bin/osac {binary}", commands)
                self.assertEqual(commands[-1], "rm test-container")
                self.assertFalse((github_env.parent / "osac-cli-bin").exists())

    def test_other_component_keys_do_not_extract_cli(self) -> None:
        for workflow in PIPELINES:
            for image_key in (
                "operator.image.repository",
                "service.images.service.tag",
                "service.images.service.repository.extra",
            ):
                with self.subTest(workflow=workflow, image_key=image_key):
                    result, github_env, log, _ = self._run_workflow(workflow, image_key)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(github_env.read_text(), "")
                    self.assertFalse(log.exists())

    def test_extraction_failure_aborts_before_test_image_build(self) -> None:
        for workflow in PIPELINES:
            for image_key in ("service.images.service", "service.images.service.repository"):
                with self.subTest(workflow=workflow, image_key=image_key):
                    result, github_env, log, image_cli = self._run_workflow(
                        workflow, image_key, build_test_image=True, cp_exit_code=42
                    )
                    self.assertEqual(result.returncode, 42, result.stderr)
                    self.assertEqual(github_env.read_text(), "")
                    self.assertFalse(image_cli.exists())
                    self.assertEqual(log.read_text().splitlines()[-1], "rm test-container")


class FlavorRegistryTests(unittest.TestCase):
    def test_real_registry_commands_with_fake_dependencies(self) -> None:
        fake_digest = "sha256:" + "a" * 64
        fake_canaries = (
            "FAKE_TEST_PASSWORD\nAuthorization: Bearer FAKE_TEST_TOKEN\nCookie: FAKE_TEST_COOKIE\n"
            "::error::FAKE_TEST_COMMAND\nZmFrZTp0ZXN0"
        )
        cases = (
            (0, fake_digest, "", "", "valid"),
            (42, "", "unauthorized", "registry-auth", "valid"),
            (43, "", "manifest unknown", "image-not-found-or-hidden", "valid"),
            (44, "", "status code: 404 (Not Found); unauthorized", "image-not-found-or-hidden", "valid"),
            (45, "", "open auth.json: permission denied", "local-permission", "valid"),
            (46, "", "sudo: a password is required", "sudo-policy-or-execution", "valid"),
            (47, "", "x509: certificate signed by unknown authority", "tls", "valid"),
            (48, "", "connection refused", "network", "valid"),
            (49, "", "denied; FAKE_TEST_404", "unclassified", "valid"),
            (50, "", "invalid username/password", "registry-auth", "valid"),
            (51, "", "invalid username or password", "registry-auth", "valid"),
            (52, "", "denied: requested access to the resource is denied", "registry-access", "valid"),
            (53, "", "denied: access forbidden", "registry-access", "valid"),
            (54, "", "status code: 401", "registry-auth", "valid"),
            (55, "", "status code: 403 (Forbidden)", "registry-access", "valid"),
            (56, "", "status code: 404; status code: 403", "image-not-found-or-hidden", "valid"),
            (126, "", "", "command-not-executable", "valid"),
            (127, "", "", "command-not-found", "valid"),
            (0, fake_canaries, "", "invalid-digest-output", "valid"),
            (0, fake_digest, "", "missing-authfile", "missing"),
            (0, fake_digest, "", "malformed-or-empty-authfile", "malformed"),
            (0, fake_digest, "", "authfile-unreadable", "unreadable"),
        )
        for workflow in (PIPELINES[0], PIPELINES[-1]):
            document = yaml.safe_load((WORKFLOWS / workflow).read_text())
            prepare = next(
                step["run"]
                for step in document["jobs"]["e2e"]["steps"]
                if step.get("name") == "Prepare cluster environment"
            )
            functions = prepare[prepare.index("set +x") : prepare.index("DIGEST_FILE=")]
            probe = prepare[prepare.index("REMOTE_DIGEST=$(") : prepare.index("exec 9>")]
            pull = next(line.strip() for line in prepare.splitlines() if "registry_command pull" in line)
            self.assertNotIn("REGISTRY_AUTH_FILE", pull)
            for status, stdout, stderr, evidence, auth in cases:
                with (
                    self.subTest(workflow=workflow, status=status, evidence=evidence),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    authfile = root / ".config/containers/auth.json"
                    authfile.parent.mkdir(parents=True)
                    fake_auth = '{"auths":{"quay.io/osac-project":{"auth":"ZmFrZTp0ZXN0"}}}'
                    if auth != "missing":
                        authfile.write_text("{FAKE_TEST_INVALID" if auth == "malformed" else fake_auth)
                        if auth == "unreadable":
                            authfile.chmod(0)
                    (root / "fake-test-not-executable").write_text("not executable")
                    for executable in ("skopeo", "sudo"):
                        stub = root / executable
                        stub.write_text(
                            '#!/bin/bash\nprintf "%s\\n" "$*" > "$FAKE_ARGS"\n'
                            'if [[ "$FAKE_STATUS" == 126 ]]; then exec "$HOME/fake-test-not-executable"; fi\n'
                            'if [[ "$FAKE_STATUS" == 127 ]]; then exec "$HOME/fake-test-missing-command"; fi\n'
                            'printf "%s" "$FAKE_STDOUT"\nprintf "%s" "$FAKE_STDERR" >&2\nexit "$FAKE_STATUS"\n'
                        )
                        stub.chmod(0o700)
                    env = {
                        **os.environ,
                        "HOME": temporary,
                        "TMPDIR": temporary,
                        "PATH": temporary + os.pathsep + os.environ["PATH"],
                        "FLAVOR_IMAGE": "quay.io/osac-project/cluster-flavors:fake-test",
                        "FAKE_STATUS": str(status),
                        "FAKE_STDOUT": stdout,
                        "FAKE_STDERR": stderr + "\n" + fake_canaries,
                        "FAKE_ARGS": str(root / "args"),
                    }
                    result = subprocess.run(
                        ["bash", "-xeuc", functions + probe + 'printf "%s" "$REMOTE_DIGEST"'],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, fake_digest if not evidence else "")
                    self.assertIn(evidence, result.stderr)
                    if status and auth == "valid":
                        self.assertIn(f"status={status}", result.stderr)
                    if status == 44:
                        self.assertIn("registry-auth", result.stderr)
                    if status == 56:
                        self.assertIn("registry-access", result.stderr)
                    if auth == "valid":
                        self.assertIn(f"--authfile {authfile}", (root / "args").read_text())
                    for canary in ("FAKE_TEST", "Authorization:", "Cookie:", "::error::", "ZmFrZTp0ZXN0"):
                        self.assertNotIn(canary, result.stdout + result.stderr)
                    result = subprocess.run(["bash", "-euc", functions + pull], env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, status)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(
                        "consumer=sudo-cluster-tool auth=consumer-default server-and-env=unverified", result.stderr
                    )
                    self.assertEqual(
                        (root / "args").read_text().strip(),
                        "python3 /usr/local/bin/cluster-tool pull " + env["FLAVOR_IMAGE"],
                    )
                    self.assertNotIn("FAKE_TEST", result.stderr)
                    self.assertNotIn("ZmFrZTp0ZXN0", result.stderr)
                    if status:
                        self.assertIn(f"status={status}", result.stderr)
                        self.assertIn(evidence, result.stderr)
                    self.assertEqual(list(root.glob("tmp.*")), [])
                    if auth != "missing":
                        self.assertTrue(authfile.exists())

    def test_real_secret_merge_and_existing_root_copy(self) -> None:
        action = yaml.safe_load((WORKFLOWS.parent / "actions/fetch-and-write-secrets/action.yml").read_text())
        script = next(step["run"] for step in action["runs"]["steps"] if step.get("name") == "Write secrets to disk")
        for scenario in (
            "valid",
            "malformed",
            "missing",
            "missing-user",
            "missing-password",
            "unset-user",
            "unset-password",
            "policy",
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sudo = root / "sudo"
                sudo.write_text(
                    '#!/bin/bash\nprintf "%s\\n" "$*" >> "$HOME/sudo-calls"\n'
                    'if [[ "$FAKE_POLICY" == policy ]]; then exit 77; fi\n'
                    'if [[ "$1" == cp ]]; then cp "$2" "$HOME/fake-root-auth.json"; fi\n'
                )
                sudo.chmod(0o700)
                fake_pull = {
                    "auths": {
                        "quay.io/osac-project/cluster-flavors": {"auth": "FAKE_TEST_REPO"},
                        "quay.io": {"auth": "FAKE_TEST_OLD", "identitytoken": "FAKE_TEST_PRESERVED"},
                    }
                }
                fake_snapshot = {"auths": {"quay.io": {"auth": "FAKE_TEST_NEW"}}}
                env = {
                    **os.environ,
                    "HOME": temporary,
                    "RUNNER_TEMP": temporary,
                    "PATH": temporary + os.pathsep + os.environ["PATH"],
                    "PULL_SECRET": {"malformed": "{FAKE_TEST_INVALID", "missing": ""}.get(
                        scenario, json.dumps(fake_pull)
                    ),
                    "SNAPSHOT_REGISTRY_AUTH": json.dumps(fake_snapshot),
                    "QUAY_USER": "" if scenario == "missing-user" else "fake-test-user",
                    "QUAY_PASSWORD": "" if scenario == "missing-password" else "fake-test-password",
                    "AAP_LICENSE": "",
                    "FAKE_POLICY": scenario,
                }
                if scenario in ("unset-user", "unset-password"):
                    env.pop("QUAY_USER" if scenario == "unset-user" else "QUAY_PASSWORD")
                result = subprocess.run(["bash", "-xeuc", script], env=env, capture_output=True, text=True)
                self.assertNotIn("FAKE_TEST", result.stdout + result.stderr)
                self.assertNotIn("fake-test-password", result.stdout + result.stderr)
                if scenario == "policy":
                    self.assertEqual(result.returncode, 77)
                    self.assertFalse((root / "fake-root-auth.json").exists())
                elif scenario != "valid":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("invalid-auth-input", result.stderr)
                    self.assertFalse((root / "sudo-calls").exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    merged = json.loads((root / "fake-root-auth.json").read_text())["auths"]
                    self.assertEqual(merged["quay.io"]["auth"], "FAKE_TEST_NEW")
                    self.assertEqual(merged["quay.io"]["identitytoken"], "FAKE_TEST_PRESERVED")
                    self.assertEqual(merged["quay.io/osac-project/cluster-flavors"]["auth"], "FAKE_TEST_REPO")
                    self.assertIn("quay.io/osac-project", merged)
                    self.assertEqual(
                        (root / "containers-auth-base.json").read_bytes(), (root / "fake-root-auth.json").read_bytes()
                    )
                    self.assertEqual((root / ".config/containers/auth.json").stat().st_mode & 0o777, 0o600)
                    self.assertEqual((root / "pull-secret.json").read_text(), env["PULL_SECRET"])


if __name__ == "__main__":
    unittest.main()

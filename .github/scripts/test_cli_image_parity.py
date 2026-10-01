#!/usr/bin/env python3
"""Exercise workflow CLI extraction and test-image forwarding without a cluster.

Run: uv run python .github/scripts/test_cli_image_parity.py
These are infrastructure unit tests, not the E2E suites owned by the monorepo.
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()

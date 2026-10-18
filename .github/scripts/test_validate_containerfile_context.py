#!/usr/bin/env python3
"""Regression tests for component Containerfile build-context validation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
VALIDATOR = SCRIPTS / "validate_containerfile_context.py"
WORKFLOWS = SCRIPTS.parent / "workflows"
PIPELINES = (
    "e2e-caas-full-install.yml",
    "e2e-bmaas-full-install.yml",
    "e2e-bmaas-netris-full-install.yml",
    "e2e-vmaas-full-install.yml",
    "e2e-vmaas-netris-full-install.yml",
    "e2e-stretched-netris-full-install.yml",
    "e2e-full-regression.yml",
)


class ContainerfileContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="container context ")
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        (self.repo / "go.work").write_text("go 1.26\n")
        adapters = self.repo / "osac-metering" / "adapters"
        adapters.mkdir(parents=True)
        (adapters / "main.go").write_text("package main\n")
        (adapters / "Containerfile.echo-adapter").write_text(
            "FROM example.invalid/builder AS builder\n"
            "COPY --chown=1001:0 go.work ./\n"
            "COPY osac-metering/adapters/main.go ./\n"
            "FROM scratch\n"
            "COPY --from=builder /output /output\n"
        )
        assets = self.repo / "assets"
        assets.mkdir()
        (assets / "one.txt").write_text("one\n")

    def run_validator(self, context: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                "-I",
                str(VALIDATOR),
                "--repo-root",
                str(self.repo),
                "--containerfile",
                "osac-metering/adapters/Containerfile.echo-adapter",
                "--context",
                context,
                "--label",
                "components[6]",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_rejects_echo_adapter_subdirectory_context(self) -> None:
        result = self.run_validator("osac-metering")

        self.assertEqual(result.returncode, 1)
        self.assertIn("components[6]", result.stderr)
        self.assertIn("source 'go.work'", result.stderr)
        self.assertIn("missing from build context 'osac-metering'", result.stderr)
        self.assertIn("use context '.'", result.stderr)

    def test_accepts_echo_adapter_repository_root_context(self) -> None:
        result = self.run_validator(".")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Validated components[6]", result.stdout)

    def test_accepts_remote_stage_options_json_tabs_and_globs(self) -> None:
        containerfile = self.repo / "Containerfile.semantics"
        containerfile.write_text(
            "FROM scratch AS generated\n"
            "ADD --checksum=sha256:deadbeef https://example.invalid/archive.tar /archive.tar\n"
            "ADD git@example.invalid:team/repository.git /repository\n"
            "COPY --from=generated /generated /generated\n"
            "COPY --chown=1000:1000 --chmod=0644 assets/*.txt /assets/\n"
            'COPY ["assets/one.txt", "/one.txt"]\n'
            "COPY\tassets/one.txt /tabbed.txt\n"
        )

        result = subprocess.run(
            [
                "python3",
                "-I",
                str(VALIDATOR),
                "--repo-root",
                str(self.repo),
                "--containerfile",
                containerfile.name,
                "--context",
                ".",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_glob_matching_hidden_file(self) -> None:
        (self.repo / ".settings.cfg").write_text("setting=true\n")
        containerfile = self.repo / "Containerfile.hidden-glob"
        containerfile.write_text("FROM scratch\nCOPY *.cfg /settings/\n")

        result = subprocess.run(
            [
                "python3",
                "-I",
                str(VALIDATOR),
                "--repo-root",
                str(self.repo),
                "--containerfile",
                containerfile.name,
                "--context",
                ".",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflows_validate_every_context_before_first_image_build(self) -> None:
        invocation = "python3 .github/scripts/validate_containerfile_context.py"
        loop = "for i in $(seq 0 $((COUNT - 1))); do"
        for workflow in PIPELINES:
            with self.subTest(workflow=workflow):
                content = (WORKFLOWS / workflow).read_text()
                step = content.split("- name: Build and load component images", 1)[1]
                self.assertIn(invocation, step)
                validation_loop = step.index(loop)
                validator = step.index(invocation, validation_loop)
                validation_loop_end = step.index("\n          done", validator)
                build_loop = step.index(loop, validation_loop_end)
                self.assertLess(validation_loop, validator)
                self.assertLess(validator, validation_loop_end)
                self.assertLess(validation_loop_end, build_loop)
                self.assertLess(build_loop, step.index("podman build"))
                self.assertIn('sha="${COMPONENT_SHA[$i]}"', step)


if __name__ == "__main__":
    unittest.main()

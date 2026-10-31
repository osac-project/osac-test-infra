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

    def run_validator(
        self, context: str, containerfile: str = "osac-metering/adapters/Containerfile.echo-adapter"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                "-I",
                str(VALIDATOR),
                "--repo-root",
                str(self.repo),
                "--containerfile",
                containerfile,
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

    def test_accepts_remote_heredoc_stage_options_json_tabs_and_globs(self) -> None:
        containerfile = self.repo / "Containerfile.semantics"
        containerfile.write_text(
            "FROM scratch AS generated\n"
            "ADD --checksum=sha256:deadbeef https://example.invalid/archive.tar /archive.tar\n"
            "ADD http://example.invalid/archive.tar /second-archive.tar\n"
            "COPY --from=generated /generated /generated\n"
            "COPY --chown=1000:1000 --chmod=0644 assets/*.txt /assets/\n"
            'COPY ["assets/one.txt", "/one.txt"]\n'
            "COPY\tassets/one.txt /tabbed.txt\n"
            "COPY <<EOF /inline.txt\ninline\nEOF\n"
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

    def test_validates_local_add_source_containing_url_delimiter(self) -> None:
        containerfile = self.repo / "Containerfile.local-url-delimiter"
        containerfile.write_text("FROM scratch\nADD assets/data://archive /archive\n")
        local_source = self.repo / "assets" / "data:" / "archive"
        local_source.parent.mkdir()
        local_source.write_text("archive\n")

        existing = self.run_validator(".", containerfile.name)
        local_source.unlink()
        missing = self.run_validator(".", containerfile.name)

        self.assertEqual(existing.returncode, 0, existing.stderr)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("source 'assets/data://archive'", missing.stderr)

    def test_validates_add_sources_not_supported_as_remote_by_buildah(self) -> None:
        containerfile = self.repo / "Containerfile.non-http-remote"
        containerfile.write_text("FROM scratch\nADD git@example.invalid:team/repository.git /repository\n")

        result = self.run_validator(".", containerfile.name)

        self.assertEqual(result.returncode, 1)
        self.assertIn("source 'git@example.invalid:team/repository.git'", result.stderr)

    def test_resolves_parent_component_before_checking_source(self) -> None:
        containerfile = self.repo / "Containerfile.parent-component"
        containerfile.write_text("FROM scratch\nCOPY dir/../file /file\n")
        (self.repo / "file").write_text("root file\n")

        result = self.run_validator(".", containerfile.name)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_does_not_match_component_removed_by_parent_traversal(self) -> None:
        containerfile = self.repo / "Containerfile.parent-component-missing"
        containerfile.write_text("FROM scratch\nCOPY dir/../missing /missing\n")
        misleading = self.repo / "dir" / "missing"
        misleading.parent.mkdir()
        misleading.write_text("wrong file\n")

        result = self.run_validator(".", containerfile.name)

        self.assertEqual(result.returncode, 1)
        self.assertIn("source 'dir/../missing'", result.stderr)

    def test_strips_leading_slash_and_clamps_parent_traversal_to_context(self) -> None:
        containerfile = self.repo / "Containerfile.context-root"
        containerfile.write_text(
            "FROM scratch\n"
            "COPY /leading.txt /leading.txt\n"
            "COPY ../../clamped.txt /clamped.txt\n"
            "COPY dir/../../../mixed.txt /mixed.txt\n"
        )
        for filename in ("leading.txt", "clamped.txt", "mixed.txt"):
            (self.repo / filename).write_text(f"{filename}\n")

        result = self.run_validator(".", containerfile.name)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_parent_normalization_is_lexical_across_symlink(self) -> None:
        containerfile = self.repo / "Containerfile.symlink-parent"
        containerfile.write_text("FROM scratch\nCOPY link/../file /file\n")
        (self.repo / "file").write_text("context root\n")
        (self.repo / "link").symlink_to(self.repo.parent, target_is_directory=True)

        result = self.run_validator(".", containerfile.name)

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

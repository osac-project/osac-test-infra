#!/usr/bin/env python3
"""Synthetic SOS fixtures through the actual collection/upload action scripts."""

import io
import json
import os
import pathlib
import subprocess
import tarfile
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github/scripts/hypervisor-sosreport.sh"
DESTINATION = "osac-project/osac-ci-artifacts"


class SosTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="fixture_sos_")
        self.addCleanup(self.temporary.cleanup)
        self.temp = pathlib.Path(self.temporary.name)
        self.candidate = self.temp / "osac-hypervisor-sosreport"
        self.private = self.temp / "osac-private-upload"
        self.artifacts = self.temp / "osac-artifacts"
        self.artifacts.mkdir()
        (self.artifacts / "fixture_ordinary.txt").write_text("ordinary output\n")
        (self.artifacts / ".redaction-complete").touch()
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        self.env = os.environ | {
            "PATH": f"{self.bin}:{os.defpath}",
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_WORKSPACE": str(REPO),
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_JOB": "fixture_job",
            "GITHUB_STEP_SUMMARY": str(self.temp / "fixture_public_summary"),
            "PRIVATE_REPO": DESTINATION,
            "GH_DISPATCH_TOKEN": "fixture_transport_only",
        }
        self.stub("curl", 'while [[ $1 != -o ]]; do shift; done; cp "$RUNNER_TEMP/fixture_metadata" "$2"; printf 200')
        self.metadata()
        self.env["KNOWN_SECRET_ENV"] = "FIXTURE_SECRET"
        self.env["FIXTURE_SECRET"] = "fixture_rejected_secret_value"

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/bash\nset -eu\n{body}\n")
        path.chmod(0o755)

    def metadata(self, private: bool | None = True, name: str = DESTINATION) -> None:
        (self.temp / "fixture_metadata").write_text(json.dumps({"private": private, "full_name": name}))

    def candidate_tree(self, text: str = "masked fixture diagnostic\n") -> pathlib.Path:
        self.candidate.mkdir(exist_ok=True)
        tree = self.candidate / "report"
        tree.mkdir(exist_ok=True)
        (tree / "fixture_diagnostic.txt").write_text(text)
        (self.candidate / "binding").write_text(f"{DESTINATION}:123:1:fixture_job\n")
        return tree

    def run_script(self, mode: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), mode], env=self.env, capture_output=True, timeout=15)

    def test_clean_tree_ordinary_copy_and_retry(self) -> None:
        self.candidate_tree()
        result = self.run_script("prepare")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.private / "hypervisor-sosreport/fixture_diagnostic.txt").is_file())
        self.assertTrue((self.private / "fixture_ordinary.txt").is_file())
        self.assertFalse((self.artifacts / "hypervisor-sosreport").exists())
        self.assertFalse(self.candidate.exists())
        self.run_script("prepare")
        self.assertFalse((self.private / "hypervisor-sosreport").exists())

    def test_public_unknown_credentials_and_mismatch_withhold(self) -> None:
        for variable, value in [
            ("PRIVATE_REPO", ""),
            ("PRIVATE_REPO", "fixture/public"),
            ("GH_DISPATCH_TOKEN", ""),
            ("GITHUB_RUN_ATTEMPT", "2"),
        ]:
            with self.subTest(variable=variable):
                saved = self.env[variable]
                self.env[variable] = value
                self.candidate_tree()
                self.run_script("prepare")
                self.assertFalse((self.private / "hypervisor-sosreport").exists())
                self.assertFalse(self.candidate.exists())
                self.env[variable] = saved

    def test_metadata_public_unknown_and_error(self) -> None:
        for private, name in [(False, DESTINATION), (None, DESTINATION), (True, "fixture/wrong")]:
            self.metadata(private, name)
            self.candidate_tree()
            self.run_script("prepare")
            self.assertFalse((self.private / "hypervisor-sosreport").exists())
        self.stub("curl", "exit 22")
        self.candidate_tree()
        self.run_script("prepare")
        self.assertFalse((self.private / "hypervisor-sosreport").exists())

    def test_secret_nested_binary_symlink_and_fifo_withheld(self) -> None:
        for kind in ("secret", "archive", "binary", "symlink", "fifo", "map", "logs"):
            with self.subTest(kind=kind):
                tree = self.candidate_tree()
                path = tree / "fixture_sensitive"
                if kind == "secret":
                    path.write_text("password=fixture_rejected_secret_value\n")
                elif kind == "archive":
                    with tarfile.open(path, "w:gz") as archive:
                        item = tarfile.TarInfo("../fixture_escape")
                        item.size = 3
                        archive.addfile(item, io.BytesIO(b"bad"))
                elif kind == "binary":
                    path.write_bytes(b"\x00\xff\x00\xff")
                elif kind == "symlink":
                    path.symlink_to(self.artifacts)
                elif kind == "fifo":
                    os.mkfifo(path)
                elif kind == "map":
                    (tree / "fixture-private_map").write_text("fixture_mapping")
                else:
                    (tree / "sos_logs").mkdir()
                result = self.run_script("prepare")
                self.assertFalse((self.private / "hypervisor-sosreport").exists(), kind)
                self.assertEqual(result.stdout, b"")
                summary = self.temp / "fixture_public_summary"
                self.assertFalse(summary.exists(), "SOS paths must not enter public summaries")

    def test_collect_has_no_public_output_or_fallback(self) -> None:
        self.stub("sudo", 'printf "fixture_raw_failure\\n" >&2; exit 1')
        action = yaml.safe_load((REPO / ".github/actions/collect-artifacts/action.yml").read_text())
        result = subprocess.run(["bash", "-c", action["runs"]["steps"][0]["run"]], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(b"fixture_raw_failure", result.stdout + result.stderr)
        self.assertFalse(self.candidate.exists())
        self.assertFalse((self.artifacts / "hypervisor-sosreport").exists())

    def test_cancel_and_timeout_withhold_without_changing_ordinary(self) -> None:
        self.candidate_tree()
        self.stub("curl", 'kill -TERM "$PPID"; exit 1')
        self.assertNotEqual(self.run_script("prepare").returncode, 0)
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.private.exists())
        self.assertTrue((self.artifacts / "fixture_ordinary.txt").exists())
        self.stub("sudo", "exit 124")
        self.assertNotEqual(self.run_script("collect").returncode, 0)
        self.assertFalse(self.candidate.exists())

    def test_fixed_root_command_and_before_gather(self) -> None:
        helper = (REPO / "scripts/osac-hypervisor-sosreport").read_text()
        for flags in [
            "--build --clean --batch --no-report --no-env-vars",
            "--log-size=25 --journal-size=25",
            "--only-plugins=networking,firewall_tables,kernel,libvirt,podman,block,lvm2,nvme,systemd,system,rpm",
            "--no-update",
            "--config-file=",
            "--map-file=",
            "--tmp-dir=",
        ]:
            self.assertIn(flags, helper)
        self.assertNotIn("systemd-run", helper)
        action = yaml.safe_load((REPO / ".github/actions/collect-artifacts/action.yml").read_text())
        self.assertIn(" collect ", action["runs"]["steps"][0]["run"])
        self.assertEqual(action["runs"]["steps"][1]["name"], "Gather and redact")

    def test_actual_launcher_completion_parser(self) -> None:
        helper = (REPO / "scripts/osac-hypervisor-sosreport").read_text()
        parser = helper[helper.index("awk '/^Successfully") : helper.index('rm -rf -- "$tree/sos_logs"')]
        tree = self.temp / "fixture_tree"
        (tree / "sos_logs").mkdir(parents=True)
        for name in ("sos.log", "ui.log"):
            (tree / "sos_logs" / name).touch()
        completion = (
            f"Successfully obfuscated 1 report(s)\nYour sos report build tree has been generated in:\n\t{tree}\n"
        )
        for output, accepted in [
            (completion, True),
            ("ERROR: kernel command failed\n" + completion, True),
            (completion + "ERROR: Unable to obfuscate report: fixture_failure\n", False),
            ("fixture_raw_report\n", False),
            (completion.replace("Successfully", "Never"), False),
        ]:
            (self.temp / "command.log").write_text(output)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", parser],
                env=self.env | {"scratch": str(self.temp)},
                capture_output=True,
            )
            self.assertEqual(result.returncode == 0, accepted, output)
        (self.temp / "command.log").write_text(completion)
        (tree / "sos_logs/sos.log").write_text("[cleaner] Could not clean fixture_file\n")
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", parser], env=self.env | {"scratch": str(self.temp)}, capture_output=True
        )
        self.assertNotEqual(result.returncode, 0)

    def stage(self) -> pathlib.Path | None:
        action = yaml.safe_load((REPO / ".github/actions/publish-private-artifacts/action.yml").read_text())
        environment = self.env | {
            "SOURCE_DIR_INPUT": str(self.private),
            "ARTIFACT_NAME": "fixture_artifacts",
            "HOST_LABEL": "fixture_host",
            "OSAC_ARTIFACT_STAGING_ROOT": str(self.temp / "fixture_staging"),
            "GITHUB_OUTPUT": str(self.temp / "fixture_outputs"),
        }
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", action["runs"]["steps"][0]["run"]], env=environment, capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.temp / "publish-private-artifacts.json"
        return pathlib.Path(json.loads(state.read_text())["dir"]) if state.exists() else None

    def test_actual_private_handoff_and_optional_failure(self) -> None:
        self.candidate_tree()
        self.assertEqual(self.run_script("prepare").returncode, 0)
        staged = self.stage()
        self.assertTrue((staged / "hypervisor-sosreport/fixture_diagnostic.txt").is_file())
        self.assertFalse(list(staged.rglob("*.tar*")))
        self.assertFalse((staged / "sos_logs").exists())
        self.assertFalse((self.artifacts / "hypervisor-sosreport").exists())
        self.candidate_tree("fixture_rejected_secret_value")
        self.assertNotEqual(self.run_script("prepare").returncode, 0)
        staged = self.stage()
        self.assertTrue((staged / "fixture_ordinary.txt").is_file())
        self.assertFalse((staged / "hypervisor-sosreport").exists())

    def test_scanner_failure_cannot_fall_back_to_unscanned_ordinary(self) -> None:
        self.candidate_tree()
        self.assertEqual(self.run_script("prepare").returncode, 0)
        self.stub("docker", "exit 1")
        action = yaml.safe_load((REPO / ".github/actions/upload-artifacts/action.yml").read_text())
        step = next(item for item in action["runs"]["steps"] if item["name"] == "Scan artifacts with gitleaks")
        command = step["run"].replace("${{ github.workspace }}", str(REPO))
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", command],
            capture_output=True,
            env=self.env
            | {
                "ARTIFACT_DIR": str(self.artifacts),
                "GITLEAKS_CONFIG": str(REPO / ".github/scripts/.gitleaks.toml"),
                "GITLEAKS_IMAGE": "fixture_no_real_image",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(self.stage())
        self.assertFalse((self.artifacts / "hypervisor-sosreport").exists())


if __name__ == "__main__":
    unittest.main()

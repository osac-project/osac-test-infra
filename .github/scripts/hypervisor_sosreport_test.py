#!/usr/bin/env python3
"""Synthetic, unprivileged checks of the host collector and private handoff."""

import contextlib
import gzip
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HOST = load("fixture_host", REPO / "scripts/osac-hypervisor-sosreport")
HANDOFF = load("fixture_handoff", REPO / ".github/scripts/hypervisor-sosreport.py")


class HostTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture_sos_")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.report = self.root / "sosreport-fixture-obfuscated"
        self.report.mkdir()
        (self.report / "fixture.txt").write_text("fixture diagnostic\n")
        self.output = (
            f"Successfully obfuscated 1 report(s)\nYour sos report build tree has been generated in:\n\t{self.report}\n"
        ).encode()

    def test_selects_only_positive_cleaner_and_final_build_output(self):
        self.assertEqual(HOST.selected_tree(self.root, self.output), self.report)
        for output in (
            b"",
            self.output.replace(b"Successfully obfuscated 1 report(s)", b"fixture no completion"),
            self.output + b"ERROR: Unable to obfuscate report: fixture failure\n",
            self.output + b"Could not write private map file: fixture failure\n",
            self.output.replace(b"Successfully obfuscated 1 report(s)\n", b"")
            + b"Successfully obfuscated 1 report(s)\n",
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                HOST.selected_tree(self.root, output)

    def test_rejects_symlink_and_outside_output(self):
        link = self.root / "fixture_link"
        link.symlink_to(self.report, target_is_directory=True)
        for path in (link, self.root.parent):
            with self.subTest(path=path), self.assertRaises(ValueError):
                HOST.selected_tree(self.root, self.output.replace(str(self.report).encode(), str(path).encode()))

    def test_required_options_and_no_automatic_upload(self):
        command = HOST.report_command(self.root, "/usr/sbin/sos")
        for option in ("--no-env-vars", "--cleaner", "--no-update", "--batch", "--build"):
            self.assertIn(option, command)
        self.assertFalse(any(argument.startswith("--upload") for argument in command))
        self.assertIn(f"--config-file={self.root}/sos.conf", command)

    def test_log_and_mapping_not_in_transport(self):
        (self.report / "sos_logs").mkdir()
        (self.report / "sos_logs/sos.log").write_text("fixture raw log")
        (self.report / "fixture-private_map").write_text("fixture mapping")
        (self.report / "fixture_link").symlink_to("fixture.txt")
        stream = io.BytesIO()
        HOST.pack_tree(self.report, stream)
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            self.assertEqual(archive.getnames(), ["fixture.txt"])

    def test_captured_command_failures_deadlines_and_output_limit(self):
        for command, timeout, limit, exception in (
            ([sys.executable, "-c", "raise SystemExit(2)"], 1, 100, ValueError),
            ([sys.executable, "-c", "import time; time.sleep(10)"], 0.02, 100, TimeoutError),
            ([sys.executable, "-c", "print('fixture_' * 100)"], 1, 10, ValueError),
        ):
            with self.subTest(command=command), self.assertRaises(exception):
                HOST.capture(command, timeout=timeout, limit=limit)

    def test_helper_and_sudo_are_fixed_and_root_owned_on_install(self):
        helper = (REPO / "scripts/osac-hypervisor-sosreport").read_text()
        setup = (REPO / "scripts/machine-init.sh").read_text()
        self.assertIn('/usr/local/sbin/osac-hypervisor-sosreport ""', setup)
        self.assertIn('install -m 0755 "${SCRIPT_DIR}/${helper}"', setup)
        for setting in ("RuntimeMaxSec=140", "MemoryMax=1G", "size=384M", "KillMode=control-group"):
            self.assertIn(setting, helper)

    def test_missing_required_flag_or_unsupported_version_withholds(self):
        for version, help_output in (("4.9.0", b""), ("4.10.0", b"--batch --build")):
            with self.subTest(version=version), mock.patch.dict(sys.modules, {"sos": mock.Mock(__version__=version)}):
                with mock.patch.object(HOST.tempfile, "TemporaryDirectory") as temporary:
                    temporary.return_value.__enter__.return_value = str(self.root)
                    with mock.patch.object(HOST, "capture", return_value=help_output) as capture:
                        with (
                            mock.patch.object(HOST.pathlib.Path, "is_file", return_value=True),
                            self.assertRaises(ValueError),
                        ):
                            HOST.worker()
                        self.assertLessEqual(capture.call_count, 1)

    def test_supported_worker_has_a_real_positive_path_without_host_commands(self):
        help_output = b"--no-env-vars --cleaner --no-update --build --batch --map-file"
        with (
            mock.patch.dict(sys.modules, {"sos": mock.Mock(__version__="4.10.0")}),
            mock.patch.object(HOST.tempfile, "TemporaryDirectory") as temporary,
            mock.patch.object(HOST, "capture", side_effect=[help_output, self.output]) as capture,
        ):
            temporary.return_value.__enter__.return_value = str(self.root)
            with (
                mock.patch.object(HOST.pathlib.Path, "is_file", return_value=True),
                mock.patch.object(HOST.sys, "stdout") as output,
            ):
                HOST.worker()
                self.assertTrue(output.buffer.write.called)
            self.assertEqual(capture.call_count, 2)


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture_handoff_")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "RUNNER_TEMP": str(self.root),
                "GITHUB_RUN_ID": "fixture_42",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_JOB": "fixture_job",
                "PRIVATE_REPO": HANDOFF.DESTINATION,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        staging = mock.patch.object(HANDOFF, "STAGING_ROOT", self.root)
        staging.start()
        self.addCleanup(staging.stop)

    def fixture_archive(self, name="fixture.txt", kind=tarfile.REGTYPE, data=b"fixture diagnostics"):
        path = self.root / "fixture.tar"
        with tarfile.open(path, "w") as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/fixture_outside"
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(member, io.BytesIO(data) if member.size else None)
        return path

    def test_destination_binding_includes_attempt_and_job(self):
        binding = HANDOFF.binding()
        self.assertTrue(HANDOFF.matches(binding))
        for field in binding:
            wrong = dict(binding, **{field: "fixture_wrong"})
            self.assertFalse(HANDOFF.matches(wrong), field)
        for destination in ("", "fixture/public", "osac-project/other"):
            with mock.patch.dict(os.environ, {"PRIVATE_REPO": destination}):
                self.assertFalse(HANDOFF.matches(binding))

    def test_metadata_must_be_authenticated_private_and_exact(self):
        for metadata in ({}, {"private": False}, {"private": True, "full_name": "fixture/wrong"}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                HANDOFF.check_metadata(metadata, HANDOFF.DESTINATION)
        HANDOFF.check_metadata({"private": True, "full_name": HANDOFF.DESTINATION}, HANDOFF.DESTINATION)
        with mock.patch.dict(os.environ, {"GH_DISPATCH_TOKEN": ""}), self.assertRaises(ValueError):
            HANDOFF.verify_destination()

    def test_transport_rejects_links_traversal_duplicates_and_large_members(self):
        for name, kind in (
            ("../fixture_escape", tarfile.REGTYPE),
            ("/fixture_absolute", tarfile.REGTYPE),
            ("fixture_link", tarfile.SYMTYPE),
            ("fixture_hard", tarfile.LNKTYPE),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as output:
                with self.assertRaises(ValueError):
                    HANDOFF.unpack(self.fixture_archive(name, kind), pathlib.Path(output))
        with tempfile.TemporaryDirectory(dir=self.root) as output:
            HANDOFF.unpack(self.fixture_archive(), pathlib.Path(output))
            self.assertTrue((pathlib.Path(output) / "fixture.txt").exists())
        with mock.patch.object(HANDOFF, "MAX_MEMBER", 1), tempfile.TemporaryDirectory(dir=self.root) as output:
            with self.assertRaises(ValueError):
                HANDOFF.unpack(self.fixture_archive(), pathlib.Path(output))
        archive = self.fixture_archive()
        with tarfile.open(archive, "a") as transport:
            member = tarfile.TarInfo("fixture.txt")
            transport.addfile(member, io.BytesIO())
        with tempfile.TemporaryDirectory(dir=self.root) as output, self.assertRaises(ValueError):
            HANDOFF.unpack(archive, pathlib.Path(output))

    def seed_candidate(self, data=b"fixture diagnostics"):
        candidate = HANDOFF.candidate()
        candidate.mkdir(mode=0o700)
        (candidate / "report.tar").write_bytes(self.fixture_archive(data=data).read_bytes())
        (candidate / "binding.json").write_text(json.dumps(HANDOFF.binding()))
        (candidate / "k8s-secrets.json").write_text('{"items": []}')
        stage = self.root / "fixture_private_stage"
        stage.mkdir(mode=0o700)
        (stage / "fixture_ordinary.txt").write_text("fixture ordinary artifact")
        (self.root / "publish-private-artifacts.json").write_text(json.dumps({"dir": str(stage)}))
        return stage

    def fixture_scanner(self):
        scanner = self.root / "fixture_scanner"
        scanner.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "if sys.argv[1] == 'rm': raise SystemExit(0)\n"
            "print('fixture_private_scanner_log')\n"
            "mode = os.environ.get('FIXTURE_SCANNER_MODE', 'clean')\n"
            "if mode == 'failure': raise SystemExit(2)\n"
            "output = next(arg.split(':/out:')[0] for arg in sys.argv if ':/out:' in arg)\n"
            "if mode != 'missing':\n"
            " findings = [] if mode == 'clean' else [{'RuleID': 'fixture'}]\n"
            " pathlib.Path(output, 'gitleaks.json').write_text(json.dumps(findings))\n"
            "raise SystemExit(1 if mode == 'finding' else 0)\n"
        )
        scanner.chmod(0o700)
        return str(scanner)

    def test_real_gate_and_stub_scanner_publish_only_into_private_stage(self):
        stage = self.seed_candidate()
        with (
            mock.patch.object(HANDOFF, "verify_destination"),
            mock.patch.object(HANDOFF.shutil, "which", return_value=self.fixture_scanner()),
            contextlib.redirect_stdout(io.StringIO()) as log,
        ):
            HANDOFF.publish()
        self.assertEqual(log.getvalue(), "")
        archive = stage / "hypervisor-sosreport.tar.gz"
        self.assertTrue(archive.is_file())
        with tarfile.open(archive) as report:
            self.assertEqual(report.extractfile("hypervisor-sosreport/fixture.txt").read(), b"fixture diagnostics")
        self.assertFalse(HANDOFF.candidate().exists())
        self.assertFalse((self.root / "osac-artifacts").exists())

    def test_generic_artifact_directory_cannot_be_used_as_private_staging(self):
        stage = self.seed_candidate()
        with (
            mock.patch.object(HANDOFF, "STAGING_ROOT", self.root / "fixture_expected_private"),
            mock.patch.object(HANDOFF, "verify_destination"),
            mock.patch.object(HANDOFF, "scan") as scan,
            mock.patch.object(sys, "argv", ["fixture", "publish"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(HANDOFF.main(), 1)
        scan.assert_not_called()
        self.assertFalse(HANDOFF.candidate().exists())
        self.assertFalse((stage / "hypervisor-sosreport.tar.gz").exists())

    def test_missing_cluster_artifacts_still_use_private_publisher_staging(self):
        for mode in ("clean", "failure"):
            with self.subTest(mode=mode):
                old_stage = self.seed_candidate()
                shutil.rmtree(old_stage)
                staging = self.root / "fixture_staging"
                action = yaml.safe_load((REPO / ".github/actions/publish-private-artifacts/action.yml").read_text())
                step = next(step for step in action["runs"]["steps"] if step.get("id") == "stage")
                outputs = self.root / "fixture_stage_outputs"
                result = subprocess.run(
                    ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                    env=os.environ
                    | {
                        "GITHUB_WORKSPACE": str(REPO),
                        "SOURCE_DIR_INPUT": "",
                        "ARTIFACT_NAME": "fixture_host",
                        "HOST_LABEL": "fixture_host",
                        "GITHUB_OUTPUT": str(outputs),
                        "OSAC_ARTIFACT_STAGING_ROOT": str(staging),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("staged=true", outputs.read_text())
                state = json.loads((self.root / "publish-private-artifacts.json").read_text())
                stage = pathlib.Path(state["dir"])
                self.assertEqual(list(stage.iterdir()), [])
                with (
                    mock.patch.object(HANDOFF, "STAGING_ROOT", staging),
                    mock.patch.object(HANDOFF, "verify_destination"),
                    mock.patch.object(HANDOFF.shutil, "which", return_value=self.fixture_scanner()),
                    mock.patch.dict(os.environ, {"FIXTURE_SCANNER_MODE": mode}),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(HANDOFF.publish(), mode == "clean")
                self.assertEqual((stage / "hypervisor-sosreport.tar.gz").exists(), mode == "clean")
                self.assertFalse((self.root / "osac-artifacts").exists())
                self.assertFalse(HANDOFF.candidate().exists())
                shutil.rmtree(staging)

    def test_actual_publisher_bash_step_consumes_verified_candidate(self):
        stage = self.seed_candidate()
        workspace = self.root / "fixture_workspace"
        wrapper = workspace / ".github/scripts/hypervisor-sosreport.py"
        wrapper.parent.mkdir(parents=True)
        metadata = json.dumps({"private": True, "full_name": HANDOFF.DESTINATION}).encode()
        wrapper.write_text(
            "import json, pathlib, runpy\nfrom unittest import mock\n"
            f"module = runpy.run_path({str(REPO / '.github/scripts/hypervisor-sosreport.py')!r})\n"
            f"module['publish'].__globals__['STAGING_ROOT'] = pathlib.Path({str(self.root)!r})\n"
            "response = mock.MagicMock()\n"
            f"response.__enter__.return_value.read.return_value = {metadata!r}\n"
            "with mock.patch('urllib.request.build_opener') as opener:\n"
            " opener.return_value.open.return_value = response\n"
            " raise SystemExit(module['main']())\n"
        )
        binaries = self.root / "fixture_bin"
        binaries.mkdir()
        (binaries / "docker").symlink_to(self.fixture_scanner())
        action = yaml.safe_load((REPO / ".github/actions/publish-private-artifacts/action.yml").read_text())
        step = next(step for step in action["runs"]["steps"] if step["name"].startswith("Verify private visibility"))
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            env=os.environ
            | {
                "GITHUB_WORKSPACE": str(workspace),
                "GITHUB_OUTPUT": str(self.root / "fixture_outputs"),
                "GH_DISPATCH_TOKEN": "fixture_token",
                "PATH": f"{binaries}:{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue((stage / "hypervisor-sosreport.tar.gz").is_file())

    def test_scanner_findings_failure_and_missing_result_withhold(self):
        for mode in ("failure", "finding", "missing"):
            with self.subTest(mode=mode):
                stage = self.seed_candidate()
                with (
                    mock.patch.dict(os.environ, {"FIXTURE_SCANNER_MODE": mode}),
                    mock.patch.object(HANDOFF, "verify_destination"),
                    mock.patch.object(HANDOFF.shutil, "which", return_value=self.fixture_scanner()),
                    contextlib.redirect_stdout(io.StringIO()) as log,
                ):
                    HANDOFF.publish()
                self.assertNotIn("fixture_private_scanner_log", log.getvalue())
                self.assertFalse((stage / "hypervisor-sosreport.tar.gz").exists())
                self.assertTrue((stage / "fixture_ordinary.txt").exists())
                self.assertFalse(HANDOFF.candidate().exists())
                (stage / "fixture_ordinary.txt").unlink()
                stage.rmdir()

    def test_nested_archive_and_known_secret_withhold_whole_report(self):
        for data in (
            gzip.compress(gzip.compress(b"fixture nested")),
            self.fixture_archive().read_bytes(),
            b"fixture_sensitive_value_1029384756",
        ):
            with self.subTest(data=data):
                stage = self.seed_candidate(data)
                secret = self.root / "fixture_secret.json"
                secret.write_text(json.dumps({"password": "fixture_sensitive_value_1029384756"}))
                with (
                    mock.patch.dict(os.environ, {"KNOWN_SECRET_FILES": str(secret)}),
                    mock.patch.object(HANDOFF, "verify_destination"),
                    mock.patch.object(HANDOFF.shutil, "which") as scanner,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    HANDOFF.publish()
                scanner.assert_not_called()
                self.assertFalse((stage / "hypervisor-sosreport.tar.gz").exists())
                (stage / "fixture_ordinary.txt").unlink()
                stage.rmdir()

    def test_metadata_error_and_false_metadata_prevent_dispatch(self):
        for metadata in ({"private": False, "full_name": HANDOFF.DESTINATION}, None):
            with self.subTest(metadata=metadata):
                stage = self.seed_candidate()
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(metadata).encode()
                with (
                    mock.patch.dict(os.environ, {"GH_DISPATCH_TOKEN": "fixture_token"}),
                    mock.patch.object(HANDOFF.urllib.request, "build_opener") as opener,
                    mock.patch.object(sys, "argv", ["fixture", "publish"]),
                    contextlib.redirect_stdout(io.StringIO()) as log,
                ):
                    opener.return_value.open.return_value = response
                    self.assertEqual(HANDOFF.main(), 1)
                self.assertNotIn("fixture_token", log.getvalue())
                self.assertFalse(HANDOFF.candidate().exists())
                self.assertFalse((stage / "hypervisor-sosreport.tar.gz").exists())
                (stage / "fixture_ordinary.txt").unlink()
                stage.rmdir()

    def test_destination_mismatch_or_missing_auth_withholds_without_scanning(self):
        for destination in ("", "fixture/public", "osac-project/other"):
            with self.subTest(destination=destination):
                stage = self.seed_candidate()
                with (
                    mock.patch.dict(os.environ, {"PRIVATE_REPO": destination}),
                    mock.patch.object(HANDOFF, "verify_destination"),
                    mock.patch.object(HANDOFF, "scan") as scan,
                ):
                    HANDOFF.publish()
                scan.assert_not_called()
                self.assertFalse((stage / "hypervisor-sosreport.tar.gz").exists())
                self.assertFalse(HANDOFF.candidate().exists())
                (stage / "fixture_ordinary.txt").unlink()
                stage.rmdir()
        self.seed_candidate()
        with (
            mock.patch.dict(os.environ, {"GH_DISPATCH_TOKEN": ""}),
            mock.patch.object(sys, "argv", ["fixture", "publish"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(HANDOFF.main(), 1)
        self.assertFalse(HANDOFF.candidate().exists())

    def test_metadata_network_error_never_uses_a_public_fallback(self):
        stage = self.seed_candidate()
        with (
            mock.patch.dict(os.environ, {"GH_DISPATCH_TOKEN": "fixture_token"}),
            mock.patch.object(HANDOFF.urllib.request, "build_opener") as opener,
            mock.patch.object(sys, "argv", ["fixture", "publish"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            opener.return_value.open.side_effect = TimeoutError("fixture metadata timeout")
            self.assertEqual(HANDOFF.main(), 1)
        self.assertFalse(HANDOFF.candidate().exists())
        self.assertFalse((stage / "hypervisor-sosreport.tar.gz").exists())

    def test_retry_reuses_completed_candidate_and_stale_attempt_is_replaced(self):
        self.seed_candidate()
        with mock.patch.object(HANDOFF, "capture") as capture:
            HANDOFF.collect()
        capture.assert_not_called()
        with (
            mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}),
            mock.patch.object(HANDOFF, "capture", side_effect=TimeoutError("fixture timeout")),
            self.assertRaises(TimeoutError),
        ):
            HANDOFF.collect()
        self.assertFalse(HANDOFF.candidate().exists())

    def test_collection_nonzero_or_cancellation_removes_candidate(self):
        for failure in (ValueError("fixture nonzero"), InterruptedError("fixture cancellation")):
            with self.subTest(failure=failure), mock.patch.object(HANDOFF, "capture", side_effect=failure):
                with self.assertRaises(type(failure)):
                    HANDOFF.collect()
                self.assertFalse(HANDOFF.candidate().exists())
        with (
            mock.patch.object(sys, "argv", ["fixture", "collect"]),
            mock.patch.object(HANDOFF, "capture", side_effect=ValueError("fixture original test failed")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(HANDOFF.main(), 0)
            self.assertNotIn("fixture original test failed", output.getvalue())

    def test_publisher_visibility_precedes_dispatch_and_cleanup_always_runs(self):
        action = yaml.safe_load((REPO / ".github/actions/publish-private-artifacts/action.yml").read_text())
        steps = action["runs"]["steps"]
        names = [step["name"] for step in steps]
        self.assertLess(
            names.index("Verify private visibility and prepare host report"),
            names.index("Dispatch the collect workflow"),
        )
        dispatch = next(step for step in steps if step["name"] == "Dispatch the collect workflow")
        self.assertIn("steps.private-gate.outputs.verified == 'true'", dispatch["if"])
        upload = yaml.safe_load((REPO / ".github/actions/upload-artifacts/action.yml").read_text())
        self.assertEqual(upload["runs"]["steps"][-1]["if"], "always()")
        collector = yaml.safe_load((REPO / ".github/actions/collect-artifacts/action.yml").read_text())
        self.assertNotIn("KUBECONFIG", collector["runs"]["steps"][0].get("env", {}))

    def test_public_collection_clears_stale_candidate_without_root(self):
        candidate = HANDOFF.candidate()
        candidate.mkdir()
        (candidate / "fixture_stale").write_text("fixture old report")
        with mock.patch.dict(os.environ, {"PRIVATE_REPO": ""}), mock.patch.object(HANDOFF, "capture") as capture:
            HANDOFF.collect()
        capture.assert_not_called()
        self.assertFalse(candidate.exists())

    def test_workflow_collection_precedes_teardown_and_has_same_route(self):
        for suite in ("vmaas", "bmaas", "caas", "full-regression"):
            filename = f"e2e-{suite}-full-install.yml" if suite != "full-regression" else "e2e-full-regression.yml"
            workflow = yaml.safe_load((REPO / ".github/workflows" / filename).read_text())
            steps = next(
                job["steps"]
                for job in workflow["jobs"].values()
                if any(step.get("uses", "").endswith("/collect-artifacts") for step in job.get("steps", []))
            )
            teardown = next(index for index, step in enumerate(steps) if "teardown.sh" in step.get("run", ""))
            collections = [
                (index, step) for index, step in enumerate(steps) if step.get("uses", "").endswith("/collect-artifacts")
            ]
            uploads = [step for step in steps if step.get("uses", "").endswith("/upload-artifacts")]
            for index, step in collections:
                self.assertLess(index, teardown)
                self.assertEqual(step["with"]["private-repo"], uploads[0]["with"]["private-repo"])
            self.assertEqual(steps[teardown]["if"], "always()")


if __name__ == "__main__":
    unittest.main(verbosity=2)

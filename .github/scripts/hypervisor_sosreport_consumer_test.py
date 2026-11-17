#!/usr/bin/env python3
"""Synthetic private payloads through real pinned containers and postrun Bash."""

import base64
import contextlib
import hashlib
import io
import json
import os
import pathlib
import runpy
import shutil
import unittest
import zipfile
from unittest import mock

import yaml

SCRIPTS = pathlib.Path(__file__).resolve().parent
FEATURE = runpy.run_path(str(SCRIPTS / "hypervisor_sosreport_test.py"))
PRIVATE = runpy.run_path(str(SCRIPTS / "scan_run_logs_private_test.py"))
HOST = FEATURE["HOST"]
HANDOFF = FEATURE["HANDOFF"]
FIXTURE_CANARY = "gh" + "p_" + hashlib.sha256(b"fixture_synthetic_sos_canary").hexdigest()[:36]


class PrivateConsumerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = FEATURE["HandoffTest"]()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.stage = self.fixture.seed_candidate()
        self.tree = self.fixture.root / "fixture_cleaned_report"
        self.tree.mkdir()
        for name, data in {
            "sos_commands/libvirt/virsh_-r_list_--all": "fixture masked domain\n",
            "sos_commands/systemd/journalctl_--no-pager": "fixture service could not start\n" * 24576,
            "sos_commands/networking/ip_-d_address": "fixture obfuscated host 10.0.0.1\n",
            "etc/os-release": "NAME=fixture_synthetic_os\n",
            "proc/meminfo": "MemTotal: 1048576 kB\n",
            "sos_reports/sos.html": "<html>fixture masked diagnostic report</html>\n",
        }.items():
            path = self.tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)

    def publish(self, canary=False):
        if canary:
            value = (
                "api_key=" + hashlib.sha256(b"fixture_synthetic_generic_api_canary").hexdigest()
                if canary == "scanner"
                else "token=" + FIXTURE_CANARY
            )
            (self.tree / "fixture_unscrubbed").write_text(value)
        with (HANDOFF.candidate() / "report.tar").open("wb") as stream:
            HOST.pack_tree(self.tree, stream)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"private": True, "full_name": HANDOFF.DESTINATION}
        ).encode()
        with (
            mock.patch.dict(os.environ, {"GH_DISPATCH_TOKEN": "fixture_metadata_token"}),
            mock.patch.object(HANDOFF.urllib.request, "build_opener") as opener,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            opener.return_value.open.return_value = response
            self.assertTrue(HANDOFF.publish())
        self.assertNotIn(FIXTURE_CANARY, output.getvalue())
        self.assertFalse(HANDOFF.candidate().exists())

    def payload_zip(self, inject=False):
        report_tree = self.stage / "hypervisor-sosreport"
        if inject:
            (report_tree / "fixture_unscrubbed").write_text("token=" + FIXTURE_CANARY)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as transport:
            for path in self.stage.rglob("*"):
                if path.is_file():
                    transport.write(path, path.relative_to(self.stage))
        return base64.b64encode(buffer.getvalue()).decode()

    def scan_payload(self, root, payload, injected):
        fixture = PRIVATE["ScanPrivateArtifactsTest"]()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        (fixture.bin / "engine").unlink()
        (fixture.bin / "engine").symlink_to(shutil.which("podman") or shutil.which("docker"))
        (fixture.bin / "sleep").unlink()
        fixture.routes[f"GET /repos/{PRIVATE['PRIVATE']}/actions/artifacts/77/zip"]["zip_b64"] = payload
        fixture.run_script.__func__.__globals__["SCRIPT"] = root / ".github/scripts/scan-run-logs.sh"
        result, status = fixture.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(status["SCAN_OK"], "true")
        self.assertEqual(status["LEAKS_FOUND"], str(injected).lower())
        self.assertEqual(status["PURGE_OK"], "true")
        self.assertEqual(bool([call for call in fixture.calls() if call["method"] == "DELETE"]), injected)
        self.assertFalse((fixture.output / "redacted").exists())
        self.assertNotIn(FIXTURE_CANARY, (fixture.output / "findings.json").read_text())
        evidence = os.environ.get("SOS_CONSUMER_EVIDENCE")
        if evidence:
            with pathlib.Path(evidence).open("a") as output:
                output.write(json.dumps({"root": str(root), "injected": injected, "status": status}) + "\n")

    def test_real_producer_withholds_unscrubbed_canary_without_losing_ordinary_artifacts(self):
        self.publish(canary=True)
        self.assertEqual([path.name for path in self.stage.iterdir()], ["fixture_ordinary.txt"])

    def test_real_gitleaks_withholds_generic_canary_after_release_gate(self):
        self.publish(canary="scanner")
        self.assertEqual([path.name for path in self.stage.iterdir()], ["fixture_ordinary.txt"])

    def test_generated_payload_is_inspectable_by_trusted_main_and_postmerge_consumers(self):
        self.publish()
        upload = yaml.safe_load((SCRIPTS.parents[1] / ".github/actions/upload-artifacts/action.yml").read_text())
        public = next(
            step for step in upload["runs"]["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")
        )
        public_path = public["with"]["path"].splitlines()[0].replace("${{ runner.temp }}", str(self.fixture.root))
        ordinary = pathlib.Path(public_path) / "fixture_public_ordinary.txt"
        ordinary.parent.mkdir()
        ordinary.write_text("fixture ordinary public artifact")
        with mock.patch.dict(os.environ, {"PRIVATE_REPO": ""}):
            HANDOFF.collect()
        self.assertEqual([path.name for path in pathlib.Path(public_path).rglob("*")], [ordinary.name])
        self.assertTrue((self.stage / "hypervisor-sosreport").is_dir())
        roots = [SCRIPTS.parents[1]]
        if os.environ.get("SOS_TRUSTED_MAIN"):
            roots.insert(0, pathlib.Path(os.environ["SOS_TRUSTED_MAIN"]))
        for root in roots:
            with self.subTest(root=root, injected=False):
                self.scan_payload(root, self.payload_zip(), False)
        for root in roots:
            with self.subTest(root=root, injected=True):
                self.scan_payload(root, self.payload_zip(inject=True), True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

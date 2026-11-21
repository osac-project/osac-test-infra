#!/usr/bin/env python3
"""Synthetic tests of native collection through the shared artifact boundary."""

from __future__ import annotations

import json
import pathlib
import runpy
import subprocess
import sys
import time
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github/workflows"
FAMILIES = (
    "vmaas",
    "vmaas-full-install",
    "caas-full-install",
    "bmaas-full-install",
    "full-regression",
    "vmaas-netris-full-install",
    "bmaas-netris-full-install",
    "stretched-netris-full-install",
    "caas-agentless-net-full-install",
)
SENTINEL = "synthetic-Credential-739520-testing-only"
IMAGE = "quay-io-openshift-release-dev-ocp-v4-0-art-dev-sha256-native"
UPLOAD = runpy.run_path(str(REPO / ".github/scripts/upload_artifacts_test.py"))


class CollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.consumer = UPLOAD["UploadArtifactsTest"]()
        self.consumer.setUp()
        self.addCleanup(self.consumer.doCleanups)
        self.temp = self.consumer.temp
        self.tree = self.consumer.artifacts / "mgmt-cluster/must-gather"
        self.config = self.temp / "kubeconfig"
        self.config.touch()
        binaries = self.temp / "bin"
        (binaries / "oc-data").write_text(f"""#!{sys.executable}
import base64, gzip, json, os, pathlib, sys, time
arguments = sys.argv[1:]
credential = {SENTINEL!r}
if arguments[:2] == ['adm', 'must-gather']:
    pathlib.Path(os.environ['NATIVE_ARGUMENTS']).write_text(json.dumps(arguments))
    root = pathlib.Path(next(value.split('=', 1)[1] for value in arguments if value.startswith('--dest-dir=')))
    target = root / {IMAGE!r}
    target.mkdir()
    (target / 'version').write_text('openshift/must-gather\\n')
    service = target / 'host_service_logs/masters/kubelet_service.log'
    service.parent.mkdir(parents=True)
    service.write_text('service history\\n' + json.dumps({{'password': credential}}) + '\\n')
    resource = target / 'namespaces/osac-e2e-ci/pods/native-node/resource.yaml'
    resource.parent.mkdir(parents=True)
    resource.write_text('credential: ' + credential + '\\n')
    encoded = base64.b64encode(gzip.compress(credential.encode(), mtime=0)).decode()
    (target / 'encoded-archive.log').write_text('error native-archive-control:\\n' +
        '\\n'.join(encoded[index:index + 31] for index in range(0, len(encoded), 31)) + '\\n')
    print(json.dumps({{'token': credential}}), flush=True)
    mode = os.environ.get('NATIVE_MODE', 'success')
    if mode == 'oversize':
        with (target / 'large.log').open('wb') as handle:
            handle.truncate(128 * 1024 * 1024 + 1)
    if mode == 'hang':
        time.sleep(90)
    sys.exit(1 if mode == 'failed' else 0)
if arguments[:2] == ['get', 'secrets'] and arguments[-2:] == ['-o', 'json']:
    print(json.dumps({{'items': [{{'data': {{'credential': base64.b64encode(credential.encode()).decode()}}}}]}}))
    sys.exit(0)
if '-o' in arguments and arguments[arguments.index('-o') + 1] == 'json':
    print('{{"items": []}}')
    sys.exit(0)
sys.exit(1)
""")
        (binaries / "oc-data").chmod(0o755)
        (binaries / "oc").write_text("""#!/bin/bash
case "$*" in
  "adm must-gather"*|*" -o json") exec "$0-data" "$@" ;;
  *) exit 1 ;;
esac
""")
        (binaries / "oc").chmod(0o755)
        (binaries / "timeout").write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$TIMEOUT_ARGUMENTS"
if [[ "$1" == "-k" ]]; then shift 2; fi
seconds="$1"
shift
if [[ "$1" == "oc" && "${2:-}" == "adm" ]]; then seconds=0.4; fi
if [[ "$1" == */gather-osac-logs.sh ]]; then seconds=30; fi
exec /usr/bin/timeout -k 0.2 "$seconds" "$@"
""")
        (binaries / "timeout").chmod(0o755)
        self.environment = self.consumer.environment | {
            "GITHUB_WORKSPACE": str(REPO),
            "KUBECONFIG": str(self.config),
            "COLLECT_TIMEOUT_SECONDS": "80",
            "NATIVE_ARGUMENTS": str(self.temp / "native-arguments"),
            "TIMEOUT_ARGUMENTS": str(self.temp / "timeout-arguments"),
            "OSAC_E2E_OUTPUT_DIR": str(self.temp / "absent"),
        }
        self.consumer.environment = self.environment

    def collect(self, mode: str = "success") -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(REPO / ".github/scripts/collect-artifacts.sh")],
            env=self.environment | {"NATIVE_MODE": mode},
            capture_output=True,
            text=True,
            timeout=40,
        )

    def test_real_collection_redaction_scan_and_release_preserve_native_context(self) -> None:
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        self.assertTrue((self.consumer.artifacts / ".redaction-complete").exists())
        self.assertNotIn(SENTINEL, (self.tree / "command.log").read_text())
        self.assertNotIn("native-archive-control", (self.consumer.artifacts / "failure-summary.txt").read_text())
        service = self.tree / IMAGE / "host_service_logs/masters/kubelet_service.log"
        self.assertIn("REDACTED", service.read_text())
        resource = self.tree / IMAGE / "namespaces/osac-e2e-ci/pods/native-node/resource.yaml"
        self.assertIn(SENTINEL, resource.read_text())
        inventory = self.consumer.inventory
        self.assertEqual(inventory.stat().st_mode & 0o777, 0o600)
        scanner_control = "synthetic-scanner-only-928385-testing"
        scanned = self.tree / IMAGE / "scanner-control.log"
        scanned.write_text(scanner_control)
        self.consumer.scanner("redact-once")
        for step in ("Verify redaction complete", "Scan artifacts with gitleaks", "Release gate - censor secrets"):
            result = self.consumer.run_step(step)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        self.assertNotIn(SENTINEL, resource.read_text())
        self.assertEqual(scanned.read_text(), "[REDACTED]")
        self.assertFalse((self.tree / IMAGE / "encoded-archive.log").exists())
        self.assertEqual((self.tree / IMAGE / "version").read_text(), "openshift/must-gather\n")
        self.assertFalse(inventory.exists())
        arguments = json.loads((self.temp / "native-arguments").read_text())
        self.assertEqual(len(arguments), 4)
        self.assertTrue(arguments[-1].startswith("--timeout="))
        seconds = int(arguments[-1].removeprefix("--timeout=").removesuffix("s"))
        self.assertGreater(seconds, 0)
        self.assertLess(seconds, 20)

    def test_failure_timeout_and_recollection_keep_only_downloaded_redacted_data(self) -> None:
        for mode in ("failed", "hang"):
            with self.subTest(mode=mode):
                self.tree.mkdir(parents=True, exist_ok=True)
                (self.tree / "stale.log").write_text("previous collection")
                result = self.collect(mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("did not complete", result.stdout)
                self.assertFalse((self.tree / "stale.log").exists())
                self.assertTrue((self.consumer.artifacts / ".redaction-complete").exists())
                self.assertNotIn(SENTINEL, (self.tree / "command.log").read_text())

    def test_oversize_withholds_must_gather_not_other_diagnostics(self) -> None:
        result = self.collect("oversize")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("exceeds 128 MiB", result.stdout)
        self.assertFalse(self.tree.exists())
        self.assertTrue((self.consumer.artifacts / "failure-summary.txt").exists())
        self.assertTrue((self.consumer.artifacts / ".redaction-complete").exists())

    def test_missing_cluster_and_failed_redaction_invalidate_stale_marker(self) -> None:
        self.config.unlink()
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.consumer.artifacts / ".redaction-complete").exists())
        self.config.touch()
        (self.consumer.artifacts / ".redaction-complete").touch()
        python = self.temp / "bin/python3"
        python.write_text("#!/bin/bash\nexit 1\n")
        python.chmod(0o755)
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.consumer.artifacts / ".redaction-complete").exists())
        self.assertEqual(self.consumer.run_step("Verify redaction complete").returncode, 1)

    def test_interrupted_wrapper_never_renews_marker(self) -> None:
        with subprocess.Popen(
            ["bash", str(REPO / ".github/scripts/collect-artifacts.sh")],
            env=self.environment | {"NATIVE_MODE": "hang"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            deadline = time.monotonic() + 5
            while not (self.temp / "native-arguments").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.temp / "native-arguments").exists())
            process.terminate()
            process.communicate(timeout=40)
        self.assertFalse((self.consumer.artifacts / ".redaction-complete").exists())

    def test_ancestor_links_are_refused_without_modifying_target(self) -> None:
        outside = self.temp / "outside"
        outside.mkdir()
        management = self.consumer.artifacts / "mgmt-cluster"
        management.symlink_to(outside, target_is_directory=True)
        result = self.collect()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_compact_boot_without_cluster_drops_previous_must_gather(self) -> None:
        self.config.unlink()
        compact = self.consumer.artifacts / "compact-boot"
        compact.mkdir()
        (compact / ".collection-complete").touch()
        self.tree.mkdir(parents=True)
        (self.tree / "stale.log").write_text(SENTINEL)
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.consumer.artifacts / ".redaction-complete").exists())
        self.assertFalse(self.tree.exists())


class WorkflowTests(unittest.TestCase):
    def test_all_nine_workflow_families_collect_before_teardown_and_upload_safely(self) -> None:
        action = yaml.safe_load((REPO / ".github/actions/collect-artifacts/action.yml").read_text())
        self.assertEqual(action["inputs"]["timeout-seconds"]["default"], "900")
        for family in FAMILIES:
            with self.subTest(family=family):
                workflow = yaml.safe_load((WORKFLOWS / f"e2e-{family}.yml").read_text())
                jobs = workflow["jobs"]
                job = next(
                    value
                    for value in jobs.values()
                    if any(step.get("uses") == "./.github/actions/collect-artifacts" for step in value.get("steps", []))
                )
                steps = job["steps"]
                collections = [
                    index
                    for index, step in enumerate(steps)
                    if step.get("uses") == "./.github/actions/collect-artifacts"
                ]
                uploads = [
                    index
                    for index, step in enumerate(steps)
                    if step.get("uses") == "./.github/actions/upload-artifacts"
                ]
                self.assertEqual(len(collections), 2)
                self.assertEqual(len(uploads), 2)
                self.assertIn("!cancelled()", steps[collections[0]]["if"])
                self.assertIn("cancelled()", steps[collections[1]]["if"])
                for cancelled in (False, True):
                    for outcome in ("success", "failure", "skipped", "cancelled"):
                        for indexes, identifier in ((collections, "gather"), (uploads, "upload")):
                            for position, index in enumerate(indexes):
                                expression = steps[index]["if"]
                                expression = expression.replace("always()", "True")
                                expression = expression.replace("!cancelled()", repr(not cancelled))
                                expression = expression.replace("cancelled()", repr(cancelled))
                                expression = expression.replace(f"steps.{identifier}.outcome", repr(outcome))
                                expression = expression.replace("&&", " and ").replace("||", " or ")
                                expected = (
                                    not cancelled
                                    if position == 0
                                    else (cancelled and outcome in ("skipped", "cancelled"))
                                )
                                self.assertEqual(eval(expression, {"__builtins__": {}}), expected)
                self.assertEqual(steps[collections[1]]["with"]["timeout-seconds"], "120")
                for index in collections + uploads:
                    self.assertTrue(steps[index].get("continue-on-error", False))
                teardown = next((index for index, step in enumerate(steps) if step["name"] == "Teardown"), None)
                if teardown is not None:
                    self.assertLess(max(collections), teardown)
                    self.assertLess(teardown, min(uploads))
                else:
                    self.assertIn("e2e", jobs["teardown"]["needs"])
                for step in steps[: collections[0]]:
                    if (
                        family.endswith("netris-full-install") or family == "caas-agentless-net-full-install"
                    ) and step.get("name", "").startswith("Gather "):
                        self.assertIn("!cancelled()", step["if"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Synthetic contracts for the real collector, normalizer and workflow consumers."""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
HELPER = REPO / ".github/scripts/collect-must-gather.py"
SENTINEL = "synthetic-Credential-739520-testing-only"
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


class CollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temp = pathlib.Path(self.temporary.name)
        self.artifacts = self.temp / "osac-artifacts"
        self.tree = self.artifacts / "mgmt-cluster/must-gather"
        self.config = self.temp / "kubeconfig"
        self.config.write_text("synthetic kubeconfig\n")
        binaries = self.temp / "bin"
        binaries.mkdir()
        oc = binaries / "oc"
        oc.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, subprocess, sys, time
args = sys.argv[1:]
mode = os.environ.get('TEST_MODE', 'success')
if args[:2] == ['adm', 'must-gather']:
    pathlib.Path(os.environ['TEST_ARGS']).write_text(json.dumps(args))
    dest = pathlib.Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--dest-dir=')))
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'kubelet_service.log').write_text('history\\n{{"password": "{SENTINEL}"}}\\n')
    (dest / 'version').write_text('openshift/must-gather\\n')
    if mode == 'failed':
        sys.exit(7)
    if mode == 'hang':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
        pathlib.Path(os.environ['TEST_CHILD']).write_text(str(child.pid))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(90)
elif '--raw=/readyz' in args:
    sys.exit(1 if mode == 'unavailable' else 0)
elif 'secrets' in args:
    print('{{"items": []}}')
elif args[:2] == ['get', 'namespace']:
    sys.exit(1)
elif '-o' in args and args[args.index('-o') + 1] == 'json':
    print('{{"items": []}}')
""")
        oc.chmod(0o755)
        self.environment = os.environ | {
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(self.temp),
            "GITHUB_WORKSPACE": str(REPO),
            "KUBECONFIG": str(self.config),
            "COLLECT_TIMEOUT_SECONDS": "12",
            "TEST_ARGS": str(self.temp / "args.json"),
            "TEST_CHILD": str(self.temp / "child.pid"),
            "OSAC_E2E_OUTPUT_DIR": str(self.temp / "absent"),
        }

    def collect(self, mode: str = "success") -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(REPO / ".github/scripts/collect-artifacts.sh")],
            env=self.environment | {"TEST_MODE": mode},
            capture_output=True,
            text=True,
            timeout=25,
        )

    def history(self) -> str:
        return next(self.tree.glob("*.log")).read_text()

    def test_shared_collection_redacts_and_marks_last(self) -> None:
        self.environment["OSAC_GATHER_COLLECT_ONLY"] = "true"
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.artifacts / ".redaction-complete").exists())
        self.assertIn("history", self.history())
        self.assertNotIn(SENTINEL, self.history())
        args = json.loads((self.temp / "args.json").read_text())
        self.assertFalse(any(arg.startswith("--image") for arg in args))
        self.assertEqual(json.loads((self.tree / "collection-status.json").read_text())["status"], "complete")

    def test_failed_and_timed_out_collections_keep_safe_partial_output(self) -> None:
        for mode, expected in (("failed", "partial"), ("hang", "timed-out")):
            with self.subTest(mode=mode):
                result = self.collect(mode)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((self.artifacts / ".redaction-complete").exists())
                self.assertNotIn(SENTINEL, self.history())
                self.assertEqual(json.loads((self.tree / "collection-status.json").read_text())["status"], expected)
        child = int((self.temp / "child.pid").read_text())
        state = pathlib.Path(f"/proc/{child}/stat")
        self.assertTrue(not state.exists() or state.read_text().split()[2] == "Z")

    def test_absent_empty_and_unreachable_cluster_are_explicit_skips(self) -> None:
        for mode, config in (("success", ""), ("unavailable", "synthetic")):
            self.config.write_text(config)
            result = self.collect(mode)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((self.tree / "collection-status.json").read_text())["status"], "skipped")
            self.assertTrue((self.artifacts / ".redaction-complete").exists())

    def test_repeated_collection_drops_previous_tree_and_marker(self) -> None:
        self.tree.mkdir(parents=True)
        (self.tree / "stale.txt").write_text(SENTINEL)
        (self.artifacts / ".redaction-complete").touch()
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.tree / "stale.txt").exists())

    def test_redaction_failure_never_renews_a_previous_marker(self) -> None:
        self.artifacts.mkdir()
        (self.artifacts / ".redaction-complete").touch()
        sed = self.temp / "bin/sed"
        sed.write_text("#!/bin/sh\nexit 2\n")
        sed.chmod(0o755)
        result = self.collect()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.artifacts / ".redaction-complete").exists())

    def test_empty_normalized_files_and_plaintext_extensionless_files_are_supported(self) -> None:
        self.tree.mkdir(parents=True)
        (self.tree / "version").write_text("openshift/must-gather\n")
        (self.tree / "empty.log").touch()
        for flag in ("--normalize", "--check"):
            if flag == "--check":
                (self.tree / "collection-status.json").write_text('{"status":"complete","reason":"none"}')
            result = subprocess.run([sys.executable, str(HELPER), str(self.tree), flag], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_collect_action_reaches_the_real_wrapper_with_its_budget_and_kubeconfig(self) -> None:
        action = yaml.safe_load((REPO / ".github/actions/collect-artifacts/action.yml").read_text())
        step = action["runs"]["steps"][0]
        self.assertEqual(step["env"]["KUBECONFIG"], "${{ inputs.kubeconfig-path }}")
        self.assertEqual(step["env"]["COLLECT_TIMEOUT_SECONDS"], "${{ inputs.timeout-seconds }}")
        self.assertEqual(step["run"], 'bash "${GITHUB_WORKSPACE}/.github/scripts/collect-artifacts.sh"')

    def test_interruption_kills_descendants_without_completing_redaction(self) -> None:
        process = subprocess.Popen(
            [sys.executable, str(HELPER), str(self.artifacts), "--timeout-seconds", "60"],
            env=self.environment | {"TEST_MODE": "hang"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 30
            while not (self.temp / "child.pid").exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)
            self.assertTrue((self.temp / "child.pid").exists())
            process.send_signal(signal.SIGTERM)
            self.assertNotEqual(process.wait(timeout=5), 0)
            self.assertFalse((self.artifacts / ".redaction-complete").exists())
            child = int((self.temp / "child.pid").read_text())
            state = pathlib.Path(f"/proc/{child}/stat")
            self.assertTrue(not state.exists() or state.read_text().split()[2] == "Z")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_normalizer_withholds_opaque_and_deceptive_content_without_listing_names(self) -> None:
        self.tree.mkdir(parents=True)
        samples = {
            "version": b"openshift/must-gather\n",
            "timestamp": b"2026-01-01\n",
            "gather.logs": b"plain log\n",
            "error.stderr": b"plain stderr\n",
            "resources.yaml": b"kind: List\n",
            "status.json": b'{"ok": true}',
            f"{SENTINEL}.yaml": gzip.compress(SENTINEL.encode()),
            "nested.log": gzip.compress(gzip.compress(SENTINEL.encode())),
            "binary.json": b"\x00" + SENTINEL.encode(),
            "unsupported.bin": SENTINEL.encode(),
            "tar.txt": b"a" * 257 + b"ustar" + SENTINEL.encode(),
            "invalid.txt": b"\xff" + SENTINEL.encode(),
            "unicode-control.txt": ("\u0085" + SENTINEL).encode(),
            "nonascii.txt": "ordinary Unicode: café\n".encode(),
            "encoded.json": json.dumps(
                {"payload": base64.b64encode(gzip.compress(SENTINEL.encode())).decode()}
            ).encode(),
            "nested-encoded.yaml": b"payload: " + base64.b64encode(base64.b64encode(gzip.compress(SENTINEL.encode()))),
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr(f"../../{SENTINEL}.yaml", gzip.compress(SENTINEL.encode()))
        samples[f"{SENTINEL}.log"] = archive.getvalue()
        for name, content in samples.items():
            (self.tree / name).write_bytes(content)
        (self.tree / "link.log").symlink_to(self.config)
        (self.tree / "linked-directory").symlink_to(self.temp, target_is_directory=True)
        os.link(self.config, self.tree / "hardlink.log")
        os.mkfifo(self.tree / "pipe.log")
        (self.tree / "unreadable.log").write_text(SENTINEL)
        (self.tree / "unreadable.log").chmod(0)
        with (self.tree / "oversized.log").open("wb") as handle:
            handle.truncate(33 * 1024 * 1024)
        result = subprocess.run(
            [sys.executable, str(HELPER), str(self.tree), "--normalize"], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        published = b"".join(path.read_bytes() for path in self.tree.rglob("*") if path.is_file())
        self.assertNotIn(SENTINEL.encode(), published)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        texts = [path.read_text() for path in self.tree.glob("*.txt")]
        self.assertIn("openshift/must-gather\n", texts)
        self.assertIn("plain stderr\n", texts)
        self.assertIn("ordinary Unicode: café\n", texts)
        manifest = (self.tree / "omissions.json").read_text()
        self.assertNotIn(".yaml", manifest)
        self.assertGreater(sum(json.loads(manifest).values()), 0)
        self.assertEqual(len(list(self.tree.rglob("*.json"))), 2)

    def test_names_and_untrusted_metadata_never_survive_normalization(self) -> None:
        nested = self.tree / SENTINEL
        nested.mkdir(parents=True)
        (nested / f"{SENTINEL}.yaml").write_text("kind: List\n")
        (self.tree / "omissions.json").write_text(json.dumps({SENTINEL: SENTINEL}))
        (self.tree / "collection-status.json").write_text(json.dumps({SENTINEL: SENTINEL}))
        result = subprocess.run([sys.executable, str(HELPER), str(self.tree), "--normalize"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SENTINEL, "\n".join(str(path.relative_to(self.tree)) for path in self.tree.rglob("*")))
        self.assertNotIn(SENTINEL.encode(), (self.tree / "omissions.json").read_bytes())
        self.assertNotIn(SENTINEL.encode(), result.stdout + result.stderr)
        self.assertTrue(any(path.read_text() == "kind: List\n" for path in self.tree.rglob("*.yaml")))

    def test_interruption_during_redaction_kills_processes_and_never_marks_complete(self) -> None:
        sed = self.temp / "bin/sed"
        sed.write_text(f"""#!{sys.executable}
import os, pathlib, signal, subprocess, time
child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(90)'])
pathlib.Path(os.environ['TEST_CHILD']).write_text(str(child.pid))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(90)
""")
        sed.chmod(0o755)
        process = subprocess.Popen(
            ["bash", str(REPO / ".github/scripts/collect-artifacts.sh")],
            env=self.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 15
            while not (self.temp / "child.pid").exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)
            self.assertTrue((self.temp / "child.pid").exists())
            process.send_signal(signal.SIGTERM)
            self.assertNotEqual(process.wait(timeout=5), 0)
            self.assertFalse((self.artifacts / ".redaction-complete").exists())
            child = int((self.temp / "child.pid").read_text())
            state = pathlib.Path(f"/proc/{child}/stat")
            self.assertTrue(not state.exists() or state.read_text().split()[2] == "Z")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class WorkflowTests(unittest.TestCase):
    def test_all_nine_workflow_families_collect_before_teardown_and_upload_safely(self) -> None:
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

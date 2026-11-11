#!/usr/bin/env python3
"""Offline registry boundary tests; all credentials and executables are fakes."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/flavor_registry.py"
SPEC = importlib.util.spec_from_file_location("flavor_registry", SCRIPT)
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)
FAKE_SECRET = "fake-only-credential-canary"
FAKE_AUTH = base64.b64encode(f"fake-user:{FAKE_SECRET}".encode()).decode()
DIGEST = "sha256:" + "a" * 64


class FlavorRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = self.directory / "environment"
        self.environment.touch()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "PATH": os.environ["PATH"],
                    "HOME": str(self.directory),
                    "RUNNER_TEMP": str(self.directory),
                    "GITHUB_ENV": str(self.environment),
                    "PULL_SECRET": json.dumps({"auths": {"registry.example": {"auth": FAKE_AUTH}}}),
                    "SNAPSHOT_REGISTRY_AUTH": json.dumps({"auths": {"quay.io": {"auth": FAKE_AUTH}}}),
                    "QUAY_USER": "fake-user",
                    "QUAY_PASSWORD": FAKE_SECRET,
                    "FLAVOR_IMAGE": "quay.io/osac-project/cluster-flavors:test",
                },
                clear=True,
            )
        )

    def initialize(self):
        registry.initialize()
        for line in self.environment.read_text().splitlines():
            name, value = line.split("=", 1)
            os.environ[name] = value
        return Path(os.environ["FLAVOR_REGISTRY_AUTH_FILE"])

    def assert_no_disclosure(self):
        output = self.stdout.getvalue() + self.stderr.getvalue()
        for secret in (FAKE_SECRET, FAKE_AUTH, "Authorization:", "Cookie:", "::error::injected"):
            self.assertNotIn(secret, output)

    def invoke(self, operation):
        with patch.object(sys, "argv", [str(SCRIPT), operation]):
            return registry.main()

    def test_initialize_private_unique_and_preserves_namespace_and_shared_consumers(self):
        shared = self.directory / "shared.json"
        shared.write_text("unchanged")
        os.environ["REGISTRY_AUTH_FILE"] = str(shared)
        first = self.initialize()
        second = self.initialize()
        self.assertNotEqual(first, second)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o700)
        self.assertEqual(shared.read_text(), "unchanged")
        self.assertEqual(os.environ["REGISTRY_AUTH_FILE"], str(shared))
        auths = json.loads(first.read_text())["auths"]
        self.assertEqual(auths["quay.io/osac-project"]["auth"], FAKE_AUTH)
        self.assertEqual(set(auths), {"quay.io", "registry.example", "quay.io/osac-project"})
        self.assert_no_disclosure()

    def test_missing_and_malformed_auth_never_fall_back(self):
        for contents, category in (
            (None, "auth-missing"),
            (FAKE_SECRET, "auth-malformed"),
            ('{"auths": []}', "auth-malformed"),
            ('{"auths": {}}', "auth-missing"),
            ('{"auths": {"quay.io": {"auth": "invalid"}}}', "auth-malformed"),
        ):
            with self.subTest(category=category, contents=contents):
                authfile = self.initialize()
                if contents is None:
                    authfile.unlink()
                else:
                    authfile.write_text(contents)
                with patch.object(registry, "run_captured") as run:
                    self.assertEqual(self.invoke("inspect"), 1)
                    run.assert_not_called()
                self.assertIn(f"category={category}", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_unset_selection_fails_closed(self):
        self.assertEqual(self.invoke("inspect"), 1)
        self.assertIn("auth-missing", self.stderr.getvalue())

    def test_insecure_permissions_and_symlinks_rejected(self):
        authfile = self.initialize()
        authfile.chmod(0o644)
        self.assertEqual(self.invoke("inspect"), 1)
        authfile.chmod(0o600)
        target = authfile.with_name("other.json")
        authfile.rename(target)
        authfile.symlink_to(target)
        self.assertEqual(self.invoke("inspect"), 1)
        self.assertIn("auth-permissions", self.stderr.getvalue())

    def test_init_error_removes_partial_file(self):
        with patch.object(registry.json, "dump", side_effect=OSError(FAKE_SECRET)):
            self.assertEqual(self.invoke("init"), 1)
        self.assertEqual(list(self.directory.glob("flavor-registry-*")), [])
        self.assert_no_disclosure()

    def test_inspect_explicit_selection_and_allowlisted_digest(self):
        authfile = self.initialize()
        with patch.object(registry, "versions"), patch.object(registry, "run_captured") as run:
            run.return_value = subprocess.CompletedProcess([], 0, DIGEST + "\n", FAKE_SECRET)
            self.assertEqual(self.invoke("inspect"), 0)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--authfile") + 1], str(authfile))
            self.assertEqual(run.call_args.kwargs["env"]["REGISTRY_AUTH_FILE"], str(authfile))
            self.assertEqual(self.stdout.getvalue(), DIGEST + "\n")
            run.return_value = subprocess.CompletedProcess([], 0, FAKE_SECRET, "")
            self.assertEqual(self.invoke("inspect"), 1)
        self.assertIn("digest-invalid", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_failures_categorized_without_raw_stdout_or_stderr(self):
        self.initialize()
        for error, category in (
            ("unauthorized", "auth-denied"),
            ("no such host", "network"),
            ("x509", "tls"),
            ("manifest unknown", "image-not-found"),
            ("status code: 503", "registry-unavailable"),
            ("surprise", "unclassified"),
        ):
            with (
                self.subTest(category=category),
                patch.object(registry, "versions"),
                patch.object(registry, "run_captured") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    [],
                    17,
                    FAKE_SECRET,
                    f"{error}\n{FAKE_AUTH}\nAuthorization: Bearer {FAKE_SECRET}\nCookie: {FAKE_SECRET}\n::error::injected",
                )
                self.assertEqual(self.invoke("inspect"), 1)
                self.assertIn(f"exit=17 category={category}", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_versions_only_emit_numeric_allowlist(self):
        for version in (f"skopeo version 1.20.0\n{FAKE_SECRET}", FAKE_SECRET):
            with patch.object(
                registry, "run_captured", return_value=subprocess.CompletedProcess([], 0, version, FAKE_SECRET)
            ):
                registry.versions()
        self.assertIn("skopeo_version=1.20.0", self.stderr.getvalue())
        self.assertIn("skopeo_version=unknown", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_privileged_boundary_and_inner_shell_inherit_selected_file(self):
        authfile = self.initialize()
        fake_skopeo = self.directory / "skopeo"
        fake_skopeo.write_text(
            f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
            "assert sys.argv[1] == 'copy'\n"
            "assert os.environ['REGISTRY_AUTH_FILE'] == os.environ['FLAVOR_REGISTRY_AUTH_FILE']\n"
            "assert pathlib.Path(os.environ['REGISTRY_AUTH_FILE']).is_file()\n"
            f"assert json.loads(pathlib.Path(os.environ['REGISTRY_AUTH_FILE']).read_text())['auths']['quay.io/osac-project']['auth'] == {FAKE_AUTH!r}\n"
            f"print({FAKE_SECRET!r})\n"
        )
        fake_skopeo.chmod(0o700)
        fake_cluster_tool = self.directory / "cluster-tool"
        fake_cluster_tool.write_text(
            "import subprocess, sys\n"
            "assert sys.argv[1:] == ['pull', 'quay.io/osac-project/cluster-flavors:test', '--server', 'local']\n"
            "result = subprocess.run('skopeo copy docker://quay.io/osac-project/cluster-flavors:test dir:unused', "
            "shell=True, capture_output=True, text=True)\n"
            "print(result.stdout)\nprint(result.stderr, file=sys.stderr)\nsys.exit(result.returncode)\n"
        )
        captured = registry.run_captured
        read_bytes = Path.read_bytes

        def source_or_auth(path):
            if str(path) == "/usr/local/bin/cluster-tool":
                return b"fake-cluster-tool-source"
            return read_bytes(path)

        def execute(command, *, env=None):
            self.assertEqual(command[:2], ["python3", "/usr/local/bin/cluster-tool"])
            self.assertEqual(env["REGISTRY_AUTH_FILE"], str(authfile))
            return captured([sys.executable, str(fake_cluster_tool), *command[2:]], env=env)

        boundary_env = {name: os.environ[name] for name in ("FLAVOR_REGISTRY_AUTH_FILE", "FLAVOR_IMAGE")}
        boundary_env["PATH"] = str(self.directory) + os.pathsep + os.environ["PATH"]
        boundary_env["REGISTRY_AUTH_FILE"] = str(self.directory / "wrong-auth.json")
        with (
            patch.dict(os.environ, boundary_env, clear=True),
            patch.object(registry.os, "geteuid", return_value=0),
            patch.object(registry, "versions"),
            patch.object(registry, "run_captured", side_effect=execute),
            patch.object(Path, "read_bytes", source_or_auth),
        ):
            self.assertEqual(self.invoke("pull"), 0)
        self.assertIn("context=local-root", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_pull_requires_root_and_categorizes_failure(self):
        self.initialize()
        with (
            patch.object(registry, "versions"),
            patch.object(registry.os, "geteuid", return_value=0),
            patch.object(Path, "read_bytes", return_value=os.environ["PULL_SECRET"].encode()),
            patch.object(
                registry,
                "run_captured",
                return_value=subprocess.CompletedProcess([], 1, FAKE_SECRET, "unauthorized " + FAKE_AUTH),
            ),
        ):
            self.assertEqual(self.invoke("pull"), 1)
        self.assertIn("operation=pull exit=1 category=auth-denied", self.stderr.getvalue())
        with (
            patch.object(registry, "versions"),
            patch.object(registry, "selected_authfile", return_value="unused"),
            patch.object(registry.os, "geteuid", return_value=12345),
        ):
            self.assertEqual(self.invoke("pull"), 1)
        self.assertIn("privileged-context-required", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_init_rejects_missing_and_malformed_inputs_without_disclosure(self):
        os.environ["QUAY_PASSWORD"] = ""
        self.assertEqual(self.invoke("init"), 1)
        self.assertIn("auth-missing", self.stderr.getvalue())
        os.environ["PULL_SECRET"] = FAKE_SECRET
        self.assertEqual(self.invoke("init"), 1)
        self.assertIn("auth-malformed", self.stderr.getvalue())
        self.assertEqual(list(self.directory.glob("flavor-registry-*")), [])
        self.assert_no_disclosure()

    def test_cleanup_idempotent_and_does_not_remove_shared_auth(self):
        authfile = self.initialize()
        shared = self.directory / "shared.json"
        shared.write_text("unchanged")
        os.environ["REGISTRY_AUTH_FILE"] = str(shared)
        registry.cleanup()
        registry.cleanup()
        self.assertFalse(authfile.parent.exists())
        self.assertTrue(shared.exists())
        os.environ["FLAVOR_REGISTRY_AUTH_FILE"] = str(shared)
        self.assertEqual(self.invoke("cleanup"), 1)
        self.assertTrue(shared.exists())

    def test_workflows_select_both_consumers_and_always_cleanup(self):
        for workflow in ("e2e-caas-full-install.yml", "e2e-full-regression.yml"):
            document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
            steps = next(
                job["steps"]
                for job in document["jobs"].values()
                if any(step.get("name") == "Prepare cluster environment" for step in job.get("steps", []))
            )
            setup = next(step for step in steps if step.get("name") == "Fetch and write secrets")
            self.assertEqual(setup["with"]["flavor-registry-auth"], "true")
            prepare = next(step for step in steps if step.get("name") == "Prepare cluster environment")
            self.assertIn("flavor_registry.py inspect || true", prepare["run"])
            self.assertIn(
                "sudo --preserve-env=FLAVOR_REGISTRY_AUTH_FILE,FLAVOR_IMAGE python3 .github/scripts/flavor_registry.py pull 9>&-",
                prepare["run"],
            )
            cleanup = steps[steps.index(prepare) + 1]
            self.assertEqual(cleanup["if"], "always()")
            self.assertEqual(cleanup["run"], "python3 .github/scripts/flavor_registry.py cleanup")


if __name__ == "__main__":
    unittest.main()

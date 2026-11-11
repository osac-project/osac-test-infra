#!/usr/bin/env python3
"""Offline registry boundary tests; all credentials and executables are fakes."""

import base64
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
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
    def setUp(self) -> None:
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

    def initialize(self) -> Path:
        registry.initialize()
        for line in self.environment.read_text().splitlines():
            name, value = line.split("=", 1)
            os.environ[name] = value
        return Path(os.environ["FLAVOR_REGISTRY_AUTH_FILE"])

    def assert_no_disclosure(self) -> None:
        output = self.stdout.getvalue() + self.stderr.getvalue()
        for secret in (FAKE_SECRET, FAKE_AUTH, "Authorization:", "Cookie:", "::error::injected"):
            self.assertNotIn(secret, output)

    def invoke(self, operation: str) -> int:
        with patch.object(sys, "argv", [str(SCRIPT), operation]):
            return registry.main()

    def test_initialize_private_unique_and_preserves_namespace_and_shared_consumers(self) -> None:
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

    def test_merge_matches_existing_jq_and_repository_scope_wins(self) -> None:
        os.environ["PULL_SECRET"] = json.dumps(
            {
                "auths": {
                    "quay.io/osac-project": {"auth": FAKE_AUTH, "identitytoken": "fake-token"},
                    "quay.io/osac-project/cluster-flavors": {"auth": FAKE_AUTH},
                    "registry.example": {},
                }
            }
        )
        os.environ["SNAPSHOT_REGISTRY_AUTH"] = json.dumps(
            {
                "auths": {
                    "quay.io/osac-project": {"auth": base64.b64encode(b"other:fake").decode()},
                    "registry.example": {"identitytoken": "fake-token"},
                }
            }
        )
        authfile = self.initialize()
        expected = subprocess.run(
            [
                "jq",
                "-n",
                "(env.PULL_SECRET|fromjson).auths * (env.SNAPSHOT_REGISTRY_AUTH|fromjson).auths * "
                '{"quay.io/osac-project": {"auth": ((env.QUAY_USER + ":" + env.QUAY_PASSWORD)|@base64)}}',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(authfile.read_text())["auths"], json.loads(expected.stdout))
        registry.selected_authfile(os.environ["FLAVOR_IMAGE"])
        self.assertIn("authfile_scope=repository", self.stderr.getvalue())
        self.assertNotIn("quay.io", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_valid_container_auth_formats_and_safe_scope_labels(self) -> None:
        auths = registry.auths_from_json(
            json.dumps(
                {
                    "auths": {
                        "registry.example": {},
                        "other.example": {"auth": ""},
                        "quay.io/osac-project": {"auth": FAKE_AUTH + "\r\n", "identitytoken": "fake-token"},
                    }
                }
            )
        )
        self.assertEqual(registry.auth_scope(auths, os.environ["FLAVOR_IMAGE"]), "namespace")
        self.assertEqual(registry.auth_scope({"quay.io": {}}, os.environ["FLAVOR_IMAGE"]), "registry")
        self.assertEqual(
            registry.auth_scope({"https://quay.io/v1/": {}}, os.environ["FLAVOR_IMAGE"]), "legacy-registry"
        )
        self.assertEqual(registry.auth_scope({}, os.environ["FLAVOR_IMAGE"]), "unmatched")

    def test_mixed_and_local_errors_do_not_blame_credentials(self) -> None:
        for error, expected in (
            ("manifest unknown; fallback: unauthorized", "image-not-found-or-auth"),
            ("status code: 404; unauthorized", "image-not-found-or-auth"),
            ("HTTP status: 404 Not Found; unauthorized", "image-not-found-or-auth"),
            ("open local file: permission denied", "permission-denied"),
            ("operation denied", "unclassified"),
            (
                "sudo: sorry, you are not allowed to set the following environment variables: REGISTRY_AUTH_FILE",
                "execution-policy",
            ),
        ):
            with self.subTest(error=error):
                self.assertEqual(registry.failure_category(error), expected)

    def test_missing_and_malformed_auth_never_fall_back(self) -> None:
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

    def test_unset_selection_fails_closed(self) -> None:
        self.assertEqual(self.invoke("inspect"), 1)
        self.assertIn("auth-missing", self.stderr.getvalue())

    def test_insecure_permissions_and_symlinks_rejected(self) -> None:
        authfile = self.initialize()
        authfile.chmod(0o644)
        self.assertEqual(self.invoke("inspect"), 1)
        authfile.chmod(0o600)
        target = authfile.with_name("other.json")
        authfile.rename(target)
        authfile.symlink_to(target)
        self.assertEqual(self.invoke("inspect"), 1)
        self.assertIn("auth-permissions", self.stderr.getvalue())

    def test_init_error_removes_partial_file(self) -> None:
        with patch.object(registry.json, "dump", side_effect=OSError(FAKE_SECRET)):
            self.assertEqual(self.invoke("init"), 1)
        self.assertEqual(list(self.directory.glob("flavor-registry-*")), [])
        self.assert_no_disclosure()

    def test_inspect_explicit_selection_and_allowlisted_digest(self) -> None:
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

    def test_failures_categorized_without_raw_stdout_or_stderr(self) -> None:
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
                    f"{error}\n{FAKE_AUTH}\nAuthorization: Bearer {FAKE_SECRET}\n"
                    f"Cookie: {FAKE_SECRET}\n::error::injected",
                )
                self.assertEqual(self.invoke("inspect"), 1)
                self.assertIn(f"exit=17 category={category}", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_versions_only_emit_numeric_allowlist(self) -> None:
        for version in (f"skopeo version 1.20.0\n{FAKE_SECRET}", FAKE_SECRET, "skopeo version " + "9" * 10000 + ".0.0"):
            with patch.object(
                registry, "run_captured", return_value=subprocess.CompletedProcess([], 0, version, FAKE_SECRET)
            ):
                registry.versions()
        self.assertIn("skopeo_version=1.20.0", self.stderr.getvalue())
        self.assertIn("skopeo_version=unknown", self.stderr.getvalue())
        self.assertNotIn("9" * 100, self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_unreadable_file_and_parent_symlink_fail_safely(self) -> None:
        authfile = self.initialize()
        with patch.object(Path, "read_bytes", side_effect=PermissionError(FAKE_SECRET)):
            self.assertEqual(self.invoke("inspect"), 1)
        self.assertIn("auth-unreadable", self.stderr.getvalue())
        target = authfile.parent.with_name("moved")
        authfile.parent.rename(target)
        authfile.parent.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.invoke("inspect"), 1)
        self.assertEqual(self.invoke("cleanup"), 1)
        self.assertTrue((target / "auth.json").exists())
        self.assert_no_disclosure()

    def test_privileged_boundary_and_inner_shell_inherit_selected_file(self) -> None:
        authfile = self.initialize()
        fake_skopeo = self.directory / "skopeo"
        fake_skopeo.write_text(
            f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
            "assert sys.argv[1] == 'copy'\n"
            "assert pathlib.Path(os.environ['REGISTRY_AUTH_FILE']).is_file()\n"
            "auths = json.loads(pathlib.Path(os.environ['REGISTRY_AUTH_FILE']).read_text())['auths']\n"
            f"assert auths['quay.io/osac-project']['auth'] == {FAKE_AUTH!r}\n"
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

        def source_or_auth(path: Path) -> bytes:
            if str(path) == "/usr/local/bin/cluster-tool":
                return b"fake-cluster-tool-source"
            return read_bytes(path)

        def execute(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            self.assertEqual(
                command[:7],
                [
                    "sudo",
                    "-n",
                    "-u",
                    "root",
                    f"REGISTRY_AUTH_FILE={authfile}",
                    "/usr/bin/python3",
                    "/usr/local/bin/cluster-tool",
                ],
            )
            self.assertEqual(env["REGISTRY_AUTH_FILE"], str(authfile))
            sudo_env = {name: env[name] for name in ("PATH", "REGISTRY_AUTH_FILE")}
            return captured([sys.executable, str(fake_cluster_tool), *command[7:]], env=sudo_env)

        boundary_env = {name: os.environ[name] for name in ("FLAVOR_REGISTRY_AUTH_FILE", "FLAVOR_IMAGE")}
        boundary_env["PATH"] = str(self.directory) + os.pathsep + os.environ["PATH"]
        boundary_env["REGISTRY_AUTH_FILE"] = str(self.directory / "wrong-auth.json")
        with (
            patch.dict(os.environ, boundary_env, clear=True),
            patch.object(registry, "versions"),
            patch.object(registry, "run_captured", side_effect=execute),
            patch.object(Path, "read_bytes", source_or_auth),
        ):
            self.assertEqual(self.invoke("pull"), 0)
        self.assertIn("target_uid=0 server=local", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_pull_categorizes_failure_without_exposing_raw_output(self) -> None:
        self.initialize()
        with (
            patch.object(registry, "versions"),
            patch.object(Path, "read_bytes", return_value=os.environ["PULL_SECRET"].encode()),
            patch.object(
                registry,
                "run_captured",
                return_value=subprocess.CompletedProcess([], 1, FAKE_SECRET, "unauthorized " + FAKE_AUTH),
            ),
        ):
            self.assertEqual(self.invoke("pull"), 1)
        self.assertIn("operation=pull exit=1 category=auth-denied", self.stderr.getvalue())
        self.assert_no_disclosure()

    def test_init_rejects_missing_and_malformed_inputs_without_disclosure(self) -> None:
        os.environ["QUAY_PASSWORD"] = ""
        self.assertEqual(self.invoke("init"), 1)
        self.assertIn("auth-missing", self.stderr.getvalue())
        os.environ["PULL_SECRET"] = FAKE_SECRET
        self.assertEqual(self.invoke("init"), 1)
        self.assertIn("auth-malformed", self.stderr.getvalue())
        self.assertEqual(list(self.directory.glob("flavor-registry-*")), [])
        self.assert_no_disclosure()

    def test_cleanup_idempotent_and_does_not_remove_shared_auth(self) -> None:
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

    def test_workflows_select_both_consumers_and_always_cleanup(self) -> None:
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
            self.assertIn("python3 .github/scripts/flavor_registry.py pull 9>&-", prepare["run"])
            cleanup = steps[steps.index(prepare) + 1]
            self.assertEqual(cleanup["if"], "always()")
            self.assertEqual(cleanup["run"], "python3 .github/scripts/flavor_registry.py cleanup")

    def test_sudo_policy_allows_only_existing_pull_command_and_auth_path(self) -> None:
        policy = (ROOT / "scripts/machine-init.sh").read_text()
        self.assertIn("Cmnd_Alias CLUSTER_TOOL_PULL            = /usr/bin/python3 ${CLUSTER_TOOL_BIN} pull *", policy)
        self.assertIn('Defaults!CLUSTER_TOOL_PULL env_keep += "REGISTRY_AUTH_FILE"', policy)
        self.assertNotIn("SETENV:", policy)
        directive = next(line for line in policy.splitlines() if line.startswith("Defaults!CLUSTER_TOOL_PULL"))
        sudoers = self.directory / "sudoers"
        sudoers.write_text(
            "Cmnd_Alias CLUSTER_TOOL_PULL = /usr/bin/python3 /usr/local/bin/cluster-tool pull *\n"
            + directive
            + "\nrunner ALL=(root) NOPASSWD: CLUSTER_TOOL_PULL\n"
        )
        subprocess.run(["visudo", "-c", "-f", str(sudoers)], capture_output=True, check=True)


if __name__ == "__main__":
    unittest.main()

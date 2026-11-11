#!/usr/bin/env python3
"""Exercise the pinned public cluster-tool consumer with fake skopeo, never sudo."""

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("flavor_registry", Path(__file__).with_name("flavor_registry.py"))
registry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registry)


class CopyVerified(Exception):
    pass


class ConsumerTests(unittest.TestCase):
    def test_actual_pull_shell_inheritance_and_failure_channel(self) -> None:
        source = Path(os.environ["CLUSTER_TOOL_SOURCE"])
        tree = ast.parse(source.read_text())
        nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in ("ExecutionEnv", "cmd_pull")
        ]
        self.assertEqual(len(nodes), 2)
        namespace = {
            "subprocess": subprocess,
            "os": os,
            "sys": sys,
            "REMOTE_BASE": "/unused",
            "time": SimpleNamespace(sleep=lambda _seconds: None),
            "print_step": lambda *args: None,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            authfile = directory / "auth.json"
            authfile.write_text('{"auths":{"quay.io/osac-project":{"auth":"ZmFrZTpmYWtl"}}}')
            authfile.chmod(0o600)
            skopeo = directory / "skopeo"
            skopeo.write_text(
                f"#!{sys.executable}\nimport os, pathlib, sys\n"
                "assert sys.argv[1] == 'copy'\n"
                "assert pathlib.Path(os.environ['REGISTRY_AUTH_FILE']).read_text().startswith('{')\n"
                "assert 'FLAVOR_REGISTRY_AUTH_FILE' not in os.environ\n"
                "print('fake-stdout-canary')\n"
                "print('manifest unknown; unauthorized; fake-stderr-canary', file=sys.stderr)\n"
                "sys.exit(int(os.environ['FAKE_EXIT']))\n"
            )
            skopeo.chmod(0o700)
            for status in (0, 1):
                with (
                    self.subTest(status=status),
                    patch.dict(
                        os.environ,
                        {
                            "REGISTRY_AUTH_FILE": str(authfile),
                            "PATH": temporary + os.pathsep + os.environ["PATH"],
                            "FAKE_EXIT": str(status),
                        },
                        clear=True,
                    ),
                ):
                    executor = namespace["ExecutionEnv"](host="local", host_ip="127.0.0.1")
                    actual_run = executor.run
                    calls = []

                    def run(
                        command: str, *, check: bool = True, calls: list[str] = calls, actual_run: Callable = actual_run
                    ) -> subprocess.CompletedProcess[str]:
                        if command.startswith("skopeo copy "):
                            calls.append(command)
                            result = actual_run(command, check=check)
                            if result.returncode == 0:
                                raise CopyVerified
                            return result
                        return subprocess.CompletedProcess(command, 0, "", "")

                    executor.run = run
                    namespace["env"] = executor
                    captured_stdout = io.StringIO()
                    with redirect_stdout(captured_stdout):
                        if status == 0:
                            with self.assertRaises(CopyVerified):
                                namespace["cmd_pull"](
                                    SimpleNamespace(image="quay.io/osac-project/cluster-flavors:test")
                                )
                        else:
                            with self.assertRaises(SystemExit) as failure:
                                namespace["cmd_pull"](
                                    SimpleNamespace(image="quay.io/osac-project/cluster-flavors:test")
                                )
                    self.assertEqual(len(calls), 1 if status == 0 else 10)
                    self.assertNotIn("fake-stderr-canary", captured_stdout.getvalue())
                    if status:
                        message = str(failure.exception)
                        self.assertIn("Pull failed after 10 attempts", message)
                        self.assertIn("fake-stderr-canary", message)
                        output = io.StringIO()
                        with (
                            redirect_stdout(output),
                            redirect_stderr(output),
                            patch.object(registry, "versions"),
                            patch.object(registry, "selected_authfile", return_value=str(authfile)),
                            patch.object(Path, "read_bytes", return_value=b"fake-source"),
                            patch.object(
                                registry,
                                "run_captured",
                                return_value=subprocess.CompletedProcess([], 1, captured_stdout.getvalue(), message),
                            ),
                        ):
                            self.assertEqual(
                                registry.registry_operation("pull", "quay.io/osac-project/cluster-flavors:test"), 1
                            )
                        self.assertIn("category=image-not-found-or-auth", output.getvalue())
                        self.assertNotIn("canary", output.getvalue())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Run scripts/osac-install-cluster-tool against a local repository, and check
that the sudoers policy, fleet role and workflow wire it up safely."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts/osac-install-cluster-tool"
WORKFLOW = REPO / ".github/workflows/fleet-cluster-tool-install.yml"

GOOD_V1 = "#!/usr/bin/env python3\nprint('cluster-tool one')\n"
GOOD_V2 = "#!/usr/bin/env python3\nprint('cluster-tool two')\n"
BROKEN = "#!/usr/bin/env python3\ndef broken(:\n"


def run(*cmd: str, cwd: pathlib.Path | None = None, env: dict | None = None):
    return subprocess.run(
        cmd, cwd=cwd, env=env, check=False, capture_output=True, text=True
    )


def git(cwd: pathlib.Path, *args: str) -> str:
    result = run(
        "git",
        "-c", "user.name=test",
        "-c", "user.email=test@example.com",
        "-c", "commit.gpgsign=false",
        "-c", "tag.gpgsign=false",
        *args,
        cwd=cwd,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@unittest.skipIf(
    os.geteuid() == 0,
    "the helper ignores its path overrides as root and would touch the real host",
)
class HelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.origin = root / "origin.git"
        work = root / "work"
        run("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        run("git", "clone", "-q", str(self.origin), str(work))
        git(work, "checkout", "-q", "-b", "main")
        self.work = work
        self.commit("cluster-tool", GOOD_V1, "v1.0.0")
        self.commit("cluster-tool", GOOD_V2, "v1.0.1")
        git(work, "push", "-q", "origin", "main", "--tags")
        # A tag on a branch that never reached main.
        git(work, "checkout", "-q", "-b", "unmerged")
        self.commit("cluster-tool", GOOD_V2 + "# unreviewed\n", "v9.9.9")
        self.commit("cluster-tool", GOOD_V2 + "# unreviewed 2\n", "v9.9.8", lightweight=True)
        # A tag on main whose file does not compile.
        git(work, "checkout", "-q", "main")
        self.commit("cluster-tool", GOOD_V1 + "# lightweight\n", "v1.0.3", lightweight=True)
        self.commit("cluster-tool", BROKEN, "v1.0.2")
        git(work, "push", "-q", "origin", "main", "unmerged", "--tags")

        self.bin = root / "bin" / "cluster-tool"
        self.version = root / "share" / "VERSION"
        self.env = {
            **os.environ,
            "OSAC_CT_REPO_URL": str(self.origin),
            "OSAC_CT_BIN": str(self.bin),
            "OSAC_CT_VERSION_FILE": str(self.version),
            "OSAC_CT_LOCK": str(root / "lock"),
        }

    def commit(self, name: str, content: str, tag: str, lightweight: bool = False) -> None:
        (self.work / name).write_text(content)
        git(self.work, "add", name)
        git(self.work, "commit", "-q", "-m", tag)
        if lightweight:
            git(self.work, "tag", tag)
        else:
            git(self.work, "tag", "-a", tag, "-m", tag)

    def install(self, *args: str, env: dict | None = None):
        return run(sys.executable, "-I", str(HELPER), *args, env=env or self.env)

    def test_installs_tag_atomically_with_mode_and_version_record(self) -> None:
        result = self.install("v1.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bin.read_text(), GOOD_V2)
        self.assertEqual(self.bin.stat().st_mode & 0o777, 0o755)
        commit = git(self.work, "rev-parse", "v1.0.1^{commit}")
        self.assertEqual(self.version.read_text(), f"v1.0.1 {commit}\n")
        self.assertEqual(
            [p.name for p in self.bin.parent.iterdir()], ["cluster-tool"],
            "no staging file may be left next to the binary",
        )

    def test_upgrade_and_rollback_replace_the_binary(self) -> None:
        self.assertEqual(self.install("v1.0.0").returncode, 0)
        self.assertEqual(self.bin.read_text(), GOOD_V1)
        self.assertEqual(self.install("v1.0.1").returncode, 0)
        self.assertEqual(self.bin.read_text(), GOOD_V2)
        self.assertEqual(self.install("v1.0.0").returncode, 0)
        self.assertEqual(self.bin.read_text(), GOOD_V1)

    def test_installing_the_current_tag_again_is_a_no_op_on_the_binary(self) -> None:
        self.assertEqual(self.install("v1.0.1").returncode, 0)
        before = self.bin.stat().st_ino
        result = self.install("v1.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already matches v1.0.1", result.stdout)
        self.assertEqual(self.bin.stat().st_ino, before)

    def test_tag_not_on_main_is_refused_and_nothing_changes(self) -> None:
        self.assertEqual(self.install("v1.0.0").returncode, 0)
        result = self.install("v9.9.9")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not on the default branch", result.stderr)
        self.assertEqual(self.bin.read_text(), GOOD_V1)
        self.assertEqual(self.version.read_text().split()[0], "v1.0.0")

    def test_file_that_does_not_compile_is_refused_before_install(self) -> None:
        self.assertEqual(self.install("v1.0.0").returncode, 0)
        result = self.install("v1.0.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not compile", result.stderr)
        self.assertEqual(self.bin.read_text(), GOOD_V1)

    def test_lightweight_tags_resolve_to_their_commit_like_annotated_ones(self) -> None:
        result = self.install("v1.0.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.version.read_text(), f"v1.0.3 {git(self.work, 'rev-parse', 'v1.0.3')}\n"
        )
        self.assertEqual(self.bin.read_text(), GOOD_V1 + "# lightweight\n")

    def test_lightweight_tag_not_on_main_is_refused(self) -> None:
        result = self.install("v9.9.8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not on the default branch", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_version_record_is_world_readable_even_under_a_restrictive_umask(self) -> None:
        previous = os.umask(0o077)
        try:
            result = self.install("v1.0.1")
        finally:
            os.umask(previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.version.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.bin.stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            sorted(p.name for p in self.version.parent.iterdir()), ["VERSION"],
            "no staged record may be left behind",
        )

    def test_unknown_tag_is_refused(self) -> None:
        result = self.install("v7.7.7")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v7.7.7 is not a tag of", result.stderr)
        self.assertFalse(self.bin.exists())

    def test_only_one_strictly_formatted_tag_is_accepted(self) -> None:
        for bad in (
            ["main"], ["v1.0"], ["1.0.1"], ["v1.0.1-rc1"], ["v1.0.1 "],
            ["v1.0.1\n"], ["../v1.0.1"], ["--help"], ["v1.0.1", "v1.0.0"], [],
        ):
            with self.subTest(args=bad):
                result = self.install(*bad)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage:", result.stderr)
                self.assertFalse(self.bin.exists())

    def test_privileged_run_ignores_the_environment_overrides(self) -> None:
        source = HELPER.read_text()
        self.assertIn("if os.geteuid() != 0:", source)
        self.assertIn('_REPO_URL = "https://github.com/osac-project/cluster-tool.git"', source)
        # Overrides are read only inside the unprivileged branch.
        before_branch = source.split("if os.geteuid() != 0:")[0]
        self.assertNotIn("os.environ", before_branch.split("def settings")[1])


class WiringTest(unittest.TestCase):
    def test_sudoers_allows_exactly_the_helper_with_one_version_argument(self) -> None:
        text = (REPO / "scripts/machine-init.sh").read_text()
        self.assertIn(
            "Cmnd_Alias CLUSTER_TOOL_INSTALL = "
            "/usr/local/sbin/osac-install-cluster-tool v[0-9]*.[0-9]*.[0-9]*",
            text,
        )
        grant = text.split("NOPASSWD:")[1].split("EOF")[0]
        self.assertIn("CLUSTER_TOOL_INSTALL", grant)

    def test_helper_is_shipped_by_the_script_and_the_fleet_role(self) -> None:
        self.assertIn("osac-install-cluster-tool", (REPO / "scripts/machine-init.sh").read_text())
        role = (REPO / "fleet/roles/machine_base/tasks/runner_user.yml").read_text()
        self.assertIn("- osac-install-cluster-tool", role)
        self.assertTrue(os.access(HELPER, os.X_OK), "helper must be executable in git")

    def workflow(self) -> dict:
        return yaml.safe_load(WORKFLOW.read_text())

    def test_workflow_is_on_demand_only_with_a_required_tag(self) -> None:
        wf = self.workflow()
        triggers = wf.get("on") or wf.get(True)
        self.assertEqual(list(triggers), ["workflow_dispatch"])
        self.assertTrue(triggers["workflow_dispatch"]["inputs"]["tag"]["required"])
        self.assertEqual(wf["permissions"], {})

    def test_workflow_runs_only_from_main_and_targets_osac_1_to_15_only(self) -> None:
        text = WORKFLOW.read_text()
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("^osac-[0-9]+$", text)
        self.assertNotRegex(text, r"\$\{\{[^}]*secrets\.")
        self.assertNotIn("secrets: inherit", text)

    def test_workflow_never_interpolates_inputs_into_shell(self) -> None:
        for job in self.workflow()["jobs"].values():
            for step in job["steps"]:
                run_text = step.get("run", "")
                self.assertNotRegex(run_text, r"\$\{\{\s*(inputs|github\.event)", step.get("name"))

    def test_workflow_stops_a_bad_tag_early_and_serializes_per_host(self) -> None:
        install = self.workflow()["jobs"]["install"]
        self.assertIs(install["strategy"]["fail-fast"], True)
        self.assertLessEqual(install["strategy"]["max-parallel"], 3)
        self.assertEqual(install["runs-on"], "${{ matrix.host }}")
        self.assertIn("matrix.host", install["concurrency"]["group"])
        self.assertIs(install["concurrency"]["cancel-in-progress"], False)

    def test_workflow_verifies_the_installed_tool_as_the_runner_user(self) -> None:
        text = WORKFLOW.read_text()
        self.assertRegex(text, r"sudo -n /usr/local/sbin/osac-install-cluster-tool")
        self.assertIn("/usr/local/bin/cluster-tool --help", text)
        self.assertTrue(re.search(r"VERSION", text))


if __name__ == "__main__":
    unittest.main()

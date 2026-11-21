"""Regression tests for scripts/osac-sos-report's fd-anchored machinery.

The helper runs as root on the CI fleet, where a bug means a failed or
unsafe post-test step — these tests exercise everything that does not need
root (the file-descriptor plumbing) on the developer's machine. The
root-only paths (sudo validation, the root-owned staging check) stay
untested here by design.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import signal
import stat
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "osac-sos-report")
_SPEC = importlib.util.spec_from_file_location(
    "osac_sos_report", _SCRIPT, loader=SourceFileLoader("osac_sos_report", _SCRIPT)
)
osac_sos_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(osac_sos_report)

_DIRECTORY_FLAGS = osac_sos_report._DIRECTORY_FLAGS


class FakeSos:
    """Writes what sos --build writes, where it writes it: a sosreport-*
    directory and a *-private_map file, both inside --tmp-dir. The map is
    written only after a successful cleaner run in real sos, so omit_map
    simulates the failure mode where the report is handed over
    unobfuscated."""

    def __init__(self, path, omit_map=False):
        self.path = path
        self.omit_map = omit_map

    def __call__(self):
        map_line = (
            ""
            if self.omit_map
            else "printf '{}\\n' > \"${work_root}/sosreport-fake-private_map\"\n"
        )
        pathlib.Path(self.path).write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in --tmp-dir=*) work_root=\"${arg#--tmp-dir=}\";; esac\n"
            "done\n"
            "work=\"$(mktemp -d \"${work_root}/sos.XXXX\")\"\n"
            "mkdir -p \"${work}/sosreport-fake/sos_commands\"\n"
            "printf 'host data\\n' > \"${work}/sosreport-fake/sos_commands/networking.txt\"\n"
            "mv \"${work}/sosreport-fake\" \"${work_root}/sosreport-fake\"\n"
            "rmdir \"${work}\"\n"
            + map_line
        )
        os.chmod(self.path, 0o755)


class FdMachineryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.uid = os.getuid()
        self.gid = os.getgid()
        # The directory the helper treats as RUNNER_TEMP: fd-opened like the
        # real one, owned by the invoking user (this user, standing in for
        # github-runner).
        self.runner_temp_fd = os.open(self.tmpdir, _DIRECTORY_FLAGS)

    def tearDown(self):
        os.close(self.runner_temp_fd)

    def _ensure(self, parent_fd, name):
        fd = osac_sos_report.ensure_output_directory(parent_fd, name, self.uid, self.gid)
        self.addCleanup(os.close, fd)
        return fd

    def test_ensure_output_directory_accepts_ordinary_directory(self):
        # Regression: the type check once passed st_mode to os.path.isdir,
        # which treats an int as a file descriptor -- false for every real
        # directory, failing the helper on every run.
        fd = self._ensure(self.runner_temp_fd, "osac-artifacts")
        self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode))

    def test_ensure_output_directory_rejects_symlink(self):
        os.mkdir(os.path.join(self.tmpdir, "real"))
        os.symlink("real", os.path.join(self.tmpdir, "link"))
        with self.assertRaises(SystemExit):
            osac_sos_report.ensure_output_directory(self.runner_temp_fd, "link", self.uid, self.gid)

    def test_remove_tree_recurses_into_directories(self):
        # Regression: a directory whose st_mode was passed to os.path.isdir
        # looked like a file, so cleanup unlinked it, failed, and left the
        # staging behind for the next run's mkdir to trip over.
        root = os.path.join(self.tmpdir, "staging")
        nested = os.path.join(root, "sosreport-fake", "sos_commands")
        os.makedirs(nested)
        pathlib.Path(nested, "networking.txt").write_text("data\n")
        fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            osac_sos_report.remove_tree(fd)
        finally:
            os.close(fd)
        # remove_tree empties the tree; the caller rmdir's the root itself
        # (a directory cannot be removed through the fd used to walk it).
        self.assertTrue(os.path.exists(root))
        self.assertEqual(os.listdir(root), [])

    def test_run_sos_normalize_and_move_report(self):
        fake = FakeSos(os.path.join(self.tmpdir, "fake-sos"))
        fake()
        saved_candidates = osac_sos_report._SOS_BINARY_CANDIDATES
        osac_sos_report._SOS_BINARY_CANDIDATES = (fake.path,)
        try:
            staging = os.path.join(self.tmpdir, "staging")
            os.mkdir(staging)
            staging_fd = os.open(staging, _DIRECTORY_FLAGS)
            try:
                # The fake sos writes into the directory the fd pins, not
                # into the path -- same contract as the real one.
                osac_sos_report.run_sos(staging_fd)
                self.assertEqual(
                    sorted(os.listdir(staging_fd)), ["sosreport-fake", "sosreport-fake-private_map"]
                )
                osac_sos_report.normalize_report(staging_fd, self.uid, self.gid)
                report_fd = self._ensure(self.runner_temp_fd, "osac-artifacts")
                osac_sos_report.move_report(staging_fd, report_fd)
                # Only the directory moves; the private map stays behind.
                self.assertEqual(os.listdir(staging_fd), ["sosreport-fake-private_map"])
                moved = os.path.join(self.tmpdir, "osac-artifacts", "sosreport-fake")
                info = os.stat(moved)
                self.assertEqual(info.st_uid, self.uid)
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o750)
                file_info = os.stat(os.path.join(moved, "sos_commands", "networking.txt"))
                self.assertEqual(file_info.st_uid, self.uid)
                self.assertEqual(stat.S_IMODE(file_info.st_mode), 0o640)
            finally:
                os.close(staging_fd)
        finally:
            osac_sos_report._SOS_BINARY_CANDIDATES = saved_candidates

    def test_run_sos_rejects_report_without_cleaner_map(self):
        # Regression: real sos exits 0 even when the cleaner failed and the
        # report was never obfuscated. The private map is written only on a
        # successful cleaner run, so its absence must fail the collection
        # before anything is handed off.
        fake = FakeSos(os.path.join(self.tmpdir, "fake-sos"), omit_map=True)
        fake()
        saved_candidates = osac_sos_report._SOS_BINARY_CANDIDATES
        osac_sos_report._SOS_BINARY_CANDIDATES = (fake.path,)
        try:
            staging = os.path.join(self.tmpdir, "staging")
            os.mkdir(staging)
            staging_fd = os.open(staging, _DIRECTORY_FLAGS)
            try:
                with self.assertRaises(SystemExit):
                    osac_sos_report.run_sos(staging_fd)
                # The unobfuscated report stays in staging, never moved.
                self.assertEqual(os.listdir(staging_fd), ["sosreport-fake"])
                self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "osac-artifacts")))
            finally:
                os.close(staging_fd)
        finally:
            osac_sos_report._SOS_BINARY_CANDIDATES = saved_candidates

    def test_chown_tree_skips_symlinks(self):
        root = os.path.join(self.tmpdir, "tree")
        os.makedirs(root)
        pathlib.Path(root, "file.txt").write_text("data\n")
        # Disposable target outside the walked tree, with a mode the walk
        # would change if it followed the link: the test asserts the target
        # is untouched rather than trusting chown's no-follow flag alone.
        target = os.path.join(self.tmpdir, "link-target.txt")
        pathlib.Path(target).write_text("target\n")
        os.chmod(target, 0o600)
        os.symlink(target, os.path.join(root, "target-link"))
        fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            osac_sos_report.chown_tree(fd, self.uid, self.gid)
        finally:
            os.close(fd)
        self.assertEqual(os.stat(os.path.join(root, "file.txt")).st_uid, self.uid)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertTrue(os.path.islink(os.path.join(root, "target-link")))


class SlowSos:
    """A fake sos that spawns a long-lived descendant, records its PID, and
    never finishes -- for exercising the process-group cleanup branches."""

    def __init__(self, path):
        self.path = path

    def __call__(self):
        pathlib.Path(self.path).write_text(
            "#!/usr/bin/env bash\n"
            "sleep 300 &\n"
            'echo "$!" > "$(dirname "$0")/descendant.pid"\n'
            "sleep 300\n"
        )
        os.chmod(self.path, 0o755)


class RunSosProcessGroupTests(unittest.TestCase):
    """Both cleanup branches must terminate the whole process group: an
    orphaned plugin command would keep writing into the staging directory
    the cleanup is deleting."""

    def setUp(self):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmpdir = directory.name
        self.pid_file = os.path.join(self.tmpdir, "descendant.pid")
        self.staging = os.path.join(self.tmpdir, "staging")
        os.mkdir(self.staging)
        self.saved_candidates = osac_sos_report._SOS_BINARY_CANDIDATES
        self.saved_timeout = osac_sos_report._SOS_TIMEOUT_SECONDS
        slow = SlowSos(os.path.join(self.tmpdir, "slow-sos"))
        slow()
        osac_sos_report._SOS_BINARY_CANDIDATES = (slow.path,)

    def tearDown(self):
        osac_sos_report._SOS_BINARY_CANDIDATES = self.saved_candidates
        osac_sos_report._SOS_TIMEOUT_SECONDS = self.saved_timeout

    def _run(self):
        staging_fd = os.open(self.staging, _DIRECTORY_FLAGS)
        try:
            osac_sos_report.run_sos(staging_fd)
        finally:
            os.close(staging_fd)

    def _descendant_pid(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return int(pathlib.Path(self.pid_file).read_text().strip())
            except (FileNotFoundError, ValueError):
                time.sleep(0.05)
        raise AssertionError("fake sos never recorded its descendant")

    @staticmethod
    def _exited(pid):
        # A zombie counts as exited: its parent is dead, init reaps it
        # whenever it gets around to it.
        try:
            status = pathlib.Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return True
        return status.rpartition(")")[2].split()[0] == "Z"

    def _wait_for_exit(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._exited(pid):
                return
            time.sleep(0.05)
        raise AssertionError(f"descendant {pid} survived the process-group kill")

    def test_timeout_kills_the_process_group(self):
        osac_sos_report._SOS_TIMEOUT_SECONDS = 1
        with self.assertRaises(SystemExit):
            self._run()
        self._wait_for_exit(self._descendant_pid())

    def test_cancellation_kills_the_process_group(self):
        def interrupt(*_):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGALRM, interrupt)
        signal.alarm(1)
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._run()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self._wait_for_exit(self._descendant_pid())


if __name__ == "__main__":
    unittest.main()

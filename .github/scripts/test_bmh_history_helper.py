"""Regression coverage for the installed, scoped serial-log reader."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOADER = importlib.machinery.SourceFileLoader("serial_helper", str(ROOT / "scripts/osac-bmh-history"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
helper = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(helper)


class SerialHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.store = helper.HistoryStore(self.base / "logs", 1001)
        self.prefix = "virtual-bmh-aabbccdd"
        self.directory = self.store.prepare(self.prefix)
        self.name = self.prefix + "-01.serial.log"

    def test_current_and_every_rotation_are_readable_and_bounded(self):
        for suffix in ("", ".0", ".1", ".2"):
            path = self.directory / (self.name + suffix)
            path.write_bytes(b"x" * (helper.MAX_BYTES + 1))
            path.chmod(0o600)
            self.assertEqual(len(self.store.read(self.prefix, path.name)), helper.MAX_BYTES)
        self.assertEqual(len(self.store.list(self.prefix)), 4)

    def test_other_run_and_uid_cannot_read(self):
        (self.directory / self.name).write_bytes(b"private")
        with self.assertRaises(OSError):
            helper.HistoryStore(self.base / "logs", 1002).read(self.prefix, self.name)
        for name in ("/etc/shadow", "../" + self.name, "virtual-bmh-ffffffff-01.serial.log", self.name + ".3"):
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                self.store.read(self.prefix, name)

    def test_symlink_fifo_hardlink_and_writable_directory_rejected(self):
        outside = self.base / "outside"
        outside.write_bytes(b"outside")
        path = self.directory / self.name
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.store.read(self.prefix, self.name)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(OSError):
            self.store.read(self.prefix, self.name)
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(OSError):
            self.store.read(self.prefix, self.name)
        self.directory.chmod(0o777)
        with self.assertRaises(OSError):
            self.store.list(self.prefix)

    def test_duplicate_prepare_rejects_concurrent_owner(self):
        with self.assertRaises(FileExistsError):
            self.store.prepare(self.prefix)

    def test_directory_symlink_and_foreign_file_owner_rejected(self):
        (self.directory / self.name).write_bytes(b"private")
        original = helper.os.fstat
        def foreign_owner(descriptor):
            information = original(descriptor)
            if helper.stat.S_ISREG(information.st_mode):
                return mock.Mock(st_mode=information.st_mode, st_nlink=1, st_uid=os.geteuid() + 1)
            return information
        with mock.patch.object(helper.os, "fstat", side_effect=foreign_owner), self.assertRaises(OSError):
            self.store.read(self.prefix, self.name)
        moved = self.directory.with_name("saved")
        self.directory.rename(moved)
        self.directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.store.read(self.prefix, self.name)

    def test_cleanup_refuses_any_remaining_domain_and_unreachable_libvirt(self):
        path = self.directory / self.name
        path.write_bytes(b"private")
        for result in (subprocess.CompletedProcess([], 0, self.prefix + "-ff\n"),):
            with mock.patch.object(helper.subprocess, "run", return_value=result), self.assertRaises(ValueError):
                self.store.cleanup(self.prefix)
        with mock.patch.object(helper.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 5)), self.assertRaises(subprocess.TimeoutExpired):
            self.store.cleanup(self.prefix)
        self.assertTrue(path.exists())
        with mock.patch.object(helper.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "virtual-bmh-ffffffff-01\n")):
            self.store.cleanup(self.prefix)
            self.store.cleanup(self.prefix)
        self.assertFalse(self.directory.exists())

    def test_backend_uses_verified_old_backend_and_honest_defaults(self):
        with mock.patch.object(helper.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Running against daemon: 6.0.0\n")), \
             mock.patch.object(helper.socket, "socket") as socket, \
             mock.patch.object(helper.Path, "exists", return_value=True), \
             mock.patch.object(helper, "daemon_settings", return_value={}):
            capability = helper.backend()
        socket.return_value.__enter__.return_value.connect.assert_called_once_with("/run/libvirt/virtlogd-sock")
        self.assertTrue(capability["defaults_assumed"])
        self.assertEqual((capability["max_bytes"], capability["backups"]), (2097152, 3))

    def test_config_missing_commented_changed_and_nonstandard(self):
        def metadata(path):
            if str(path).endswith("cmdline"):
                return b"/usr/sbin/virtlogd\0--timeout=120\0"
            raise AssertionError(path)
        def text(path):
            if str(path).endswith(".pid"):
                return "123"
            if str(path) == "/proc/stat":
                return "btime 100\n"
            if str(path).endswith("/stat"):
                return "123 (virtlogd) " + " ".join(["0"] * 19 + ["100"])
            return '#max_size = 0\n#max_backups = 99\n'
        with mock.patch.object(helper.Path, "read_text", text), mock.patch.object(helper.Path, "read_bytes", metadata), \
             mock.patch.object(helper.Path, "stat", return_value=mock.Mock(st_mtime=1)), \
             mock.patch.object(helper.Path, "exists", return_value=False):
            self.assertEqual(helper.daemon_settings("virtlogd", "/etc/libvirt/virtlogd.conf"), {})
            with mock.patch.object(helper.Path, "exists", return_value=True):
                self.assertEqual(helper.daemon_settings("virtlogd", "/etc/libvirt/virtlogd.conf"), {})
            with mock.patch.object(helper.Path, "stat", return_value=mock.Mock(st_mtime=200)), self.assertRaises(ValueError):
                helper.daemon_settings("virtlogd", "/etc/libvirt/virtlogd.conf")
            with mock.patch.object(helper.Path, "read_bytes", return_value=b"virtlogd\0-f/custom\0"), self.assertRaises(ValueError):
                helper.daemon_settings("virtlogd", "/etc/libvirt/virtlogd.conf")

    def test_missing_and_commented_options_are_assumed_defaults(self):
        self.assertEqual(helper.logging_bounds({}, {}), (2097152, 3))
        self.assertEqual(helper.logging_bounds({"stdio_handler": "logd"}, {"max_size": "2MiB", "max_backups": "3"}), (2097152, 3))
        for values in ({"max_size": "0"}, {"max_backups": "4"}, {"max_size": "3MiB"}):
            with self.assertRaises(ValueError):
                helper.logging_bounds({}, values)

    def test_default_policy_installs_only_narrow_helper_not_head(self):
        init = (ROOT / "scripts/machine-init.sh").read_text()
        self.assertIn("osac-bmh-history", init)
        self.assertIn("/usr/local/sbin/osac-bmh-history *", init)
        self.assertNotIn("NOPASSWD: ALL", init)
        self.assertNotIn("/usr/bin/head", init)
        self.assertIn("osac-bmh-history", (ROOT / "fleet/roles/machine_base/tasks/runner_user.yml").read_text())


if __name__ == "__main__":
    unittest.main()

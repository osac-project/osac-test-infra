#!/usr/bin/env python3
"""Tests for activate-ironic.sh.

Runs the script as a subprocess against the fake oc from test_fakes.py,
verifying the applied Provisioning manifest (the metal3 configuration
this environment relies on) and the exit-code contract when the oc waits
fail.

Run directly: python3 .github/scripts/test_activate_ironic.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Import the sibling module without requiring package install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fakes

SCRIPT = Path(__file__).resolve().parent / "activate-ironic.sh"


class ActivateIronicTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)

        self.fakes = test_fakes.FakeBins(self.temp, ["oc"])
        self.capture_dir = self.fakes.capture_dir
        self.env = self.fakes.env()

    def _run(self, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(env_extra)
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def test_provisioning_cr_is_applied_before_any_wait(self) -> None:
        """The Provisioning CR must be applied first and disable virtual-media TLS."""
        # Failing the first wait keeps the run short and proves the manifest
        # was applied before any waiting happened.
        result = self._run(OC_WAIT_EXIT="86")
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)

        applies = sorted(self.capture_dir.glob("apply-*.yaml"))
        self.assertEqual(len(applies), 1)
        manifest = applies[0].read_text()
        for expected in (
            "kind: Provisioning",
            'provisioningNetwork: "Disabled"',
            "disableVirtualMediaTLS: true",
            "watchAllNamespaces: true",
        ):
            self.assertIn(expected, manifest)

    def test_succeeds_when_the_waits_pass(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Webhook endpoint ready", result.stdout)
        self.assertIn("Ironic is active and ready to accept BareMetalHosts.", result.stdout)
        # Exactly the Provisioning CR is applied.
        self.assertEqual(len(list(self.capture_dir.glob("apply-*.yaml"))), 1)


if __name__ == "__main__":
    unittest.main()

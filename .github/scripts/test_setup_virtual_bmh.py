#!/usr/bin/env python3
"""Contract tests for setup-virtual-bmh.sh."""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = ROOT / ".github/scripts/setup-virtual-bmh.sh"


def _top_level_spec(manifest: str) -> dict[str, str]:
    """Parse scalar fields directly below spec in this small manifest."""
    lines = manifest.splitlines()
    spec_start = lines.index("spec:") + 1
    fields: dict[str, str] = {}
    for line in lines[spec_start:]:
        if line and not line.startswith("  "):
            break
        if not line.startswith("  ") or line.startswith("    "):
            continue
        key, separator, value = line.strip().partition(":")
        if separator:
            fields[key] = value.strip().strip('"')
    return fields


class SetupVirtualBmhTests(unittest.TestCase):
    def test_host_compatibility_and_retry_guards(self) -> None:
        """Check the disk paths, cgroup manager, and retry guard ordering."""
        setup = SETUP_SCRIPT.read_text()
        teardown = (ROOT / ".github/scripts/teardown.sh").read_text()

        self.assertIn('if [[ "${VM_DISK_ROOT}" != /* ]]', setup)
        self.assertIn('VM_DISK_DIR="${VM_DISK_ROOT%/}/virtual-bmh-disks-${CLONE_NAME}"', setup)
        self.assertIn('if [[ "${BMH_DISK_ROOT}" != /* ]]', teardown)
        self.assertIn('BMH_DISK_DIR="${BMH_DISK_ROOT%/}/virtual-bmh-disks-${CLONE_NAME}"', teardown)
        self.assertIn("podman --cgroup-manager=cgroupfs run --detach", setup)
        self.assertIn("raise SystemExit(0 if disk ==", setup)
        self.assertLess(
            setup.index('if ${VIRSH} dominfo "${VM_NAME}"'),
            setup.index("qemu-img create -f qcow2"),
        )
        self.assertLess(
            setup.index('if [[ -f "${SUSHY_PID_FILE}" ]]'),
            setup.index("nohup env"),
        )

    def test_virtual_media_downloads_are_published_over_http(self) -> None:
        """The generated Provisioning CR must disable virtual-media TLS."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            captured_manifest = temp / "provisioning.yaml"

            fake_oc = fake_bin / "oc"
            fake_oc.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    if [[ "$1" == "apply" ]]; then
                      cat > "${CAPTURED_PROVISIONING_MANIFEST}"
                      exit 0
                    fi
                    if [[ "$1" == "wait" ]]; then
                      exit 86
                    fi
                    exit 87
                    """
                )
            )
            fake_oc.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "CAPTURED_PROVISIONING_MANIFEST": str(captured_manifest),
                    "CLONE_NAME": "transport-contract",
                    "HOME": str(temp),
                    "KUBECONFIG": str(temp / "kubeconfig"),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )

            result = subprocess.run(
                ["bash", str(SETUP_SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, check=False
            )

            self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
            spec = _top_level_spec(captured_manifest.read_text())
            self.assertEqual(spec.get("disableVirtualMediaTLS"), "true")


if __name__ == "__main__":
    unittest.main()

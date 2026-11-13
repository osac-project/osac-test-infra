#!/usr/bin/env python3
"""Check the full-regression overlay against a real installer checkout.

Run: OSAC_INSTALLER_DIR=/path/to/osac/osac-installer python3 -I .github/scripts/test_full_ci_lvms.py
Requires PyYAML, yq v4, Helm, and `make helm-infra-deps` in that checkout.
This is an infrastructure contract check, not a migrated E2E suite.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = Path(os.environ["OSAC_INSTALLER_DIR"]).resolve()


class FullCILVMSTests(unittest.TestCase):
    def test_workflow_merge_renders_only_sno_data_disk(self) -> None:
        workflow = yaml.safe_load((ROOT / ".github/workflows/e2e-full-regression.yml").read_text())
        install = next(
            step["run"]
            for step in workflow["jobs"]["e2e"]["steps"]
            if "make install-infra PLATFORM=openshift PROFILE=full-ci" in step.get("run", "")
        )
        merge = re.search(
            r"yq eval-all[^\n]*\\\n.*?mv values/full-ci/infra.yaml.merged values/full-ci/infra.yaml", install, re.DOTALL
        )
        self.assertIsNotNone(merge, "Missing first-install infrastructure overlay merge")
        self.assertLess(merge.end(), install.index("make install-infra"))
        originals = {path: path.read_bytes() for path in (INSTALLER / "values").rglob("*.yaml")}
        source = INSTALLER / "values/full-ci/infra.yaml"
        original_values = yaml.safe_load(originals[source])

        def render(values: Path) -> dict:
            result = subprocess.run(
                [
                    "helm",
                    "template",
                    "osac-infra",
                    str(INSTALLER / "charts/osac-infra"),
                    "--namespace",
                    "osac-infra",
                    "--values",
                    str(values),
                    "--show-only",
                    "templates/lvms.yaml",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            config = yaml.safe_load(result.stdout)
            return yaml.safe_load(config["data"]["config.yaml"])["spec"]["storage"]["deviceClasses"][0]

        original_device_class = render(source)
        try:
            with tempfile.TemporaryDirectory(prefix="full-ci-lvms-") as directory:
                temporary = Path(directory)
                values = temporary / "values/full-ci/infra.yaml"
                values.parent.mkdir(parents=True)
                shutil.copy2(source, values)
                subprocess.run(
                    ["bash", "-euo", "pipefail", "-c", merge.group(0)],
                    cwd=temporary,
                    env={**os.environ, "OVERLAY_DIR": str(ROOT / ".github/overlays")},
                    check=True,
                )
                device_class = render(values)
                self.assertEqual(device_class["deviceSelector"], {"optionalPaths": ["/dev/sdb"]})
                merged_values = yaml.safe_load(values.read_text())
                expected_values = yaml.safe_load(originals[source])
                expected_values["lvms"]["deviceSelector"] = {"optionalPaths": ["/dev/sdb"]}
                expected_values["keycloak"]["devFixtures"] = {"enabled": True}
                self.assertEqual(merged_values, expected_values, "Only the two CI overrides may change values")
                self.assertEqual(device_class["nodeSelector"], original_device_class["nodeSelector"])
                self.assertEqual(device_class["name"], "vg1")
        finally:
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertEqual(original_device_class["deviceSelector"], original_values["lvms"]["deviceSelector"])


if __name__ == "__main__":
    unittest.main()

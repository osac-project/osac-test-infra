#!/usr/bin/env python3
"""The option catalog of infra/netris/targets.md lists every group_vars key."""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


class CatalogTest(unittest.TestCase):
    def test_catalog_matches_group_vars(self):
        group_vars = (ROOT / "infra/netris/inventory/group_vars/all.yml").read_text()
        targets = (ROOT / "infra/netris/targets.md").read_text()
        keys = set(re.findall(r"^([a-zA-Z_][a-zA-Z0-9_]*):", group_vars, re.M))
        catalog = targets.split("## Ansible `EXTRA_VARS` catalog", 1)[1].split("\n## ", 1)[0]
        rows = set(re.findall(r"^\| `([a-zA-Z0-9_]+)` \|", catalog, re.M))
        self.assertEqual(keys, rows)


if __name__ == "__main__":
    unittest.main()

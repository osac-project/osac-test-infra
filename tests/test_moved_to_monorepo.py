"""
⚠️  THE OSAC E2E TEST SUITE HAS MOVED — DO NOT ADD TESTS HERE.

As of OSAC-3593, the e2e test suite lives in the osac mono-repo. Any
modification, addition, or deletion of tests must be done there, NOT in
osac-test-infra:

    https://github.com/osac-project/osac

New tests added to this repository will not be maintained and may be
removed without notice. Port your change to the mono-repo instead.

This file is a deliberate placeholder: it keeps pytest discovery green and
broadcasts the move via its skip reason. It intentionally requires no
fixtures and no live cluster.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(
    reason="OSAC-3593: the e2e test suite moved to the osac mono-repo "
    "(https://github.com/osac-project/osac). Add/modify/delete tests there, not here."
)
def test_suite_has_moved_to_the_osac_monorepo() -> None:
    """Placeholder — see the module docstring. Do not add tests to this repo."""

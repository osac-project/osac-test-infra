from __future__ import annotations

import logging
import os

import pytest

_results_log = logging.getLogger("e2e.results")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if report.when == "call":
        _results_log.info("%s %s (%.2fs)", report.outcome.upper(), report.nodeid, report.duration)
    elif report.when == "setup" and report.skipped:
        _results_log.info("SKIPPED %s", report.nodeid)

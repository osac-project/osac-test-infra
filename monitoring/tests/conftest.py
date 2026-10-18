"""Shared fixtures for the monitoring/scripts unit tests.

The scripts have hyphenated file names and are run directly, not imported
as a package, so they are loaded from their paths. Each is loaded once per
session: workflow-exporter.py registers its Prometheus metrics at import
time, and a second import would register them again and fail.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from helpers import FakeGitHub

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def wfe():
    return _load("workflow_exporter", "workflow-exporter.py")


@pytest.fixture(scope="session")
def digest():
    return _load("post_ci_digest", "post-ci-digest.py")


@pytest.fixture
def github(wfe, monkeypatch):
    fake = FakeGitHub(wfe.API_URL)
    monkeypatch.setattr(wfe.requests, "get", fake.get)
    return fake


@pytest.fixture
def exporter(wfe, github, tmp_path, monkeypatch):
    """A WorkflowExporter on an empty DB in tmp_path, with no token and no
    repo filter, talking to the fake GitHub API."""
    monkeypatch.setattr(wfe, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(wfe, "DB_FILE", str(tmp_path / "workflow-exporter.db"))
    monkeypatch.setattr(wfe, "LEGACY_CACHE_FILE", str(tmp_path / "workflow-exporter-cache.json"))
    monkeypatch.setattr(wfe, "TOKEN", None)
    monkeypatch.setattr(wfe, "REPOS_FILTER", [])
    return wfe.WorkflowExporter()

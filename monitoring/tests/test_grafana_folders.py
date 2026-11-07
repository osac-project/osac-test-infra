"""The Grafana dashboards live in two folders, because they are two products.

CI Fleet covers runners, machines, jobs and queue health. ci-observe covers the
VMaaS, CaaS and BMaaS E2E runs, their tests and the pull requests. The split is
by filename so the repo stays flat and every dashboard test keeps reading its
file at one path.

The ci-observe folder holds seven pages: the Overview (the entry page), three
across-runs pages (Where the time goes, Why runs fail, Test history), Pull
requests, the runs list and one E2E run.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = REPO_ROOT / "monitoring" / "config" / "grafana" / "dashboards"
PROVISIONING = REPO_ROOT / "monitoring" / "config" / "grafana" / "dashboards.yml"
SETUP = REPO_ROOT / "monitoring" / "scripts" / "monitoring-setup.sh"


def providers() -> dict[str, dict]:
    return {p["folder"]: p for p in yaml.safe_load(PROVISIONING.read_text())["providers"]}


def test_two_providers_one_per_folder() -> None:
    by_folder = providers()
    assert set(by_folder) == {"CI Fleet", "ci-observe"}
    assert by_folder["CI Fleet"]["options"]["path"].endswith("/dashboards/ci-fleet")
    assert by_folder["ci-observe"]["options"]["path"].endswith("/dashboards/ci-observe")
    for provider in by_folder.values():
        assert provider["type"] == "file"
        assert provider["orgId"] == 1
        assert provider["options"]["foldersFromFilesStructure"] is False


def test_every_dashboard_file_belongs_to_a_folder() -> None:
    files = sorted(p.name for p in DASHBOARDS.glob("*.json"))
    assert files, "no dashboards found"
    for name in files:
        expected = "ci-observe" if name.startswith("ci-observe-") else "CI Fleet"
        assert expected in providers(), f"{name} has no provider"


def test_known_dashboards_are_in_the_expected_folder() -> None:
    ci_observe = {
        "ci-observe-home.json",  # Overview, the entry page
        "ci-observe-time.json",  # Where the time goes
        "ci-observe-failures.json",  # Why runs fail
        "ci-observe-test.json",  # Test history
        "ci-observe-pull-requests.json",  # Pull requests
        "ci-observe-runs-home.json",  # every run
        "ci-observe-runs.json",  # one E2E run
    }
    ci_fleet = {
        "runner-health.json",
        "runner-status.json",
        "workflow-jobs.json",
        "workflow-metrics.json",
        "ci-health.json",
    }
    expected = {**{name: "ci-observe" for name in ci_observe}, **{name: "CI Fleet" for name in ci_fleet}}
    for name, folder in expected.items():
        assert (DASHBOARDS / name).exists(), f"{name} is missing"
        actual = "ci-observe" if name.startswith("ci-observe-") else "CI Fleet"
        assert actual == folder, f"{name} belongs to {folder}"

    assert {path.name for path in DASHBOARDS.glob("ci-observe-*.json")} == ci_observe
    assert {path.name for path in DASHBOARDS.glob("*.json")} == set(expected)

    # The Run evidence sections are rows on the run page. Trends became Test
    # history and Where the time goes.
    for merged in (
        "ci-observe-cluster.json", "ci-observe-run-evidence.json", "ci-observe-comparison.json",
        "ci-observe-trends.json",
    ):
        assert not (DASHBOARDS / merged).exists(), f"{merged} was merged into another page"


def test_dashboard_titles_name_their_folder() -> None:
    """A dashboard titled "OSAC CI - ..." in CI Fleet reads as a different product."""
    for path in sorted(DASHBOARDS.glob("*.json")):
        dashboard = json.loads(path.read_text())
        prefix = "CI Observe — " if path.name.startswith("ci-observe-") else "CI Fleet — "
        assert dashboard["title"].startswith(prefix), (
            f"{path.name} is titled {dashboard['title']!r}, expected it to start with {prefix!r}"
        )


def test_setup_script_splits_the_files_by_name() -> None:
    script = SETUP.read_text()
    assert "deploy_dashboards()" in script, "the dashboard copy step has no single function"
    assert script.count("deploy_dashboards\n") >= 2, "both deploy paths must call deploy_dashboards"
    assert "ci-observe-*)" in script, "the split needs a ci-observe prefix branch"
    assert "ci-fleet/${base}" in script, "everything else must go to CI Fleet"
    assert 'ci-fleet"/*.json' in script, "stale Fleet dashboards must be removed"
    assert 'ci-observe"/*.json' in script, "stale ci-observe dashboards must be removed"

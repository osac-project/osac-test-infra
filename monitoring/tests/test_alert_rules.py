"""The ci-observe alert rules: valid, and firing in the cases a plain age
comparison cannot see -- a store whose freshness was never read since the
exporter started, a gap check that stopped answering. Runs promtool's rule
unit tests (alert-rules.test.yml) when promtool is on PATH; the structural
checks run everywhere."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
RULES = HERE.parent / "config" / "alert-rules.yml"


def ci_observe_rules():
    groups = yaml.safe_load(RULES.read_text())["groups"]
    return {rule["alert"]: rule for group in groups if group["name"] == "ci-observe" for rule in group["rules"]}


def test_every_store_the_exporter_pins_has_a_stale_and_an_unknown_alert():
    rules = ci_observe_rules()
    stale = rules["CiObserveHourlyStoreStale"]["expr"]
    unknown = rules["CiObserveStoreUnknown"]["expr"]
    for store in ("trends", "spans", "logs", "prs"):
        assert store in stale, f"{store} has no staleness alert"
        assert f'absent(ci_observe_store_last_success{{store="{store}"}})' in unknown
    assert 'absent(ci_observe_store_last_success{store="audit"})' in rules["CiObserveAuditUnknown"]["expr"]
    assert "absent(github_actions_gap_check_last_success)" in rules["CiObserveGapCheckStalled"]["expr"]


def test_a_spent_github_budget_alerts():
    """Every ci-observe store and page draws on one token's budget: spent, they
    all answer 403, and on 2026-11-22 nothing said so for eleven hours."""
    rule = ci_observe_rules()["CiObserveGitHubBudgetExhausted"]
    assert "github_actions_api_rate_limit_remaining" in rule["expr"]
    assert rule["for"] == "15m"


@pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool is unavailable")
def test_the_rules_pass_their_promtool_unit_tests():
    for command in (["promtool", "check", "rules", str(RULES)],
                    ["promtool", "test", "rules", str(HERE / "alert-rules.test.yml")]):
        result = subprocess.run(command, capture_output=True, text=True, cwd=HERE, check=False)
        assert result.returncode == 0, result.stdout + result.stderr

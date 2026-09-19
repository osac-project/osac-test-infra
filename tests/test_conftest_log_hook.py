from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST_SOURCE = REPO_ROOT / "conftest.py"
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _result_lines(output: str) -> list[str]:
    return [ln for ln in output.splitlines() if _TS_RE.search(ln) and "e2e.results" in ln]


def _run_pytest_in_tempdir(test_code: str, *, workers: int = 2) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copy(CONFTEST_SOURCE, Path(tmpdir) / "conftest.py")
        (Path(tmpdir) / "test_sample.py").write_text(test_code)
        (Path(tmpdir) / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n"
            "log_cli = true\n"
            'log_cli_date_format = "%Y-%m-%dT%H:%M:%S"\n'
            'log_cli_format = "%(asctime)s %(levelname)-8s %(name)s %(message)s"\n'
            'log_cli_level = "INFO"\n'
        )

        env = {**os.environ}
        env.pop("PYTEST_XDIST_WORKER", None)

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-n", str(workers), "-v", "test_sample.py"],
            capture_output=True,
            text=True,
            cwd=tmpdir,
            env=env,
            timeout=60,
        )
        return result.stdout + "\n" + result.stderr


def test_xdist_hook_emits_one_result_per_test() -> None:
    """Each test should produce exactly one timestamped result line, not duplicates from worker+controller."""
    output = _run_pytest_in_tempdir(
        """
def test_alpha():
    pass

def test_beta():
    pass
"""
    )
    result_lines = _result_lines(output)
    alpha_lines = [ln for ln in result_lines if "test_alpha" in ln]
    beta_lines = [ln for ln in result_lines if "test_beta" in ln]
    assert len(alpha_lines) == 1, f"Expected 1 result line for test_alpha, got {len(alpha_lines)}: {alpha_lines}"
    assert len(beta_lines) == 1, f"Expected 1 result line for test_beta, got {len(beta_lines)}: {beta_lines}"
    assert "PASSED" in alpha_lines[0]
    assert "PASSED" in beta_lines[0]


def test_xdist_hook_logs_skipped_test() -> None:
    """A test skipped at setup should produce exactly one SKIPPED result line."""
    output = _run_pytest_in_tempdir(
        """
import pytest

@pytest.mark.skip(reason="deliberate skip")
def test_skipped():
    pass

def test_passing():
    pass
"""
    )
    result_lines = _result_lines(output)
    skipped_lines = [ln for ln in result_lines if "test_skipped" in ln]
    passing_lines = [ln for ln in result_lines if "test_passing" in ln]
    assert len(skipped_lines) == 1, f"Expected 1 SKIPPED line, got {len(skipped_lines)}: {skipped_lines}"
    assert "SKIPPED" in skipped_lines[0]
    assert len(passing_lines) == 1, f"Expected 1 PASSED line, got {len(passing_lines)}: {passing_lines}"
    assert "PASSED" in passing_lines[0]

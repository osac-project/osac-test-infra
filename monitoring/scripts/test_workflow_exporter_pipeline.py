#!/usr/bin/env python3
"""Tests for how workflow-exporter.py counts the pull-request flow's runs as "e2e".

Run directly: python3 monitoring/scripts/test_workflow_exporter_pipeline.py
Stdlib unittest only. The exporter's third-party imports (requests,
prometheus_client) are replaced by mocks, so the test needs no dependency.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

for _name in ("requests", "prometheus_client"):
    sys.modules.setdefault(_name, mock.MagicMock())

_SPEC = importlib.util.spec_from_file_location(
    "workflow_exporter", os.path.join(os.path.dirname(__file__), "workflow-exporter.py")
)
workflow_exporter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_exporter)
Exporter = workflow_exporter.WorkflowExporter

SUITE = "caas / e2e-caas-full-install / e2e"


def job(name, conclusion="success", status="completed", started=True):
    return {
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "steps": [{"name": "Run E2E tests", "conclusion": conclusion, "started_at": "t" if started else None}],
    }


def record(workflow="E2E for pull requests"):
    return {"workflow": workflow, "category": "automation"}


class WorkflowIdentityTests(unittest.TestCase):
    def test_the_workflow_file_names_the_run(self):
        run = {"name": "E2E #12 (suites checked in the comment)", "path": ".github/workflows/e2e-pr.yml"}
        self.assertEqual(Exporter._pipeline_workflow_name(run), "E2E for pull requests")

    def test_a_pinned_path_still_matches(self):
        run = {"path": ".github/workflows/e2e-queue.yml@refs/heads/main"}
        self.assertEqual(Exporter._pipeline_workflow_name(run), "E2E for the merge queue")

    def test_other_workflows_are_not_the_flow(self):
        self.assertEqual(Exporter._pipeline_workflow_name({"path": ".github/workflows/ci.yml"}), "")
        self.assertEqual(Exporter._pipeline_workflow_name({}), "")


class JobKindTests(unittest.TestCase):
    def test_suite_job_names(self):
        for name in (SUITE, "vmaas / e2e-vmaas-full-install / e2e", "e2e-bmaas-netris-full-install"):
            self.assertTrue(Exporter._is_suite_job({"name": name}), name)

    def test_orchestration_jobs_are_not_suites(self):
        for name in ("decide / Decide", "track / Suite checks", "finish / Record the result", "caas", "plan"):
            self.assertFalse(Exporter._is_suite_job({"name": name}), name)

    def test_gate_detection_by_name_is_unchanged(self):
        for name in ("decide / Decide", "track / Suite checks", "finish / Record the result", "plan / Plan", "plan"):
            self.assertFalse(Exporter._is_gate_job(name), name)
        self.assertTrue(Exporter._is_gate_job("e2e-caas-gate"))
        self.assertTrue(Exporter._is_gate_job("e2e-readiness"))


class CategoryTests(unittest.TestCase):
    def refined(self, jobs, active=False, workflow="E2E for pull requests"):
        rec = record(workflow)
        Exporter._refine_pipeline_category(rec, jobs, active)
        return rec["category"]

    def test_decide_only_run_is_automation(self):
        jobs = [job("decide / Decide"), job("caas", "skipped"), job("finish", "skipped")]
        self.assertEqual(self.refined(jobs), "automation")

    def test_a_suite_that_ran_is_e2e(self):
        self.assertEqual(self.refined([job("decide / Decide"), job(SUITE)]), "e2e")
        self.assertEqual(self.refined([job("decide / Decide"), job(SUITE, "failure")]), "e2e")

    def test_a_skipped_suite_is_not_e2e(self):
        self.assertEqual(self.refined([job("decide / Decide"), job(SUITE, "skipped")]), "automation")

    def test_a_suite_cancelled_before_it_started_is_not_e2e(self):
        self.assertEqual(self.refined([job(SUITE, "cancelled", started=False)]), "automation")
        self.assertEqual(self.refined([job(SUITE, "cancelled", started=True)]), "e2e")

    def test_a_suite_waiting_for_a_runner_counts_only_while_the_run_is_active(self):
        waiting = job(SUITE, None, status="queued")
        waiting["conclusion"] = None
        self.assertEqual(self.refined([waiting], active=True), "e2e")
        self.assertEqual(self.refined([waiting], active=False), "automation")

    def test_no_jobs_is_automation(self):
        self.assertEqual(self.refined([]), "automation")
        self.assertEqual(self.refined(None), "automation")

    def test_other_workflows_are_untouched(self):
        rec = {"workflow": "E2E CaaS Full Install", "category": "e2e"}
        Exporter._refine_pipeline_category(rec, [job("decide / Decide")])
        self.assertEqual(rec["category"], "e2e")

    def test_orchestration_failure_is_not_counted_as_an_e2e_failure(self):
        jobs = [job("decide / Decide", "failure"), job(SUITE, "skipped")]
        Exporter._refine_pipeline_category(record(), jobs)
        self.assertTrue(Exporter._is_gate_only_failure(jobs))
        self.assertEqual(Exporter._extract_failed_steps(Exporter, jobs), [])
        self.assertEqual(Exporter._extract_step_durations(Exporter, jobs), [])

    def test_a_failed_suite_next_to_a_failed_orchestration_job_is_still_a_failure(self):
        jobs = [job("finish / Record the result", "failure"), job(SUITE, "failure")]
        Exporter._refine_pipeline_category(record(), jobs)
        self.assertFalse(Exporter._is_gate_only_failure(jobs))
        self.assertEqual(len(Exporter._extract_failed_steps(Exporter, jobs)), 1)

    def test_other_workflows_keep_every_job_even_when_it_is_named_like_ours(self):
        # A job called "plan / x" or "track / x" in any other workflow is a real job there.
        jobs = [job("plan / Plan", "failure"), job("track / Build", "failure")]
        Exporter._refine_pipeline_category({"workflow": "CI", "category": "ci"}, jobs)
        self.assertFalse(Exporter._is_gate_only_failure(jobs))
        self.assertEqual(len(Exporter._extract_failed_steps(Exporter, jobs)), 2)
        timed = job("plan / Plan")
        timed["steps"] = [
            {"name": "Run plan", "status": "completed", "started_at": "2026-10-18T00:00:00Z",
             "completed_at": "2026-10-18T00:00:30Z"}
        ]
        self.assertEqual(
            Exporter._extract_step_durations(Exporter, [timed]), [{"name": "Run plan", "duration_s": 30}]
        )


class StoredRowsTests(unittest.TestCase):
    """The startup migration of rows stored before the flow had its own handling."""

    def exporter_with_rows(self, rows):
        exporter = Exporter.__new__(Exporter)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        conn = sqlite3.connect(tmp.name)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT, display_name TEXT,"
            " category TEXT, runner_name TEXT, failure_reason TEXT)"
        )
        conn.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()

        class Db:
            def __enter__(self_inner):
                return conn

            def __exit__(self_inner, *exc):
                conn.commit()

        exporter._db = lambda: Db()
        return exporter, conn

    def test_run_names_become_the_workflow_and_the_runner_decides_the_category(self):
        rows = [
            (1, "osac", "E2E fork/osac:fix/x @ " + "a" * 40, "osac / x", "e2e", "", "infra"),
            (2, "osac", "E2E #12 (suites checked in the comment)", "osac / y", "e2e", "osac-1-runner-04", ""),
            (3, "osac", "E2E review relay", "osac / E2E review relay", "e2e", "", ""),
            (4, "osac", "E2E CaaS Full Install", "osac / E2E CaaS Full Install", "e2e", "", ""),
            (5, "osac", "E2E CaaS Netris Full Install / PR #1748 @ " + "b" * 40, "osac / n", "e2e", "", ""),
            (6, "osac", "E2E on CodeRabbit approval", "osac / o", "automation", "", ""),
            (7, "osac", "CI", "osac / CI", "ci", "", ""),
            (8, "osac", "E2E vmaas:nightly @ not-a-sha", "osac / v", "e2e", "", ""),
        ]
        exporter, conn = self.exporter_with_rows(rows)
        exporter._normalize_pipeline_rows_if_needed()
        got = {r["id"]: (r["workflow"], r["category"], r["failure_reason"]) for r in conn.execute("SELECT * FROM jobs")}
        self.assertEqual(got[1], ("E2E for pull requests", "automation", "n/a"))
        self.assertEqual(got[2], ("E2E for pull requests", "e2e", ""))
        self.assertEqual(got[3], ("E2E review relay", "automation", "n/a"))
        self.assertEqual(got[4], ("E2E CaaS Full Install", "e2e", ""))  # a real suite workflow stays
        # Nothing else is touched: other E2E workflows, bot workflows, CI, odd names.
        self.assertEqual(got[5], ("E2E CaaS Netris Full Install / PR #1748 @ " + "b" * 40, "e2e", ""))
        self.assertEqual(got[6], ("E2E on CodeRabbit approval", "automation", ""))
        self.assertEqual(got[7], ("CI", "ci", ""))
        self.assertEqual(got[8], ("E2E vmaas:nightly @ not-a-sha", "e2e", ""))

    def test_running_it_twice_changes_nothing_more(self):
        rows = [(1, "osac", "E2E #12 (suites checked in the comment)", "osac / y", "e2e", "", "")]
        exporter, conn = self.exporter_with_rows(rows)
        exporter._normalize_pipeline_rows_if_needed()
        first = [tuple(r) for r in conn.execute("SELECT * FROM jobs")]
        exporter._normalize_pipeline_rows_if_needed()
        self.assertEqual(first, [tuple(r) for r in conn.execute("SELECT * FROM jobs")])

    def test_startup_recategorize_leaves_the_flow_alone(self):
        rows = [(1, "osac", "E2E for pull requests", "osac / E2E for pull requests", "automation", "", "n/a")]
        exporter, conn = self.exporter_with_rows(rows)
        exporter._recategorize_jobs_if_needed()
        self.assertEqual(conn.execute("SELECT category FROM jobs").fetchone()["category"], "automation")


if __name__ == "__main__":
    unittest.main()

"""Jobs pagination and pagination-target trust of workflow-exporter.py's
source-facts outbox (W01c).

The outbox is the explicit opt-in shadow feature behind
SOURCE_FACTS_OUTBOX_DIR; these tests enable a real SourceFactsOutbox (on a
real temporary directory) directly on the shared exporter fixture. GitHub
is the existing FakeGitHub double, routed by exact URL string — the same
convention as the golden exporter tests, which keep asserting the
historical ``?filter=latest&per_page=30`` single-page behavior with the
feature off.

Covers:
- off by default: single page, no outbox, zero extra calls;
- enabled: the run's actual attempt endpoint at every ingestion site
  (same call count), full Link pagination without truncation, and the
  unchanged None-on-failure semantics plus an honest coverage gap;
- strictly validated pagination targets: a Link naming any other run,
  attempt, repo, endpoint, host, or a changed query context is refused
  without being followed, nothing foreign is ingested, and the gap states
  the refusal; loops and page caps are refused too;
- the zombie recheck keeps its legacy one-page behavior with the outbox
  on: no pagination, no emission, no extra calls;
- per-request budget gating of historical attempts and artifacts,
  including each Link tail page (no negative budgets, mid-list declines
  are incomplete with a bounded reason and retried as pending
  obligations);
- end-to-end deferral retries for history and artifacts.

No real GitHub access, credentials, or live environment is used.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from helpers import FakeGitHub, FakeResponse, iso, make_gh_job, make_run, make_step

if TYPE_CHECKING:
    import workflow_exporter


def strict_loads(line: str) -> dict:
    """Parse one persisted line the way the documented consumer contract
    requires: any JSON constant (NaN/Infinity) or duplicate object key is
    a failure, never silently accepted."""

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-JSON constant {value}")

    def no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate object key")
        return dict(pairs)

    return json.loads(line, parse_constant=reject_constant, object_pairs_hook=no_dup_keys)


def records(outbox: workflow_exporter.SourceFactsOutbox) -> list[dict]:
    """All envelopes written so far, in segment/write order, parsed
    strictly; a torn trailing line (the only incomplete line a failed
    write can leave) is ignored, anything invalid mid-segment fails."""
    root = Path(outbox.root)
    if not root.exists():
        return []
    out: list[dict] = []
    for segment in sorted(root.glob("sf-*.jsonl")):
        lines = [line for line in segment.read_text().splitlines() if line.strip()]
        for i, line in enumerate(lines):
            try:
                out.append(strict_loads(line=line))
            except ValueError:
                if i != len(lines) - 1:
                    raise
    return out


def of_kind(recs: list[dict], kind: str) -> list[dict]:
    return [r for r in recs if r["kind"] == kind]


def kinds(recs: list[dict]) -> list[str]:
    return [r["kind"] for r in recs]


@pytest.fixture
def R(wfe: ModuleType) -> Callable[[str], str]:
    return lambda path: f"/repos/{wfe.ORG}/{path}"


@pytest.fixture
def sf(
    wfe: ModuleType, exporter: workflow_exporter.WorkflowExporter, tmp_path: Path
) -> workflow_exporter.SourceFactsOutbox:
    outbox = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
    exporter._source_facts = outbox
    return outbox


class TestOffByDefault:
    def test_feature_is_entirely_off_without_the_env_var(self, wfe: ModuleType) -> None:
        assert wfe.SOURCE_FACTS_OUTBOX_DIR is None
        assert wfe.SOURCE_FACTS_ARTIFACTS is False
        assert not wfe.SourceFactsOutbox().enabled

    def test_single_page_only_and_no_extra_calls(
        self, exporter: workflow_exporter.WorkflowExporter, github: FakeGitHub, R: Callable[[str], str]
    ) -> None:
        assert exporter._source_facts.enabled is False
        route = R("osac/actions/runs/1/jobs?filter=latest&per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url="https://evil.example/page2"))
        jobs = exporter._fetch_run_jobs("osac", 1)
        assert [j["name"] for j in jobs] == ["a"]
        assert github.count(route) == 1
        # The untrusted-looking next link is not even considered: with the
        # outbox off there is no pagination to validate.
        assert not any("evil.example" in call for call in github.calls)

    def test_ingestion_keeps_the_historical_latest_url(
        self, exporter: workflow_exporter.WorkflowExporter, github: FakeGitHub, R: Callable[[str], str]
    ) -> None:
        route = R("osac/actions/runs/100/jobs?filter=latest&per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        assert exporter._process_completed_run("osac", make_run(run_attempt=2))
        assert github.calls == [route]


class TestAttemptRouting:
    def test_completed_run_uses_the_attempt_endpoint(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        route = R("osac/actions/runs/100/attempts/2/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        assert exporter._process_completed_run("osac", make_run(run_attempt=2))
        assert github.count(route) == 1
        assert not any("filter=latest" in call for call in github.calls)

    def test_active_in_progress_run_uses_the_attempt_endpoint(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=queued&per_page=100"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        github.add(
            R("osac/actions/runs?status=in_progress&per_page=100"),
            FakeResponse(
                payload={
                    "workflow_runs": [make_run(id=7, status="in_progress", conclusion=None, run_attempt=4)],
                    "total_count": 1,
                }
            ),
        )
        route = R("osac/actions/runs/7/attempts/4/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        runs, complete = exporter._fetch_active_runs("osac")
        assert complete and len(runs) == 1
        assert github.count(route) == 1

    def test_backfill_uses_the_attempt_endpoint(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        github.add(
            R("osac/actions/runs/5/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add("/p1", FakeResponse(payload={"workflow_runs": [make_run(id=5)], "total_count": 1}))
        assert exporter._backfill_page("osac", f"{wfe.API_URL}/p1") == (1, 1)
        assert of_kind(recs=records(outbox=sf), kind="jobs")[0]["payload"]["window"] == "backfill"

    def test_backfill_range_records_runs_jobs_and_coverage(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        since = datetime(2026, 10, 1, tzinfo=UTC)
        until = since + timedelta(hours=1)
        route = f"/repos/{wfe.ORG}/osac/actions/runs?status=completed&per_page=100&created={iso(since)}..{iso(until)}"
        github.add(
            route, FakeResponse(payload={"workflow_runs": [make_run(id=9, conclusion="failure")], "total_count": 1})
        )
        github.add(
            R("osac/actions/runs/9/attempts/1/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure", [make_step("Run E2E tests", "failure")])]}),
        )
        assert exporter._backfill_range("osac", since, until) == (1, 1)
        with exporter._db() as conn:
            row = conn.execute("SELECT failed_step, runner_name FROM jobs WHERE id = 9").fetchone()
        assert row["failed_step"] == "e2e → Run E2E tests"
        assert row["runner_name"] == "box-runner-01"
        recs = records(outbox=sf)
        assert of_kind(recs=recs, kind="run")[0]["payload"]["window"] == "backfill"
        assert of_kind(recs=recs, kind="jobs")[0]["payload"]["window"] == "backfill"
        (cov,) = of_kind(recs=recs, kind="coverage")
        assert cov["payload"] == {
            "window": "backfill_range",
            "since": iso(since),
            "until": iso(until),
            "complete": True,
        }


class TestJobsPagination:
    def route_pages(
        self, wfe: ModuleType, github: FakeGitHub, R: Callable[[str], str], pages_of_jobs: list[list[dict[str, object]]]
    ) -> list[str]:
        """Route a chain of jobs pages for run 100 attempt 1; page N+1's
        URL is the same path with &page=N+1. Returns the page routes."""
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        routes: list[str] = []
        for i, page in enumerate(pages_of_jobs, start=1):
            route = base if i == 1 else f"{base}&page={i}"
            nxt = None if i == len(pages_of_jobs) else f"{wfe.API_URL}{base}&page={i + 1}"
            github.add(route, FakeResponse(payload={"jobs": page}, next_url=nxt))
            routes.append(route)
        return routes

    def test_all_pages_and_steps_retained(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        jobs = [make_gh_job(f"job-{i}", "success", [make_step(f"step-{i}")]) for i in range(70)]
        routes = self.route_pages(wfe, github, R, [jobs[0:30], jobs[30:60], jobs[60:70]])
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        for route in routes:
            assert github.count(route) == 1
        (jobs_rec,) = of_kind(recs=records(outbox=sf), kind="jobs")
        assert jobs_rec["page_complete"] is True
        assert jobs_rec["run_id"] == 100 and jobs_rec["run_attempt"] == 1
        assert len(jobs_rec["payload"]["jobs"]) == 70
        # The two tail pages were preflighted against the attempt budget.
        assert sf._attempt_calls_left == wfe.SOURCE_FACTS_ATTEMPT_CALL_BUDGET - 2
        # The legacy row also keeps every step from every page.
        with exporter._db() as conn:
            steps = json.loads(conn.execute("SELECT steps_json FROM jobs WHERE id = 100").fetchone()[0])
        assert len(steps) == 70

    def test_midpage_failure_is_none_plus_gap_not_empty(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        github.add(f"{base}&page=2", FakeResponse(500, text="later page broke"))
        sf.new_cycle()
        run = make_run()
        # Unchanged None-on-failure semantics: nothing stored, retried later.
        assert not exporter._process_completed_run("osac", run)
        recs = records(outbox=sf)
        assert kinds(recs=recs) == ["run", "coverage"]
        gap = of_kind(recs=recs, kind="coverage")[0]
        assert gap["payload"]["event"] == "jobs_unknown"
        assert gap["payload"]["reason"] == "fetch_failed"
        assert gap["payload"]["pages_fetched"] == 1
        assert gap["run_id"] == 100 and gap["run_attempt"] == 1
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    def test_first_page_failure_is_a_gap_too(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        assert not exporter._process_completed_run("osac", make_run())
        recs = records(outbox=sf)
        assert kinds(recs=recs) == ["run", "coverage"]
        assert of_kind(recs=recs, kind="coverage")[0]["payload"]["event"] == "jobs_unknown"

    def test_link_cycle_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        page2 = f"{base}&page=2"
        github.add(base, FakeResponse(payload={"jobs": []}, next_url=f"{wfe.API_URL}{page2}"))
        github.add(page2, FakeResponse(payload={"jobs": []}, next_url=f"{wfe.API_URL}{base}"))
        sf.new_cycle()
        result = exporter._fetch_run_jobs_paged(repo="osac", run_id=100, run_attempt=1)
        assert result == (None, False, 2, "pagination_refused")
        assert github.count(base) == 1 and github.count(page2) == 1

    def test_page_cap_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        cap = exporter.MAX_JOB_PAGES
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        for i in range(1, cap + 1):
            route = base if i == 1 else f"{base}&page={i}"
            github.add(route, FakeResponse(payload={"jobs": []}, next_url=f"{wfe.API_URL}{base}&page={i + 1}"))
        sf.new_cycle()
        sf._attempt_calls_left = cap  # every tail page preflighted
        result = exporter._fetch_run_jobs_paged(repo="osac", run_id=100, run_attempt=1)
        assert result == (None, False, cap, "pagination_refused")
        assert len(github.calls) == cap
        assert sf._attempt_calls_left == 1  # cap - 1 takes, never negative


class TestForeignPaginationTargets:
    """A Link target naming any other listing — run, attempt, repo,
    endpoint, host, or a changed query context — is refused: never
    followed, never ingested, an honest gap with the refusal reason."""

    def ingest(
        self,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        next_url: str,
    ) -> bool:
        first = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(first, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=next_url))
        return exporter._process_completed_run("osac", make_run())

    def assert_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        forbidden: str,
    ) -> None:
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert not any(forbidden in call for call in github.calls)
        gap = of_kind(recs=records(outbox=sf), kind="coverage")[0]
        assert gap["payload"]["event"] == "jobs_unknown"
        assert gap["payload"]["reason"] == "pagination_refused"
        assert of_kind(recs=records(outbox=sf), kind="jobs") == []

    def test_foreign_run_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        foreign = f"{wfe.API_URL}{R('osac/actions/runs/999/attempts/1/jobs?per_page=30')}&page=2"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "/runs/999")

    def test_foreign_attempt_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        foreign = f"{wfe.API_URL}{R('osac/actions/runs/100/attempts/7/jobs?per_page=30')}&page=2"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "/attempts/7/")

    def test_foreign_repo_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        foreign = f"{wfe.API_URL}/repos/{wfe.ORG}/osac-test-infra/actions/runs/100/attempts/1/jobs?per_page=30&page=2"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "osac-test-infra")

    def test_foreign_endpoint_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        foreign = f"{wfe.API_URL}{R('osac/actions/runs/100/artifacts?per_page=100')}&page=2"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "/artifacts")

    def test_spoofed_host_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        evil = "https://evil.example/steal?token=x"
        before = wfe.source_facts_dropped.labels(reason="pagination_refused")._value.get()
        assert not self.ingest(exporter, github, R, wfe, evil)
        assert wfe.source_facts_dropped.labels(reason="pagination_refused")._value.get() == before + 1
        # The refusal log is value-free: no URL, no token, no payload.
        assert "evil.example" not in caplog.text
        gap = of_kind(recs=records(outbox=sf), kind="coverage")[0]
        assert gap["payload"]["reason"] == "pagination_refused"

    def test_added_query_param_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        first = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        foreign = f"{wfe.API_URL}{first}&page=2&filter=latest"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "filter=latest")

    def test_changed_per_page_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        foreign = f"{wfe.API_URL}{R('osac/actions/runs/100/attempts/1/jobs?per_page=100')}&page=2"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "per_page=100")

    def test_dropped_query_param_refused(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        # per_page is part of the listing's identity: a next link without
        # it is a changed query context, not the same listing.
        foreign = f"{wfe.API_URL}{R('osac/actions/runs/100/attempts/1/jobs')}?page=2"
        assert not self.ingest(exporter, github, R, wfe, foreign)
        self.assert_refused(sf, exporter, github, "jobs?page=2")

    def test_ingestion_tail_declined_at_rate_floor(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        # At the rate floor the legacy first page is still fetched, the
        # tail page is never requested, and the owed fact becomes pending.
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        sf.new_cycle()
        exporter._api_rate_remaining = wfe.SOURCE_FACTS_RATE_RESERVE
        assert not exporter._process_completed_run("osac", make_run())
        assert github.count(base) == 1
        assert not any("page=2" in call for call in github.calls)
        recs = records(outbox=sf)
        gap = of_kind(recs=recs, kind="coverage")[0]
        assert gap["payload"]["event"] == "jobs_unknown"
        assert gap["payload"]["reason"] == "rate_limit"
        assert gap["payload"]["pages_fetched"] == 1
        assert of_kind(recs=recs, kind="jobs") == []  # never a truncated complete listing
        assert [p.kind for p in sf.pending_facts()] == ["jobs"]
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    def test_ingestion_tail_declined_at_zero_budget(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        sf.new_cycle()
        sf._attempt_calls_left = 0
        assert not exporter._process_completed_run("osac", make_run())
        assert github.count(base) == 1
        assert not any("page=2" in call for call in github.calls)
        gap = of_kind(recs=records(outbox=sf), kind="coverage")[0]
        assert gap["payload"]["reason"] == "budget"
        assert gap["payload"]["pages_fetched"] == 1
        assert [p.kind for p in sf.pending_facts()] == ["jobs"]
        assert sf._attempt_calls_left == 0  # never negative

    def test_ingestion_tail_budget_stops_between_pages(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        # Budget for one tail page only: two pages are fetched, the third
        # is never requested, the listing stays incomplete, and a later
        # cycle with budget completes it through the pending obligation.
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("j1")]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        github.add(
            f"{base}&page=2",
            FakeResponse(payload={"jobs": [make_gh_job("j2")]}, next_url=f"{wfe.API_URL}{base}&page=3"),
        )
        github.add(f"{base}&page=3", FakeResponse(payload={"jobs": [make_gh_job("j3")]}))
        sf.new_cycle()
        sf._attempt_calls_left = 1
        assert not exporter._process_completed_run("osac", make_run())
        assert github.count(base) == 1
        assert github.count(f"{base}&page=2") == 1
        assert github.count(f"{base}&page=3") == 0
        gap = of_kind(recs=records(outbox=sf), kind="coverage")[0]
        assert gap["payload"]["reason"] == "budget"
        assert gap["payload"]["pages_fetched"] == 2
        assert sf._attempt_calls_left == 0
        assert [p.kind for p in sf.pending_facts()] == ["jobs"]
        sf.new_cycle()
        exporter._retry_pending_source_facts()
        (jobs_rec,) = of_kind(recs=records(outbox=sf), kind="jobs")
        assert jobs_rec["page_complete"] is True
        assert [j["name"] for j in jobs_rec["payload"]["jobs"]] == ["j1", "j2", "j3"]
        assert sf.pending_facts() == []


class TestPaginationValidator:
    def test_absent_link_ends_pagination_cleanly(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        # No rel=next at all: not a refusal, just the end of the list.
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("a")]}))
        jobs, complete, pages, reason = exporter._fetch_run_jobs_paged(repo="osac", run_id=100, run_attempt=1)
        assert complete is True and pages == 1 and reason is None
        assert [j["name"] for j in jobs] == ["a"]
        assert exporter._validated_pagination_target(next_url=None, first_url=base) is None
        assert exporter._validated_pagination_target(next_url="", first_url=base) is None

    def test_cursor_only_change_is_followed(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        wfe: ModuleType,
        R: Callable[[str], str],
    ) -> None:
        first = f"{wfe.API_URL}{R('osac/actions/runs/1/attempts/1/jobs?per_page=30')}"
        nxt = f"{first}&page=2"
        assert exporter._validated_pagination_target(next_url=nxt, first_url=first) == nxt
        # The same URL with an unchanged query is its own valid target.
        assert exporter._validated_pagination_target(next_url=first, first_url=first) == first


class TestZombieRecheck:
    def test_zombie_probe_fetches_but_never_emits(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        old = iso(datetime.now(UTC) - timedelta(hours=1))
        route = R("osac/actions/runs/1/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": []}))
        run = {"id": 1, "run_attempt": 1, "run_started_at": old}
        assert exporter._is_zombie_run("osac", run)
        assert github.count(route) == 1
        assert records(outbox=sf) == []

    def test_zombie_recheck_stays_single_page_with_outbox_on(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        # Even with the outbox on, the zombie recheck keeps the legacy
        # one-page behavior: no pagination, no emission, no extra calls.
        old = iso(datetime.now(UTC) - timedelta(hours=1))
        route = R("osac/actions/runs/1/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": []}, next_url=f"{wfe.API_URL}{route}&page=2"))
        run = {"id": 1, "run_attempt": 1, "run_started_at": old}
        assert exporter._is_zombie_run("osac", run)
        assert github.count(route) == 1
        assert not any("page=2" in call for call in github.calls)
        assert records(outbox=sf) == []

    def test_legacy_fetch_stays_single_page_with_outbox_on(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        route = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=f"{wfe.API_URL}{route}&page=2"))
        jobs = exporter._fetch_run_jobs("osac", 100, 1)
        assert [j["name"] for j in jobs] == ["a"]
        assert github.count(route) == 1
        assert not any("page=2" in call for call in github.calls)


class TestAttemptHistory:
    def test_lookback_records_earlier_attempts(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        run = make_run(run_attempt=3)
        github.add(
            R("osac/actions/runs/100/attempts/3/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        for n in (2, 1):
            github.add(
                R(f"osac/actions/runs/100/attempts/{n}"),
                FakeResponse(payload=make_run(run_attempt=n, conclusion="failure")),
            )
            github.add(
                R(f"osac/actions/runs/100/attempts/{n}/jobs?per_page=30"),
                FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
            )
        sf.new_cycle()
        assert exporter._process_completed_run("osac", run)
        recs = records(outbox=sf)
        assert kinds(recs=recs) == ["run", "jobs", "run_attempt", "run_attempt"]
        hist = of_kind(recs=recs, kind="run_attempt")
        assert [h["run_attempt"] for h in hist] == [2, 1]
        assert all(h["payload"]["run"]["run_attempt"] == h["run_attempt"] for h in hist)
        assert all(h["payload"]["run"]["conclusion"] == "failure" for h in hist)
        # Documented request count: the current attempt's jobs page, plus
        # per earlier attempt its run object and its jobs page — each
        # gated before it is made.
        attempt_calls = [c for c in github.calls if "/attempts/" in c]
        assert len(attempt_calls) == 5
        assert sf._attempt_calls_left == wfe.SOURCE_FACTS_ATTEMPT_CALL_BUDGET - 4

    def test_every_history_page_is_gated_before_the_request(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/1"), FakeResponse(payload=make_run(run_attempt=1, conclusion="failure"))
        )
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        github.add(f"{base}&page=2", FakeResponse(payload={"jobs": [make_gh_job("b")]}))
        sf.new_cycle()
        sf._attempt_calls_left = 3  # run object + two gated job pages
        assert exporter._process_completed_run("osac", run)
        (hist,) = of_kind(recs=records(outbox=sf), kind="run_attempt")
        assert hist["page_complete"] is True
        assert len(hist["payload"]["jobs"]) == 2
        assert sf._attempt_calls_left == 0

    def test_midlist_budget_decline_is_incomplete_and_pending(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/1"), FakeResponse(payload=make_run(run_attempt=1, conclusion="failure"))
        )
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(base, FakeResponse(payload={"jobs": [make_gh_job("a")]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        sf.new_cycle()
        sf._attempt_calls_left = 2  # run object + first page only
        assert exporter._process_completed_run("osac", run)
        # The tail page was never requested: the gate runs before it.
        assert github.count(f"{base}&page=2") == 0
        recs = records(outbox=sf)
        gap = next(c for c in of_kind(recs=recs, kind="coverage") if c["payload"]["event"] == "jobs_unknown")
        assert gap["payload"]["reason"] == "budget"
        assert gap["payload"]["pages_fetched"] == 1
        assert of_kind(recs=recs, kind="run_attempt") == []  # incomplete, never faked
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]
        # A later cycle with budget retries and completes it.
        sf.new_cycle()
        github.add(f"{base}&page=2", FakeResponse(payload={"jobs": [make_gh_job("b")]}))
        exporter._retry_pending_source_facts()
        (hist,) = of_kind(recs=records(outbox=sf), kind="run_attempt")
        assert hist["page_complete"] is True
        assert len(hist["payload"]["jobs"]) == 2
        assert sf.pending_facts() == []

    def test_budget_counts_each_request_and_defers_explicitly(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(run_attempt=3)
        github.add(
            R("osac/actions/runs/100/attempts/3/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/2"), FakeResponse(payload=make_run(run_attempt=2, conclusion="failure"))
        )
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        sf.new_cycle()
        sf._attempt_calls_left = 2  # exactly attempt 2's two requests
        assert exporter._process_completed_run("osac", run)
        recs = records(outbox=sf)
        assert [h["run_attempt"] for h in of_kind(recs=recs, kind="run_attempt")] == [2]
        deferred = [c for c in of_kind(recs=recs, kind="coverage") if c["payload"]["event"] == "deferred"]
        assert [(d["run_attempt"], d["payload"]["reason"]) for d in deferred] == [(1, "budget")]
        assert [p.run_attempt for p in sf.pending_facts()] == [1]

    def test_rate_floor_defers(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        # At the reserve exactly: extra calls are deferred, never blocked
        # from the legacy path.
        exporter._api_rate_remaining = wfe.SOURCE_FACTS_RATE_RESERVE
        assert exporter._process_completed_run("osac", run)
        recs = records(outbox=sf)
        assert of_kind(recs=recs, kind="run_attempt") == []
        deferred = of_kind(recs=recs, kind="coverage")
        assert deferred[0]["payload"]["reason"] == "rate_limit"
        assert deferred[0]["run_attempt"] == 1

    def test_rate_unknown_never_blocks(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/1"), FakeResponse(payload=make_run(run_attempt=1, conclusion="failure"))
        )
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        sf.new_cycle()
        exporter._api_rate_remaining = None  # no header seen yet
        assert exporter._process_completed_run("osac", run)
        assert [h["run_attempt"] for h in of_kind(recs=records(outbox=sf), kind="run_attempt")] == [1]

    def test_previously_current_attempt_not_refetched(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run(run_attempt=1))
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        assert exporter._process_completed_run("osac", make_run(run_attempt=2))
        # Attempt 1's jobs were recorded as a completed current attempt:
        # its run object is never fetched for "history".
        assert not any(call.endswith("/attempts/1") for call in github.calls)
        assert github.count(R("osac/actions/runs/100/attempts/1/jobs?per_page=30")) == 1
        assert of_kind(recs=records(outbox=sf), kind="run_attempt") == []

    def test_attempt_run_fetch_failure_is_an_unknown(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(R("osac/actions/runs/100/attempts/1"), FakeResponse(500))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", run)
        gap = [
            c for c in of_kind(recs=records(outbox=sf), kind="coverage") if c["payload"]["event"] == "attempt_unknown"
        ]
        assert len(gap) == 1 and gap[0]["run_attempt"] == 1
        assert of_kind(recs=records(outbox=sf), kind="run_attempt") == []
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]

    def test_jobs_gate_deferral_is_an_incomplete_unknown(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/1"), FakeResponse(payload=make_run(run_attempt=1, conclusion="failure"))
        )
        sf.new_cycle()
        sf._attempt_calls_left = 1  # the attempt's run object only
        assert exporter._process_completed_run("osac", run)
        recs = records(outbox=sf)
        assert of_kind(recs=recs, kind="run_attempt") == []
        gap = next(c for c in of_kind(recs=recs, kind="coverage") if c["payload"]["event"] == "jobs_unknown")
        assert gap["run_attempt"] == 1
        assert gap["payload"]["reason"] == "budget"
        assert gap["payload"]["pages_fetched"] == 0
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]

    def test_history_jobs_fetch_failure_is_an_unknown(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/1"), FakeResponse(payload=make_run(run_attempt=1, conclusion="failure"))
        )
        github.add(R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(500))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", run)
        gap = [c for c in of_kind(recs=records(outbox=sf), kind="coverage") if c["payload"]["event"] == "jobs_unknown"]
        assert len(gap) == 1 and gap[0]["run_attempt"] == 1
        assert gap[0]["payload"]["pages_fetched"] == 0
        assert of_kind(recs=records(outbox=sf), kind="run_attempt") == []
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]

    def test_single_attempt_runs_never_fetch_history(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        assert of_kind(recs=records(outbox=sf), kind="run_attempt") == []
        assert not any(call.endswith("/attempts/1") for call in github.calls)

    def test_backfill_and_initial_load_never_fetch_history(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        github.add(
            R("osac/actions/runs/5/attempts/4/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add("/p1", FakeResponse(payload={"workflow_runs": [make_run(id=5, run_attempt=4)], "total_count": 1}))
        assert exporter._backfill_page("osac", f"{wfe.API_URL}/p1") == (1, 1)
        # Only the current attempt's jobs page was fetched for the run.
        assert [c for c in github.calls if "/attempts/" in c] == [R("osac/actions/runs/5/attempts/4/jobs?per_page=30")]

    def test_deferred_history_retried_end_to_end(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(run_attempt=2)
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        sf._attempt_calls_left = 0  # the run-object gate defers attempt 1
        assert exporter._process_completed_run("osac", run)
        pending = sf.pending_facts()
        assert len(pending) == 1 and pending[0].kind == "run_attempt"
        # A later cycle with budget restores the obligation.
        sf.new_cycle()
        github.add(
            R("osac/actions/runs/100/attempts/1"), FakeResponse(payload=make_run(run_attempt=1, conclusion="failure"))
        )
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        exporter._retry_pending_source_facts()
        assert [h["run_attempt"] for h in of_kind(recs=records(outbox=sf), kind="run_attempt")] == [1]
        assert sf.pending_facts() == []

    def test_first_decline_queues_every_owed_lookback_identity(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Budget zero at attempt 3 with lookback 2: both owed attempts
        # (2 and 1) queue as pending, not just the first one the gate
        # declined on.
        run = make_run(run_attempt=3)
        github.add(
            R("osac/actions/runs/100/attempts/3/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        sf._attempt_calls_left = 0
        assert exporter._process_completed_run("osac", run)
        pending = sf.pending_facts()
        assert sorted(p.run_attempt for p in pending) == [1, 2]
        assert all(p.kind == "run_attempt" for p in pending)
        deferred = [c for c in of_kind(recs=records(outbox=sf), kind="coverage") if c["payload"]["event"] == "deferred"]
        assert [d["run_attempt"] for d in deferred] == [2, 1]
        with exporter._db() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        counter = wfe.completed_runs.labels(
            org=wfe.ORG, repo="osac", workflow=run["name"], conclusion="success"
        )._value.get()

        # The next cycle — a real collect, not a manual observer —
        # completes both owed attempts, two gated calls each, and never
        # double-counts legacy rows or counters.
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 0)
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        for n in (2, 1):
            github.add(
                R(f"osac/actions/runs/100/attempts/{n}"),
                FakeResponse(payload=make_run(run_attempt=n, conclusion="failure")),
            )
            github.add(
                R(f"osac/actions/runs/100/attempts/{n}/jobs?per_page=30"),
                FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
            )
        sf.new_cycle()
        exporter.collect()
        assert [p.run_attempt for p in sf.pending_facts()] == []
        hist = of_kind(recs=records(outbox=sf), kind="run_attempt")
        assert [h["run_attempt"] for h in hist] == [2, 1]
        assert sf._attempt_calls_left == wfe.SOURCE_FACTS_ATTEMPT_CALL_BUDGET - 4
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == rows
        assert (
            wfe.completed_runs.labels(org=wfe.ORG, repo="osac", workflow=run["name"], conclusion="success")._value.get()
            == counter
        )
        # A duplicate retry with nothing pending makes no extra calls.
        calls = len(github.calls)
        exporter._retry_pending_source_facts()
        assert len(github.calls) == calls


class TestArtifacts:
    def make_artifacts_outbox(
        self, wfe: ModuleType, exporter: workflow_exporter.WorkflowExporter, tmp_path: Path
    ) -> workflow_exporter.SourceFactsOutbox:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), artifacts_enabled=True)
        exporter._source_facts = sf
        return sf

    def test_off_by_default_even_with_the_outbox_on(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        assert sf.artifacts_enabled is False
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        assert not any("artifacts" in call for call in github.calls)

    def test_enabled_records_paginated_artifacts(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        base = R("osac/actions/runs/100/artifacts?per_page=100")
        github.add(
            base,
            FakeResponse(payload={"artifacts": [{"id": 1, "name": "junit"}]}, next_url=f"{wfe.API_URL}{base}&page=2"),
        )
        github.add(f"{base}&page=2", FakeResponse(payload={"artifacts": [{"id": 2, "name": "logs"}]}))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        (art,) = of_kind(recs=records(outbox=sf), kind="artifacts")
        assert art["page_complete"] is True
        assert art["run_id"] == 100
        assert [a["id"] for a in art["payload"]["artifacts"]] == [1, 2]
        # Each page was gated before it was requested.
        assert sf._artifact_calls_left == wfe.SOURCE_FACTS_ARTIFACT_CALL_BUDGET - 2

    def test_midlist_artifact_decline_is_incomplete_and_pending(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        base = R("osac/actions/runs/100/artifacts?per_page=100")
        github.add(base, FakeResponse(payload={"artifacts": [{"id": 1}]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        sf.new_cycle()
        sf._artifact_calls_left = 1  # the first page only
        assert exporter._process_completed_run("osac", make_run())
        assert github.count(f"{base}&page=2") == 0
        gap = of_kind(recs=records(outbox=sf), kind="coverage")[0]
        assert gap["payload"]["event"] == "artifacts_unknown"
        assert gap["payload"]["reason"] == "budget"
        assert gap["payload"]["pages_fetched"] == 1
        assert of_kind(recs=records(outbox=sf), kind="artifacts") == []
        assert [p.kind for p in sf.pending_facts()] == ["artifacts"]

    def test_budget_deferral_is_explicit(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        sf._artifact_calls_left = 0
        assert exporter._process_completed_run("osac", make_run())
        cov = [c for c in of_kind(recs=records(outbox=sf), kind="coverage") if c["payload"].get("reason")]
        assert cov[0]["payload"] == {"window": "artifacts", "event": "deferred", "pages_fetched": 0, "reason": "budget"}
        assert not any("artifacts" in call for call in github.calls)
        assert [p.kind for p in sf.pending_facts()] == ["artifacts"]

    def test_rate_floor_deferral_is_explicit(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        exporter._api_rate_remaining = wfe.SOURCE_FACTS_RATE_RESERVE
        assert exporter._process_completed_run("osac", make_run())
        cov = [c for c in of_kind(recs=records(outbox=sf), kind="coverage") if c["payload"].get("reason")]
        assert cov[0]["payload"]["reason"] == "rate_limit"

    def test_fetch_failure_is_an_unknown_not_empty(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(R("osac/actions/runs/100/artifacts?per_page=100"), FakeResponse(500))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        gap = of_kind(recs=records(outbox=sf), kind="coverage")
        assert gap[0]["payload"]["event"] == "artifacts_unknown"
        assert gap[0]["payload"]["reason"] == "fetch_failed"
        assert of_kind(recs=records(outbox=sf), kind="artifacts") == []

    def test_midpage_failure_is_an_unknown_not_empty(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        base = R("osac/actions/runs/100/artifacts?per_page=100")
        github.add(base, FakeResponse(payload={"artifacts": [{"id": 1}]}, next_url=f"{wfe.API_URL}{base}&page=2"))
        github.add(f"{base}&page=2", FakeResponse(500))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        gap = of_kind(recs=records(outbox=sf), kind="coverage")[0]
        assert gap["payload"]["event"] == "artifacts_unknown"
        assert gap["payload"]["pages_fetched"] == 1
        assert of_kind(recs=records(outbox=sf), kind="artifacts") == []

    def test_spoofed_artifact_link_refused(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/artifacts?per_page=100"),
            FakeResponse(payload={"artifacts": []}, next_url="https://evil.example/artifacts?page=2"),
        )
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        assert of_kind(recs=records(outbox=sf), kind="artifacts") == []
        assert not any("evil.example" in call for call in github.calls)

    def test_artifact_link_cycle_refused(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        base = R("osac/actions/runs/100/artifacts?per_page=100")
        github.add(base, FakeResponse(payload={"artifacts": []}, next_url=f"{wfe.API_URL}{base}&page=2"))
        github.add(f"{base}&page=2", FakeResponse(payload={"artifacts": []}, next_url=f"{wfe.API_URL}{base}"))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        assert of_kind(recs=records(outbox=sf), kind="artifacts") == []
        gap = of_kind(recs=records(outbox=sf), kind="coverage")
        assert gap[0]["payload"]["event"] == "artifacts_unknown"
        assert gap[0]["payload"]["pages_fetched"] == 2

    def test_recorded_once_per_run(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(R("osac/actions/runs/100/artifacts?per_page=100"), FakeResponse(payload={"artifacts": [{"id": 1}]}))
        sf.new_cycle()
        run = make_run()
        exporter._record_run_artifacts(repo="osac", run=run, window="recent_completed")
        calls = len(github.calls)
        exporter._record_run_artifacts(repo="osac", run=run, window="recent_completed")
        assert len(github.calls) == calls
        assert len(of_kind(recs=records(outbox=sf), kind="artifacts")) == 1

    def test_artifacts_refresh_once_per_new_attempt(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        # The listing is run-wide but keyed per observed attempt: a rerun
        # fetches it again, the same attempt never does.
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        base = R("osac/actions/runs/100/artifacts?per_page=100")
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        github.add(base, FakeResponse(payload={"artifacts": [{"id": 1}]}))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run(run_attempt=1))
        assert github.count(base) == 1
        # The rerun sees a changed listing and records it.
        github.add(base, FakeResponse(payload={"artifacts": [{"id": 1}, {"id": 2}]}))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run(run_attempt=2))
        assert github.count(base) == 2
        observations = of_kind(recs=records(outbox=sf), kind="artifacts")
        assert [(o["run_attempt"], [a["id"] for a in o["payload"]["artifacts"]]) for o in observations] == [
            (1, [1]),
            (2, [1, 2]),
        ]
        # Reprocessing the same attempt fetches nothing.
        calls = len(github.calls)
        exporter._record_run_artifacts(repo="osac", run=make_run(run_attempt=2), window="recent_completed")
        assert len(github.calls) == calls

    def test_deferred_artifacts_retried_end_to_end(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = self.make_artifacts_outbox(wfe, exporter, tmp_path)
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        sf.new_cycle()
        sf._artifact_calls_left = 0  # defers the artifacts obligation
        assert exporter._process_completed_run("osac", make_run())
        assert sf.pending_facts()[0].kind == "artifacts"
        sf.new_cycle()
        github.add(R("osac/actions/runs/100/artifacts?per_page=100"), FakeResponse(payload={"artifacts": [{"id": 1}]}))
        exporter._retry_pending_source_facts()
        assert len(of_kind(recs=records(outbox=sf), kind="artifacts")) == 1
        assert sf.pending_facts() == []

"""The at-a-glance half of ci-observe: E2E health, where the time goes, why
runs fail, and every test's history, across runs.

Endpoints (all GET, all JSON, all 200: a missing store or an empty window is
a note in the document, never an error):

    /api/ci-observe/health     pass rate, time to result, failure causes,
                               phases, per suite and per day
    /api/ci-observe/time       phases, steps, runner wait, per day
    /api/ci-observe/failures   causes, failing steps, failed runs, failing
                               and flaky tests, error messages
    /api/ci-observe/tests      every test: runs, failures, flakiness,
                               duration, share of test time, trend
    /api/ci-observe/test       one test: every run, its trend, where its time
                               goes and what it waits for
    /api/ci-observe/waits      what the tests wait for in the product, by
                               resource kind and by wait, and shared fixtures

Common parameters: days (7 by default, at most KEEP_DAYS), suite (all,
vmaas, caas, bmaas), repo (all, osac, osac-test-infra), trigger (all, pr,
merge_queue, scheduled, manual). Every number compares the window with the
window just before it when the stores reach back that far.

The population is the standard VMaaS, CaaS and BMaaS suite runs
(ci_observe_phases.suite_key): a full-install run, a pull-request suite run,
or one suite of a merge-queue run. GitHub owns pass or fail.

Stores, all read-only here:
- workflow-exporter.db `jobs` (live, 30 days) and `captures` (run page
  links), owned by the workflow exporter;
- trends.db `suite_runs` (the same rows, archived for KEEP_DAYS),
  `test_runs` (JUnit per test per run), and the span rollup's `test_spans`,
  `wait_spans`, `fixture_spans`.

Definitions the pages state in their descriptions:
- Pass rate = passed / (passed + failed). Cancelled runs are not counted.
- Time to result = from GitHub creating the suite's first job to its last
  job finishing (waiting for a runner included), measured on passing runs.
  A row stored before job timelines existed falls back to its run duration.
- A failure's cause is the phase of its first failed step
  (ci_observe_phases.failure_cause).
- A flaky test passed and failed on the same commit in the window.

Documents are cached for CACHE_SECONDS per parameter set, and concurrent
requests for the same document share one computation: one dashboard load
asks the same URL from many panels.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_observe_phases as phases

ORG = os.getenv("GITHUB_ORG", "osac-project")
KEEP_DAYS = int(os.getenv("CI_OBSERVE_KEEP_DAYS", "90"))
DEFAULT_DAYS = 7
CACHE_SECONDS = 120
CACHE_MAX_ENTRIES = 64
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

RUN_PAGE = phases.RUN_PAGE
TEST_PAGE = phases.TEST_PAGE
TIME_PAGE = phases.TIME_PAGE
FAILURES_PAGE = phases.FAILURES_PAGE
RUNS_PAGE = phases.RUNS_PAGE
PR_PAGE = phases.PR_PAGE

# Columns of a suite-run row, in both the exporter's jobs table and the
# trends.db archive.
RUN_COLUMNS = (
    "id", "run_id", "repo", "workflow", "pr_display", "conclusion", "event", "duration_s", "created_at",
    "updated_at", "run_attempt", "failed_step", "steps_json", "failure_reason", "head_sha", "url",
    "runner_name", "timeline_json",
)
MESSAGE_SIGNATURE_RES = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.I), "<id>"),
    (re.compile(r"\b\d+(\.\d+)?s?\b"), "N"),
)


# -- small helpers -------------------------------------------------------------

def percentile(values, fraction):
    """Nearest-rank percentile, None for no values (the rollups' rule)."""
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    rank = math.ceil(fraction * len(values))
    return round(values[min(max(rank, 1), len(values)) - 1], 3)


def mean(values):
    values = [value for value in values if value is not None]
    return round(sum(values) / len(values), 3) if values else None


def ratio(part, whole):
    return round(part / whole, 4) if whole else None


def change(current, previous):
    """(current - previous) / previous, None when either is unknown or zero."""
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / previous, 4)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def message_signature(message):
    """A failure message with its instance-specific parts replaced, so the
    same error in two runs groups together."""
    text = " ".join((message or "").split())
    for pattern, replacement in MESSAGE_SIGNATURE_RES:
        text = pattern.sub(replacement, text)
    return text[:200]


def time_parts(row):
    """(setup, waits, actions, teardown) seconds of one recorded test.

    Waits are every poll_until wait the test made, in any phase; setup,
    actions (the call phase) and teardown are each phase minus its waits. The
    four add up to the test's setup + call + teardown time.
    """
    setup = max(0.0, (row.get("setup_s") or 0) - (row.get("wait_setup_s") or 0))
    actions = max(0.0, (row.get("call_s") or 0) - (row.get("wait_call_s") or 0))
    teardown = max(0.0, (row.get("teardown_s") or 0) - (row.get("wait_teardown_s") or 0))
    return setup, row.get("wait_s") or 0.0, actions, teardown


def test_url(test_key, days, suite="all"):
    return phases.link_to_test(test_key, days, suite)


def suite_page(page, suite, filters):
    return phases.link_to_suite(page, suite, filters.days, filters.repo, filters.trigger)


def _param(params, name, default=""):
    values = params.get(name) if hasattr(params, "get") else None
    if isinstance(values, list):
        return (values[0] if values else default) or default
    return values or default


# -- filters -------------------------------------------------------------------

class Filters:
    """The common parameters, validated. An unknown value is ignored with a
    note, never an error."""

    TRIGGERS = ("pr", "merge_queue", "scheduled", "manual", "push", "other")

    def __init__(self, params, now):
        self.notes = []
        try:
            days = int(_param(params, "days", str(DEFAULT_DAYS)))
        except (TypeError, ValueError):
            days = DEFAULT_DAYS
        self.days = max(1, min(days, KEEP_DAYS))
        suite = _param(params, "suite", "all").strip().lower()
        # The runs list names all three suites "core".
        if suite == "core":
            suite = "all"
        if suite not in ("all", *phases.SUITES):
            self.notes.append(f"suite {suite!r} is not VMaaS, CaaS or BMaaS; showing all three")
            suite = "all"
        self.suite = suite
        repo = _param(params, "repo", "all").strip()
        if repo.lower() == "all" or not repo:
            repo = ""
        elif not REPO_NAME_RE.match(repo):
            self.notes.append(f"repository {repo!r} is not a repository name; the filter was ignored")
            repo = ""
        self.repo = repo
        trigger = _param(params, "trigger", "all").strip().lower()
        if trigger not in ("all", *self.TRIGGERS):
            self.notes.append(f"trigger {trigger!r} is unknown; the filter was ignored")
            trigger = "all"
        self.trigger = trigger
        self.now = now
        self.window_to = now
        self.window_from = now - timedelta(days=self.days)
        self.previous_from = now - timedelta(days=2 * self.days)
        self.has_previous = 2 * self.days <= KEEP_DAYS

    @property
    def suites(self):
        return phases.SUITES if self.suite == "all" else (self.suite,)

    def key(self):
        return (self.days, self.suite, self.repo, self.trigger)

    def matches(self, fact):
        if fact["suite"] not in self.suites:
            return False
        if self.repo and fact["repo"] != self.repo:
            return False
        return self.trigger == "all" or fact["trigger"] == self.trigger

    def window(self):
        return {
            "days": self.days,
            "suite": self.suite,
            "repo": self.repo or "all",
            "trigger": self.trigger,
            "window_from": iso(self.window_from),
            "window_to": iso(self.window_to),
            "previous_from": iso(self.previous_from) if self.has_previous else "",
        }


# -- the stores ------------------------------------------------------------------

class Stores:
    """Read-only connections to the exporter DB and trends.db."""

    def __init__(self, jobs_db, trends_db):
        self.jobs_db = jobs_db
        self.trends_db = trends_db

    @staticmethod
    def _open(path):
        if not path or not os.path.exists(path):
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _tables(conn):
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    @staticmethod
    def _columns(conn, table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _select_runs(self, conn, table, since, until, exclude_ids=()):
        columns = self._columns(conn, table)
        select = ", ".join(column if column in columns else f"NULL AS {column}" for column in RUN_COLUMNS)
        where = "created_at >= ? AND created_at < ?"
        if table == "jobs":
            where += " AND category = 'e2e'"
        rows = conn.execute(f"SELECT {select} FROM {table} WHERE {where}", (iso(since), iso(until))).fetchall()
        return [dict(row) for row in rows if row["id"] not in exclude_ids]

    def suite_run_rows(self, since, until):
        """(rows, notes): the exporter's live rows, then the archive's rows
        the exporter no longer holds."""
        notes = []
        rows = []
        live_ids = set()
        conn = self._open(self.jobs_db)
        if conn is None:
            notes.append("the workflow exporter's job history is not readable, so only archived runs are shown")
        else:
            try:
                if "jobs" in self._tables(conn):
                    rows = self._select_runs(conn, "jobs", since, until)
                    live_ids = {row["id"] for row in rows}
            except sqlite3.Error as exc:
                notes.append(f"the job history could not be read: {exc}")
            finally:
                conn.close()
        conn = self._open(self.trends_db)
        if conn is not None:
            try:
                if "suite_runs" in self._tables(conn):
                    rows.extend(self._select_runs(conn, "suite_runs", since, until, live_ids))
            except sqlite3.Error as exc:
                notes.append(f"the run archive could not be read: {exc}")
            finally:
                conn.close()
        return rows, notes

    def captures(self):
        """{run id: [(capture id, attempt, suite), ...]} from the registry."""
        conn = self._open(self.jobs_db)
        if conn is None:
            return {}
        try:
            if "captures" not in self._tables(conn):
                return {}
            out = defaultdict(list)
            for row in conn.execute("SELECT capture_id, run_id, run_attempt, suite FROM captures WHERE run_id != ''"):
                out[str(row["run_id"])].append((row["capture_id"], str(row["run_attempt"] or ""), row["suite"] or ""))
            return out
        except sqlite3.Error:
            return {}
        finally:
            conn.close()

    def trends_rows(self, table, where, args, columns="*"):
        """Rows of a trends.db table, [] when the table or file is missing."""
        conn = self._open(self.trends_db)
        if conn is None:
            return None
        try:
            if table not in self._tables(conn):
                return None
            available = self._columns(conn, table)
            if columns != "*":
                columns = ", ".join(column if column in available else f"NULL AS {column}" for column in columns)
            return [dict(row) for row in conn.execute(f"SELECT {columns} FROM {table} WHERE {where}", args)]
        except sqlite3.Error:
            return None
        finally:
            conn.close()


# -- suite-run facts -------------------------------------------------------------

def suite_run_fact(row, captures):
    """One suite run, as every page reads it."""
    workflow = row.get("workflow") or ""
    suite = phases.suite_key(workflow)
    if not suite:
        return None
    if (row.get("failure_reason") or "").lower() == "gate" or workflow.lower().endswith("-gate"):
        return None
    conclusion = (row.get("conclusion") or "").lower()
    timeline = phases.load_timeline(row.get("timeline_json"))
    try:
        steps = json.loads(row.get("steps_json") or "[]")
    except (TypeError, ValueError):
        steps = []
    steps = steps if isinstance(steps, list) else []
    timing = phases.run_phases(timeline, steps)
    step_seconds = []
    if timeline:
        for job in timeline:
            for step in job.get("steps") or []:
                seconds = phases.seconds_between(step.get("started_at"), step.get("completed_at"))
                if seconds is not None and step.get("name"):
                    step_seconds.append((step["name"], seconds))
    else:
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("duration_s"), (int, float)) and step.get("name"):
                step_seconds.append((step["name"], float(step["duration_s"])))
    duration = row.get("duration_s")
    total = timing["total_s"] if timing["total_s"] is not None else (float(duration) if duration else None)
    cause = phases.failure_cause(conclusion, row.get("failed_step"), timeline)
    run_id = str(row.get("run_id") or row.get("id") or "")
    attempt = str(row.get("run_attempt") or 1)
    trigger = phases.trigger_key(row.get("event"), workflow)
    pr = phases.pr_number(row.get("pr_display"), workflow)
    repo = row.get("repo") or ""
    github_url = f"https://github.com/{ORG}/{repo}/actions/runs/{run_id}" if repo and run_id else (row.get("url") or "")
    capture_id = ""
    for candidate, capture_attempt, capture_suite in captures.get(run_id, []):
        if capture_attempt in ("", attempt) and capture_suite in ("", suite):
            capture_id = candidate
            break
    created = phases.parse_time(row.get("created_at"))
    updated = phases.parse_time(row.get("updated_at"))
    details = github_url
    if capture_id:
        details = f"{RUN_PAGE}?orgId=1&var-capture_id={quote(capture_id, safe='')}"
        if created and updated:
            details += f"&from={int(created.timestamp() * 1000)}&to={int(updated.timestamp() * 1000)}"
    return {
        "id": row.get("id"),
        "run_id": run_id,
        "attempt": int(attempt) if attempt.isdigit() else 1,
        "repo": repo,
        "suite": suite,
        "suite_label": phases.suite_label(suite),
        "trigger": trigger,
        "trigger_label": phases.TRIGGER_LABELS.get(trigger, trigger),
        "workflow": workflow,
        "pr": pr,
        "sha": phases.tested_sha(workflow, row.get("head_sha")),
        "created_at": row.get("created_at") or "",
        "created": created,
        "day": (row.get("created_at") or "")[:10],
        "finished_at": row.get("updated_at") or "",
        "conclusion": conclusion,
        "result": phases.result_key(conclusion),
        "total_s": total,
        "queue_s": timing["queue_s"],
        "phases": timing["phases"] if timing["measured"] else None,
        "timing_source": timing["source"] if timing["measured"] else "",
        "cause": cause["cause"],
        "cause_label": cause["label"],
        "area": cause["area"],
        "failed_step": cause["step"],
        "failed_phase": cause["phase"],
        "capture_id": capture_id,
        "github_url": github_url,
        "details_url": details,
        "pr_url": f"{PR_PAGE}?var-pr_number={pr}&var-pr_repo={quote(repo, safe='')}" if pr else "",
        "_steps": step_seconds,
    }


# -- the documents ---------------------------------------------------------------

class CIObserveInsights:
    """The insights endpoints. Built once per exporter; reads the stores on
    every uncached request."""

    KINDS = ("health", "time", "failures", "tests", "test", "waits")

    def __init__(self, jobs_db, trends_db, clock=None):
        self.stores = Stores(jobs_db, trends_db)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache = {}
        self._inflight = {}
        self._lock = threading.Lock()

    # -- cache -------------------------------------------------------------
    def get_json(self, kind, params):
        if kind not in self.KINDS:
            return {"notes": [f"unknown document {kind!r}"]}
        now = self._clock()
        filters = Filters(params, now)
        extra = tuple(sorted(
            (name, _param(params, name)) for name in ("test", "q", "sort", "limit") if _param(params, name)
        ))
        key = (kind, filters.key(), extra)
        while True:
            with self._lock:
                entry = self._cache.get(key)
                if entry and entry[0] > time.monotonic():
                    return entry[1]
                event = self._inflight.get(key)
                leader = event is None
                if leader:
                    event = threading.Event()
                    self._inflight[key] = event
            if not leader:
                event.wait(timeout=60)
                continue
            try:
                document = getattr(self, f"_{kind}")(filters, params)
                document.setdefault("notes", [])
                document["notes"] = filters.notes + document["notes"]
                document["window"] = [filters.window()]
                document["generated_at"] = iso(now)
                # The one row a page's "About these numbers" panel reads: what
                # is counted, over which window, and what is missing.
                document["about"] = [{
                    "population": document.get("population", ""),
                    "window": (
                        f"{filters.window_from:%Y-%m-%d %H:%M} to {filters.window_to:%Y-%m-%d %H:%M} UTC"
                    ),
                    "missing": "; ".join(document["notes"]) or "Nothing is missing in this window.",
                    "generated_at": document["generated_at"],
                }]
                with self._lock:
                    self._cache[key] = (time.monotonic() + CACHE_SECONDS, document)
                    while len(self._cache) > CACHE_MAX_ENTRIES:
                        self._cache.pop(next(iter(self._cache)))
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                event.set()
            return document

    # -- facts -------------------------------------------------------------
    def _facts(self, filters, since=None, until=None):
        since = since or filters.previous_from
        until = until or filters.window_to
        rows, notes = self.stores.suite_run_rows(since, until)
        captures = self.stores.captures()
        facts = []
        for row in rows:
            fact = suite_run_fact(row, captures)
            if fact is not None and filters.matches(fact):
                facts.append(fact)
        facts.sort(key=lambda fact: (fact["created_at"], str(fact["id"])))
        current = [fact for fact in facts if fact["created"] and fact["created"] >= filters.window_from]
        previous = [fact for fact in facts if fact["created"] and fact["created"] < filters.window_from]
        return current, previous, notes

    @staticmethod
    def _finished(facts):
        return [fact for fact in facts if fact["result"] in ("passed", "failed", "cancelled")]

    @staticmethod
    def _rates(facts):
        passed = sum(1 for fact in facts if fact["result"] == "passed")
        failed = sum(1 for fact in facts if fact["result"] == "failed")
        cancelled = sum(1 for fact in facts if fact["result"] == "cancelled")
        passing_times = [fact["total_s"] for fact in facts if fact["result"] == "passed"]
        return {
            "runs": passed + failed + cancelled,
            "passed": passed,
            "failed": failed,
            "cancelled": cancelled,
            "pass_rate": ratio(passed, passed + failed),
            "median_result_s": percentile(passing_times, 0.5),
            "p90_result_s": percentile(passing_times, 0.9),
            "median_queue_s": percentile([fact["queue_s"] for fact in facts], 0.5),
            "queue_measured": sum(1 for fact in facts if fact["queue_s"] is not None),
        }

    def _test_rows(self, filters, since=None, columns="*"):
        """test_runs rows in the window (and the previous one), filtered."""
        since = since or filters.previous_from
        where = "day >= ?"
        args = [since.strftime("%Y-%m-%d")]
        rows = self.stores.trends_rows("test_runs", where, args, columns)
        if rows is None:
            return None
        out = []
        for row in rows:
            suite = row.get("suite_key") or phases.suite_key(row.get("workflow")) or ""
            if suite not in filters.suites:
                continue
            if filters.repo and row.get("repo") != filters.repo:
                continue
            trigger = row.get("trigger") or phases.trigger_key("", row.get("workflow"))
            if filters.trigger != "all" and trigger != filters.trigger:
                continue
            row["suite_key"] = suite
            row["trigger"] = trigger
            row["test_key"] = row.get("test_key") or phases.canonical_test_key(row.get("test_id"))
            row["created"] = phases.parse_time(row.get("created_at"))
            out.append(row)
        return out

    # -- health ------------------------------------------------------------
    def _health(self, filters, params):
        current, previous, notes = self._facts(filters)
        finished = self._finished(current)
        rates = self._rates(finished)
        previous_rates = self._rates(self._finished(previous)) if filters.has_previous else {}
        failed = [fact for fact in finished if fact["result"] == "failed"]
        by_area = defaultdict(int)
        for fact in failed:
            by_area[fact["area"]] += 1
        test_rows = self._test_rows(filters, since=filters.window_from, columns=(
            "test_key", "test_id", "suite_key", "workflow", "repo", "trigger", "passed", "source_sha", "day",
            "created_at"))
        flaky, failing = 0, 0
        if test_rows is not None:
            outcomes = defaultdict(lambda: defaultdict(set))
            failing_keys = set()
            for row in test_rows:
                outcomes[(row["suite_key"], row["test_key"])][row.get("source_sha") or ""].add(bool(row["passed"]))
                if not row["passed"]:
                    failing_keys.add((row["suite_key"], row["test_key"]))
            flaky = sum(
                1 for shas in outcomes.values()
                if any(sha and len(results) == 2 for sha, results in shas.items())
            )
            failing = len(failing_keys)
        else:
            notes.append("no per-test rollup is stored yet, so flaky and failing test counts are not measured")
        runner_seconds = sum(fact["total_s"] or 0 for fact in finished)
        failed_seconds = sum(fact["total_s"] or 0 for fact in failed)
        tile = {
            **rates,
            "pass_rate_prev": previous_rates.get("pass_rate"),
            "pass_rate_delta": (
                round(rates["pass_rate"] - previous_rates["pass_rate"], 4)
                if rates["pass_rate"] is not None and previous_rates.get("pass_rate") is not None else None
            ),
            "median_result_prev_s": previous_rates.get("median_result_s"),
            "median_result_delta": change(rates["median_result_s"], previous_rates.get("median_result_s")),
            "failures_tests": by_area["Tests"],
            "failures_build": by_area["Build"],
            "failures_install": by_area["OSAC install"],
            "failures_infra": by_area["CI infrastructure"],
            "failures_blocked": by_area["Blocked"],
            "infra_share": ratio(by_area["CI infrastructure"], len(failed)),
            "tests_share": ratio(by_area["Tests"], len(failed)),
            "reruns": sum(1 for fact in finished if fact["attempt"] > 1),
            "runner_hours": round(runner_seconds / 3600, 1),
            "failed_runner_hours": round(failed_seconds / 3600, 1),
            "failed_runner_share": ratio(failed_seconds, runner_seconds),
            "flaky_tests": flaky if test_rows is not None else None,
            "failing_tests": failing if test_rows is not None else None,
            "timed_runs": sum(1 for fact in finished if fact["timing_source"] == "timeline"),
        }
        suites = []
        for suite in (*filters.suites, "all") if filters.suite == "all" else filters.suites:
            group = [fact for fact in finished if suite == "all" or fact["suite"] == suite]
            previous_group = [fact for fact in self._finished(previous) if suite == "all" or fact["suite"] == suite]
            group_rates = self._rates(group)
            previous_group_rates = self._rates(previous_group) if filters.has_previous else {}
            group_failed = [fact for fact in group if fact["result"] == "failed"]
            causes = defaultdict(int)
            for fact in group_failed:
                causes[fact["cause"]] += 1
            top = max(causes.items(), key=lambda item: item[1]) if causes else ("", 0)
            suites.append({
                "suite": suite,
                "suite_label": phases.suite_label(suite),
                **group_rates,
                "pass_rate_prev": previous_group_rates.get("pass_rate"),
                "pass_rate_delta": (
                    round(group_rates["pass_rate"] - previous_group_rates["pass_rate"], 4)
                    if group_rates["pass_rate"] is not None and previous_group_rates.get("pass_rate") is not None
                    else None
                ),
                "median_result_prev_s": previous_group_rates.get("median_result_s"),
                "median_result_delta": change(
                    group_rates["median_result_s"], previous_group_rates.get("median_result_s")),
                "top_cause": phases.CAUSE_LABELS.get(top[0], "") if top[0] else "",
                "top_cause_share": ratio(top[1], len(group_failed)),
                "time_url": suite_page(TIME_PAGE, suite, filters),
                "failures_url": suite_page(FAILURES_PAGE, suite, filters),
            })
        daily = self._daily_health(finished, filters)
        return {
            "population": (
                f"VMaaS, CaaS and BMaaS suite runs created in the last {filters.days} days "
                "(full-install, pull-request and merge-queue suite runs); GitHub owns pass or fail"
            ),
            "tiles": [tile],
            "suites": suites,
            "daily": daily,
            "causes": self._cause_rows(finished, filters),
            "phases": self._phase_rows(finished, filters),
            "recent_failures": self._failed_runs(failed, limit=15),
            "notes": notes + self._coverage_notes(finished),
        }

    def _daily_health(self, finished, filters):
        days = defaultdict(list)
        for fact in finished:
            days[fact["day"]].append(fact)
        out = []
        for day in sorted(days):
            group = days[day]
            row = {"day": day, **{key: value for key, value in self._rates(group).items() if key != "queue_measured"}}
            for suite in phases.SUITES:
                suite_group = [fact for fact in group if fact["suite"] == suite]
                suite_rates = self._rates(suite_group)
                row[f"{suite}_pass_rate"] = suite_rates["pass_rate"]
                row[f"{suite}_median_s"] = suite_rates["median_result_s"]
                row[f"{suite}_runs"] = suite_rates["runs"]
            out.append(row)
        return out

    def _cause_rows(self, finished, filters):
        """One row per suite (and all), one count column per cause."""
        rows = []
        for suite in (*filters.suites, "all") if filters.suite == "all" else filters.suites:
            group = [
                fact for fact in finished
                if fact["result"] == "failed" and (suite == "all" or fact["suite"] == suite)
            ]
            row = {"category": phases.suite_label(suite), "suite": suite, "failed": len(group)}
            for cause in phases.CAUSE_KEYS:
                row[cause] = sum(1 for fact in group if fact["cause"] == cause)
            for area, key in phases.AREA_KEYS.items():
                row[key] = sum(1 for fact in group if fact["area"] == area)
            rows.append(row)
        return rows

    def _phase_rows(self, finished, filters, result="passed"):
        """One row per suite (and all): the mean seconds a passing run spent
        in each phase, so the segments add up to the mean run."""
        rows = []
        for suite in (*filters.suites, "all") if filters.suite == "all" else filters.suites:
            group = [
                fact for fact in finished
                if fact["phases"] and (result == "all" or fact["result"] == result)
                and (suite == "all" or fact["suite"] == suite)
            ]
            row = {"category": phases.suite_label(suite), "suite": suite, "runs": len(group)}
            for phase in phases.PHASE_KEYS:
                values = [fact["phases"][phase] for fact in group]
                if phase == "queue":
                    values = [fact["queue_s"] for fact in group if fact["queue_s"] is not None]
                row[phase] = mean(values)
            rows.append(row)
        return rows

    @staticmethod
    def _coverage_notes(finished):
        notes = []
        untimed = sum(1 for fact in finished if fact["timing_source"] != "timeline")
        if finished and untimed:
            notes.append(
                f"{untimed} of {len(finished)} runs were stored before job timelines existed: their phases come "
                "from step durations and their wait for a runner is not measured"
            )
        return notes

    def _failed_runs(self, failed, limit=100, test_failures=None):
        rows = []
        for fact in sorted(failed, key=lambda fact: fact["created_at"], reverse=True)[:limit]:
            tests = (test_failures or {}).get((fact["run_id"], fact["suite"]), [])
            rows.append({
                "created_at": fact["created_at"],
                "suite": fact["suite"],
                "suite_label": fact["suite_label"],
                "trigger_label": fact["trigger_label"],
                "repo": fact["repo"],
                "pr": f"#{fact['pr']}" if fact["pr"] else "",
                "pr_url": fact["pr_url"],
                "cause": fact["cause"],
                "cause_label": fact["cause_label"],
                "area": fact["area"],
                "failed_step": fact["failed_step"],
                "failed_tests": len(tests),
                "first_failed_test": tests[0]["name"] if tests else "",
                "message": tests[0]["message"] if tests else "",
                "duration_s": fact["total_s"],
                "run": fact["run_id"],
                "details_url": fact["details_url"],
                "github_url": fact["github_url"],
            })
        return rows

    # -- time --------------------------------------------------------------
    def _time(self, filters, params):
        current, previous, notes = self._facts(filters)
        finished = self._finished(current)
        previous_finished = self._finished(previous)
        passed = [fact for fact in finished if fact["result"] == "passed" and fact["phases"]]
        previous_passed = [fact for fact in previous_finished if fact["result"] == "passed" and fact["phases"]]
        phase_table = []
        total_mean = mean([fact["total_s"] for fact in passed])
        for phase in phases.PHASE_KEYS:
            if phase == "queue":
                values = [fact["queue_s"] for fact in passed if fact["queue_s"] is not None]
                previous_values = [fact["queue_s"] for fact in previous_passed if fact["queue_s"] is not None]
            else:
                values = [fact["phases"][phase] for fact in passed]
                previous_values = [fact["phases"][phase] for fact in previous_passed]
            phase_mean = mean(values)
            phase_table.append({
                "phase": phase,
                "label": phases.PHASE_LABELS[phase],
                "mean_s": phase_mean,
                "median_s": percentile(values, 0.5),
                "p90_s": percentile(values, 0.9),
                "share": ratio(phase_mean or 0, total_mean) if phase_mean is not None else None,
                "mean_prev_s": mean(previous_values) if filters.has_previous else None,
                "delta": change(phase_mean, mean(previous_values)) if filters.has_previous else None,
                "runs": len(values),
            })
        phase_daily = []
        by_day = defaultdict(list)
        for fact in passed:
            by_day[fact["day"]].append(fact)
        for day in sorted(by_day):
            group = by_day[day]
            row = {"day": day, "runs": len(group)}
            for phase in phases.PHASE_KEYS:
                if phase == "queue":
                    row[phase] = mean([fact["queue_s"] for fact in group if fact["queue_s"] is not None])
                else:
                    row[phase] = mean([fact["phases"][phase] for fact in group])
            row["total_s"] = mean([fact["total_s"] for fact in group])
            phase_daily.append(row)
        queue_daily = []
        by_day = defaultdict(list)
        for fact in finished:
            if fact["queue_s"] is not None:
                by_day[fact["day"]].append(fact["queue_s"])
        for day in sorted(by_day):
            queue_daily.append({
                "day": day,
                "median_s": percentile(by_day[day], 0.5),
                "p90_s": percentile(by_day[day], 0.9),
                "runs": len(by_day[day]),
            })
        total_seconds = sum(fact["total_s"] or 0 for fact in finished)
        totals = {
            "runs": len(finished),
            "passing_runs": len(passed),
            "runner_hours": round(total_seconds / 3600, 1),
            "failed_hours": round(sum(f["total_s"] or 0 for f in finished if f["result"] == "failed") / 3600, 1),
            "cancelled_hours": round(
                sum(f["total_s"] or 0 for f in finished if f["result"] == "cancelled") / 3600, 1),
            "mean_result_s": total_mean,
            "median_result_s": percentile([fact["total_s"] for fact in passed], 0.5),
            "p90_result_s": percentile([fact["total_s"] for fact in passed], 0.9),
            "median_queue_s": percentile([fact["queue_s"] for fact in finished], 0.5),
            "p90_queue_s": percentile([fact["queue_s"] for fact in finished], 0.9),
        }
        totals["failed_share"] = ratio(totals["failed_hours"], totals["runner_hours"])
        return {
            "population": (
                f"passing VMaaS, CaaS and BMaaS suite runs in the last {filters.days} days for the phases and "
                "steps; every finished run for the runner wait and the hours"
            ),
            "phases": self._phase_rows(finished, filters),
            "phase_table": phase_table,
            "phase_daily": phase_daily,
            "queue_daily": queue_daily,
            "steps": self._step_rows(passed, previous_passed, filters),
            "totals": [totals],
            "notes": notes + self._coverage_notes(finished),
        }

    def _step_rows(self, passed, previous_passed, filters):
        """Every step a passing run executed: how long it takes and how much
        of the run it is. Steps of a timeline carry their job; old rows only
        their name."""
        def collect(facts):
            durations = defaultdict(list)
            for fact in facts:
                seen = defaultdict(float)
                for step, seconds in self._fact_steps(fact):
                    seen[step] += seconds
                for step, seconds in seen.items():
                    durations[step].append(seconds)
            return durations

        current = collect(passed)
        before = collect(previous_passed)
        run_mean = mean([fact["total_s"] for fact in passed]) or 0
        rows = []
        for step, values in current.items():
            step_mean = mean(values)
            previous_median = percentile(before.get(step, []), 0.5) if filters.has_previous else None
            median = percentile(values, 0.5)
            phase = phases.step_phase(step)
            rows.append({
                "step": step,
                "phase": phase,
                "phase_label": phases.PHASE_LABELS[phase],
                "runs": len(values),
                "median_s": median,
                "p90_s": percentile(values, 0.9),
                "mean_s": step_mean,
                "share": ratio((step_mean or 0) * len(values) / max(len(passed), 1), run_mean),
                "median_prev_s": previous_median,
                "delta": change(median, previous_median),
                "delta_s": round(median - previous_median, 1) if median is not None and previous_median else None,
            })
        rows.sort(key=lambda row: -((row["mean_s"] or 0) * row["runs"]))
        return rows

    @staticmethod
    def _fact_steps(fact):
        """(step name, seconds) of one run: from its timeline when stored,
        else from the stored step durations."""
        return fact.get("_steps", [])

    # -- failures ------------------------------------------------------------
    def _failures(self, filters, params):
        current, previous, notes = self._facts(filters)
        finished = self._finished(current)
        failed = [fact for fact in finished if fact["result"] == "failed"]
        previous_failed = [fact for fact in self._finished(previous) if fact["result"] == "failed"]
        test_rows = self._test_rows(filters) or []
        window_day = filters.window_from.strftime("%Y-%m-%d")
        current_tests = [row for row in test_rows if row["day"] >= window_day]
        failures_by_run = defaultdict(list)
        for row in current_tests:
            if not row["passed"]:
                failures_by_run[(str(row["run_id"]), row["suite_key"])].append({
                    "name": phases.describe_test(row["test_key"])["name"],
                    "message": row.get("message") or "",
                })
        by_area = defaultdict(int)
        for fact in failed:
            by_area[fact["area"]] += 1
        causes = []
        for cause in phases.CAUSE_KEYS:
            count = sum(1 for fact in failed if fact["cause"] == cause)
            before = sum(1 for fact in previous_failed if fact["cause"] == cause)
            if not count and not before:
                continue
            causes.append({
                "cause": cause,
                "label": phases.CAUSE_LABELS[cause],
                "area": phases.CAUSE_AREAS[cause],
                "failures": count,
                "share": ratio(count, len(failed)),
                "previous": before if filters.has_previous else None,
                "delta": count - before if filters.has_previous else None,
            })
        causes.sort(key=lambda row: -row["failures"])
        daily = []
        by_day = defaultdict(list)
        for fact in finished:
            by_day[fact["day"]].append(fact)
        for day in sorted(by_day):
            row = {"day": day, "passed": sum(1 for fact in by_day[day] if fact["result"] == "passed")}
            day_failed = [fact for fact in by_day[day] if fact["result"] == "failed"]
            row["failed"] = len(day_failed)
            for cause in phases.CAUSE_KEYS:
                row[cause] = sum(1 for fact in day_failed if fact["cause"] == cause)
            for area, key in phases.AREA_KEYS.items():
                row[key] = sum(1 for fact in day_failed if fact["area"] == area)
            daily.append(row)
        steps = defaultdict(list)
        for fact in failed:
            if fact["failed_step"]:
                steps[fact["failed_step"]].append(fact)
        step_rows = []
        for step, group in steps.items():
            latest = max(group, key=lambda fact: fact["created_at"])
            step_rows.append({
                "step": step,
                "phase_label": phases.PHASE_LABELS.get(phases.step_phase(step), ""),
                "cause_label": latest["cause_label"],
                "failures": len(group),
                "share": ratio(len(group), len(failed)),
                "suites": ", ".join(sorted({fact["suite_label"] for fact in group})),
                "last_failed_at": latest["created_at"],
                "last_run_url": latest["details_url"],
            })
        step_rows.sort(key=lambda row: (-row["failures"], row["step"]))
        tile = {
            "runs": len(finished),
            "failed": len(failed),
            "failure_rate": ratio(len(failed), sum(1 for fact in finished if fact["result"] in ("passed", "failed"))),
            "cancelled": sum(1 for fact in finished if fact["result"] == "cancelled"),
            "tests": by_area["Tests"],
            "build": by_area["Build"],
            "install": by_area["OSAC install"],
            "infra": by_area["CI infrastructure"],
            "blocked": by_area["Blocked"],
            "failed_prev": len(previous_failed) if filters.has_previous else None,
        }
        tests = self._test_summaries(test_rows, filters)
        failing = [row for row in tests if row["failed"]]
        failing.sort(key=lambda row: (-row["failed"], -(row["fail_rate"] or 0), row["test"]))
        messages = defaultdict(lambda: {"count": 0, "tests": set(), "last_seen": "", "example": "", "run": ""})
        for row in current_tests:
            if row["passed"] or not row.get("message"):
                continue
            signature = message_signature(row["message"])
            entry = messages[signature]
            entry["count"] += 1
            entry["tests"].add(row["test_key"])
            if (row.get("created_at") or "") > entry["last_seen"]:
                entry["last_seen"] = row.get("created_at") or ""
                entry["example"] = row["message"]
                entry["run"] = str(row["run_id"])
        message_rows = [
            {
                "message": signature,
                "example": entry["example"],
                "count": entry["count"],
                "tests": len(entry["tests"]),
                "last_seen": entry["last_seen"],
                "run": entry["run"],
            }
            for signature, entry in messages.items()
        ]
        message_rows.sort(key=lambda row: (-row["count"], row["message"]))
        if not test_rows:
            notes.append("no per-test rollup is stored for this window, so failing tests and messages are empty")
        return {
            "population": (
                f"failed VMaaS, CaaS and BMaaS suite runs in the last {filters.days} days; a run's cause is the "
                "phase of its first failed step"
            ),
            "tiles": [tile],
            "by_suite": self._cause_rows(finished, filters),
            "daily": daily,
            "causes": causes,
            "steps": step_rows,
            "runs": self._failed_runs(failed, limit=100, test_failures=failures_by_run),
            "tests": failing[:100],
            "flaky": sorted(
                (row for row in tests if row["flaky_shas"]),
                key=lambda row: (-row["flaky_shas"], -row["failed"], row["test"]),
            )[:100],
            "messages": message_rows[:50],
            "notes": notes,
        }

    # -- tests -------------------------------------------------------------
    def _test_summaries(self, rows, filters):
        """One row per (suite, test) over the window, with the previous
        window's median for the trend."""
        window_day = filters.window_from.strftime("%Y-%m-%d")
        groups = defaultdict(lambda: {"current": [], "previous": []})
        for row in rows:
            bucket = "current" if row["day"] >= window_day else "previous"
            groups[(row["suite_key"], row["test_key"])][bucket].append(row)
        total_time = sum(
            row["duration_s"] or 0 for group in groups.values() for row in group["current"]
        )
        summaries = []
        for (suite, key), group in groups.items():
            current = group["current"]
            if not current:
                continue
            durations = [row["duration_s"] for row in current]
            failures = [row for row in current if not row["passed"]]
            shas = defaultdict(set)
            for row in current:
                if row.get("source_sha"):
                    shas[row["source_sha"]].add(bool(row["passed"]))
            median = percentile(durations, 0.5)
            previous_median = percentile([row["duration_s"] for row in group["previous"]], 0.5)
            last_failure = max(failures, key=lambda row: row.get("created_at") or "") if failures else None
            labels = phases.describe_test(key)
            summaries.append({
                "test_key": key,
                "test": labels["name"],
                "group": labels["group"],
                "suite": suite,
                "suite_label": phases.suite_label(suite),
                "runs": len(current),
                "passed": len(current) - len(failures),
                "failed": len(failures),
                "fail_rate": ratio(len(failures), len(current)),
                "flaky_shas": sum(1 for results in shas.values() if len(results) == 2),
                "median_s": median,
                "p90_s": percentile(durations, 0.9),
                "mean_s": mean(durations),
                "total_s": round(sum(value or 0 for value in durations), 1),
                "share": ratio(sum(value or 0 for value in durations), total_time),
                "median_prev_s": previous_median if filters.has_previous else None,
                "delta": change(median, previous_median) if filters.has_previous else None,
                "last_failed_at": (last_failure or {}).get("created_at") or "",
                "last_message": (last_failure or {}).get("message") or "",
                "test_url": test_url(key, filters.days, suite),
            })
        return summaries

    def _tests(self, filters, params):
        rows = self._test_rows(filters)
        if rows is None:
            return {"tests": [], "options": [], "notes": ["no per-test rollup has been written yet"]}
        tests = self._test_summaries(rows, filters)
        query = _param(params, "q").strip().lower()
        if query:
            tests = [row for row in tests if query in row["test_key"].lower() or query in row["test"].lower()]
        sort = _param(params, "sort", "time").strip().lower()
        orders = {
            "time": lambda row: (-(row["total_s"] or 0), row["test"]),
            "failures": lambda row: (-row["failed"], -(row["fail_rate"] or 0), row["test"]),
            "flaky": lambda row: (-row["flaky_shas"], -row["failed"], row["test"]),
            "slower": lambda row: (-(row["delta"] if row["delta"] is not None else -9), row["test"]),
            "slowest": lambda row: (-(row["median_s"] or 0), row["test"]),
            "name": lambda row: (row["group"], row["test"]),
        }
        tests.sort(key=orders.get(sort, orders["time"]))
        try:
            limit = max(1, min(int(_param(params, "limit", "500")), 2000))
        except ValueError:
            limit = 500
        options = [
            {"text": f"{row['suite_label']} · {row['group']} · {row['test']}", "value": row["test_key"]}
            for row in sorted(tests, key=orders["time"])
        ]
        return {
            "population": (
                f"tests of VMaaS, CaaS and BMaaS suite runs in the last {filters.days} days, from the JUnit "
                "artifacts; a skipped test is not a run"
            ),
            "tests": tests[:limit],
            "options": options,
            "notes": [] if tests else ["no test matches this window and filter"],
        }

    def _resolve_test(self, rows, wanted):
        """The canonical key the page asked for: exact, then by its canonical
        form, then the one test whose key contains the text."""
        keys = {row["test_key"] for row in rows}
        if wanted in keys:
            return wanted, ""
        canonical = phases.canonical_test_key(wanted)
        if canonical in keys:
            return canonical, ""
        matches = sorted(key for key in keys if wanted.lower() in key.lower())
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            return matches[0], f"{len(matches)} tests contain {wanted!r}; showing {matches[0]}"
        return "", f"no test in this window matches {wanted!r}"

    def _test(self, filters, params):
        wanted = _param(params, "test").strip()
        empty = {
            "population": (
                f"runs of one test in the VMaaS, CaaS and BMaaS suite runs of the last {filters.days} days, from "
                "the JUnit reports; where its time goes, from the recorded runs' traces"
            ),
            "summary": [], "runs": [], "daily": [], "breakdown": [], "breakdown_daily": [], "waits": [],
            "fixtures": [], "messages": [],
        }
        rows = self._test_rows(filters)
        if rows is None:
            return {**empty, "notes": ["no per-test rollup has been written yet"]}
        if not wanted:
            return {**empty, "notes": ["choose a test above"]}
        key, note = self._resolve_test(rows, wanted)
        if not key:
            return {**empty, "notes": [note]}
        notes = [note] if note else []
        mine = [row for row in rows if row["test_key"] == key]
        window_day = filters.window_from.strftime("%Y-%m-%d")
        current = [row for row in mine if row["day"] >= window_day]
        summary = self._test_summaries(mine, filters)
        captures = self.stores.captures()
        runs = []
        for row in sorted(current, key=lambda row: row.get("created_at") or ""):
            run_id = str(row["run_id"])
            capture = next((cid for cid, _, suite in captures.get(run_id, []) if suite in ("", row["suite_key"])), "")
            github = f"https://github.com/{ORG}/{row['repo']}/actions/runs/{run_id}"
            runs.append({
                "time": row.get("created_at") or "",
                "duration_s": row["duration_s"],
                "passed_s": row["duration_s"] if row["passed"] else None,
                "failed_s": None if row["passed"] else row["duration_s"],
                "result": "passed" if row["passed"] else "failed",
                "run": run_id,
                "sha": row.get("source_sha") or "",
                "short_sha": (row.get("source_sha") or "")[:10],
                "suite_label": phases.suite_label(row["suite_key"]),
                "trigger_label": phases.TRIGGER_LABELS.get(row["trigger"], row["trigger"]),
                "pr": f"#{row['pr']}" if row.get("pr") else "",
                "message": row.get("message") or "",
                "details_url": (
                    f"{RUN_PAGE}?orgId=1&var-capture_id={quote(capture, safe='')}" if capture else github
                ),
                "github_url": github,
            })
        daily = []
        by_day = defaultdict(list)
        for row in current:
            by_day[row["day"]].append(row)
        for day in sorted(by_day):
            group = by_day[day]
            daily.append({
                "day": day,
                "median_s": percentile([row["duration_s"] for row in group], 0.5),
                "p90_s": percentile([row["duration_s"] for row in group], 0.9),
                "runs": len(group),
                "passes": sum(1 for row in group if row["passed"]),
                "failures": sum(1 for row in group if not row["passed"]),
            })
        messages = defaultdict(lambda: {"count": 0, "last_seen": "", "example": "", "run": ""})
        for row in current:
            if row["passed"] or not row.get("message"):
                continue
            entry = messages[message_signature(row["message"])]
            entry["count"] += 1
            if (row.get("created_at") or "") >= entry["last_seen"]:
                entry["last_seen"] = row.get("created_at") or ""
                entry["example"] = row["message"]
                entry["run"] = str(row["run_id"])
        message_rows = sorted(
            ({"message": signature, **entry} for signature, entry in messages.items()),
            key=lambda row: (-row["count"], row["message"]),
        )
        spans = self._spans_for(filters, key)
        return {
            "population": empty["population"],
            "test_key": key,
            "summary": [{
                **(summary[0] if summary else {}),
                "recorded_runs": spans["recorded_runs"],
            }] if summary else [],
            "runs": runs,
            "daily": daily,
            "breakdown": spans["breakdown"],
            "breakdown_daily": spans["breakdown_daily"],
            "waits": spans["waits"],
            "fixtures": spans["fixtures"],
            "messages": message_rows[:30],
            "notes": notes + spans["notes"],
        }

    def _spans_for(self, filters, key):
        """The span rollup's view of one test in the window."""
        window_day = filters.window_from.strftime("%Y-%m-%d")
        out = {"recorded_runs": 0, "breakdown": [], "breakdown_daily": [], "waits": [], "fixtures": [], "notes": []}
        tests = self.stores.trends_rows("test_spans", "test_key = ? AND day >= ?", [key, window_day])
        if tests is None:
            out["notes"].append("no span rollup has been written yet, so where the test's time goes is not measured")
            return out
        kept = self._span_filter(filters)
        tests = [row for row in tests if kept(row)]
        if not tests:
            out["notes"].append("no recorded run of this test in the window, so its time breakdown is empty")
            return out
        out["recorded_runs"] = len({row["capture_id"] for row in tests})
        split = [time_parts(row) for row in tests]
        out["breakdown"] = [{
            "category": f"Average of {len(split)} recorded run(s)",
            "setup": mean([value[0] for value in split]),
            "waits": mean([value[1] for value in split]),
            "actions": mean([value[2] for value in split]),
            "teardown": mean([value[3] for value in split]),
        }]
        by_day = defaultdict(list)
        for row, value in zip(tests, split, strict=True):
            by_day[row["day"]].append(value)
        out["breakdown_daily"] = [
            {
                "day": day,
                "setup": mean([value[0] for value in values]),
                "waits": mean([value[1] for value in values]),
                "actions": mean([value[2] for value in values]),
                "teardown": mean([value[3] for value in values]),
            }
            for day, values in sorted(by_day.items())
        ]
        captures = {row["capture_id"] for row in tests}
        waits = self.stores.trends_rows("wait_spans", "test_key = ? AND day >= ?", [key, window_day]) or []
        waits = [row for row in waits if row["capture_id"] in captures]
        grouped = defaultdict(list)
        for row in waits:
            grouped[(row["signature"], row["resource_kind"])].append(row)
        test_time = sum(row.get("duration_s") or 0 for row in tests)
        for (signature, kind), group in grouped.items():
            durations = [row["duration_s"] for row in group]
            out["waits"].append({
                "wait": signature,
                "resource_kind": kind,
                "count": len(group),
                "per_run": round(len(group) / len(captures), 2),
                "median_s": percentile(durations, 0.5),
                "p90_s": percentile(durations, 0.9),
                "max_s": max((value for value in durations if value is not None), default=None),
                "total_s": round(sum(value or 0 for value in durations), 1),
                "share": ratio(sum(value or 0 for value in durations), test_time),
                "timeouts": sum(1 for row in group if row.get("failed")),
                "phase": ", ".join(sorted({row["phase"] for row in group if row["phase"]})),
            })
        out["waits"].sort(key=lambda row: -row["total_s"])
        fixtures = self.stores.trends_rows("fixture_spans", "test_key = ? AND day >= ?", [key, window_day]) or []
        grouped = defaultdict(list)
        for row in fixtures:
            if row["capture_id"] in captures:
                grouped[(row["fixture"], row["scope"])].append(row["duration_s"])
        out["fixtures"] = sorted(
            (
                {
                    "fixture": fixture,
                    "scope": scope,
                    "setups": len(values),
                    "median_s": percentile(values, 0.5),
                    "p90_s": percentile(values, 0.9),
                    "total_s": round(sum(value or 0 for value in values), 1),
                }
                for (fixture, scope), values in grouped.items()
            ),
            key=lambda row: -row["total_s"],
        )
        return out

    def _span_filter(self, filters):
        """Whether a span-rollup row is in the filtered population. The
        rollup knows the suite; the repository and the trigger come from the
        suite run with the same run id."""
        if not filters.repo and filters.trigger == "all":
            return lambda row: row.get("suite") in filters.suites
        current, previous, _ = self._facts(filters)
        run_ids = {fact["run_id"] for fact in current + previous}
        return lambda row: row.get("suite") in filters.suites and str(row.get("run_id") or "") in run_ids

    # -- waits -------------------------------------------------------------
    def _waits(self, filters, params):
        since = filters.previous_from.strftime("%Y-%m-%d")
        window_day = filters.window_from.strftime("%Y-%m-%d")
        waits = self.stores.trends_rows("wait_spans", "day >= ?", [since])
        empty = {"kinds": [], "waits": [], "chart": [], "fixtures": [], "coverage": [], "test_time": []}
        if waits is None:
            return {**empty, "notes": ["no span rollup has been written yet, so product waits are not measured"]}
        kept = self._span_filter(filters)
        runs = self.stores.trends_rows("span_runs", "day >= ?", [since]) or []
        runs = [row for row in runs if kept(row)]
        # Only complete traces: an incomplete one would add waits without
        # the test time they belong to.
        complete = {row["capture_id"] for row in runs if row["state"] == "ok"}
        current_runs = {row["capture_id"] for row in runs if row["day"] >= window_day and row["state"] == "ok"}
        tests = self.stores.trends_rows("test_spans", "day >= ?", [window_day]) or []
        tests = [row for row in tests if row["capture_id"] in current_runs]
        test_time = sum(row.get("duration_s") or 0 for row in tests)
        waits = [row for row in waits if row["capture_id"] in complete]
        current = [row for row in waits if row["day"] >= window_day]
        previous = [row for row in waits if row["day"] < window_day]

        def summarize(group_key):
            groups = defaultdict(lambda: {"current": [], "previous": []})
            for row in current:
                groups[group_key(row)]["current"].append(row)
            for row in previous:
                groups[group_key(row)]["previous"].append(row)
            out = []
            for key, group in groups.items():
                if not group["current"]:
                    continue
                durations = [row["duration_s"] for row in group["current"]]
                median = percentile(durations, 0.5)
                previous_median = percentile([row["duration_s"] for row in group["previous"]], 0.5)
                total = sum(value or 0 for value in durations)
                out.append({
                    "key": key,
                    "waits": len(durations),
                    "tests": len({row["test_key"] for row in group["current"] if row["test_key"]}),
                    "median_s": median,
                    "p90_s": percentile(durations, 0.9),
                    "max_s": max((value for value in durations if value is not None), default=None),
                    "total_s": round(total, 1),
                    "share": ratio(total, test_time),
                    "timeouts": sum(1 for row in group["current"] if row.get("failed")),
                    "median_prev_s": previous_median if filters.has_previous else None,
                    "delta": change(median, previous_median) if filters.has_previous else None,
                })
            out.sort(key=lambda row: -row["total_s"])
            return out

        kinds = [
            {"resource_kind": row.pop("key") or "(unnamed)", **row}
            for row in summarize(lambda row: row["resource_kind"])
        ]
        signatures = []
        for row in summarize(lambda row: (row["signature"], row["resource_kind"])):
            signature, kind = row.pop("key")
            signatures.append({"wait": signature, "resource_kind": kind or "(unnamed)", **row})
        fixtures = self.stores.trends_rows("fixture_spans", "day >= ?", [window_day]) or []
        grouped = defaultdict(list)
        for row in fixtures:
            if row["capture_id"] in current_runs and row["scope"] in ("session", "module", "package"):
                grouped[(row["fixture"], row["scope"])].append(row["duration_s"])
        fixture_rows = sorted(
            (
                {
                    "fixture": fixture,
                    "scope": scope,
                    "setups": len(values),
                    "median_s": percentile(values, 0.5),
                    "p90_s": percentile(values, 0.9),
                    "total_s": round(sum(value or 0 for value in values), 1),
                }
                for (fixture, scope), values in grouped.items()
            ),
            key=lambda row: -row["total_s"],
        )
        coverage = {
            "recorded_runs": len(current_runs),
            "incomplete_runs": sum(
                1 for row in runs if row["day"] >= window_day and row["state"] != "ok"
            ),
            "waits": len(current),
            "wait_hours": round(sum(row["duration_s"] or 0 for row in current) / 3600, 1),
            "test_hours": round(test_time / 3600, 1),
        }
        coverage["wait_share"] = ratio(coverage["wait_hours"], coverage["test_hours"])
        notes = []
        if not current_runs:
            notes.append("no recorded run in this window has a complete trace yet")
        return {
            "population": (
                f"poll_until waits of recorded VMaaS, CaaS and BMaaS runs in the last {filters.days} days, from "
                "the span rollup of their traces"
            ),
            "kinds": kinds,
            "waits": signatures[:100],
            "chart": [
                {"category": row["resource_kind"], "hours": round(row["total_s"] / 3600, 2)} for row in kinds[:10]
            ],
            "fixtures": fixture_rows[:30],
            "coverage": [coverage],
            "test_time": self._test_time_rows(tests, filters),
            "notes": notes,
        }

    @staticmethod
    def _test_time_rows(tests, filters):
        """Where the recorded tests' time went, per suite and for all: hours
        of setup, waiting for the product, test actions and teardown. The
        four parts add up to the tests' own time."""
        rows = []
        for suite in (*filters.suites, "all") if filters.suite == "all" else filters.suites:
            group = [row for row in tests if suite == "all" or row.get("suite") == suite]
            split = [time_parts(row) for row in group]
            total = sum(sum(parts) for parts in split)
            rows.append({
                "category": phases.suite_label(suite),
                "suite": suite,
                "tests": len(group),
                "recorded_runs": len({row["capture_id"] for row in group}),
                "setup": round(sum(parts[0] for parts in split) / 3600, 2) if group else None,
                "waits": round(sum(parts[1] for parts in split) / 3600, 2) if group else None,
                "actions": round(sum(parts[2] for parts in split) / 3600, 2) if group else None,
                "teardown": round(sum(parts[3] for parts in split) / 3600, 2) if group else None,
                "wait_share": ratio(sum(parts[1] for parts in split), total),
            })
            for index, part in enumerate(("setup", "waits", "actions", "teardown")):
                rows[-1][f"{part}_share"] = ratio(sum(parts[index] for parts in split), total)
        return rows

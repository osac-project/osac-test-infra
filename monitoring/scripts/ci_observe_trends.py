"""Per-test JUnit rollup and the ci-observe history archive (trends.db).

The exporter stores job and step history, not test history, so a per-test
trend over every CI run needs a second store. This script builds it: it
lists the E2E runs the exporter already knows about, downloads each run's
JUnit artifact from GitHub, and writes one row per test per run
(`test_runs`) plus one row per test per day per source SHA (`test_rollup`)
into a SQLite file of its own, trends.db. The exporter's own DB is opened
read-only and never altered.

Why a rollup and not a live query: GitHub deletes a run's artifacts after
30 days, so a per-test outcome is only readable for a month. The rollup is
the only way to keep it, and trends.db keeps it for CI_OBSERVE_KEEP_DAYS
(90 by default), longer than any GitHub or exporter retention.

The pass is incremental and runs every hour. A run whose JUnit was already
read (`run_fetches`) is not downloaded again: its stored rows are reused
when its day is rebuilt. Only finished runs that were never read, runs read
by an older parser, and runs whose artifact could not be fetched are read.
Rebuilding a day replaces its rows, so rerunning never doubles a day.

    # On the central machine, next to the exporter's data directory:
    set -a; . ~/.monitoring-server/.env; set +a
    CACHE_DIR=~/.monitoring-server/data ./ci_observe_trends.py --days 30

    # Re-read every run of a day and replace it, after a rollup bug:
    CACHE_DIR=~/.monitoring-server/data ./ci_observe_trends.py --day 2026-11-02 --force

One run contributes one artifact set to the rollup. A run whose artifact
has already expired contributes nothing: it is counted in rollup_days as
unavailable, never as a green zero.

Each test row of a run also says which suite artifact it came from
(`suite_key`: vmaas, caas, bmaas), why the run happened (`trigger`), the
pull request (`pr`) and, for a failure, the first line of its JUnit message
(`message`, the bounded message attribute only, never the stack trace).
`test_key` is the test's canonical id, the join key to the span rollup.

The same pass copies the exporter's E2E job rows into `suite_runs`, so the
ci-observe time and failure pages can look back further than the
exporter's 30-day job history.

The same file holds the read-only compare query behind
GET /api/ci-observe/trends/compare (see compare_document): two source SHAs,
one window of days, the tests both revisions ran and how they differ.

Reuse: the bounded artifact download and the zip/JUnit extraction come
from ci_observe_run_evidence.py, imported directly (that module starts no
server). Only the per-testcase walk below is new: the run-evidence parser
aggregates counts per testsuite and caps failed tests, which is right for
one run's page and wrong for a per-test time series.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

# The sibling run-evidence module is not a package: add this directory to
# sys.path the way workflow-exporter.py does before importing it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_observe_phases as phases
from ci_observe_run_evidence import CIObserveRunEvidence

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
# Same environment variable and default as workflow-exporter.py, so both
# resolve to the same data directory and the endpoint finds trends.db
# next to workflow-exporter.db without extra configuration.
CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
EXPORTER_DB = os.path.join(CACHE_DIR, "workflow-exporter.db")
TRENDS_DB = os.path.join(CACHE_DIR, "trends.db")
TOKEN = os.getenv("PRIVATE_GITHUB_TOKEN")

# Artifacts are the only per-test record, and GitHub deletes them after 30
# days. Listing runs older than that would only spend API calls on
# artifacts that are already gone, so the default window is the retention
# window.
ARTIFACT_RETENTION_DAYS = 30
DEFAULT_DAYS = ARTIFACT_RETENTION_DAYS
# How long trends.db keeps what it read. The artifacts are gone after 30
# days, so these rows are the only copy; the archive outlives every other
# store on purpose, so a trend can look back a quarter.
KEEP_DAYS = int(os.getenv("CI_OBSERVE_KEEP_DAYS", "90"))
# The parser version a run was read with. A run read by an older parser is
# read again while its artifact still exists, so new fields fill in.
PARSER_VERSION = 2
# A run with no readable artifact is checked once more when the first check
# came soon after it ended; an artifact can trail the run's completion.
NO_DATA_RECHECK_WITHIN = timedelta(hours=3)
RECHECK_AFTER = timedelta(minutes=50)
# A failure message is one line: the bounded JUnit message attribute, never
# the stack trace, so a reader can group failures by what they say.
MESSAGE_MAX_CHARS = 300
# Terminal color codes survive in a message as "[31m" once the XML hygiene
# has dropped the escape byte; they are noise to a reader and to grouping.
ANSI_CODE_RE = re.compile(r"\x1b?\[[0-9;]*[A-Za-z]")
# One run normally has one artifact; a pull-request-flow run can have one
# per suite that ran. The caps keep a single run from dominating a batch.
MAX_ARTIFACTS_PER_RUN = 4
MAX_JUNIT_FILES = CIObserveRunEvidence.NATIVE_MAX_JUNIT_FILES
# Both row shapes of the exporter's jobs table carry the run id in url: a
# run row ends with /actions/runs/<run id>, a pull-request-flow suite row
# with /actions/runs/<run id>/job/<job id>.
RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")

ROLLUP_COLUMNS = (
    "day", "repo", "workflow", "suite", "test_id", "source_sha",
    "runs", "passes", "failures", "p50_duration_s", "p95_duration_s",
    "p99_duration_s", "mean_duration_s",
)
# Columns added after the first release. CREATE TABLE IF NOT EXISTS does not
# alter an existing table, so open_trends_db adds them with ALTER TABLE. A
# row written before that change keeps NULL there: "not measured", never 0.
ADDED_ROLLUP_COLUMNS = ("p99_duration_s", "mean_duration_s")
# test_runs columns added with the hourly pass. A row stored before keeps
# NULL until its run is read again: unknown, not empty.
ADDED_RUN_COLUMNS = ("suite_key", "test_key", "trigger", "pr", "message")
TEST_RUN_COLUMNS = (
    "day", "repo", "workflow", "suite", "test_id", "source_sha", "run_id",
    "created_at", "duration_s", "passed", *ADDED_RUN_COLUMNS,
)
SUITE_RUN_COLUMNS = (
    "id", "run_id", "repo", "workflow", "branch", "pr_display", "status",
    "conclusion", "event", "trigger", "duration_s", "created_at", "updated_at",
    "run_attempt", "failed_step", "steps_json", "failure_reason", "head_sha",
    "url", "runner_name", "timeline_json",
)
# The artifact a JUnit file came from names the suite: VMaaS uploads
# osac-logs-full-install-<run id>, the others insert their suite.
ARTIFACT_SUITE_RE = re.compile(r"^osac-logs-(?:([a-z0-9-]+)-)?full-install-\d+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS test_rollup (
    day TEXT NOT NULL,
    repo TEXT NOT NULL,
    workflow TEXT NOT NULL,
    suite TEXT NOT NULL,
    test_id TEXT NOT NULL,
    source_sha TEXT NOT NULL,
    runs INTEGER NOT NULL,
    passes INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    p50_duration_s REAL,
    p95_duration_s REAL,
    p99_duration_s REAL,
    mean_duration_s REAL,
    PRIMARY KEY (day, repo, workflow, suite, test_id, source_sha)
);
CREATE INDEX IF NOT EXISTS idx_test_rollup_test ON test_rollup(test_id, day);
CREATE INDEX IF NOT EXISTS idx_test_rollup_day ON test_rollup(day);
-- One row per test per run: the raw durations behind a day's percentiles.
-- The history endpoint needs them for a true window percentile and for the
-- recent-run table, which the daily rollup cannot provide. Replaced with
-- the day's test_rollup rows, so a rerun corrects both together.
CREATE TABLE IF NOT EXISTS test_runs (
    day TEXT NOT NULL,
    repo TEXT NOT NULL,
    workflow TEXT NOT NULL,
    suite TEXT NOT NULL,
    test_id TEXT NOT NULL,
    source_sha TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    duration_s REAL,
    passed INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_runs_test ON test_runs(test_id, day);
CREATE INDEX IF NOT EXISTS idx_test_runs_day ON test_runs(day);
CREATE INDEX IF NOT EXISTS idx_test_runs_run ON test_runs(run_id);
-- One row per run this script tried to read: whether its JUnit was stored,
-- and with which parser version. An hourly pass reads only what is new.
CREATE TABLE IF NOT EXISTS run_fetches (
    run_id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);
-- The exporter's E2E job rows, copied every pass and kept for KEEP_DAYS, so
-- the time and failure pages outlive the exporter's 30-day job history.
CREATE TABLE IF NOT EXISTS suite_runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    workflow TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    pr_display TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    conclusion TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    duration_s INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    run_attempt INTEGER,
    failed_step TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    failure_reason TEXT NOT NULL DEFAULT '',
    head_sha TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    runner_name TEXT NOT NULL DEFAULT '',
    timeline_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_suite_runs_created ON suite_runs(created_at);
-- One row per rolled-up day: how many runs the day had, how many of them
-- still had a readable artifact, and how many did not. A day with
-- runs_unavailable > 0 is a partial day, not a day without failures.
CREATE TABLE IF NOT EXISTS rollup_days (
    day TEXT PRIMARY KEY,
    runs_listed INTEGER NOT NULL,
    runs_parsed INTEGER NOT NULL,
    runs_unavailable INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    -- The run ids whose outcomes are stored for this day. A replacement
    -- must include every one of them; counts are not enough.
    parsed_run_ids TEXT NOT NULL DEFAULT '[]'
);
"""


def percentile(values, fraction):
    """The nearest-rank percentile of values, None for an empty list.

    Nearest rank is used rather than interpolation because with a handful
    of runs a day the interpolated value is a number no run actually
    produced. Durations are seconds, rounded to milliseconds like the rest
    of the monitoring code.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    index = min(max(rank, 1), len(ordered)) - 1
    return round(ordered[index], 3)


def mean_value(values):
    """The arithmetic mean of values, None for an empty list.

    Durations are seconds rounded to milliseconds like the rest of the
    monitoring code.
    """
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def stored_rows(exporter_db, since_day):
    """The exporter's job rows created on or after since_day (YYYY-MM-DD).

    Opened read-only: this script never writes the exporter's DB. created_at
    is stored as an ISO 8601 UTC string ("2026-11-01T04:05:06Z"), so a
    lexicographic comparison against a date prefix selects the window.
    """
    conn = sqlite3.connect(f"file:{exporter_db}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        wanted = ("id", "repo", "workflow", "category", "url", "created_at", "head_sha",
                  "event", "pr_display", "conclusion", "status", "updated_at")
        select = ", ".join(column if column in columns else f"'' AS {column}" for column in wanted)
        rows = conn.execute(
            f"SELECT {select} FROM jobs WHERE created_at >= ? ORDER BY created_at",
            (since_day,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def run_row_score(row, run_id):
    """How well one stored row stands in for the run itself.

    The exporter stores a run's own row with the run id as the row id; a
    suite row carries a job id. A row with a non-empty head_sha beats one
    without. Ordering the stored rows therefore never decides which source
    SHA a run is rolled up under.
    """
    has_sha = 1 if (row.get("head_sha") or "").strip() else 0
    is_run_row = 1 if row.get("id") == run_id else 0
    return is_run_row, has_sha


def candidate_runs(rows):
    """Group the stored job rows into one entry per GitHub run to parse.

    A run is a candidate when at least one of its stored rows has category
    "e2e": the run's own row for a direct E2E workflow, or a pull-request
    flow's suite row. The run-level row is preferred for the workflow
    label, so the two suites of one pull-request run do not become two
    workflows; the source SHA comes from the run-level row when it has one
    (the exporter stores it per row, so a suite row carries a copy).

    An empty head_sha never wins over a non-empty one. The exporter writes
    rows over time: a suite row stored before head_sha existed keeps its
    empty value, and a suite row whose job payload carried no html_url
    stores the run's URL (so it looks like the run's own row). Picking the
    last row per run id let such a row overwrite the run row and blank the
    SHA of every test it contributed, which is how a whole day's rollup
    ended up with source_sha ''.

    Returns dicts with run_id, repo, workflow, day, created_at, source_sha,
    trigger, pr, finished and finished_at, sorted oldest first. Rows with no
    run id in their url are skipped: the URL is the only stored link from a
    suite row to its run. A run is finished when every one of its E2E rows
    has a conclusion; until then it has no artifact to read.
    """
    run_rows = {}
    run_shas = {}
    for row in rows:
        match = RUN_ID_RE.search(row.get("url") or "")
        if not match:
            continue
        run_id = int(match.group(1))
        sha = (row.get("head_sha") or "").strip()
        if sha and not run_shas.get(run_id):
            run_shas[run_id] = sha
        if "/job/" in (row.get("url") or ""):
            continue
        current = run_rows.get(run_id)
        if current is None or run_row_score(row, run_id) > run_row_score(current, run_id):
            run_rows[run_id] = row

    grouped = {}
    for row in rows:
        if (row.get("category") or "") != "e2e":
            continue
        match = RUN_ID_RE.search(row.get("url") or "")
        if not match:
            continue
        run_id = int(match.group(1))
        entry = grouped.setdefault(
            run_id,
            {"run_id": run_id, "repo": "", "workflows": [], "created_at": "", "source_sha": "",
             "pr": "", "finished": True, "finished_at": "", "events": []},
        )
        entry["repo"] = entry["repo"] or (row.get("repo") or "")
        entry["source_sha"] = entry["source_sha"] or (row.get("head_sha") or "").strip()
        created = row.get("created_at") or ""
        if created and (not entry["created_at"] or created < entry["created_at"]):
            entry["created_at"] = created
        if row.get("workflow"):
            entry["workflows"].append(row["workflow"])
        entry["pr"] = entry["pr"] or phases.pr_number(row.get("pr_display"), row.get("workflow"))
        entry["events"].append((row.get("event") or "", row.get("workflow") or ""))
        if not (row.get("conclusion") or "").strip():
            entry["finished"] = False
        updated = row.get("updated_at") or ""
        if updated > entry["finished_at"]:
            entry["finished_at"] = updated

    runs = []
    for run_id, entry in grouped.items():
        parent = run_rows.get(run_id) or {}
        workflows = sorted(set(entry.pop("workflows")))
        if parent.get("workflow"):
            workflow = parent["workflow"]
        elif len(workflows) == 1:
            workflow = workflows[0]
        else:
            # One run, several suite rows, no run-level row stored: the
            # label names them all rather than picking one suite.
            workflow = " + ".join(workflows)
        entry["workflow"] = workflow
        entry["repo"] = entry["repo"] or (parent.get("repo") or "")
        # Why the run happened: the run row says it for a flow run (a merge
        # group, a pull request); a direct run says it itself.
        events = entry.pop("events")
        source = (parent.get("event") or "", parent.get("workflow") or "") if parent else events[0]
        entry["trigger"] = phases.trigger_key(*source)
        if entry["trigger"] == "other" and events:
            entry["trigger"] = phases.trigger_key(*events[0])
        entry["pr"] = entry["pr"] or phases.pr_number(parent.get("pr_display"), parent.get("workflow"))
        # The run row's SHA wins, then any non-empty SHA of the run's own
        # rows, then any non-empty SHA stored anywhere for that run.
        entry["source_sha"] = (
            (parent.get("head_sha") or "").strip()
            or entry["source_sha"]
            or run_shas.get(run_id, "")
        )
        # The day is the run's UTC creation date: the rollup's calendar
        # axis, and the same day the exporter's own dashboards group by.
        entry["day"] = entry["created_at"][:10]
        runs.append(entry)
    return sorted(runs, key=lambda run: (run["created_at"], run["run_id"]))


def failure_message(case):
    """The first line of a failed testcase's message attribute, bounded, or
    "" for a pass. Only the attribute: the stack trace inside the element
    never leaves the artifact, the same rule as the run page."""
    problem = case.find("failure")
    if problem is None:
        problem = case.find("error")
    if problem is None:
        return ""
    text, _ = CIObserveRunEvidence._bound_junit_message(problem.get("message") or "")
    text = ANSI_CODE_RE.sub("", text)
    first = next((" ".join(line.split()) for line in text.splitlines() if line.strip()), "")
    if not first:
        first = (problem.get("type") or problem.tag or "").strip()
    return first[:MESSAGE_MAX_CHARS]


def parse_junit_outcomes(files):
    """One row per test that ran, from a run's JUnit XML files.

    files: [(name, bytes)] as CIObserveRunEvidence._extract_junit_files
    returns them, or [(name, bytes, suite_key)] when the caller knows which
    suite's artifact a file came from. Returns (cases, reason): cases is a
    list of dicts with suite, test_id, passed, duration_s, suite_key and
    message; when a file cannot be parsed at all, cases is None and reason
    says which file and why.

    A testcase holding <failure> or <error> failed; every other testcase
    passed. A <skipped> testcase is left out of both counts: "was not run"
    is neither a pass nor a failure, and counting it as a pass would hide a
    test that never ran. A test that appears twice in one suite's files (an
    artifact listed the same file twice) counts once, and the failure wins
    over the pass. The same test in two suites' artifacts of one run (a
    reference test the VMaaS and BMaaS suites both run) is two rows.

    The XML hygiene (deleting bytes XML forbids rather than refusing the
    whole file) and the time attribute handling are the run-evidence
    module's, reused here so both readers of the same artifact agree.
    """
    merged = {}
    order = []
    for entry in files:
        name, content = entry[0], entry[1]
        suite_key = entry[2] if len(entry) > 2 else ""
        if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
            cleaned = content
        else:
            cleaned = content.translate(None, CIObserveRunEvidence.ILLEGAL_XML_CONTROL_BYTES)
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return None, f"{name} is not valid XML"
        suites = list(root.iter("testsuite"))
        if not suites:
            return None, f"{name} has no testsuite element"
        for suite in suites:
            suite_name = (suite.get("name") or name).strip()
            for case in suite.findall("testcase"):
                test_name = case.get("name") or ""
                if not test_name:
                    continue
                if case.find("skipped") is not None:
                    continue
                failed = case.find("failure") is not None or case.find("error") is not None
                test_id = f"{case.get('classname') or ''}::{test_name}"
                duration = CIObserveRunEvidence._junit_time(case)
                message = failure_message(case) if failed else ""
                key = (suite_key, suite_name, test_id)
                row = merged.get(key)
                if row is None:
                    merged[key] = {
                        "suite": suite_name,
                        "test_id": test_id,
                        "passed": not failed,
                        "duration_s": duration,
                        "suite_key": suite_key,
                        "message": message,
                    }
                    order.append(key)
                    continue
                if failed:
                    row["passed"] = False
                    row["message"] = row["message"] or message
                if duration is not None and (row["duration_s"] is None or duration > row["duration_s"]):
                    row["duration_s"] = duration
    return [merged[key] for key in order], None


def rollup_rows(runs):
    """The test_rollup rows for a set of parsed runs.

    runs: dicts carrying day, repo, workflow, source_sha and cases (as
    parse_junit_outcomes returns them). One row per
    (day, repo, workflow, suite, test id, source SHA). runs is the number
    of runs the test appeared in that day and SHA, so passes + failures
    equals runs and a rerun of the same SHA adds to runs without adding a
    new SHA -- that is what makes "passed and failed on the same SHA"
    readable straight off the rows.
    """
    groups = {}
    for run in runs:
        for case in run.get("cases") or []:
            key = (run["day"], run["repo"], run["workflow"], case["suite"], case["test_id"], run["source_sha"])
            group = groups.setdefault(key, {"runs": 0, "passes": 0, "failures": 0, "durations": []})
            group["runs"] += 1
            if case["passed"]:
                group["passes"] += 1
            else:
                group["failures"] += 1
            if case["duration_s"] is not None:
                group["durations"].append(case["duration_s"])

    rows = []
    for key in sorted(groups):
        day, repo, workflow, suite, test_id, source_sha = key
        group = groups[key]
        rows.append({
            "day": day,
            "repo": repo,
            "workflow": workflow,
            "suite": suite,
            "test_id": test_id,
            "source_sha": source_sha,
            "runs": group["runs"],
            "passes": group["passes"],
            "failures": group["failures"],
            "p50_duration_s": percentile(group["durations"], 0.50),
            "p95_duration_s": percentile(group["durations"], 0.95),
            "p99_duration_s": percentile(group["durations"], 0.99),
            "mean_duration_s": mean_value(group["durations"]),
        })
    return rows


def test_run_rows(runs):
    """One row per test per run, for the history endpoint and the ci-observe
    test pages.

    runs: the same dicts rollup_rows consumes, optionally carrying trigger
    and pr. A skipped test never reaches here (parse_junit_outcomes leaves it
    out), so every row is a pass or a failure. A testcase without a duration
    keeps a NULL duration: it counts in runs but is excluded from every
    percentile. suite_key falls back from the artifact to the run's workflow
    and then to the test's own directory, so a merge-queue run's rows still
    name their suite.
    """
    rows = []
    for run in runs:
        run_suite = phases.suite_key(run.get("workflow"))
        for case in run.get("cases") or []:
            key = case.get("test_key") or phases.canonical_test_key(case["test_id"])
            suite_key = (
                case.get("suite_key")
                or run_suite
                or phases.describe_test(key)["suite"]
            )
            rows.append({
                "day": run["day"],
                "repo": run["repo"],
                "workflow": run["workflow"],
                "suite": case["suite"],
                "test_id": case["test_id"],
                "source_sha": run["source_sha"],
                "run_id": run["run_id"],
                "created_at": run["created_at"],
                "duration_s": case["duration_s"],
                "passed": 1 if case["passed"] else 0,
                "suite_key": suite_key,
                "test_key": key,
                "trigger": run.get("trigger") or "",
                "pr": run.get("pr") or "",
                "message": case.get("message") or "",
            })
    return rows


def artifact_suite(name):
    """The suite an osac-logs artifact belongs to ("vmaas", "caas", "bmaas",
    ...), "" for a name that is not one."""
    match = ARTIFACT_SUITE_RE.match(name or "")
    if not match:
        return ""
    return match.group(1) or "vmaas"


def fetch_state(reason):
    """"error" for a failure worth retrying (GitHub did not answer), "no_data"
    for an answer that will not change (no artifact, expired, unparseable)."""
    text = (reason or "").lower()
    if text.startswith("artifact listing failed") or "download failed" in text:
        return "error"
    return "no_data"


class ArtifactFetcher:
    """One run's JUnit outcomes, from GitHub.

    It reuses ci_observe_run_evidence's bounded artifact download and
    zip/JUnit extraction by standing in for the workflow exporter those
    two methods call back into: they read .headers and
    .NATIVE_MAX_DOWNLOAD_BYTES and report rate-limit headers through
    ._update_rate_limit, so this class provides exactly that slice. No
    exporter instance, no server, no second copy of the parser's bounds.
    """

    def __init__(self, token=None, org=ORG, api_url=API_URL):
        self.token = token if token is not None else TOKEN
        self.org = org
        self.api_url = api_url
        self.headers = {}
        if self.token:
            self.headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        # Read through by CIObserveRunEvidence._download_artifact.
        self.NATIVE_MAX_DOWNLOAD_BYTES = CIObserveRunEvidence.NATIVE_MAX_DOWNLOAD_BYTES
        self._evidence = CIObserveRunEvidence(self)

    def _update_rate_limit(self, response):
        """The evidence module reports rate-limit headers through its
        exporter. A batch job logs the budget instead of exporting a gauge."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            logger.debug("GitHub API rate limit remaining: %s", remaining)

    def _artifacts(self, repo, run_id):
        """The run's own artifacts that match the native-evidence name
        pattern: (artifacts, reason)."""
        url = f"{self.api_url}/repos/{self.org}/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
        try:
            response = requests.get(url, headers=self.headers, timeout=60)
        except requests.exceptions.RequestException:
            return None, "artifact listing failed (network error)"
        if not response.ok:
            return None, f"artifact listing failed with HTTP {response.status_code}"
        artifacts = [
            artifact for artifact in response.json().get("artifacts") or []
            if CIObserveRunEvidence._is_native_artifact(artifact.get("name") or "", run_id)
        ]
        if not artifacts:
            return None, "no osac-logs-[<suite>-]full-install artifact"
        # A live artifact before an expired one, so a rerun that left both
        # behind still parses the ones that exist.
        return sorted(artifacts, key=lambda artifact: bool(artifact.get("expired"))), None

    def fetch(self, repo, run_id):
        """(cases, reason) for one run: the per-testcase rows of every live
        artifact it still has, or a short reason when it has none.

        A run with no readable artifact returns (None, reason) and is never
        recorded as a run with zero failures: "the evidence is gone" and
        "the tests passed" must not look the same on a trend.
        """
        artifacts, reason = self._artifacts(repo, run_id)
        if artifacts is None:
            return None, reason
        files = []
        unfinished = []
        for artifact in artifacts[:MAX_ARTIFACTS_PER_RUN]:
            name = artifact.get("name") or ""
            if artifact.get("expired"):
                unfinished.append(f"{name}: expired (30-day retention)")
                continue
            if (artifact.get("size_in_bytes") or 0) > self.NATIVE_MAX_DOWNLOAD_BYTES:
                unfinished.append(f"{name}: larger than the download bound")
                continue
            data, download_reason = self._evidence._download_artifact(artifact.get("archive_download_url") or "")
            if data is None:
                unfinished.append(f"{name}: {download_reason}")
                continue
            artifact_files, extract_reason = self._evidence._extract_junit_files(data)
            if artifact_files is None:
                unfinished.append(f"{name}: {extract_reason}")
                continue
            suite_key = artifact_suite(name)
            files.extend((file_name, content, suite_key) for file_name, content in artifact_files)
        if not files:
            return None, "; ".join(unfinished) or "no readable artifact"
        cases, parse_reason = parse_junit_outcomes(files[:MAX_JUNIT_FILES])
        if cases is None:
            return None, parse_reason
        if not cases:
            return None, "artifact has no testcase that ran"
        if unfinished:
            logger.info("run %s: read what remains (%s)", run_id, "; ".join(unfinished))
        return cases, None


def open_trends_db(path):
    """Open (creating if needed) the rollup DB. Separate file from the
    exporter's, so a slow rollup write can never block the exporter's
    poller or corrupt its history."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(test_rollup)")}
    for column in ADDED_ROLLUP_COLUMNS:
        if column not in columns:
            # Existing rows read NULL here, not 0: the rollup never measured
            # them, and a zero would look like an instant test.
            conn.execute(f"ALTER TABLE test_rollup ADD COLUMN {column} REAL")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(rollup_days)")}
    if "parsed_run_ids" not in columns:
        conn.execute("ALTER TABLE rollup_days ADD COLUMN parsed_run_ids TEXT NOT NULL DEFAULT '[]'")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(test_runs)")}
    added = [column for column in ADDED_RUN_COLUMNS if column not in columns]
    for column in added:
        conn.execute(f"ALTER TABLE test_runs ADD COLUMN {column} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_test_runs_key ON test_runs(test_key, day)")
    conn.commit()
    _fill_test_keys(conn)
    return conn


def _fill_test_keys(conn):
    """Give rows stored before test_key and suite_key existed the two keys
    their own columns already imply: the canonical test id, and the suite of
    the run's workflow or of the test's directory. The trigger, pull request
    and message stay NULL until the run is read again: they are not in the
    stored row."""
    rows = conn.execute(
        "SELECT rowid, test_id, workflow, suite_key FROM test_runs"
        " WHERE test_key IS NULL OR suite_key IS NULL"
    ).fetchall()
    if not rows:
        return
    updates = []
    for row in rows:
        key = phases.canonical_test_key(row["test_id"])
        suite = row["suite_key"] or phases.suite_key(row["workflow"]) or phases.describe_test(key)["suite"]
        updates.append((key, suite, row["rowid"]))
    with conn:
        conn.executemany("UPDATE test_runs SET test_key = ?, suite_key = ? WHERE rowid = ?", updates)
    logger.info("gave %d stored test row(s) their canonical test key", len(updates))


def day_is_superset(previous_ids, current_ids):
    """Can this pass replace the stored day?

    Every run whose outcomes are stored must still be represented in this
    pass. A count is not enough: if A parsed and B was unavailable on one
    pass, then A expires and B becomes readable, the count stays 1 while
    A's outcomes, the only copy, would be deleted.
    """
    return set(previous_ids) <= set(current_ids)


def write_day(conn, day, rows, coverage, runs=None):
    """Replace one day's rollup rows, raw run rows and coverage row in one
    transaction.

    Idempotent by construction: the day's rows are deleted before the new
    ones are inserted, so rerunning a day corrects it instead of doubling
    it. Other days are untouched. `runs` is optional so a caller holding
    only aggregate rows (a test fixture or a repair) still replaces the
    day; the raw rows are deleted either way, because leaving an older
    day's durations behind the new counts would make the two disagree.
    """
    aggregate_rows = [
        {**{"p99_duration_s": None, "mean_duration_s": None}, **row} for row in rows
    ]
    with conn:
        conn.execute("DELETE FROM test_rollup WHERE day = ?", (day,))
        conn.executemany(
            f"INSERT INTO test_rollup ({', '.join(ROLLUP_COLUMNS)})"
            f" VALUES ({', '.join(':' + column for column in ROLLUP_COLUMNS)})",
            aggregate_rows,
        )
        conn.execute("DELETE FROM test_runs WHERE day = ?", (day,))
        if runs:
            defaults = dict.fromkeys(ADDED_RUN_COLUMNS)
            conn.executemany(
                f"INSERT INTO test_runs ({', '.join(TEST_RUN_COLUMNS)})"
                f" VALUES ({', '.join(':' + column for column in TEST_RUN_COLUMNS)})",
                [{**defaults, **row} for row in runs],
            )
        conn.execute("DELETE FROM rollup_days WHERE day = ?", (day,))
        conn.execute(
            "INSERT INTO rollup_days"
            " (day, runs_listed, runs_parsed, runs_unavailable, generated_at, parsed_run_ids)"
            " VALUES (:day, :runs_listed, :runs_parsed, :runs_unavailable, :generated_at, :parsed_run_ids)",
            {**coverage, "parsed_run_ids": json.dumps(coverage.get("parsed_run_ids", []))},
        )


def prune_old_days(conn: sqlite3.Connection, cutoff: str) -> None:
    """Drop per-test data, read records and archived runs from before the
    cutoff day (KEEP_DAYS back in a normal pass)."""
    with conn:
        conn.execute("DELETE FROM test_rollup WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM test_runs WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM rollup_days WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM run_fetches WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM suite_runs WHERE created_at < ?", (cutoff,))


def prune_trends_db(path: str, cutoff: str) -> None:
    conn = open_trends_db(path)
    try:
        prune_old_days(conn, cutoff)
    finally:
        conn.close()


# -- comparing two source revisions ------------------------------------------
# "Which fixes matter most?" is a question about two commits, not about two
# captures: the tests that failed on one source revision and pass on another.
# These read test_rollup directly, over one window of days, and never write.
# The population is every job the rollup stored -- all jobs, not captured
# runs -- so the document says so in its own words.

COMPARE_DEFAULT_DAYS = 30
COMPARE_MAX_DAYS = 30
# Rows of the joined table, one-sided lists included. A 30-day window on a
# busy repo holds a few thousand distinct tests; past this the reader gets a
# note instead of a document too large to render.
COMPARE_MAX_ROWS = 2000

SHA_TOTALS_SQL = """
    SELECT repo, workflow, suite, test_id, runs, failures, p50_duration_s
      FROM test_rollup
     WHERE source_sha = ? AND day >= ?
"""

TEST_FIELDS = ("repo", "workflow", "suite", "test_id")


def sha_totals(conn, source_sha, window_from):
    """One source SHA's tests in the window, keyed by test identity.

    runs and failures are summed over the window's days. p50_duration_s is
    the nearest-rank median of those days' p50 values: the durations behind
    each day were never stored, so a median of medians is the most this
    table can support. One day of ten runs therefore weighs the same as one
    day of one run, and the document has to say that.
    """
    totals = {}
    for row in conn.execute(SHA_TOTALS_SQL, (source_sha, window_from)):
        identity = tuple(row[field] for field in TEST_FIELDS)
        total = totals.setdefault(
            identity,
            {
                "repo": row["repo"], "workflow": row["workflow"],
                "suite": row["suite"], "test_id": row["test_id"],
                "runs": 0, "failures": 0, "p50_values": [],
            },
        )
        total["runs"] += row["runs"]
        total["failures"] += row["failures"]
        if row["p50_duration_s"] is not None:
            total["p50_values"].append(row["p50_duration_s"])
    for total in totals.values():
        total["p50_duration_s"] = percentile(total.pop("p50_values"), 0.50)
    return totals


def _one_side_row(total):
    row = {field: total[field] for field in TEST_FIELDS}
    row["runs"] = total["runs"]
    row["failures"] = total["failures"]
    row["p50_duration_s"] = total["p50_duration_s"]
    return row


def compare_rows(base_totals, head_totals):
    """(both, only_base, only_head) tables from two revisions' totals.

    A test is the same test when repo, workflow, suite and test id match.
    Suite alone is not a key (see ci-observe/model.md), and a test that
    exists on one side only is listed there rather than paired with a zero:
    "no row" is not "ran and passed". Deltas are head minus base, so a
    negative delta_p50_s means head is faster and a negative
    delta_failures means head fails less.
    """
    both = []
    only_base = []
    only_head = []
    for identity in sorted(set(base_totals) | set(head_totals)):
        base = base_totals.get(identity)
        head = head_totals.get(identity)
        if base is None:
            only_head.append(_one_side_row(head))
            continue
        if head is None:
            only_base.append(_one_side_row(base))
            continue
        row = {field: base[field] for field in TEST_FIELDS}
        row.update({
            "base_runs": base["runs"],
            "base_failures": base["failures"],
            "base_p50_duration_s": base["p50_duration_s"],
            "head_runs": head["runs"],
            "head_failures": head["failures"],
            "head_p50_duration_s": head["p50_duration_s"],
            "delta_failures": head["failures"] - base["failures"],
            "delta_p50_s": (
                round(head["p50_duration_s"] - base["p50_duration_s"], 3)
                if base["p50_duration_s"] is not None and head["p50_duration_s"] is not None
                else None
            ),
        })
        both.append(row)
    return both, only_base, only_head


def compare_document(trends_db, base_sha, head_sha, days=COMPARE_DEFAULT_DAYS, now=None):
    """The /api/ci-observe/trends/compare document for two source SHAs.

    days is the window counted back from now (UTC). Nothing here raises for
    a missing file, a DB that cannot be read, an unknown SHA or a missing
    parameter: each is a note, so the endpoint answers 200 and the reader is
    told what is missing instead of seeing an empty table.
    """
    days = max(1, min(int(days), COMPARE_MAX_DAYS))
    today = (now or datetime.now(timezone.utc)).date()
    window_from = (today - timedelta(days=days)).isoformat()
    document = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "days": days,
        "window_from": window_from,
        "window_to": today.isoformat(),
        "population": (
            f"all CI jobs whose per-test JUnit outcomes the daily rollup stored in the {days}-day "
            f"window from {window_from} (all jobs, not captured runs); it compares two source "
            "revisions over that window and is not a per-capture comparison"
        ),
        "source": (
            "trends.db: the daily per-test JUnit rollup written by ci_observe_trends.py "
            "(one row per day, repo, workflow, suite, test id and source SHA)"
        ),
        "rows": [],
        "only_base": [],
        "only_head": [],
        "notes": [],
    }
    if not base_sha or not head_sha:
        document["notes"].append(
            "base and head are required: both are source SHAs, for example ?base=<sha>&head=<sha>"
        )
        return document
    if not os.path.exists(trends_db):
        document["notes"].append(
            "no per-test rollup has been written on this machine yet, so there is nothing to compare"
        )
        return document
    try:
        conn = sqlite3.connect(f"file:{trends_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        document["notes"].append(f"the rollup DB could not be opened: {exc}")
        return document
    try:
        conn.row_factory = sqlite3.Row
        base_totals = sha_totals(conn, base_sha, window_from)
        head_totals = sha_totals(conn, head_sha, window_from)
    except sqlite3.Error as exc:
        document["notes"].append(f"the rollup DB could not be read: {exc}")
        return document
    finally:
        conn.close()

    if not base_totals:
        document["notes"].append(
            f"the rollup has no rows for base SHA {base_sha} on or after {window_from}"
        )
    if not head_totals:
        document["notes"].append(
            f"the rollup has no rows for head SHA {head_sha} on or after {window_from}"
        )
    both, only_base, only_head = compare_rows(base_totals, head_totals)
    for key, table in (("rows", both), ("only_base", only_base), ("only_head", only_head)):
        if len(table) > COMPARE_MAX_ROWS:
            document["notes"].append(
                f"{key} was capped at {COMPARE_MAX_ROWS} of {len(table)} test(s); narrow the window"
            )
            table = table[:COMPARE_MAX_ROWS]
        document[key] = table
    if not both:
        document["notes"].append(
            f"no test appears under both SHAs in the window from {window_from}; see only_base and only_head"
        )
    return document


def archive_suite_runs(conn, exporter_db):
    """Copy the exporter's E2E job rows into suite_runs (insert or replace).

    Every pass copies the whole stored window, a few thousand rows: a row
    can change after it was first copied (a rerun attempt, a timeline the
    exporter filled in later), and the copy is one local transaction. Rows
    the exporter already pruned stay here until KEEP_DAYS. Returns the
    number of rows copied; an exporter DB that cannot be read copies none.
    """
    try:
        source = sqlite3.connect(f"file:{exporter_db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        logger.warning("the exporter DB could not be opened for the archive: %s", exc)
        return 0
    try:
        source.row_factory = sqlite3.Row
        columns = {row["name"] for row in source.execute("PRAGMA table_info(jobs)")}
        select = ", ".join(
            column if column in columns else f"NULL AS {column}" for column in SUITE_RUN_COLUMNS
        )
        rows = [dict(row) for row in source.execute(f"SELECT {select} FROM jobs WHERE category = 'e2e'")]
    except sqlite3.Error as exc:
        logger.warning("the exporter DB could not be read for the archive: %s", exc)
        return 0
    finally:
        source.close()
    for row in rows:
        for column in SUITE_RUN_COLUMNS:
            if row.get(column) is None and column not in ("id", "duration_s", "run_attempt"):
                row[column] = "[]" if column == "steps_json" else ""
    with conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO suite_runs ({', '.join(SUITE_RUN_COLUMNS)})"
            f" VALUES ({', '.join(':' + column for column in SUITE_RUN_COLUMNS)})",
            rows,
        )
    return len(rows)


def load_fetches(conn):
    """{run id: run_fetches row}."""
    return {row["run_id"]: dict(row) for row in conn.execute("SELECT * FROM run_fetches")}


def load_stored_cases(conn, run_ids):
    """{run id: [case, ...]} rebuilt from the stored test_runs rows, so a day
    can be rebuilt without downloading its artifacts again."""
    cases = {}
    wanted = set(run_ids)
    for row in conn.execute(
        "SELECT run_id, suite, test_id, passed, duration_s, suite_key, test_key, message FROM test_runs"
    ):
        if row["run_id"] not in wanted:
            continue
        cases.setdefault(row["run_id"], []).append({
            "suite": row["suite"],
            "test_id": row["test_id"],
            "passed": bool(row["passed"]),
            "duration_s": row["duration_s"],
            "suite_key": row["suite_key"] or "",
            "test_key": row["test_key"] or "",
            "message": row["message"] or "",
        })
    return cases


def read_decision(run, fetch, has_stored, now, force=False):
    """What this pass does with one run: "fetch" its artifact, reuse its
    "stored" rows, or "skip" it (unfinished, or known to have no data).

    A run is read again only when that can change the answer: it was never
    read, an older parser read it while its artifact may still exist, or
    GitHub failed to answer. A run known to have no artifact is checked once
    more when the first check came within NO_DATA_RECHECK_WITHIN of its end.
    """
    if force:
        return "fetch"
    if not run.get("finished", True):
        return "skip"
    if fetch is None:
        return "fetch"
    checked = phases.parse_time(fetch.get("checked_at"))
    state = fetch.get("state")
    if state == "parsed":
        if not has_stored:
            return "fetch"
        if (fetch.get("version") or 0) < PARSER_VERSION:
            created = phases.parse_time(run.get("created_at"))
            if created and now - created < timedelta(days=ARTIFACT_RETENTION_DAYS - 1):
                return "fetch"
        return "stored"
    if state == "no_data":
        finished = phases.parse_time(fetch.get("finished_at") or run.get("finished_at"))
        if checked and finished and checked - finished < NO_DATA_RECHECK_WITHIN and now - checked > RECHECK_AFTER:
            return "fetch"
        return "skip"
    if checked is None or now - checked > RECHECK_AFTER:
        return "fetch"
    return "skip"


def record_fetches(conn, entries):
    """Insert or replace run_fetches rows."""
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO run_fetches"
            " (run_id, day, state, reason, version, checked_at, finished_at)"
            " VALUES (:run_id, :day, :state, :reason, :version, :checked_at, :finished_at)",
            entries,
        )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Per-test JUnit rollup and history archive for ci-observe.")
    parser.add_argument(
        "--days", type=int, default=None,
        help=(
            f"read runs created in the last N days (default {DEFAULT_DAYS}, the artifact "
            "retention; a run older than that is recorded as unavailable, because GitHub has "
            "already deleted its artifact)"
        ),
    )
    parser.add_argument(
        "--day", default=None,
        help="roll up one UTC day (YYYY-MM-DD) and replace that day's rows",
    )
    parser.add_argument(
        "--force", action="store_true",
        help=(
            "read every run of the requested days again and replace their rows even when this "
            "pass reads fewer runs than the stored rollup did; use it to repair a day already "
            "rolled up, and expect the rows of runs whose artifact has expired to be dropped"
        ),
    )
    parser.add_argument("--exporter-db", default=None, help=f"exporter SQLite file (default {EXPORTER_DB})")
    parser.add_argument("--trends-db", default=None, help=f"rollup SQLite file (default {TRENDS_DB})")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    exporter_db = args.exporter_db or EXPORTER_DB
    trends_db = args.trends_db or TRENDS_DB
    if not TOKEN:
        logger.error("PRIVATE_GITHUB_TOKEN is not set, so no artifact can be downloaded")
        return 2
    if not os.path.exists(exporter_db):
        logger.error("the exporter DB %s does not exist", exporter_db)
        return 2

    now = datetime.now(timezone.utc)
    today = now.date()
    retention_start = today - timedelta(days=ARTIFACT_RETENTION_DAYS)
    keep_start = today - timedelta(days=max(KEEP_DAYS, ARTIFACT_RETENTION_DAYS))
    if args.day:
        try:
            start = datetime.strptime(args.day, "%Y-%m-%d").date()
        except ValueError:
            logger.error("--day must be YYYY-MM-DD, got %r", args.day)
            return 2
        if start < retention_start:
            logger.error("--day is outside the %d-day retention window", ARTIFACT_RETENTION_DAYS)
            return 2
    else:
        days = min(args.days if args.days is not None else DEFAULT_DAYS, ARTIFACT_RETENTION_DAYS)
        start = today - timedelta(days=max(days, 1))

    runs = candidate_runs(stored_rows(exporter_db, start.isoformat()))
    if args.day:
        runs = [run for run in runs if run["day"] == args.day]

    conn = open_trends_db(trends_db)
    try:
        prune_old_days(conn, keep_start.isoformat())
        archived = archive_suite_runs(conn, exporter_db)
        logger.info("archived %d E2E job row(s) into suite_runs", archived)
        if not runs:
            logger.info("no stored e2e runs on or after %s: nothing to roll up", start)
            phases.record_pass(conn, "trends", now, "no stored e2e run to roll up")
            return 0
        _roll_up(conn, runs, args.force, now)
        phases.record_pass(conn, "trends", now, f"{len(runs)} run(s) considered")
    finally:
        conn.close()
    logger.info("wrote %s", trends_db)
    return 0


def _roll_up(conn, runs, force, now):
    """Read what is new, then rebuild every day whose stored answer changed."""
    fetches = load_fetches(conn)
    stored = load_stored_cases(conn, [run["run_id"] for run in runs])
    checked_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fetcher = None
    plan = {"fetch": 0, "stored": 0, "skip": 0}
    changed_days = set()
    parsed_now = {}
    no_data = []
    for run in runs:
        run_id = run["run_id"]
        decision = read_decision(run, fetches.get(run_id), run_id in stored, now, force)
        plan[decision] += 1
        if decision == "stored":
            run["cases"] = stored[run_id]
            continue
        if decision == "skip":
            run["cases"] = []
            continue
        fetcher = fetcher or ArtifactFetcher()
        cases, reason = fetcher.fetch(run["repo"], run_id)
        entry = {
            "run_id": run_id, "day": run["day"], "reason": reason or "", "version": PARSER_VERSION,
            "checked_at": checked_at, "finished_at": run.get("finished_at") or "",
        }
        if cases:
            run["cases"] = cases
            parsed_now[run_id] = {**entry, "state": "parsed"}
            changed_days.add(run["day"])
            logger.info("  %s run %s: %d test(s)", run["repo"], run_id, len(cases))
            continue
        logger.info("  %s run %s: no test data (%s)", run["repo"], run_id, reason)
        if run_id in stored and not force:
            # An older pass read this run and the artifact is gone now: keep
            # what was read, and stop asking.
            run["cases"] = stored[run_id]
            no_data.append({**entry, "state": "parsed"})
            continue
        run["cases"] = []
        no_data.append({**entry, "state": fetch_state(reason)})
    logger.info(
        "%d run(s): %d read now, %d reused from trends.db, %d skipped",
        len(runs), plan["fetch"], plan["stored"], plan["skip"],
    )
    if no_data:
        record_fetches(conn, no_data)

    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for day in sorted({run["day"] for run in runs}):
        day_runs = [run for run in runs if run["day"] == day]
        parsed_ids = sorted(run["run_id"] for run in day_runs if run.get("cases"))
        previous = conn.execute(
            "SELECT runs_listed, runs_parsed, parsed_run_ids FROM rollup_days WHERE day = ?", (day,)
        ).fetchone()
        previous_ids = json.loads(previous["parsed_run_ids"] or "[]") if previous else []
        if previous and not force:
            unchanged = (
                day not in changed_days
                and previous["runs_listed"] == len(day_runs)
                and sorted(previous_ids) == parsed_ids
            )
            if unchanged:
                continue
            # Never replace a stored day with a thinner pass. Artifacts expire
            # after 30 days, so a run read before may not be readable now, and
            # its stored rows may predate test_runs; rewriting would delete
            # the only copy.
            if not day_is_superset(previous_ids, parsed_ids):
                logger.warning(
                    "%s: stored run(s) %s are not all readable now %s; keeping the stored rollup"
                    " (--force replaces it anyway)",
                    day, sorted(set(previous_ids) - set(parsed_ids)), parsed_ids,
                )
                continue
        if previous and force:
            lost = sorted(set(previous_ids) - set(parsed_ids))
            if lost:
                logger.warning(
                    "%s: --force replaces the stored rollup; run(s) %s are not readable now and their"
                    " rows are dropped",
                    day, lost,
                )
        rows = rollup_rows(day_runs)
        write_day(conn, day, rows, {
            "day": day,
            "runs_listed": len(day_runs),
            "runs_parsed": len(parsed_ids),
            "runs_unavailable": len(day_runs) - len(parsed_ids),
            "generated_at": generated_at,
            "parsed_run_ids": parsed_ids,
        }, test_run_rows(day_runs))
        day_parsed = [parsed_now[run_id] for run_id in parsed_ids if run_id in parsed_now]
        if day_parsed:
            record_fetches(conn, day_parsed)
        logger.info(
            "%s: %d run(s) listed, %d parsed, %d without readable test data, %d rollup row(s)",
            day, len(day_runs), len(parsed_ids), len(day_runs) - len(parsed_ids), len(rows),
        )


if __name__ == "__main__":
    sys.exit(main())

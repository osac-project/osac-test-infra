"""Pull-request history for the ci-observe Pull requests surface.

A backfill, not a poller. It walks the merged and closed pull requests of
the last N days in each repository, classifies its merge-queue attempts, and
writes them to prs.db (a separate SQLite file next to the workflow
exporter's jobs DB). The exporter's existing 90-second loop never calls the
GitHub API for this data. The endpoint in workflow-exporter.py reads prs.db,
and on a miss it may make one cached, read-only lookup for the requested pull
request (see "The read side" below).

Schedule: the timer fires every hour at :05 with a random delay, so a pull
request merged now is stored within the hour instead of the next day. Each
hourly run is incremental: it lists pull requests GitHub updated since the
end of the last completed window, minus a two-hour overlap, and it skips
the merge_group query entirely when there is nothing new to store. Once a
day the same timer runs a full pass over the retention window and prunes
rows that left it. Every pass writes a row to pr_backfill_runs, which is
what the read side reports as coverage and freshness.

What is fetched, per pull request (one pass, no repeats):

  - the PR object from the closed-PR listing (opened, merged, closed,
    author, labels)
  - the issue timeline (review requests, label history, comments,
    merged/closed fallbacks)
  - the reviews (first human review and first human approval)
  - the commits (commit count)

Merge-queue runs are fetched once per repository, not per pull request:
`GET /repos/{org}/{repo}/actions/runs?event=merge_group&created=<start>..<now>`,
where start is the oldest pull request this pass will store, floored at the
retention cutoff. An hour with nothing new skips the query. The runs list
stops near 1000 results, so a busy window is split in half
until each query fits. Each run's head branch is
`gh-readonly-queue/<base>/pr-<N>-<sha>`, the only place GitHub exposes the
PR number of a queue run. A comment on a PR that links a run id adds that
run to the PR as well, because one merge group can carry up to four pull
requests while its branch names only one. One queue entry starts several
workflows that share (head_branch, head_sha); those runs become one queue
attempt, so a clean multi-workflow pass is not counted as ejections.

GitHub keeps no merge-queue history: an ejected entry disappears. An
ejection is therefore derived from a failed (or cancelled) merge_group run
plus the comment GitHub leaves on the PR. `Enqueued` is the start of the
pull request's first merge_group run; GitHub has no durable enqueue
timestamp. Both choices are the ci-observe model's, not GitHub's.

Idempotency: rows are keyed by (repo, number) and (repo, number, run_id)
and written with INSERT OR REPLACE in one transaction per pull request. A
pull request already stored is skipped on the next run unless --force is
given, so a run that hits the rate-limit budget resumes where it stopped.

The read side never writes prs.db. When a pull request is not stored it may
make one read-only GitHub lookup through the exporter's own client (the
shared token and rate-limit counter) and mark the answer source=live. That
lookup is cached for a minute so a dashboard refresh does not repeat it.

Manual use:

    python3 monitoring/scripts/ci_observe_prs.py --days 60
    python3 monitoring/scripts/ci_observe_prs.py --days 60 --mode full
    python3 monitoring/scripts/ci_observe_prs.py --days 60 --repo osac --force
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
# A GitHub repository name: letters, digits, dot, dash and underscore. It
# never contains a slash, a dot segment, a query or a fragment, so it cannot
# move a live lookup to another path.
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# The index document answers the detail panels too, with no number selected.
# A dedicated token keeps "no selection" from looking like a miss state.
NO_SELECTION_STATE = "no_selection"
NO_SELECTION_MESSAGE = "Enter a pull request number in the PR box to see its state."

CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
PRS_DB_FILE = os.path.join(CACHE_DIR, "prs.db")

# Matches the workflow exporter's 30-day job history, so a pull request and
# the jobs it produced cover the same window.
PRS_HISTORY_DAYS = int(os.getenv("JOBS_HISTORY_DAYS", "30"))
DEFAULT_INDEX_DAYS = 30
PAGE_SIZE = 100
# GitHub's actions/runs list stops near 1000 results. Split a merge-group
# window before it reaches that, the same safe limit the exporter uses.
MERGE_GROUP_SAFE_RESULT_LIMIT = 900
# Stop a run before it drains the token: 5000 requests/hour is the shared
# budget of the exporter and every interactive GitHub call. A stopped run
# leaves the remaining pull requests unstored, and the next run resumes.
MIN_RATE_LIMIT_REMAINING = 200

# The hourly timer runs at *:05. An incremental pass re-lists the last two
# hours before the previous window ended, so a pull request GitHub updated
# while that pass was running is not lost to a page boundary.
INCREMENTAL_OVERLAP = timedelta(hours=2)
# A full pass over the retention window runs once a day; the other hourly
# passes are incremental.
FULL_PASS_INTERVAL = timedelta(hours=24)
# The backfill is stale only after two hourly slots: one delayed or missed
# run still leaves the page useful.
FRESHNESS_STALE_AFTER_SECONDS = 2 * 3600
# Kept in step with ci-observe-prs.timer. Used only to say when the next pass
# starts, never to schedule one.
TIMER_MINUTE = 5
# A live lookup on an unstored pull request is read-only. One minute of cache
# keeps a dashboard refresh from spending the shared GitHub budget.
LIVE_CACHE_TTL_SECONDS = 60
# A failed lookup is cached too. Every detail panel refreshes, and while
# GitHub returns 403 or 502 each refresh would spend the shared token's
# budget again, at the moment that budget is already low. Failures clear
# sooner than answers.
LIVE_FAILURE_CACHE_TTL_SECONDS = 15
# Hourly rows for 30 days are 720; keep a little slack and drop the rest.
BACKFILL_RUNS_KEEP = 2000

# The only place a merge_group run names its pull request. Same expression
# as WorkflowExporter.MERGE_QUEUE_BRANCH_RE.
MERGE_QUEUE_BRANCH_RE = re.compile(r"^gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$")
RUN_LINK_RE = re.compile(r"/(?:actions/runs|runs)/(\d+)")

# A comment that talks about the queue, not a human comment that happens to
# mention merging. GitHub's own text has changed over time, so the match is
# deliberately loose; the comment author and URL are stored so a reader can
# check the source.
MERGE_QUEUE_COMMENT_MARKERS = ("merge queue", "merge group", "gh-readonly-queue", "removed from the merge queue")
MERGE_QUEUE_COMMENT_MAX_CHARS = 240

BOT_LOGIN_RE = re.compile(r"\[bot\]$")

STAGES = (
    ("authoring", "Authoring"),
    ("review", "Review"),
    ("waiting", "Waiting"),
    ("queue", "Queue"),
    ("merged", "Merged"),
)

# What the dashboard prints next to the derived numbers. Kept here so the
# endpoint and the dashboard state the same limit.
DERIVATION = {
    "enqueued": (
        "Enqueued is the start of the pull request's first merge_group run. "
        "GitHub exposes no durable enqueue timestamp."
    ),
    "ejections": (
        "Queue ejections are derived, not recorded by GitHub: a failed or cancelled merge_group run "
        "plus the comment GitHub leaves on the pull request. GitHub keeps no merge-queue history."
    ),
    "queue_pr_number": (
        "A merge_group run carries no pull request number of its own. The run is linked to a pull "
        "request through its gh-readonly-queue branch name and through queue comments that link the run."
    ),
    "queue_capture": (
        "A queue attempt links to its capture when Tempo has one for the run; otherwise the GitHub run "
        "is the link. A queue run older than Tempo retention has no capture to link."
    ),
    "window": (
        "The backfill covers merged and closed pull requests only. An open pull request is not stored "
        "until it is merged or closed."
    ),
    "freshness": (
        "The backfill runs every hour. Data through is the end of the last pass that finished its "
        "window; the last run time and its counts are shown even when that pass stopped early."
    ),
}

# Why a pull request the reader asked for has no stored row. Exactly one of
# these applies; a stored pull request is "stored", which is not a miss.
PR_STATE_MESSAGES = (
    (
        "open",
        "Still open",
        "The pull request is open. The backfill stores merged and closed pull requests only, so it "
        "appears here after it merges or closes.",
    ),
    (
        "not_yet_fetched",
        "Merged after the last fetch",
        "The pull request ended after the last completed backfill window. The next hourly run stores it.",
    ),
    (
        "missing",
        "Covered window, no row",
        "The backfill covered the window that holds this pull request but stored no row. The message "
        "says whether the pass stopped early or the pull request failed to fetch.",
    ),
    (
        "outside_window",
        "Outside the retention window",
        f"The pull request ended before the {PRS_HISTORY_DAYS}-day retention, so the backfill does not keep it.",
    ),
)

# Live lookups are read-only and never touch prs.db. The cache lives on the
# module because the exporter builds a CIObservePRs per request.
_LIVE_PR_CACHE = {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def parse_iso(value):
    """A datetime from GitHub's ISO-8601 text, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def seconds_between(start, end):
    """Whole seconds from start to end, or None if either is missing. The
    same rule as the exporter's _seconds_between: a missing boundary is
    unknown, never zero."""
    start_dt = parse_iso(start)
    end_dt = parse_iso(end)
    if start_dt is None or end_dt is None:
        return None
    return max(0, round((end_dt - start_dt).total_seconds()))


def percentile(sorted_values, p):
    """Linear-interpolation percentile, the same arithmetic the exporter
    uses for its p50/p95, so the two surfaces never disagree by a rounding
    rule. Empty input returns None, not 0."""
    if not sorted_values:
        return None
    n = len(sorted_values)
    k = (n - 1) * p / 100
    lower = int(k)
    upper = lower + 1
    if upper >= n:
        return round(sorted_values[lower])
    return round(sorted_values[lower] + (k - lower) * (sorted_values[upper] - sorted_values[lower]))


def human_duration(seconds):
    """A short duration for a table cell, e.g. 3d 4h, 2h 5m, 45s."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def pr_url(repo, number):
    return f"https://github.com/{ORG}/{repo}/pull/{number}"


def _iso(moment):
    """GitHub's UTC text for a datetime, the same form the API sends."""
    if moment is None:
        return ""
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_run_at(now):
    """The next hourly timer slot at :05. The timer's random delay is not
    part of the timestamp a reader sees."""
    candidate = now.astimezone(UTC).replace(minute=TIMER_MINUTE, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def _minutes_ago(seconds):
    """How old a timestamp is, in the words the freshness line uses."""
    if seconds is None:
        return "unknown"
    minutes = max(0, int(seconds) // 60)
    if minutes < 1:
        return "less than a minute ago"
    if minutes == 1:
        return "1 min ago"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago"


def _states_document():
    """The four miss states, always all four, so the dashboard can explain
    an empty detail panel instead of leaving it blank."""
    return [{"state": state, "label": label, "message": message} for state, label, message in PR_STATE_MESSAGES]


def _unique_runs(runs):
    """One entry per run id. Bisected windows share their midpoint, so the
    same run can come back from both halves."""
    unique = {}
    for run in runs:
        unique.setdefault(run["id"], run)
    return list(unique.values())


def is_bot(user):
    if not user:
        return False
    if user.get("type") == "Bot":
        return True
    return bool(BOT_LOGIN_RE.search(user.get("login") or ""))


def merge_queue_pr_number(branch):
    """The PR number embedded in a gh-readonly-queue branch, or None."""
    if not branch:
        return None
    match = MERGE_QUEUE_BRANCH_RE.match(branch)
    return int(match.group(1)) if match else None


def clean_comment_body(body):
    """The first informative line of a PR comment, without markdown."""
    text = body or ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_>`#]+", "", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    queue_line = next(
        (line for line in lines if any(marker in line.lower() for marker in MERGE_QUEUE_COMMENT_MARKERS)), lines[0]
    )
    if len(queue_line) > MERGE_QUEUE_COMMENT_MAX_CHARS:
        return queue_line[: MERGE_QUEUE_COMMENT_MAX_CHARS - 1].rstrip() + "…"
    return queue_line


def ejection_kind(conclusion, reason):
    """Name the way the attempt left the queue. Derived from the run
    conclusion first; the comment only refines it."""
    text = (reason or "").lower()
    if conclusion == "cancelled" or "cancel" in text:
        return "cancelled"
    if "timed out" in text or "timeout" in text or "exceeded" in text:
        return "timed_out"
    if conclusion and conclusion != "success":
        return "check_failed"
    if "fail" in text:
        return "check_failed"
    if "removed from the merge queue" in text:
        return "removed"
    return "unknown"


def _entry_conclusion(runs):
    """One conclusion for a queue entry. Several workflows start on the same
    entry; a real failure outranks the cancellations it caused. A skipped or
    neutral workflow does not fail an entry whose other runs passed."""
    conclusions = {run.get("conclusion") or "" for run in runs}
    for conclusion in ("failure", "timed_out", "startup_failure", "action_required"):
        if conclusion in conclusions:
            return conclusion
    if "cancelled" in conclusions:
        return "cancelled"
    if conclusions and all(conclusion in ("success", "skipped", "neutral") for conclusion in conclusions):
        return "success"
    return ""


def final_stage(state, first_review_at, first_approval_at, queued_at):
    """The last stage a pull request reached. For a merged PR that is
    Merged; for a closed one it says where it stopped."""
    if state == "merged":
        return "merged"
    if queued_at:
        return "closed in queue"
    if first_approval_at:
        return "closed after approval"
    if first_review_at:
        return "closed in review"
    return "closed in authoring"


# ---------------------------------------------------------------------------
# GitHub access
# ---------------------------------------------------------------------------
class StandaloneAPI:
    """GitHub access when no exporter is handed in (the manual backfill
    process). The exporter path uses the exporter's own _get, headers and
    token instead, so both paths share one credential and one rate-limit
    counter."""

    def __init__(self):
        token = os.getenv("PRIVATE_GITHUB_TOKEN")
        if not token:
            raise SystemExit("PRIVATE_GITHUB_TOKEN is required to backfill pull requests")
        self.headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    def _get(self, url):
        return requests.get(url, headers=self.headers, timeout=15)


# ---------------------------------------------------------------------------
# Pull-request backfill and read side
# ---------------------------------------------------------------------------
class CIObservePRs:
    """The Pull requests surface: one backfill pass over GitHub, and the
    read side the exporter's /api/ci-observe/prs endpoint calls. The api
    argument is a WorkflowExporter (it has _get, headers and _github_token)
    or a StandaloneAPI; the writer only uses _get and headers."""

    def __init__(self, db_file=None, api=None, repos=None):
        self.db_file = db_file or PRS_DB_FILE
        # The read side needs no API at all; a read with no api argument is
        # valid. backfill() requires one.
        self.api = api
        self.repos_filter = list(repos or [])
        self.api_calls = 0
        self.rate_limit_remaining = None

    # -- SQLite -------------------------------------------------------------

    def _db(self):
        conn = sqlite3.connect(self.db_file, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_file) or ".", exist_ok=True)
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pr_events (
                    repo TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    title TEXT,
                    author TEXT,
                    url TEXT,
                    state TEXT,
                    labels TEXT,
                    opened_at TEXT,
                    first_review_at TEXT,
                    first_approval_at TEXT,
                    queued_at TEXT,
                    merged_at TEXT,
                    closed_at TEXT,
                    last_attempt_at TEXT,
                    authoring_seconds INTEGER,
                    review_seconds INTEGER,
                    waiting_seconds INTEGER,
                    queue_seconds INTEGER,
                    merged_seconds INTEGER,
                    total_seconds INTEGER,
                    commit_count INTEGER,
                    review_count INTEGER,
                    review_request_count INTEGER,
                    retest_count INTEGER,
                    final_stage TEXT,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (repo, number)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pr_queue_attempts (
                    repo TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    run_id INTEGER NOT NULL,
                    run_number INTEGER,
                    run_attempt INTEGER,
                    branch TEXT,
                    head_sha TEXT,
                    run_ids TEXT,
                    conclusion TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds INTEGER,
                    url TEXT,
                    ejected INTEGER NOT NULL DEFAULT 0,
                    ejection_reason TEXT,
                    ejection_kind TEXT,
                    reason_source TEXT,
                    comment_url TEXT,
                    comment_author TEXT,
                    comment_at TEXT,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (repo, number, run_id)
                )
            """)
            # One row per pass, so the read side can say what the data
            # covers and when it last moved. The window is window_from ..
            # window_to: the range of updated_at the pass listed.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pr_backfill_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ran_at TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'full',
                    window_from TEXT NOT NULL DEFAULT '',
                    window_to TEXT NOT NULL DEFAULT '',
                    stored INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    api_calls INTEGER NOT NULL DEFAULT 0,
                    rate_limit_remaining INTEGER,
                    stopped_early TEXT NOT NULL DEFAULT '',
                    listing_complete INTEGER NOT NULL DEFAULT 0,
                    ok INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_events_merged_at ON pr_events(merged_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_events_closed_at ON pr_events(closed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_queue_attempts_pr ON pr_queue_attempts(repo, number)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_backfill_runs_ran_at ON pr_backfill_runs(ran_at)")
            # A database written before `run_ids` existed keeps every other
            # column; add the new one in place instead of dropping the file.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(pr_queue_attempts)")}
            if "run_ids" not in columns:
                conn.execute("ALTER TABLE pr_queue_attempts ADD COLUMN run_ids TEXT")

    # -- GitHub plumbing ----------------------------------------------------

    def _get(self, url):
        if self.api is None:
            raise RuntimeError("the pull-request backfill needs a GitHub API object")
        self.api_calls += 1
        resp = self.api._get(url)
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
        return resp

    def _get_page(self, url) -> tuple[list | dict | None, str | None]:
        """One page: (items, next_url or None). A non-2xx page or a network
        error is a gap in the pass, not a crash -- the caller records the
        affected pull request as incomplete so the next run retries it."""
        try:
            resp = self._get(url)
        except requests.RequestException as exc:
            logger.warning("GitHub request failed for %s: %s", url, exc)
            return None, None
        if not resp.ok:
            logger.warning("GitHub %s returned %s for %s", resp.status_code, resp.text[:200], url)
            return None, None
        return resp.json(), resp.links.get("next", {}).get("url")

    def _get_all(self, url):
        items = []
        while url:
            page, url = self._get_page(url)
            if page is None:
                return None
            items.extend(page)
        return items

    def _list_repos(self):
        """Repository names, or None when the org listing failed. A failed
        listing is not an org with no repositories: the caller must not
        record coverage over it."""
        if self.repos_filter:
            return self.repos_filter
        url = f"{API_URL}/orgs/{ORG}/repos?per_page={PAGE_SIZE}&type=all"
        repos = []
        while url:
            page, url = self._get_page(url)
            if page is None:
                return None
            repos.extend(r["name"] for r in page if not r.get("archived"))
        return repos

    # -- backfill -----------------------------------------------------------

    def backfill(self, days=PRS_HISTORY_DAYS, force=False, limit=None, now=None, mode="auto"):
        """Fetch and store merged/closed pull requests, then record the pass.

        mode="full" lists the whole retention window and prunes rows that
        left it. mode="incremental" lists only what GitHub updated since the
        last completed window, minus the overlap. mode="auto" runs a full
        pass when the last successful one is more than a day old, and an
        incremental pass otherwise.

        `now` pins the window for a reproducible replay. Returns a summary
        dict. Safe to run repeatedly: stored pull requests are skipped, and
        every write is keyed, so a second run does not duplicate a row or
        change a value.
        """
        if mode not in ("auto", "full", "incremental"):
            raise ValueError(f"unknown backfill mode: {mode!r}")
        now = now or datetime.now(UTC)
        self._init_db()
        self.api_calls = 0
        self.rate_limit_remaining = None

        cutoff = now - timedelta(days=days)
        cutoff_iso = _iso(cutoff)
        # --force means refetch every stored row, so it needs the full
        # window; an incremental pass would only list the recent overlap.
        planned_mode = "full" if force and mode == "auto" else mode
        resolved_mode, since = self._plan(days, now, planned_mode)
        summary = {
            "mode": resolved_mode,
            "days": days,
            "cutoff": cutoff_iso,
            "since": _iso(since),
            "window_from": _iso(since),
            "window_to": _iso(now),
            "repos": [],
            "prs_stored": 0,
            "prs_skipped": 0,
            "prs_failed": 0,
            "attempts_stored": 0,
            "api_calls": 0,
            "rate_limit_remaining": None,
            "stopped_early": "",
            "listing_complete": True,
            "ok": True,
        }

        repos = self._list_repos()
        if repos is None:
            summary["listing_complete"] = False
            summary["stopped_early"] = "the repository listing failed; coverage did not advance."
            repos = []
        for repo in repos:
            repo_summary = {"repo": repo, "prs_stored": 0, "prs_skipped": 0, "prs_failed": 0, "attempts_stored": 0}
            prs = self._fetch_closed_prs(repo, cutoff, since)
            if prs is None:
                # A broken listing is not an empty repository. Do not record
                # coverage past a window that was never read.
                summary["listing_complete"] = False
                summary["stopped_early"] = summary["stopped_early"] or (
                    f"the pull request listing failed for {repo}; coverage did not advance."
                )
                summary["repos"].append(repo_summary)
                continue
            pending = [pr for pr in prs if force or not self._is_stored(repo, pr["number"])]
            repo_summary["prs_skipped"] = len(prs) - len(pending)
            # merge_group runs are only needed to build attempts for pull
            # requests that are about to be written. No new pull request
            # means no queue query at all in the idle hour.
            merge_runs = (
                self._fetch_merge_group_runs(repo, self._merge_group_from(pending, cutoff), now) if pending else []
            )

            stored = 0
            stopped = False
            for pr in pending:
                attempts = self._backfill_one(repo, pr, merge_runs)
                if attempts is None:
                    repo_summary["prs_failed"] += 1
                else:
                    repo_summary["prs_stored"] += 1
                    repo_summary["attempts_stored"] += attempts
                    stored += 1

                if self.rate_limit_remaining is not None and self.rate_limit_remaining < MIN_RATE_LIMIT_REMAINING:
                    summary["stopped_early"] = (
                        f"stopped in {repo} after {stored} pull request(s): rate limit remaining "
                        f"{self.rate_limit_remaining} is below {MIN_RATE_LIMIT_REMAINING}. Re-run to resume."
                    )
                    logger.warning(summary["stopped_early"])
                    stopped = True
                if limit is not None and summary["prs_stored"] + repo_summary["prs_stored"] >= limit:
                    summary["stopped_early"] = f"stopped after --limit {limit} pull request(s)."
                    stopped = True
                if stopped:
                    break
            if resolved_mode == "full":
                self._prune(repo, cutoff_iso)
            summary["repos"].append(repo_summary)
            summary["prs_stored"] += repo_summary["prs_stored"]
            summary["prs_skipped"] += repo_summary["prs_skipped"]
            summary["prs_failed"] += repo_summary["prs_failed"]
            summary["attempts_stored"] += repo_summary["attempts_stored"]
            if stopped:
                # Later repositories were never listed, and this repository's
                # pending rows were not all stored. The window was not read:
                # coverage must not advance, and a full pass that stopped
                # here must not count as the day's full pass.
                summary["listing_complete"] = False
                break

        if self.repos_filter:
            # A --repo run reads a subset of the org. It must not move
            # org-wide coverage or count as the day's full pass, or the other
            # repositories' pull requests are skipped until the next one.
            summary["listing_complete"] = False
            summary["stopped_early"] = summary["stopped_early"] or (
                "this run was limited to "
                + ", ".join(self.repos_filter)
                + "; org-wide coverage did not advance."
            )
        summary["api_calls"] = self.api_calls
        summary["rate_limit_remaining"] = self.rate_limit_remaining
        # ok says the pass stored everything it listed. Coverage advances
        # only when the listing window was read completely, so a pass that
        # stopped early leaves coverage where it was: the pull requests it
        # missed then read "not fetched yet", not "missing".
        summary["ok"] = summary["listing_complete"] and not summary["stopped_early"] and summary["prs_failed"] == 0
        self._record_run(summary, now)
        self._prune_runs(now - timedelta(days=days))
        logger.info(
            "Pull-request backfill (%s): %d stored, %d already present, %d attempts, %d API calls",
            resolved_mode,
            summary["prs_stored"],
            summary["prs_skipped"],
            summary["attempts_stored"],
            summary["api_calls"],
        )
        return summary

    # -- pass planning and history ------------------------------------------

    def _plan(self, days, now, mode):
        """The mode this pass really runs and the updated_at floor it lists.
        A pass that cannot see a completed window falls back to a full pass,
        because there is nothing to be incremental from."""
        full_since = now - timedelta(days=days)
        if mode == "full" or (mode == "auto" and self._full_pass_due(now)):
            return "full", full_since
        coverage = parse_iso(self._coverage_until())
        if coverage is None:
            return "full", full_since
        return "incremental", max(coverage - INCREMENTAL_OVERLAP, full_since)

    def _full_pass_due(self, now):
        """True when no completed full pass ran in the last day. A full pass
        that stopped early does not count: its window was not covered."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM pr_backfill_runs WHERE mode = 'full' AND listing_complete = 1 AND ran_at >= ? LIMIT 1",
                (_iso(now - FULL_PASS_INTERVAL),),
            ).fetchone()
        return row is None

    def _last_run(self):
        """The most recent pass, completed or not. A prs.db written before
        pr_backfill_runs existed reads as "never", not as an error."""
        try:
            with self._db() as conn:
                row = conn.execute("SELECT * FROM pr_backfill_runs ORDER BY ran_at DESC, id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None

    def _coverage_until(self):
        """The end of the last pass that finished its listing window. It is
        not the same as ok: a pass can read every page and still stop before
        it stores every pull request."""
        try:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT window_to FROM pr_backfill_runs WHERE listing_complete = 1 "
                    "ORDER BY ran_at DESC, id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            return None
        return row["window_to"] if row is not None else None

    def _record_run(self, summary, now):
        """One row per pass. coverage_until reads this table, so a pass that
        stopped early must not be written as ok."""
        with self._db() as conn:
            conn.execute(
                "INSERT INTO pr_backfill_runs "
                "(ran_at, mode, window_from, window_to, stored, skipped, failed, api_calls, "
                "rate_limit_remaining, stopped_early, listing_complete, ok) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(now),
                    summary["mode"],
                    summary["window_from"],
                    summary["window_to"],
                    summary["prs_stored"],
                    summary["prs_skipped"],
                    summary["prs_failed"],
                    summary["api_calls"],
                    summary["rate_limit_remaining"],
                    summary["stopped_early"],
                    1 if summary["listing_complete"] else 0,
                    1 if summary["ok"] else 0,
                ),
            )

    def _prune_runs(self, cutoff):
        """Bound the pass history; it is diagnostic, not a second dataset."""
        cutoff_iso = _iso(cutoff)
        with self._db() as conn:
            conn.execute("DELETE FROM pr_backfill_runs WHERE ran_at < ?", (cutoff_iso,))
            conn.execute(
                "DELETE FROM pr_backfill_runs WHERE id NOT IN "
                "(SELECT id FROM pr_backfill_runs ORDER BY ran_at DESC, id DESC LIMIT ?)",
                (BACKFILL_RUNS_KEEP,),
            )

    def _merge_group_from(self, pending, cutoff):
        """The queue history of a new pull request starts at its creation,
        not at the two-hour overlap: a queue entry can predate the merge by
        days. Floored at the retention cutoff."""
        starts = [parse_iso(pr.get("created_at")) for pr in pending]
        starts = [start for start in starts if start is not None]
        return max(cutoff, min(starts)) if starts else cutoff

    # -- listing ------------------------------------------------------------

    def _fetch_closed_prs(self, repo, cutoff, since):
        """Closed PRs whose last activity is inside the retention window and
        whose updated_at is at or after `since`. Sorted by updated, so
        pagination stops as soon as a whole page is older. None means the
        listing failed; the caller must not treat that as an empty window."""
        url = (
            f"{API_URL}/repos/{ORG}/{repo}/pulls?state=closed&sort=updated&direction=desc"
            f"&per_page={PAGE_SIZE}&since={_iso(since)}"
        )
        prs = []
        while url:
            page, url = self._get_page(url)
            if page is None:
                return None
            for pr in page:
                ended = pr.get("merged_at") or pr.get("closed_at")
                if ended and parse_iso(ended) and parse_iso(ended) >= cutoff:
                    prs.append(pr)
            if page and all((parse_iso(pr.get("updated_at")) or since) < since for pr in page):
                break
        return prs

    def _fetch_merge_group_runs(self, repo, cutoff_dt, until=None) -> list[dict] | None:
        """Every merge_group run created in the window, once per repo.

        The runs list stops near 1000 results, so a window with more runs is
        split in half until each query fits. Without that split, runs past
        the cap would be dropped and their pull requests would lose queue
        attempts without any error.

        None means the fetch failed. The caller then leaves every affected
        pull request unstored instead of writing "no queue history" over a
        transient error.
        """
        if cutoff_dt is None:
            return None
        return self._fetch_merge_group_range(repo, cutoff_dt, until or datetime.now(UTC))

    def _fetch_merge_group_range(self, repo, since_dt, until_dt) -> list[dict] | None:
        since_iso = _iso(since_dt)
        until_iso = _iso(until_dt)
        url = (
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs?event=merge_group"
            f"&created={since_iso}..{until_iso}&per_page={PAGE_SIZE}"
        )
        try:
            probe = self._get(url)
        except requests.RequestException as exc:
            logger.warning("GitHub request failed for %s: %s", url, exc)
            return None
        if not probe.ok:
            logger.warning("GitHub %s returned %s for %s", probe.status_code, probe.text[:200], url)
            return None
        payload = probe.json()
        total_count = payload.get("total_count") or 0
        if total_count > MERGE_GROUP_SAFE_RESULT_LIMIT and (until_dt - since_dt) > timedelta(hours=1):
            mid = since_dt + (until_dt - since_dt) / 2
            logger.info(
                "%s [%s..%s]: %d merge_group runs, splitting at %s",
                repo,
                since_iso,
                until_iso,
                total_count,
                mid.isoformat(),
            )
            left = self._fetch_merge_group_range(repo, since_dt, mid)
            right = self._fetch_merge_group_range(repo, mid, until_dt)
            if left is None or right is None:
                return None
            return _unique_runs(left + right)

        runs = payload.get("workflow_runs") or []
        next_url = probe.links.get("next", {}).get("url")
        while next_url:
            page, next_url = self._get_page(next_url)
            if page is None:
                return None
            runs.extend(page.get("workflow_runs") or [])
        return [run for run in runs if run.get("event") == "merge_group"]

    def _is_stored(self, repo, number):
        with self._db() as conn:
            row = conn.execute("SELECT 1 FROM pr_events WHERE repo = ? AND number = ?", (repo, number)).fetchone()
        return row is not None

    def _backfill_one(self, repo, pr, merge_runs):
        """Fetch one pull request's timeline, reviews and commits, then
        write its row and its queue attempts. Returns the attempt count, or
        None when a page failed and nothing was written."""
        number = pr["number"]
        if merge_runs is None:
            # Writing the PR with zero attempts would turn a transient
            # GitHub error into "no queue history" forever.
            logger.warning("Skipping %s#%s: merge_group runs could not be fetched", repo, number)
            return None
        timeline = self._get_all(f"{API_URL}/repos/{ORG}/{repo}/issues/{number}/timeline?per_page={PAGE_SIZE}")
        reviews = self._get_all(f"{API_URL}/repos/{ORG}/{repo}/pulls/{number}/reviews?per_page={PAGE_SIZE}")
        commits = self._get_all(f"{API_URL}/repos/{ORG}/{repo}/pulls/{number}/commits?per_page={PAGE_SIZE}")
        if timeline is None or reviews is None or commits is None:
            # A partial pass would store a pull request with invented gaps.
            # Leave it unstored; the next run retries it.
            logger.warning("Skipping %s#%s: a GitHub page failed", repo, number)
            return None

        record = self._build_pr_record(repo, pr, timeline, reviews, commits)
        attempts = self._build_attempts(repo, number, merge_runs, timeline)
        self._write_pr(record, attempts)
        return len(attempts)

    # -- record building ----------------------------------------------------

    def _build_pr_record(self, repo, pr, timeline, reviews, commits):
        number = pr["number"]
        merged_at = pr.get("merged_at")
        closed_at = pr.get("closed_at")
        state = "merged" if merged_at else "closed"

        human_reviews = [
            r
            for r in reviews
            if r.get("submitted_at")
            and not is_bot(r.get("user"))
            and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
        ]
        human_reviews.sort(key=lambda r: r["submitted_at"])
        first_review_at = human_reviews[0]["submitted_at"] if human_reviews else None
        approvals = [r for r in human_reviews if r.get("state") == "APPROVED"]
        first_approval_at = approvals[0]["submitted_at"] if approvals else None

        review_requesters = {
            (event.get("requested_reviewer") or {}).get("login")
            for event in timeline
            if event.get("event") == "review_requested"
        }
        review_requesters.discard(None)
        review_requesters.discard("")

        labels = set()
        for event in timeline:
            if event.get("event") == "labeled" and (event.get("label") or {}).get("name"):
                labels.add(event["label"]["name"])
        for label in pr.get("labels") or []:
            name = label.get("name") if isinstance(label, dict) else label
            if name:
                labels.add(name)

        fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "repo": repo,
            "number": number,
            "title": pr.get("title") or "",
            "author": (pr.get("user") or {}).get("login") or "",
            "url": pr.get("html_url") or pr_url(repo, number),
            "state": state,
            "labels": ",".join(sorted(labels)),
            "opened_at": pr.get("created_at"),
            "first_review_at": first_review_at,
            "first_approval_at": first_approval_at,
            "queued_at": None,
            "merged_at": merged_at,
            "closed_at": closed_at,
            "last_attempt_at": None,
            "authoring_seconds": seconds_between(pr.get("created_at"), first_review_at),
            "review_seconds": seconds_between(first_review_at, first_approval_at),
            "waiting_seconds": None,
            "queue_seconds": None,
            "merged_seconds": None,
            "total_seconds": seconds_between(pr.get("created_at"), merged_at or closed_at),
            "commit_count": len(commits),
            "review_count": len(reviews),
            "review_request_count": len(review_requesters),
            "retest_count": self._count_retests(repo, number),
            "final_stage": final_stage(state, first_review_at, first_approval_at, None),
            "fetched_at": fetched_at,
        }

    def _build_attempts(self, repo, number, merge_runs, timeline) -> list[dict]:
        """One row per merge-queue entry this pull request took part in.

        One queue entry starts several workflows (CI, E2E, jira-reference,
        and so on). Those runs share (head_branch, head_sha), so grouping by
        that pair turns them into one attempt instead of N attempts and N-1
        false ejections.

        A run joins the PR by (a) its gh-readonly-queue branch, which names
        one PR, or (b) a PR comment that links the run id, which is how the
        other up-to-four PRs of a merge group are found. Both are derived
        links; GitHub records neither on the run.
        """
        runs_by_id = {run["id"]: run for run in merge_runs}
        linked_ids = {
            int(match)
            for event in timeline
            if event.get("event") == "commented"
            for match in RUN_LINK_RE.findall(event.get("body") or "")
            if int(match) in runs_by_id
        }
        mine = [
            run
            for run in merge_runs
            if merge_queue_pr_number(run.get("head_branch")) == number or run["id"] in linked_ids
        ]
        mine.sort(key=lambda run: run.get("created_at") or "")

        entries = {}
        for run in mine:
            branch = run.get("head_branch") or ""
            head_sha = run.get("head_sha") or ""
            # A run without both keys cannot be grouped with another run.
            key = (branch, head_sha) if branch and head_sha else (branch, head_sha, run["id"])
            entries.setdefault(key, []).append(run)
        ordered = sorted(entries.values(), key=lambda group: min(run.get("created_at") or "" for run in group))

        comments = [event for event in timeline if event.get("event") == "commented" and event.get("created_at")]
        fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        attempts = []
        for index, group in enumerate(ordered):
            next_start = None
            if index + 1 < len(ordered):
                next_start = min(run.get("created_at") or "" for run in ordered[index + 1])
            attempts.append(self._build_attempt(repo, number, group, comments, next_start, fetched_at))
        return attempts

    def _build_attempt(self, repo, number, runs, comments, next_start, fetched_at) -> dict:
        runs = sorted(runs, key=lambda run: run.get("created_at") or "")
        primary = runs[0]
        conclusion = _entry_conclusion(runs)

        starts = [run.get("run_started_at") or run.get("created_at") for run in runs]
        starts = [start for start in starts if start]
        started_at = min(starts) if starts else None
        # An entry is complete only when every one of its workflows is.
        completed_at = None
        if all(run.get("status") == "completed" for run in runs):
            completed = [run.get("updated_at") for run in runs if run.get("updated_at")]
            completed_at = max(completed) if completed else None

        failed = conclusion not in ("", "success")
        has_successor = next_start is not None
        ejected = failed or has_successor

        reason, source, comment_url, comment_author, comment_at = ("", "", "", "", "")
        if ejected and failed:
            comment = self._queue_comment(comments, primary, next_start)
            if comment is not None:
                reason = clean_comment_body(comment.get("body"))
                source = "comment"
                comment_url = comment.get("html_url") or ""
                comment_author = (comment.get("user") or {}).get("login") or ""
                comment_at = comment.get("created_at") or ""
            if not reason:
                reason = f"merge_group entry (run {primary['id']}) concluded {conclusion or 'no conclusion'}"
                source = "run"

        return {
            "repo": repo,
            "number": number,
            "run_id": primary["id"],
            "run_number": primary.get("run_number"),
            "run_attempt": primary.get("run_attempt"),
            "branch": primary.get("head_branch") or "",
            "head_sha": primary.get("head_sha") or "",
            "run_ids": json.dumps([run["id"] for run in runs]),
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": seconds_between(started_at, completed_at),
            "url": primary.get("html_url") or "",
            "ejected": 1 if ejected else 0,
            "ejection_reason": reason,
            "ejection_kind": ejection_kind(conclusion, reason) if ejected else "",
            "reason_source": source,
            "comment_url": comment_url,
            "comment_author": comment_author,
            "comment_at": comment_at,
            "fetched_at": fetched_at,
        }

    def _queue_comment(self, comments, run, next_start):
        """The queue comment that explains this failed attempt: posted after
        the run started, before the next attempt, and about the queue."""
        window_start = run.get("created_at") or ""
        window_end = next_start or ""
        fallback_end = (
            (parse_iso(window_start) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if parse_iso(window_start)
            else ""
        )
        window_end = window_end or fallback_end
        for comment in comments:
            created = comment.get("created_at") or ""
            if window_start and created < window_start:
                continue
            if window_end and created > window_end:
                continue
            body = (comment.get("body") or "").lower()
            if any(marker in body for marker in MERGE_QUEUE_COMMENT_MARKERS):
                return comment
        return None

    def _count_retests(self, repo, number):
        """E2E pull_request job rows for this PR beyond the first, the same
        definition as the exporter's pr_merges.retest_count. Reads the
        exporter's job history read-only; no API call. None when that DB is
        not there, so "unknown" never renders as "no retests"."""
        jobs_db = os.path.join(os.path.dirname(self.db_file), "workflow-exporter.db")
        if not os.path.exists(jobs_db):
            return None
        try:
            with sqlite3.connect(jobs_db, timeout=5) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE repo = ? AND pr_display = ? "
                    "AND category = 'e2e' AND event = 'pull_request'",
                    (repo, f"#{number}"),
                ).fetchone()
        except sqlite3.Error:
            return None
        return max(0, (row[0] if row else 0) - 1)

    def _write_pr(self, record, attempts):
        """One transaction: the PR row, then its attempts and their stage
        timings. Replacing by key makes a --force re-run idempotent."""
        if attempts:
            completed = [a["completed_at"] for a in attempts if a["completed_at"]]
            record["last_attempt_at"] = max(completed) if completed else None
            record["queued_at"] = attempts[0]["started_at"]
            record["waiting_seconds"] = seconds_between(record["first_approval_at"], record["queued_at"])
            queue_end = record["merged_at"] or record["last_attempt_at"] or record["closed_at"]
            record["queue_seconds"] = seconds_between(record["queued_at"], queue_end)
            record["merged_seconds"] = seconds_between(record["last_attempt_at"], record["merged_at"])
            record["final_stage"] = final_stage(
                record["state"], record["first_review_at"], record["first_approval_at"], record["queued_at"]
            )

        columns = list(record.keys())
        placeholders = ", ".join(f":{column}" for column in columns)
        with self._db() as conn:
            conn.execute(f"INSERT OR REPLACE INTO pr_events ({', '.join(columns)}) VALUES ({placeholders})", record)
            conn.execute(
                "DELETE FROM pr_queue_attempts WHERE repo = ? AND number = ?", (record["repo"], record["number"])
            )
            if attempts:
                attempt_columns = list(attempts[0].keys())
                attempt_placeholders = ", ".join(f":{column}" for column in attempt_columns)
                conn.executemany(
                    f"INSERT OR REPLACE INTO pr_queue_attempts ({', '.join(attempt_columns)}) "
                    f"VALUES ({attempt_placeholders})",
                    attempts,
                )

    def _prune(self, repo, cutoff_iso):
        """Drop pull requests that left the retention window, and their
        attempts, so the file cannot grow without bound."""
        with self._db() as conn:
            stale = conn.execute(
                "SELECT number FROM pr_events WHERE repo = ? AND COALESCE(merged_at, closed_at, opened_at) < ?",
                (repo, cutoff_iso),
            ).fetchall()
            for row in stale:
                conn.execute("DELETE FROM pr_queue_attempts WHERE repo = ? AND number = ?", (repo, row["number"]))
            conn.execute(
                "DELETE FROM pr_events WHERE repo = ? AND COALESCE(merged_at, closed_at, opened_at) < ?",
                (repo, cutoff_iso),
            )

    # -- read side ----------------------------------------------------------

    def get_json(self, params, now=None):
        """The /api/ci-observe/prs document.

        ?number=<n>[&repo=<name>]  one pull request: its row, its stages and
                                   its queue attempts
        ?days=<n>                  the index: recent pull requests and the
                                   stage funnel (median and p95 per stage)

        `now` pins "how fresh is this" for a test; the endpoint leaves it
        at the request time.
        """
        now = now or datetime.now(UTC)
        days = _param(params, "days")
        try:
            days = int(days) if days else DEFAULT_INDEX_DAYS
        except ValueError:
            days = DEFAULT_INDEX_DAYS
        days = max(1, min(days, PRS_HISTORY_DAYS))

        number = _param(params, "number")
        if number:
            try:
                number = int(number)
            except ValueError:
                return self._empty_number_document(
                    number,
                    days,
                    now,
                    note=f"{number!r} is not a pull request number.",
                    reason=f"{number!r} is not a number, so no pull request state applies.",
                    freshness=self._freshness(now) if os.path.exists(self.db_file) else None,
                )
            if not os.path.exists(self.db_file):
                return self._empty_number_document(number, days, now)
            return self.pr_document(number, _param(params, "repo"), days, now)
        if not os.path.exists(self.db_file):
            return self._empty_index_document(days, now)
        return self.index_document(days, now)

    # The endpoint is read-only: it never creates prs.db. A missing file is
    # the state before the backfill has run, and it says so instead of
    # looking like a repository with no pull requests.

    def _empty_number_document(self, number, days, now, note=None, reason=None, freshness=None):
        freshness = freshness or self._never_freshness(now)
        notes = [note] if note else []
        if reason is None:
            state = "not_yet_fetched"
            reason = (
                f"#{number} cannot be resolved: prs.db does not exist yet, so the backfill has not run "
                "and there is no live answer to fall back on."
            )
        else:
            state = "missing"
        notes.append(reason)
        return {
            "mode": "pr",
            "generated_at": _utc_now(),
            "days": days,
            "pr": None,
            "stages": [],
            "attempts": [],
            "pr_state": state,
            "pr_state_message": reason,
            "source": "none",
            "states": _states_document(),
            "notes": notes,
            "derivation": DERIVATION,
            **self._freshness_fields(now, freshness),
        }

    def _empty_index_document(self, days, now):
        freshness = self._never_freshness(now)
        return {
            "mode": "index",
            "generated_at": _utc_now(),
            "days": days,
            "prs": [],
            "funnel": _funnel([]),
            # The detail panels query this same endpoint with an empty
            # number. Present keys let them render "enter a number" instead
            # of a root-not-found error.
            "pr": None,
            "stages": [],
            "attempts": [],
            # The detail panels query this endpoint with an empty number.
            # These keys let the resolved-state panel say "enter a number"
            # instead of showing a root-not-found error.
            "pr_state": NO_SELECTION_STATE,
            "pr_state_message": NO_SELECTION_MESSAGE,
            "source": "none",
            "notes": [
                "prs.db has not been backfilled yet: run monitoring/scripts/ci_observe_prs.py "
                "--days 60. This is an empty dataset, not a repository with no pull requests."
            ],
            "states": _states_document(),
            "derivation": DERIVATION,
            **self._freshness_fields(now, freshness),
        }

    # -- freshness ----------------------------------------------------------

    def _never_freshness(self, now):
        """The freshness of a page whose backfill has never run. It reads no
        database on purpose: the read path must not create prs.db."""
        next_run = _iso(_next_run_at(now))
        return {
            "state": "never",
            "age_seconds": None,
            "last_fetched_at": None,
            "coverage_until": None,
            "coverage_age_seconds": None,
            "next_run_at": next_run,
            "last_run_mode": "",
            "stored": 0,
            "skipped": 0,
            "failed": 0,
            "api_calls": 0,
            "rate_limit_remaining": None,
            "stopped_early": "",
            "ok": False,
            "message": (
                "The pull-request backfill has not run yet, so there is no data to show. "
                f"The hourly timer starts the first pass by {next_run}."
            ),
        }

    def _freshness(self, now):
        """What the data covers, when it last moved, and when it moves next.

        Freshness is the age of the data, not the age of the last process
        start: a pass that fails its listing every hour still writes a row,
        and reporting that as fresh would hide days-old data. Data through
        comes from the last pass that finished its window; the last run time
        is reported separately because a pass can run and store nothing."""
        last = self._last_run()
        if last is None:
            return self._never_freshness(now)
        coverage = self._coverage_until()
        next_run = _iso(_next_run_at(now))
        age = seconds_between(last["ran_at"], _iso(now))
        coverage_age = seconds_between(coverage, _iso(now)) if coverage else None
        stale = (
            coverage_age is None
            or coverage_age > FRESHNESS_STALE_AFTER_SECONDS
            or (age is not None and age > FRESHNESS_STALE_AFTER_SECONDS)
        )
        state = "stale" if stale else "fresh"
        through = f"{coverage} ({_minutes_ago(coverage_age)})" if coverage else "never"
        message = (
            f"Data through {through}; refreshed hourly; a pull request merged now appears by {next_run}. "
            f"The backfill last ran at {last['ran_at']} ({_minutes_ago(age)})."
        )
        if last["stopped_early"]:
            message += f" The last run stopped early: {last['stopped_early']}"
        return {
            "state": state,
            "age_seconds": age,
            "last_fetched_at": last["ran_at"],
            "coverage_until": coverage,
            "coverage_age_seconds": coverage_age,
            "next_run_at": next_run,
            "last_run_mode": last["mode"],
            "stored": last["stored"],
            "skipped": last["skipped"],
            "failed": last["failed"],
            "api_calls": last["api_calls"],
            "rate_limit_remaining": last["rate_limit_remaining"],
            "stopped_early": last["stopped_early"],
            "ok": bool(last["ok"]),
            "message": message,
        }

    def _freshness_fields(self, now, freshness=None):
        freshness = freshness or self._freshness(now)
        return {
            "last_fetched_at": freshness["last_fetched_at"],
            "coverage_until": freshness["coverage_until"],
            "next_run_at": freshness["next_run_at"],
            "freshness": freshness,
        }

    def _run_covering(self, ended):
        """The first completed pass whose window reached `ended`. Its
        counters explain a missing row."""
        if not ended:
            return None
        try:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT * FROM pr_backfill_runs WHERE listing_complete = 1 AND window_to >= ? "
                    "ORDER BY window_to ASC, id ASC LIMIT 1",
                    (ended,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None

    # -- live resolve -------------------------------------------------------

    def _live_pr(self, repo, number):
        """One read-only GitHub lookup for a pull request that is not
        stored. Returns (pr object or None, why it failed). It never writes
        prs.db and never spends a backfill's rate-limit bookkeeping."""
        if not repo or not REPO_NAME_RE.match(repo) or repo in (".", ".."):
            return None, f"{repo!r} is not a repository name"
        if self.api is None:
            return None, "no GitHub access is configured"
        key = (repo, number)
        cached = _LIVE_PR_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < cached[3]:
            return cached[1], cached[2]
        url = f"{API_URL}/repos/{ORG}/{repo}/pulls/{number}"
        try:
            resp = self.api._get(url)
        except requests.RequestException as exc:
            return self._remember_live(
                key, None, f"the live lookup failed: {exc}", LIVE_FAILURE_CACHE_TTL_SECONDS
            )
        if resp.status_code == 404:
            return self._remember_live(key, None, f"GitHub has no pull request #{number} in {repo}")
        if not resp.ok:
            return self._remember_live(
                key,
                None,
                f"the live lookup returned HTTP {resp.status_code}",
                LIVE_FAILURE_CACHE_TTL_SECONDS,
            )
        return self._remember_live(key, resp.json(), "")

    @staticmethod
    def _remember_live(key, document, message, ttl=LIVE_CACHE_TTL_SECONDS):
        """Cache one live answer, success or failure, and prune expired
        entries. Returns (document, message) so the caller stays a return."""
        now = time.monotonic()
        _LIVE_PR_CACHE[key] = (now, document, message, ttl)
        if len(_LIVE_PR_CACHE) > 256:
            for cached_key, entry in list(_LIVE_PR_CACHE.items()):
                if now - entry[0] >= entry[3]:
                    _LIVE_PR_CACHE.pop(cached_key, None)
        return document, message

    def _live_record(self, repo, number, live):
        """The identity a live lookup can supply. It is a PR object with the
        stored fields that GitHub knows, so the detail panel renders it; the
        stages and attempts stay empty because the backfill has no row."""
        labels = {label.get("name") for label in live.get("labels") or [] if isinstance(label, dict)}
        labels.discard(None)
        return {
            "repo": repo,
            "number": number,
            "title": live.get("title") or "",
            "author": (live.get("user") or {}).get("login") or "",
            "url": live.get("html_url") or pr_url(repo, number),
            "state": live.get("state") or "",
            "labels": ",".join(sorted(labels)),
            "opened_at": live.get("created_at"),
            "merged_at": live.get("merged_at"),
            "closed_at": live.get("closed_at"),
            "fetched_at": "",
            "source": "live",
        }

    # -- detail -------------------------------------------------------------

    def pr_document(self, number, repo, days, now=None):
        now = now or datetime.now(UTC)
        with self._db() as conn:
            if repo:
                row = conn.execute("SELECT * FROM pr_events WHERE number = ? AND repo = ?", (number, repo)).fetchone()
                matches = 1 if row else 0
            else:
                rows = conn.execute(
                    "SELECT * FROM pr_events WHERE number = ? ORDER BY COALESCE(merged_at, closed_at) DESC", (number,)
                ).fetchall()
                row = rows[0] if rows else None
                matches = len(rows)
            attempts = []
            if row is not None:
                attempts = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM pr_queue_attempts WHERE repo = ? AND number = ? ORDER BY started_at",
                        (row["repo"], number),
                    )
                ]

        freshness = self._freshness(now)
        notes = []
        if row is None:
            notes.append(
                f"Pull request #{number} has no stored row in the backfill. "
                "The state below says whether it is open, not fetched yet, missing, or outside the window."
            )
        elif matches > 1 and not repo:
            notes.append(
                f"#{number} exists in {matches} repositories; showing {row['repo']}. Add &repo= to choose another."
            )

        source = "store" if row is not None else "none"
        pr_state = "stored" if row is not None else ""
        pr_state_message = ""
        if row is None:
            if repo:
                live, live_error = self._live_pr(repo, number)
            else:
                live, live_error = None, "no repository was given, so there was no live lookup"
            if live is not None:
                source = "live"
            pr_state, pr_state_message = self._miss_state(number, live, live_error, freshness, now)
            notes.append(pr_state_message)

        pr = dict(row) if row is not None else (self._live_record(repo, number, live) if live is not None else None)
        if pr is not None and row is not None:
            pr["url"] = pr.get("url") or pr_url(pr["repo"], pr["number"])
            pr["age_seconds"] = pr.get("total_seconds")
            if pr["age_seconds"] is None:
                pr["age_seconds"] = seconds_between(pr["opened_at"], _iso(now))
            pr["queue_attempt_count"] = len(attempts)
            pr["queue_history"] = "present" if attempts else "none"
            pr_state_message = f"Stored from the backfill; fetched at {pr['fetched_at']}."
            if not attempts:
                notes.append(
                    "No merge_group attempt is stored for this pull request. That is 'no queue history', "
                    "not 'no ejections': queue runs before the backfill window are gone from this file."
                )
            if any(a["ejected"] for a in attempts):
                notes.append(DERIVATION["ejections"])

        return {
            "mode": "pr",
            "generated_at": _utc_now(),
            "days": days,
            "pr": pr,
            "stages": _stages(pr) if row is not None else [],
            "attempts": [_attempt_out(row) for row in attempts],
            "pr_state": pr_state,
            "pr_state_message": pr_state_message,
            "source": source,
            "states": _states_document(),
            "notes": notes,
            "derivation": DERIVATION,
            **self._freshness_fields(now),
        }

    def _miss_state(self, number, live, live_error, freshness, now):
        """Why a pull request has no stored row. Exactly one of the four
        states applies; the order matters because an open pull request has
        no merge time to compare against coverage."""
        if live is not None and live.get("state") == "open" and not live.get("merged_at"):
            return "open", (
                f"Pull request #{number} is open. The backfill stores merged and closed pull requests only, "
                f"so it appears here after it merges or closes. The next hourly run is by "
                f"{freshness['next_run_at']}."
            )
        ended = (live or {}).get("merged_at") or (live or {}).get("closed_at")
        ended_dt = parse_iso(ended)
        if ended_dt is not None:
            if ended_dt < now - timedelta(days=PRS_HISTORY_DAYS):
                return "outside_window", (
                    f"#{number} ended at {ended}, before the {PRS_HISTORY_DAYS}-day retention. "
                    "The backfill does not keep pull requests that old."
                )
            coverage = parse_iso(freshness["coverage_until"])
            if coverage is None or ended_dt > coverage:
                return "not_yet_fetched", (
                    f"#{number} ended at {ended}, after the last completed backfill window "
                    f"(data through {freshness['coverage_until'] or 'never'}). "
                    f"The next hourly run stores it by {freshness['next_run_at']}."
                )
            return "missing", self._missing_message(number, ended, freshness)
        through = freshness["coverage_until"] or "never"
        return "missing", (
            f"The backfill covered its window through {through} and has no row for #{number}. "
            f"The live GitHub lookup gave no answer ({live_error or 'unknown reason'}). "
            f"The next hourly run is by {freshness['next_run_at']}."
        )

    def _missing_message(self, number, ended, freshness):
        """Name the pass that should have stored the row and why it did not.
        stopped_early is the rate-limit or listing failure the pass recorded;
        a failed count covers a pull request whose pages did not come back."""
        covering = self._run_covering(ended)
        if covering is not None and covering["stopped_early"]:
            return f"The backfill covered {ended} but that pass stopped early: {covering['stopped_early']}"
        if covering is not None and covering["failed"]:
            return (
                f"The backfill covered {ended} but {covering['failed']} pull request(s) failed to fetch in "
                f"that pass, and #{number} has no row. The daily full pass retries it."
            )
        return (
            f"The backfill completed the pass that covers {ended} but has no row for #{number}. "
            f"The daily full pass retries it; the next hourly run is by {freshness['next_run_at']}."
        )

    def index_document(self, days, now=None):
        now = now or datetime.now(UTC)
        cutoff = _iso(now - timedelta(days=days))
        with self._db() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pr_events WHERE COALESCE(merged_at, closed_at, opened_at) >= ? "
                    "ORDER BY COALESCE(merged_at, closed_at, opened_at) DESC",
                    (cutoff,),
                )
            ]
            attempts = {}
            for attempt in conn.execute("SELECT * FROM pr_queue_attempts WHERE started_at >= ?", (cutoff,)):
                attempts.setdefault((attempt["repo"], attempt["number"]), []).append(dict(attempt))

        funnel = _funnel(rows)
        prs = []
        for row in rows:
            row_attempts = attempts.get((row["repo"], row["number"]), [])
            row["url"] = row.get("url") or pr_url(row["repo"], row["number"])
            row["age_seconds"] = row.get("total_seconds")
            if row["age_seconds"] is None:
                row["age_seconds"] = seconds_between(row["opened_at"], _iso(now))
            row["queue_attempt_count"] = len(row_attempts)
            row["ejected_attempt_count"] = sum(1 for a in row_attempts if a["ejected"])
            row["age_display"] = human_duration(row["age_seconds"])
            prs.append(row)

        fields = self._freshness_fields(now)
        freshness = fields["freshness"]
        notes = []
        if not rows:
            notes.append("The index is empty for this window: run the backfill.")
        if freshness["state"] == "never":
            notes.append(freshness["message"])
        elif freshness["state"] == "stale":
            notes.append(
                f"The backfill is stale: {freshness['message']} Rows older than the last completed window "
                "may be missing."
            )
        return {
            "mode": "index",
            "generated_at": _utc_now(),
            "days": days,
            "prs": prs,
            "funnel": funnel,
            "pr": None,
            "stages": [],
            "attempts": [],
            # No number selected: the resolved-state panel says so.
            "pr_state": NO_SELECTION_STATE,
            "pr_state_message": NO_SELECTION_MESSAGE,
            "source": "none",
            "states": _states_document(),
            "notes": notes,
            "derivation": DERIVATION,
            **fields,
        }


# ---------------------------------------------------------------------------
# Document shaping
# ---------------------------------------------------------------------------
def _param(params, name):
    values = params.get(name) or []
    return values[0].strip() if values else ""


def _utc_now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stages(pr):
    """The five stages, always all five, with their measured duration or the
    reason it could not be measured. A missing boundary is 'unknown'; a
    stage the pull request never reached is 'not_reached'."""
    if pr is None:
        return []
    boundaries = {
        "authoring": (pr.get("opened_at"), pr.get("first_review_at"), pr.get("authoring_seconds")),
        "review": (pr.get("first_review_at"), pr.get("first_approval_at"), pr.get("review_seconds")),
        "waiting": (pr.get("first_approval_at"), pr.get("queued_at"), pr.get("waiting_seconds")),
        "queue": (pr.get("queued_at"), pr.get("merged_at") or pr.get("last_attempt_at"), pr.get("queue_seconds")),
        "merged": (pr.get("last_attempt_at"), pr.get("merged_at"), pr.get("merged_seconds")),
    }
    reasons = {
        "authoring": "no human review after the pull request opened",
        "review": "the pull request was never reviewed by a human",
        "waiting": "the pull request was never approved by a human",
        "queue": "the pull request has no merge_group attempt",
        "merged": "the pull request did not merge",
    }
    stages = []
    for key, label in STAGES:
        start, end, seconds = boundaries[key]
        if key == "merged":
            if pr.get("state") != "merged":
                state = "not_reached"
            elif not pr.get("last_attempt_at"):
                # A direct merge has no queue attempt to measure from. That
                # is unknown, not a zero-length stage.
                state = "unknown"
                reasons["merged"] = "the pull request merged without a merge_group attempt"
            else:
                state = "measured"
        elif (key == "queue" and not pr.get("queued_at")) or (key in ("review", "waiting") and not start):
            state = "not_reached"
        elif seconds is None:
            state = "unknown"
        else:
            state = "measured"
        stages.append(
            {
                "stage": key,
                "label": label,
                "start_at": start,
                "end_at": end,
                "seconds": seconds,
                "state": state,
                "missing_reason": "" if state == "measured" else reasons[key],
            }
        )
    return stages


def _funnel(rows):
    """Median and p95 duration per stage over the rows in the window. A row
    without that stage is not a zero: it is excluded from the sample, and
    `samples` says how many rows really contributed."""
    funnel = []
    for key, label in STAGES:
        values = sorted(row[f"{key}_seconds"] for row in rows if row.get(f"{key}_seconds") is not None)
        funnel.append(
            {
                "stage": key,
                "label": label,
                "samples": len(values),
                "median_seconds": percentile(values, 50),
                "p95_seconds": percentile(values, 95),
            }
        )
    return funnel


def _attempt_out(row):
    """The queue-attempt row as the dashboard reads it."""
    attempt = dict(row)
    attempt["run_ids"] = _stored_run_ids(attempt.get("run_ids"), attempt.get("run_id"))
    attempt["run_count"] = len(attempt["run_ids"])
    attempt["run_urls"] = [
        f"https://github.com/{ORG}/{attempt['repo']}/actions/runs/{run_id}" for run_id in attempt["run_ids"]
    ]
    attempt["run_url"] = attempt.get("url") or ""
    attempt["duration_display"] = human_duration(attempt.get("duration_seconds"))
    attempt["ejected"] = bool(attempt.get("ejected"))
    attempt["ejection"] = "ejected" if attempt["ejected"] else "not ejected"
    # The store does not know Tempo. The exporter fills capture_url from the
    # run cache when a capture exists; an empty value is the store-only view.
    attempt["capture_url"] = ""
    return attempt


def _stored_run_ids(value, run_id):
    """The workflow run ids of one queue attempt. A row written before the
    grouping carries only its primary run id."""
    try:
        ids = json.loads(value) if value else []
    except (TypeError, ValueError):
        ids = []
    if not ids and run_id is not None:
        ids = [run_id]
    return ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill ci-observe pull-request history into prs.db")
    parser.add_argument("--days", type=int, default=PRS_HISTORY_DAYS, help="window in days (default 30)")
    parser.add_argument("--repo", action="append", default=[], help="repository name; repeatable")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many pull requests")
    parser.add_argument("--force", action="store_true", help="refetch pull requests already stored")
    parser.add_argument("--db", default=PRS_DB_FILE, help=f"SQLite file (default {PRS_DB_FILE})")
    parser.add_argument(
        "--mode",
        choices=("auto", "full", "incremental"),
        default="auto",
        help="auto runs a full pass once a day and incremental passes the rest of the hours",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    service = CIObservePRs(db_file=args.db, api=StandaloneAPI(), repos=args.repo)
    summary = service.backfill(days=args.days, force=args.force, limit=args.limit, mode=args.mode)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

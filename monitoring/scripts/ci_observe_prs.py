"""Pull-request history for the ci-observe Pull requests surface.

One-shot backfill, not a poller. It walks the merged and closed pull
requests of the last N days in each repository, classifies its merge-queue
attempts, and writes them to prs.db (a separate SQLite file next to the
workflow exporter's jobs DB). The exporter's existing 90-second loop never
calls the GitHub API for this data: the endpoint in workflow-exporter.py
only reads prs.db.

What is fetched, per pull request (one pass, no repeats):

  - the PR object from the closed-PR listing (opened, merged, closed,
    author, labels)
  - the issue timeline (review requests, label history, comments,
    merged/closed fallbacks)
  - the reviews (first human review and first human approval)
  - the commits (commit count)

Merge-queue runs are fetched once per repository, not per pull request:
`GET /repos/{org}/{repo}/actions/runs?event=merge_group&created=<cutoff>..<now>`.
The runs list stops near 1000 results, so a busy window is split in half
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

Manual use (the service never runs this):

    python3 monitoring/scripts/ci_observe_prs.py --days 60
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
from datetime import UTC, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
PRS_DB_FILE = os.path.join(CACHE_DIR, "prs.db")

# Matches the workflow exporter's 60-day job history, so a pull request and
# the jobs it produced cover the same window.
PRS_HISTORY_DAYS = int(os.getenv("JOBS_HISTORY_DAYS", "60"))
DEFAULT_INDEX_DAYS = 30
PAGE_SIZE = 100
# GitHub's actions/runs list stops near 1000 results. Split a merge-group
# window before it reaches that, the same safe limit the exporter uses.
MERGE_GROUP_SAFE_RESULT_LIMIT = 900
# Stop a run before it drains the token: 5000 requests/hour is the shared
# budget of the exporter and every interactive GitHub call. A stopped run
# leaves the remaining pull requests unstored, and the next run resumes.
MIN_RATE_LIMIT_REMAINING = 200

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
        "Queue runs carry no ci-observe capture today, so a queue attempt links to its GitHub run, not to a capture."
    ),
    "window": (
        "The backfill covers merged and closed pull requests only. An open pull request is not stored "
        "until it is merged or closed."
    ),
}


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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_events_merged_at ON pr_events(merged_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_events_closed_at ON pr_events(closed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pr_queue_attempts_pr ON pr_queue_attempts(repo, number)")
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
        if self.repos_filter:
            return self.repos_filter
        url = f"{API_URL}/orgs/{ORG}/repos?per_page={PAGE_SIZE}&type=all"
        repos = []
        while url:
            page, url = self._get_page(url)
            if page is None:
                return repos
            repos.extend(r["name"] for r in page if not r.get("archived"))
        return repos

    # -- backfill -----------------------------------------------------------

    def backfill(self, days=PRS_HISTORY_DAYS, force=False, limit=None, now=None):
        """Fetch and store every merged/closed pull request in the window.

        `now` pins the window for a reproducible replay; the service never
        calls this. Returns a summary dict. Safe to run repeatedly: stored
        pull requests are skipped, and every write is keyed, so a second run
        does not duplicate a row or change a value.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=days)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._init_db()
        summary = {
            "days": days,
            "cutoff": cutoff_iso,
            "repos": [],
            "prs_stored": 0,
            "prs_skipped": 0,
            "prs_failed": 0,
            "attempts_stored": 0,
            "api_calls": 0,
            "rate_limit_remaining": None,
            "stopped_early": "",
        }

        for repo in self._list_repos():
            repo_summary = {"repo": repo, "prs_stored": 0, "prs_skipped": 0, "prs_failed": 0, "attempts_stored": 0}
            merge_runs = self._fetch_merge_group_runs(repo, cutoff_iso, now)
            prs = self._fetch_closed_prs(repo, cutoff)
            stored = 0
            for pr in prs:
                if force or not self._is_stored(repo, pr["number"]):
                    attempts = self._backfill_one(repo, pr, merge_runs)
                    if attempts is None:
                        repo_summary["prs_failed"] += 1
                    else:
                        repo_summary["prs_stored"] += 1
                        repo_summary["attempts_stored"] += attempts
                        stored += 1
                else:
                    repo_summary["prs_skipped"] += 1

                if self.rate_limit_remaining is not None and self.rate_limit_remaining < MIN_RATE_LIMIT_REMAINING:
                    summary["stopped_early"] = (
                        f"stopped in {repo} after {stored} pull request(s): rate limit remaining "
                        f"{self.rate_limit_remaining} is below {MIN_RATE_LIMIT_REMAINING}. Re-run to resume."
                    )
                    logger.warning(summary["stopped_early"])
                    break
                if limit is not None and summary["prs_stored"] + repo_summary["prs_stored"] >= limit:
                    summary["stopped_early"] = f"stopped after --limit {limit} pull request(s)."
                    break
            self._prune(repo, cutoff_iso)
            summary["repos"].append(repo_summary)
            summary["prs_stored"] += repo_summary["prs_stored"]
            summary["prs_skipped"] += repo_summary["prs_skipped"]
            summary["prs_failed"] += repo_summary["prs_failed"]
            summary["attempts_stored"] += repo_summary["attempts_stored"]
            if summary["stopped_early"]:
                break

        summary["api_calls"] = self.api_calls
        summary["rate_limit_remaining"] = self.rate_limit_remaining
        logger.info(
            "Pull-request backfill: %d stored, %d already present, %d attempts, %d API calls",
            summary["prs_stored"],
            summary["prs_skipped"],
            summary["attempts_stored"],
            summary["api_calls"],
        )
        return summary

    def _fetch_closed_prs(self, repo, cutoff):
        """Closed PRs whose last activity is inside the window. Sorted by
        updated, so pagination stops as soon as a whole page is older."""
        url = f"{API_URL}/repos/{ORG}/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={PAGE_SIZE}"
        prs = []
        while url:
            page, url = self._get_page(url)
            if page is None:
                break
            for pr in page:
                ended = pr.get("merged_at") or pr.get("closed_at")
                if ended and parse_iso(ended) and parse_iso(ended) >= cutoff:
                    prs.append(pr)
            if page and all((parse_iso(pr.get("updated_at")) or cutoff) < cutoff for pr in page):
                break
        return prs

    def _fetch_merge_group_runs(self, repo, cutoff_iso, until=None) -> list[dict] | None:
        """Every merge_group run created in the window, once per repo.

        The runs list stops near 1000 results, so a window with more runs is
        split in half until each query fits. Without that split, runs past
        the cap would be dropped and their pull requests would lose queue
        attempts without any error.

        None means the fetch failed. The caller then leaves every affected
        pull request unstored instead of writing "no queue history" over a
        transient error.
        """
        cutoff = parse_iso(cutoff_iso)
        if cutoff is None:
            return None
        return self._fetch_merge_group_range(repo, cutoff, until or datetime.now(UTC))

    def _fetch_merge_group_range(self, repo, since_dt, until_dt) -> list[dict] | None:
        since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        until_iso = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
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

    def get_json(self, params):
        """The /api/ci-observe/prs document.

        ?number=<n>[&repo=<name>]  one pull request: its row, its stages and
                                   its queue attempts
        ?days=<n>                  the index: recent pull requests and the
                                   stage funnel (median and p95 per stage)
        """
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
                return self._empty_number_document(number, days, note=f"{number!r} is not a pull request number.")
            if not os.path.exists(self.db_file):
                return self._empty_number_document(number, days)
            return self.pr_document(number, _param(params, "repo"), days)
        if not os.path.exists(self.db_file):
            return self._empty_index_document(days)
        return self.index_document(days)

    # The endpoint is read-only: it never creates prs.db. A missing file is
    # the state before the backfill has run, and it says so instead of
    # looking like a repository with no pull requests.

    def _empty_number_document(self, number, days, note=None):
        return {
            "mode": "pr",
            "generated_at": _utc_now(),
            "days": days,
            "pr": None,
            "stages": [],
            "attempts": [],
            "notes": [note] if note else [],
            "derivation": DERIVATION,
        }

    def _empty_index_document(self, days):
        return {
            "mode": "index",
            "generated_at": _utc_now(),
            "days": days,
            "prs": [],
            "funnel": _funnel([]),
            "notes": [
                "prs.db has not been backfilled yet: run monitoring/scripts/ci_observe_prs.py "
                "--days 60. This is an empty dataset, not a repository with no pull requests."
            ],
            "derivation": DERIVATION,
        }

    def pr_document(self, number, repo, days):
        now = datetime.now(UTC)
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

        notes = []
        if row is None:
            notes.append(
                f"Pull request #{number} is not in the backfill. Only merged and closed pull requests "
                f"are stored, and only inside the {days}-day window."
            )
        elif matches > 1 and not repo:
            notes.append(
                f"#{number} exists in {matches} repositories; showing {row['repo']}. Add &repo= to choose another."
            )

        pr = dict(row) if row is not None else None
        if pr is not None:
            pr["url"] = pr.get("url") or pr_url(pr["repo"], pr["number"])
            pr["age_seconds"] = pr.get("total_seconds")
            if pr["age_seconds"] is None:
                pr["age_seconds"] = seconds_between(pr["opened_at"], now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            pr["queue_attempt_count"] = len(attempts)
            pr["queue_history"] = "present" if attempts else "none"
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
            "stages": _stages(pr),
            "attempts": [_attempt_out(row) for row in attempts],
            "notes": notes,
            "derivation": DERIVATION,
        }

    def index_document(self, days):
        now = datetime.now(UTC)
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                row["age_seconds"] = seconds_between(row["opened_at"], _utc_now())
            row["queue_attempt_count"] = len(row_attempts)
            row["ejected_attempt_count"] = sum(1 for a in row_attempts if a["ejected"])
            row["age_display"] = human_duration(row["age_seconds"])
            prs.append(row)

        return {
            "mode": "index",
            "generated_at": _utc_now(),
            "days": days,
            "prs": prs,
            "funnel": funnel,
            "notes": [] if rows else ["The index is empty for this window: run the backfill."],
            "derivation": DERIVATION,
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
    # A queue run has no capture today. Keeping the field makes the missing
    # link explicit instead of leaving the column off the dashboard.
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
    parser.add_argument("--days", type=int, default=PRS_HISTORY_DAYS, help="window in days (default 60)")
    parser.add_argument("--repo", action="append", default=[], help="repository name; repeatable")
    parser.add_argument("--limit", type=int, default=None, help="stop after this many pull requests")
    parser.add_argument("--force", action="store_true", help="refetch pull requests already stored")
    parser.add_argument("--db", default=PRS_DB_FILE, help=f"SQLite file (default {PRS_DB_FILE})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    service = CIObservePRs(db_file=args.db, api=StandaloneAPI(), repos=args.repo)
    summary = service.backfill(days=args.days, force=args.force, limit=args.limit)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

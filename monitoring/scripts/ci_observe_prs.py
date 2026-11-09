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
  - one GraphQL query (LIFECYCLE_QUERY): files and lines changed, the
    code-owner teams each review counted for and the code-owner requests
    still pending, and the merge-queue and auto-merge events with GitHub's
    removal reasons

Together they give the lifecycle: every event in order (pr_timeline), where
the pull request was at every moment (draft, waiting for a reviewer, with
the author, waiting for another working group, approved, in the queue, out
of the queue), review rounds, re-approvals after a push or a rebase, and the
working groups that had to approve. GraphQL has its own hourly budget; a
pass stops when it runs low, and a pull request whose query failed is
stored in the previous record format and read again later.

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

GitHub records when a pull request entered and left the merge queue (the
GraphQL timeline), but not how each merge_group attempt ended. That is
derived (queue_outcome): ejected when its own merge_group runs failed or
timed out, re-queued when it was cancelled or replaced by a later attempt
(every entry behind a failing one is), passed otherwise. `Enqueued` is
GitHub's added-to-merge-queue time; a row read without the GraphQL answer
uses the start of the pull request's first merge_group run.

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
    # once, after an upgrade of the record format: read every older row now
    python3 monitoring/scripts/ci_observe_prs.py --days 90 --refresh-old
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_observe_phases as phases

logger = logging.getLogger(__name__)

ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
GRAPHQL_URL = os.getenv("GRAPHQL_URL", f"{API_URL}/graphql")
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
# How long a pull request's history is kept. Its E2E counts are read from
# the 30-day job history when the backfill stores it, so a stored row keeps
# them after the jobs are gone; 90 days matches the trends.db archive.
PRS_HISTORY_DAYS = int(os.getenv("CI_OBSERVE_KEEP_DAYS", "90"))
DEFAULT_INDEX_DAYS = 30
# The record format. A row stored by an older format is refetched once so
# the new counts fill in, a bounded number per pass so the shared GitHub
# budget is not spent in one hour. Version 3 adds the lifecycle: the event
# list, the code-owner teams, the size of the change and the merge-queue
# entries as GitHub records them. --refresh-old lifts the per-pass bound
# for a one-time catch-up.
RECORD_VERSION = 3
# The format that added the rebase and E2E counts. A version 2 row has them
# even when its lifecycle is not measured yet.
COUNTS_RECORD_VERSION = 2
LEGACY_REFRESH_PER_PASS = 100
# GraphQL has its own hourly budget, separate from the REST one.
MIN_GRAPHQL_REMAINING = 100
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
        "Enqueued is GitHub's own added-to-merge-queue time, read with one GraphQL query per pull request. "
        "A row stored before that query existed, or whose query failed, uses the start of its first "
        "merge_group run until the backfill reads it again."
    ),
    "ejections": (
        "Removed from the queue, and why, is GitHub's removed-from-merge-queue event and its reason. The "
        "queue attempts table derives each attempt's outcome from its merge_group runs: Ejected, the entry's "
        "own runs failed or timed out; Re-queued, the entry was cancelled or replaced by a later one, which "
        "is what happens behind a failing entry, after a new push or when the pull request is taken out by hand."
    ),
    "lifecycle": (
        "The lifecycle walks the pull request's events in order. Draft until ready for review. Waiting for "
        "a reviewer until a human reviews. Changes requested, or comments while it waits, hand it to the "
        "author until the next push. An approval makes it Waiting for another WG until every working group "
        "that has to approve did, then Approved, not queued. A push after the approval sends it back to the "
        "reviewers when GitHub dismissed the approval or when an approval follows before the next queue "
        "entry (a re-review). In the merge queue from GitHub's added event; Out of the queue from a removal "
        "other than the merge, until it is added again. The author's own replies in review threads are not "
        "reviews."
    ),
    "working_groups": (
        "Working groups that had to approve are the code-owner teams GitHub reports on the pull request: the "
        "teams each review was made on behalf of, plus the code-owner review requests still pending. GitHub "
        "keeps no list of a requirement once it is met, so this is read from who approved for whom, not "
        "from CODEOWNERS. maintainers, the parent of the wg-* teams, adds nothing next to a working group; "
        "alone it is one working group, any of them. A team review requested by hand looks the same as a "
        "code-owner request once the team reviewed."
    ),
    "review_rounds": (
        "A review round is one version of the code that got a human review: a review records the commit it "
        "looked at. A re-approval is an approval by a reviewer, or for a team, that had approved an earlier "
        "version; after a rebase when a force push came between the two."
    ),
    "pushes": (
        "Pushes come from the commits on the branch now and GitHub's force-push events. A rebase replaces "
        "the commits, so the pushes before the last rebase show only as that force push. Commits a few "
        "minutes apart are one push; a merge commit is the base branch merged in."
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
        "The pull request is open. The page reads it from GitHub when it opens; the saved history starts "
        "after it merges or closes.",
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
# The index document, built once for the dozens of panels that ask for it
# together; see CIObservePRs._cached_index.
INDEX_CACHE_SECONDS = 15
_INDEX_CACHE = {}
_INDEX_BUILDING = {}
_INDEX_CACHE_LOCK = threading.Lock()
# Index row columns the across-PR views read and no panel shows.
INDEX_UNSHOWN_COLUMNS = ("lifecycle", "lifecycle_seconds", "removal_reasons", "owner_teams")


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


def ordered_seconds(start, end):
    """Seconds from start to end when end is not before start; None when
    either is missing or the order is reversed. A stage whose end comes
    first did not happen in that order, and a zero would claim it took no
    time."""
    start_dt = parse_iso(start)
    end_dt = parse_iso(end)
    if start_dt is None or end_dt is None or end_dt < start_dt:
        return None
    return round((end_dt - start_dt).total_seconds())


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


def _short_moment(value: str | None) -> str:
    """A clock reading a reader can scan, e.g. 12 Mar 09:10."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    return moment.astimezone(UTC).strftime("%d %b %H:%M")


def dates_display(
    state: str | None,
    opened_at: str | None,
    merged_at: str | None,
    closed_at: str | None,
) -> str:
    """One line for the identity row: opened, then merged, closed or open."""
    opened = _short_moment(opened_at)
    parts = [f"Opened {opened}" if opened else "Opened unknown"]
    if merged_at:
        parts.append(f"Merged {_short_moment(merged_at)}")
    elif closed_at or state == "closed":
        closed = _short_moment(closed_at)
        parts.append(f"Closed {closed}" if closed else "Closed")
    else:
        parts.append("Open")
    return " · ".join(parts)


def _short_reason(text: str | None, limit: int = 90) -> str:
    """The first line of a queue reason, short enough for one table cell."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _failed_step_name(failed_step: str | None, failure_area: str = "") -> str:
    """The exact step the table should name.

    A run stores "job → step; job2 → step2". Normally the first entry is the
    first failure. When the stored area is "test", a pytest step explains the
    missing test data better than an earlier install step, so prefer the
    first test-step entry.
    """
    entries = [entry for entry in (failed_step or "").split("; ") if entry]
    if not entries:
        return ""
    if failure_area == "test":
        for entry in entries:
            step = entry.rsplit(" → ", 1)[-1].strip()
            if step in TEST_STEP_NAMES:
                return step
    return entries[0].rsplit(" → ", 1)[-1].strip()


TEST_STEP_NAMES = frozenset({
    "Run E2E tests",
    "Run E2E tests (ci-observe capture)",
    "Run CaaS e2e tests",
})


# A short suite name for the E2E runs table. The stored workflow name is
# either "E2E <suite> (<kind>)" for the pull-request flow or the full-install
# workflow's own name; both contain the suite word.
SUITE_LABELS = (
    ("vmaas", "VMaaS"),
    ("caas", "CaaS"),
    ("bmaas", "BMaaS"),
    ("netris", "Netris"),
    ("stretched", "Stretched"),
)


def _suite_label(workflow):
    text = (workflow or "").lower()
    for key, label in SUITE_LABELS:
        if key in text:
            return label
    return workflow or "E2E"


def _suite_key(workflow):
    """The short suite key a capture id carries: vmaas, caas, bmaas."""
    text = (workflow or "").lower()
    for key, _ in SUITE_LABELS:
        if key in text:
            return key
    return ""


def _iso_from_ms(value):
    return datetime.fromtimestamp(int(value) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


# How a merge-queue attempt ended, from its stored conclusion and reason.
# Read at query time, so attempts stored before the rule are classified too.
QUEUE_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
QUEUE_TIMEOUT_RE = re.compile(r"timed[ _]out|timeout|exceeded")
QUEUE_OUTCOME_LABELS = {"ejected": "ejected", "requeued": "re-queued", "passed": "passed"}


def queue_outcome(attempt):
    """"ejected", "requeued" or "passed".

    ejected: the entry's own checks failed or timed out, so GitHub removed
    the pull request from the queue. requeued: the entry ended without a
    result of its own, cancelled or replaced by a later entry. That happens
    to every entry behind one that fails, and to an entry whose pull request
    got a new push or was taken out by hand; the pull request did not fail.
    passed: the entry passed and nothing replaced it.
    """
    if not attempt.get("ejected"):
        return "passed"
    conclusion = (attempt.get("conclusion") or "").lower()
    if conclusion in QUEUE_FAILED_CONCLUSIONS:
        return "ejected"
    reason = f"{attempt.get('ejection_kind') or ''} {attempt.get('ejection_reason') or ''}".lower()
    if conclusion == "cancelled" and QUEUE_TIMEOUT_RE.search(reason):
        return "ejected"
    return "requeued"


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
    if state == "open":
        return "open in queue" if queued_at else "open"
    if queued_at:
        return "closed in queue"
    if first_approval_at:
        return "closed after approval"
    if first_review_at:
        return "closed in review"
    return "closed in authoring"


# ---------------------------------------------------------------------------
# Lifecycle: what a pull request went through, in order
# ---------------------------------------------------------------------------
# GitHub records each of these on the pull request. The REST timeline,
# reviews and commits give reviews, pushes, dismissals and draft changes;
# one GraphQL query adds what REST does not have: the merge-queue entries
# and removals with GitHub's reason, the code-owner teams a review counted
# for, and the size of the change.
LIFECYCLE_QUERY = """
query PullRequestLifecycle($owner: String!, $name: String!, $number: Int!, $after: String) {
  rateLimit { cost remaining }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      changedFiles
      additions
      deletions
      reviewRequests(first: 100) {
        nodes {
          asCodeOwner
          requestedReviewer { __typename ... on Team { slug } ... on User { login } }
        }
      }
      reviews(first: 100) {
        nodes {
          fullDatabaseId
          state
          submittedAt
          author { login }
          onBehalfOf(first: 25) { nodes { slug } }
        }
      }
      timelineItems(
        first: 100
        after: $after
        itemTypes: [
          ADDED_TO_MERGE_QUEUE_EVENT
          REMOVED_FROM_MERGE_QUEUE_EVENT
          AUTO_MERGE_ENABLED_EVENT
          AUTO_MERGE_DISABLED_EVENT
        ]
      ) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on AddedToMergeQueueEvent { createdAt actor { login } enqueuer { login } }
          ... on RemovedFromMergeQueueEvent { createdAt actor { login } reason }
          ... on AutoMergeEnabledEvent { createdAt actor { login } }
          ... on AutoMergeDisabledEvent { createdAt actor { login } reason }
        }
      }
    }
  }
}
"""
LIFECYCLE_QUERY_MAX_PAGES = 10

# The states a pull request moves through. The order is the order of one
# review cycle, which is also the order the across-PR bars stack in.
LIFECYCLE_STATES = (
    ("draft", "Draft"),
    ("waiting_review", "Waiting for a reviewer"),
    ("with_author", "With the author"),
    ("partly_approved", "Waiting for another WG"),
    ("approved", "Approved, not queued"),
    ("in_queue", "In the merge queue"),
    ("out_of_queue", "Out of the queue"),
)
LIFECYCLE_LABELS = dict(LIFECYCLE_STATES)
LIFECYCLE_ORDER = {key: index for index, (key, _) in enumerate(LIFECYCLE_STATES)}
# A removal from the queue this close to the merge is the merge itself.
QUEUE_MERGE_SLACK = timedelta(minutes=5)
# Commits this close together went up in one push.
COMMIT_PUSH_GAP = timedelta(minutes=5)

# The team whose approval any working group can give: the parent of the
# wg-* teams. A pull request that needs only this team needs one working
# group, any of them; next to a specific working group it adds nothing,
# because that working group's member approves for both.
ANY_WG_TEAMS = frozenset(
    team.strip() for team in os.getenv("CI_OBSERVE_ANY_WG_TEAMS", "maintainers").split(",") if team.strip()
)

REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
# Order of events that share a timestamp: a push before the review it
# prompted, a review before the queue entry it allowed, the merge last.
EVENT_PRIORITY = {
    "opened": 0,
    "ready": 1,
    "draft": 1,
    "review_requested": 2,
    "commit": 3,
    "merge_commit": 3,
    "force_push": 3,
    "review": 4,
    "dismissed": 5,
    "auto_merge_on": 6,
    "auto_merge_off": 6,
    "queued": 7,
    "dequeued": 8,
    "reopened": 9,
    "merged": 10,
    "closed": 10,
}
EVENT_LABELS = {
    "opened": "Opened",
    "opened_draft": "Opened as a draft",
    "ready": "Ready for review",
    "draft": "Converted to a draft",
    "review_requested": "Review requested",
    "commit": "Pushed",
    "merge_commit": "Merged the base branch in",
    "force_push": "Force-pushed (rebase or rewrite)",
    "review_approved": "Approved",
    "review_changes_requested": "Changes requested",
    "review_commented": "Reviewed with comments",
    "review_dismissed": "Review dismissed",
    "dismissed": "Approval dismissed",
    "auto_merge_on": "Merge when ready turned on",
    "auto_merge_off": "Merge when ready turned off",
    "queued": "Added to the merge queue",
    "dequeued": "Removed from the merge queue",
    "reopened": "Reopened",
    "merged": "Merged",
    "closed": "Closed",
}


def queue_reason_text(reason):
    """GitHub's removal reason as a reader reads it: FAILED_CHECKS and
    failed_checks both become "failed checks"."""
    text = " ".join((reason or "").replace("_", " ").split())
    return text.lower() if text.isupper() or "_" in (reason or "") else text


def wgs_required(teams):
    """How many working groups had to approve, from the code-owner teams
    GitHub reports. None when the teams are unknown."""
    if teams is None:
        return None
    specific = {team for team in teams if team not in ANY_WG_TEAMS}
    if specific:
        return len(specific)
    return 1 if teams else 0


def wgs_display(teams):
    """The teams for a table cell: "wg-caas, wg-infra", "any WG", "none"."""
    if teams is None:
        return ""
    specific = sorted(team for team in teams if team not in ANY_WG_TEAMS)
    if specific:
        return ", ".join(specific)
    return "any WG" if teams else "none"


def owner_teams(graph):
    """The code-owner teams of a pull request, as GitHub reports them: the
    teams each review was made on behalf of, plus the code-owner requests
    still pending. GitHub keeps no list of the requirement itself once it
    is met; a satisfied team request leaves the review's onBehalfOf. None
    when the GraphQL answer is missing."""
    if graph is None or graph.get("teams_unknown"):
        return None
    teams = set()
    for review in (graph.get("reviews") or {}).get("nodes") or []:
        for team in (review.get("onBehalfOf") or {}).get("nodes") or []:
            if team and team.get("slug"):
                teams.add(team["slug"])
    for request in (graph.get("reviewRequests") or {}).get("nodes") or []:
        reviewer = request.get("requestedReviewer") or {}
        if request.get("asCodeOwner") and reviewer.get("__typename") == "Team" and reviewer.get("slug"):
            teams.add(reviewer["slug"])
    return sorted(teams)


def _login(node):
    return (node or {}).get("login") or ""


def _review_id(review):
    """The REST id of a GraphQL review. fullDatabaseId is a BigInt, which
    GitHub sends as a string; databaseId is the deprecated 32-bit form."""
    value = review.get("fullDatabaseId", review.get("databaseId"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _graph_timeline(graph):
    """The merge-queue and auto-merge events of the GraphQL answer: every
    page _fetch_lifecycle read, or the one page of a raw answer."""
    if not graph:
        return []
    if "timeline" in graph:
        # None: GitHub refused the events; the runs stand in for them.
        return graph["timeline"] or []
    return (graph.get("timelineItems") or {}).get("nodes") or []


def _commit_time(commit):
    detail = commit.get("commit") or {}
    return (detail.get("committer") or {}).get("date") or (detail.get("author") or {}).get("date")


def _first_line(text, limit=90):
    line = next((part.strip() for part in (text or "").splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def team_members(graph):
    """Who is known to review for which code-owner team: every login a
    review was made on behalf of a team for. A later approval by the same
    person counts for that team even when GitHub left its onBehalfOf
    empty because the team request was already met."""
    members = {}
    for review in ((graph or {}).get("reviews") or {}).get("nodes") or []:
        login = _login(review.get("author"))
        for team in (review.get("onBehalfOf") or {}).get("nodes") or []:
            if login and team and team.get("slug"):
                members.setdefault(login, set()).add(team["slug"])
    return members


def required_teams(graph):
    """The working groups that had to approve, as a set; empty when any
    one approval was enough; None when the code-owner teams are unknown."""
    teams = owner_teams(graph)
    if teams is None:
        return None
    return {team for team in teams if team not in ANY_WG_TEAMS}


def _push_events(commits, opened_at, force_pushes, repo_url):
    """One event per push, not one per commit.

    The commits endpoint lists the commits on the branch now, with the
    time each was committed. Commits a few minutes apart went up in one
    push; commits made just before a force push are the rewritten branch
    that push sent, so they fold into it. A merge commit (the base branch
    merged in, usually GitHub's "Update branch") stays its own event."""
    items = []
    for commit in commits:
        moment = parse_iso(_commit_time(commit))
        if moment is None or (opened_at and moment <= parse_iso(opened_at)):
            # Work done before the pull request opened is not a push to it.
            continue
        items.append((moment, commit))
    items.sort(key=lambda item: item[0])
    groups = []
    for moment, commit in items:
        merge = len(commit.get("parents") or []) > 1
        last = groups[-1] if groups else None
        if last and not merge and not last["merge"] and moment - last["last"] <= COMMIT_PUSH_GAP:
            last["commits"].append(commit)
            last["last"] = moment
        else:
            groups.append({"merge": merge, "first": moment, "last": moment, "commits": [commit]})
    pushes = []
    for group in groups:
        folded = None
        if not group["merge"]:
            folded = next(
                (push for push in force_pushes if abs(push["moment"] - group["last"]) <= COMMIT_PUSH_GAP), None
            )
        if folded is not None:
            folded["commits"] += len(group["commits"])
            continue
        commit = group["commits"][-1]
        sha = commit.get("sha") or ""
        message = _first_line((commit.get("commit") or {}).get("message"))
        count = len(group["commits"])
        detail = f"{sha[:7]} {message}".strip()
        if count > 1:
            detail = f"{count} commits, last {detail}"
        author = _login(commit.get("author")) or ((commit.get("commit") or {}).get("author") or {}).get("name") or ""
        pushes.append({
            "at": _iso(group["last"]),
            "kind": "merge_commit" if group["merge"] else "commit",
            "actor": author,
            "detail": detail,
            "url": f"{repo_url}/commit/{sha}" if repo_url and sha else "",
        })
    return pushes


def lifecycle_events(pr, timeline, reviews, commits, graph, attempts):
    """Every event of the pull request, oldest first, as dicts with at,
    kind, actor, detail, url and the raw fields the state machine reads.

    Merge-queue entries come from GitHub's own added/removed events. A
    pull request read without them (the GraphQL answer failed) falls back
    to its merge_group attempts: entered when the first run started,
    removed when an ejected or re-queued attempt ended."""
    repo_url = (pr.get("html_url") or "").split("/pull/")[0]
    opened_at = pr.get("created_at")
    author = _login(pr.get("user"))
    events = []

    def add(at, kind, actor="", detail="", url="", **extra):
        moment = parse_iso(at)
        if moment is not None:
            # One text form for every source, so the events sort as text.
            events.append({"at": _iso(moment), "kind": kind, "actor": actor, "detail": detail, "url": url, **extra})

    draft_events = sorted(
        (event.get("created_at"), event.get("event"))
        for event in timeline
        if event.get("event") in ("ready_for_review", "convert_to_draft") and event.get("created_at")
    )
    opened_as_draft = (draft_events[0][1] == "ready_for_review") if draft_events else bool(pr.get("draft"))
    add(opened_at, "opened", author, "draft" if opened_as_draft else "", pr.get("html_url") or "",
        draft=opened_as_draft)

    reviewer_of = {review.get("id"): _login(review.get("user")) for review in reviews}
    original_states = {}
    force_pushes = []
    for event in timeline:
        if event.get("event") == "review_dismissed":
            dismissed = event.get("dismissed_review") or {}
            if dismissed.get("review_id") is not None:
                original_states[dismissed["review_id"]] = (dismissed.get("state") or "").upper()
    for event in timeline:
        kind = event.get("event")
        at = event.get("created_at")
        actor = _login(event.get("actor"))
        if kind == "ready_for_review":
            add(at, "ready", actor)
        elif kind == "convert_to_draft":
            add(at, "draft", actor)
        elif kind == "review_requested":
            team = event.get("requested_team") or {}
            reviewer = team.get("slug") or team.get("name") or _login(event.get("requested_reviewer"))
            if reviewer and not reviewer.endswith("[bot]"):
                add(at, "review_requested", actor, reviewer)
        elif kind == "head_ref_force_pushed":
            moment = parse_iso(at)
            if moment is not None:
                force_pushes.append({"moment": moment, "actor": actor, "sha": event.get("commit_id") or "",
                                     "commits": 0})
        elif kind == "review_dismissed":
            dismissed = event.get("dismissed_review") or {}
            review_id = dismissed.get("review_id")
            state = original_states.get(review_id, "")
            reviewer = reviewer_of.get(review_id, "")
            message = _first_line(dismissed.get("dismissal_message"))
            whose = f"{reviewer}'s review" if reviewer else ""
            add(at, "dismissed", actor, " · ".join(part for part in (whose, message) if part),
                review_id=review_id, state=state, reviewer=reviewer)
        elif kind == "reopened":
            add(at, "reopened", actor)

    members = team_members(graph)
    behalf = {}
    for review in ((graph or {}).get("reviews") or {}).get("nodes") or []:
        review_id = _review_id(review)
        if review_id is not None:
            behalf[review_id] = [
                team.get("slug") for team in (review.get("onBehalfOf") or {}).get("nodes") or [] if team
            ]
    for review in reviews:
        login = _login(review.get("user"))
        if not review.get("submitted_at") or is_bot(review.get("user")) or (author and login == author):
            # The author's own "review" is a reply in a review thread, not a
            # review of the change.
            continue
        state = review.get("state")
        if state not in REVIEW_STATES:
            continue
        if state == "DISMISSED":
            state = original_states.get(review.get("id")) or "DISMISSED"
        named = [team for team in behalf.get(review.get("id")) or [] if team]
        detail = f"for {', '.join(named)}" if named else ""
        add(review["submitted_at"], "review", login, detail, review.get("html_url") or "",
            state=state, review_id=review.get("id"), commit=review.get("commit_id") or "",
            teams=sorted(members.get(login, set())))

    # The pushes first: they count the commits each force push carried.
    for push in _push_events(commits, opened_at, force_pushes, repo_url):
        add(push["at"], push["kind"], push["actor"], push["detail"], push["url"])
    for push in force_pushes:
        sha = push["sha"]
        detail = f"to {sha[:7]}" if sha else ""
        if push["commits"]:
            detail = f"{detail}, {push['commits']} commit{'s' if push['commits'] != 1 else ''}".lstrip(", ")
        add(_iso(push["moment"]), "force_push", push["actor"], detail,
            f"{repo_url}/commit/{sha}" if repo_url and sha else "")

    timeline_nodes = [node for node in _graph_timeline(graph) if node]
    queue_items = [
        node for node in timeline_nodes
        if node.get("__typename") in ("AddedToMergeQueueEvent", "RemovedFromMergeQueueEvent")
    ]
    for node in timeline_nodes:
        name = node.get("__typename")
        actor = _login(node.get("actor"))
        if name == "AddedToMergeQueueEvent":
            add(node.get("createdAt"), "queued", _login(node.get("enqueuer")) or actor, source="github")
        elif name == "RemovedFromMergeQueueEvent":
            reason = node.get("reason") or ""
            add(node.get("createdAt"), "dequeued", actor, queue_reason_text(reason), reason=reason, source="github")
        elif name == "AutoMergeEnabledEvent":
            add(node.get("createdAt"), "auto_merge_on", actor)
        elif name == "AutoMergeDisabledEvent":
            add(node.get("createdAt"), "auto_merge_off", actor, queue_reason_text(node.get("reason")))
    if not queue_items:
        for attempt in attempts or []:
            add(attempt.get("started_at"), "queued", "", "", attempt.get("url") or "", source="runs")
            if queue_outcome(attempt) != "passed" and attempt.get("completed_at"):
                why = _short_reason(attempt.get("ejection_reason") or attempt.get("ejection_kind") or "")
                add(attempt["completed_at"], "dequeued", "", why, attempt.get("url") or "", reason=why, source="runs")

    if pr.get("merged_at"):
        add(pr["merged_at"], "merged", _login(pr.get("merged_by")))
    elif pr.get("closed_at"):
        add(pr["closed_at"], "closed")
    events.sort(key=lambda event: (event["at"], EVENT_PRIORITY.get(event["kind"], 5)))
    return events


def _until_end(events):
    """The events up to the merge or the close; what follows (a comment on
    a merged pull request) is not part of its way in."""
    out = []
    for event in events:
        out.append(event)
        if event["kind"] in ("merged", "closed"):
            break
    return out


def lifecycle_from_events(events, end_at, required=None):
    """Walk the events and say where the pull request was at every moment.

    Draft until ready. Waiting for a reviewer until a human reviews; a
    review that asks for changes, or comments while the pull request
    waits, hands it to the author until the next push. An approval moves
    it to Approved, not queued once every required working group has
    approved (`required`: the set of teams, empty when one approval is
    enough, None when unknown); before that it is Waiting for another WG.
    A push after the approval sends it back to the reviewer when GitHub
    dismissed the approval, or when an approval follows before the pull
    request reaches the queue again: that is a re-review. Added to the
    merge queue: In the merge queue. Removed other than by the merge: Out
    of the queue, until it is added again. Returns (segments, facts)."""
    events = _until_end(events)
    if not events:
        return [], {}
    opened = events[0]
    state = "draft" if opened.get("draft") else "waiting_review"
    started = opened["at"]
    end_dt = parse_iso(end_at)
    segments = []
    approvals = {}
    blocked = set()
    rereview = False

    def go(new_state, at):
        nonlocal state, started
        if new_state == state:
            return
        segments.append((state, started, at))
        state, started = new_state, at

    def with_reviewers():
        """Where a ready pull request outside the queue waits right now."""
        if blocked or rereview or not approvals:
            return "waiting_review"
        if required:
            covered = set().union(*approvals.values()) & required
            if covered == required:
                return "approved"
            return "partly_approved" if covered else "waiting_review"
        return "approved"

    def approval_before_next_queue(index):
        for later in events[index + 1:]:
            if later["kind"] == "queued":
                return False
            if later["kind"] == "review" and later.get("state") == "APPROVED":
                return True
        return False

    merged_dt = parse_iso(next((event["at"] for event in events if event["kind"] == "merged"), None))
    movable = ("waiting_review", "with_author", "partly_approved", "approved", "out_of_queue")
    for index, event in enumerate(events[1:], start=1):
        kind = event["kind"]
        at = event["at"]
        moment = parse_iso(at)
        if end_dt is not None and moment is not None and moment > end_dt:
            break
        if kind == "ready" and state == "draft":
            go(with_reviewers(), at)
        elif kind == "draft" and state != "in_queue":
            go("draft", at)
        elif kind == "review":
            review_state = event.get("state")
            if review_state == "APPROVED":
                key = event.get("review_id") or (event.get("actor"), at)
                approvals[key] = set(event.get("teams") or [])
                blocked.discard(event.get("actor"))
                rereview = False
                if state in movable:
                    go(with_reviewers(), at)
            elif review_state == "CHANGES_REQUESTED":
                blocked.add(event.get("actor"))
                if state in movable:
                    go("with_author", at)
            elif review_state == "COMMENTED" and state in ("waiting_review", "partly_approved"):
                go("with_author", at)
        elif kind == "dismissed":
            approvals.pop(event.get("review_id"), None)
            if event.get("state") == "CHANGES_REQUESTED":
                blocked.discard(event.get("reviewer"))
            if state in ("approved", "partly_approved"):
                go(with_reviewers(), at)
        elif kind in ("commit", "merge_commit", "force_push"):
            if state == "with_author":
                go(with_reviewers(), at)
            elif state in ("approved", "out_of_queue") and approval_before_next_queue(index):
                rereview = True
                go("waiting_review", at)
        elif kind == "queued":
            rereview = False
            go("in_queue", at)
        elif kind == "dequeued" and state == "in_queue":
            if merged_dt is not None and moment is not None and timedelta(0) <= merged_dt - moment <= QUEUE_MERGE_SLACK:
                continue
            go("out_of_queue", at)
        elif kind in ("merged", "closed"):
            break
    final = end_at or events[-1]["at"]
    final_dt = parse_iso(final)
    started_dt = parse_iso(started)
    if final_dt is not None and started_dt is not None and final_dt > started_dt:
        segments.append((state, started, final))
    out = []
    for key, start, end in segments:
        seconds = seconds_between(start, end)
        if not seconds:
            continue
        if out and out[-1]["state"] == key:
            out[-1]["end"] = end
            out[-1]["seconds"] += seconds
            continue
        out.append({"state": key, "label": LIFECYCLE_LABELS[key], "start": start, "end": end, "seconds": seconds})
    return out, lifecycle_facts(events)


def _reapproval_of(approval, earlier):
    """The earlier approval this one renews: the latest one by the same
    reviewer, or for one of the same code-owner teams."""
    teams = set(approval.get("teams") or [])
    for previous in reversed(earlier):
        if previous.get("actor") == approval.get("actor") or teams & set(previous.get("teams") or []):
            return previous
    return None


def lifecycle_facts(events):
    """The counts the pages read: review rounds, re-approvals after the code
    changed, the ones that followed a force push and how long the longest
    took, the last approval before the queue, and the queue entries and
    removals with GitHub's reasons."""
    events = _until_end(events)
    reviews = [event for event in events if event["kind"] == "review"]
    pushes = [event for event in events if event["kind"] in ("commit", "merge_commit", "force_push")]
    approvals = [event for event in reviews if event.get("state") == "APPROVED"]
    # A round is one version of the code that got a human review. A review
    # records the commit it looked at, which survives a rebase; the commit
    # list does not, so the push count is only the fallback.
    rounds = set()
    for review in reviews:
        epoch = sum(1 for push in pushes if push["at"] <= review["at"])
        rounds.add(review.get("commit") or f"push-{epoch}")
    reapprovals = 0
    rebase_rereviews = 0
    rereview_waits = []
    for index, approval in enumerate(approvals):
        previous = _reapproval_of(approval, approvals[:index])
        if previous is None:
            continue
        between = [push for push in pushes if previous["at"] < push["at"] <= approval["at"]]
        changed = bool(between) or (
            bool(previous.get("commit")) and bool(approval.get("commit")) and previous["commit"] != approval["commit"]
        )
        if not changed:
            continue
        reapprovals += 1
        if any(push["kind"] == "force_push" for push in between):
            rebase_rereviews += 1
        if between:
            # From the last change to the approval of it.
            rereview_waits.append(seconds_between(between[-1]["at"], approval["at"]) or 0)
    queued = [event for event in events if event["kind"] == "queued"]
    merged = next((event for event in events if event["kind"] == "merged"), None)

    def is_the_merge(event):
        if merged is None:
            return False
        gap = parse_iso(merged["at"]) - parse_iso(event["at"])
        return timedelta(0) <= gap <= QUEUE_MERGE_SLACK

    removals = [event for event in events if event["kind"] == "dequeued" and not is_the_merge(event)]
    first_queue = queued[0]["at"] if queued else None
    before_queue = [approval for approval in approvals if first_queue is None or approval["at"] <= first_queue]
    return {
        "review_rounds": len(rounds),
        "approval_count": len(approvals),
        "reapprovals": reapprovals,
        "rebase_rereviews": rebase_rereviews,
        "rereview_seconds": max(rereview_waits) if rereview_waits else None,
        "approvals_dismissed": sum(
            1 for event in events if event["kind"] == "dismissed" and event.get("state") == "APPROVED"
        ),
        "merge_commits": sum(1 for event in events if event["kind"] == "merge_commit"),
        "force_pushes": sum(1 for event in events if event["kind"] == "force_push"),
        "pushes": len(pushes),
        "last_approval_at": before_queue[-1]["at"] if before_queue else None,
        "enqueued_at": first_queue,
        "queue_entries": len(queued),
        "queue_removals": len(removals),
        "removal_reasons": [event.get("detail") or "unknown" for event in removals],
        "auto_merge": 1 if any(event["kind"] == "auto_merge_on" for event in events) else 0,
    }


def lifecycle_columns(events, end_at, graph):
    """The pr_events lifecycle columns of one pull request and its
    pr_timeline rows. `graph` is the GraphQL answer, or None: then the size
    and the code-owner teams are NULL ("not measured")."""
    segments, facts = lifecycle_from_events(events, end_at, required_teams(graph))
    teams = owner_teams(graph)
    size = graph or {}
    columns = {
        "files_changed": size.get("changedFiles"),
        "additions": size.get("additions"),
        "deletions": size.get("deletions"),
        "owner_teams": json.dumps(teams) if teams is not None else None,
        "wgs_required": wgs_required(teams),
        "review_rounds": facts.get("review_rounds"),
        "approval_count": facts.get("approval_count"),
        "reapprovals": facts.get("reapprovals"),
        "rebase_rereviews": facts.get("rebase_rereviews"),
        "rereview_seconds": facts.get("rereview_seconds"),
        "approvals_dismissed": facts.get("approvals_dismissed"),
        "merge_commits": facts.get("merge_commits"),
        "last_approval_at": facts.get("last_approval_at"),
        "enqueued_at": facts.get("enqueued_at"),
        "queue_entries": facts.get("queue_entries"),
        "queue_removals": facts.get("queue_removals"),
        "removal_reasons": json.dumps(facts.get("removal_reasons") or []),
        "auto_merge": facts.get("auto_merge"),
        "lifecycle": json.dumps(segments),
        "lifecycle_seconds": json.dumps(lifecycle_seconds(segments)),
    }
    opened_at = events[0]["at"] if events else None
    return columns, event_rows(events, opened_at)


def lifecycle_seconds(segments):
    """Seconds per state over the whole pull request, every state present."""
    totals = {key: 0 for key, _ in LIFECYCLE_STATES}
    for segment in segments:
        totals[segment["state"]] += segment["seconds"]
    return totals


def event_rows(events, opened_at):
    """The stored event list: what happened, who did it, when, and how long
    after the pull request opened."""
    rows = []
    for event in events:
        kind = event["kind"]
        if kind == "opened" and event.get("draft"):
            label = EVENT_LABELS["opened_draft"]
        elif kind == "review":
            label = EVENT_LABELS.get(f"review_{(event.get('state') or 'dismissed').lower()}", "Reviewed")
        elif kind == "dismissed" and event.get("state") != "APPROVED":
            label = EVENT_LABELS["review_dismissed"]
        else:
            label = EVENT_LABELS.get(kind, kind)
        rows.append({
            "at": event["at"],
            "kind": kind,
            "label": label,
            "actor": event.get("actor") or "",
            "detail": event.get("detail") or "",
            "url": event.get("url") or "",
            "since_open_s": seconds_between(opened_at, event["at"]),
        })
    return rows


# ---------------------------------------------------------------------------
# GitHub access
# ---------------------------------------------------------------------------
# pr_events columns added after the first record format.
PR_EVENT_ADDED_COLUMNS = (
    ("ready_at", "TEXT"),
    ("review_wait_seconds", "INTEGER"),
    ("force_push_count", "INTEGER"),
    ("changes_requested_count", "INTEGER"),
    ("human_review_count", "INTEGER"),
    ("e2e_runs", "INTEGER"),
    ("e2e_failures", "INTEGER"),
    ("e2e_retests", "INTEGER"),
    ("e2e_versions", "INTEGER"),
    ("record_version", "INTEGER"),
    # Third record format: the lifecycle.
    ("files_changed", "INTEGER"),
    ("additions", "INTEGER"),
    ("deletions", "INTEGER"),
    ("owner_teams", "TEXT"),
    ("wgs_required", "INTEGER"),
    ("review_rounds", "INTEGER"),
    ("approval_count", "INTEGER"),
    ("reapprovals", "INTEGER"),
    ("rebase_rereviews", "INTEGER"),
    ("rereview_seconds", "INTEGER"),
    ("approvals_dismissed", "INTEGER"),
    ("merge_commits", "INTEGER"),
    ("last_approval_at", "TEXT"),
    ("last_approval_wait_seconds", "INTEGER"),
    ("enqueued_at", "TEXT"),
    ("queue_entries", "INTEGER"),
    ("queue_removals", "INTEGER"),
    ("removal_reasons", "TEXT"),
    ("auto_merge", "INTEGER"),
    ("lifecycle", "TEXT"),
    ("lifecycle_seconds", "TEXT"),
)


def github_profile_url(login: str | None) -> str:
    """A GitHub profile link for a data link. Grafana runs a data link's
    URL through encodeURI, which escapes "[" and "]" itself and would escape
    the "%" of an escaped login a second time, so a login (letters, digits,
    hyphens, and "[bot]" for an app) goes in unescaped."""
    return f"https://github.com/{login}" if login else ""


def pr_runs_where(number):
    """The SQL condition and arguments for one pull request's E2E rows in the
    exporter's jobs table. A pull_request or merge_group row carries the
    number in pr_display; a pull-request suite run is started with
    workflow_dispatch and names the pull request only in its run name
    ("E2E suite · vmaas · #12 · <sha>")."""
    return (
        "(pr_display = ? OR LOWER(workflow) LIKE ?)",
        [f"#{number}", f"e2e suite · % · #{number} · %"],
    )


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

    def _post(self, url, body):
        return requests.post(url, json=body, headers=self.headers, timeout=30)


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
        self.graphql_calls = 0
        self.graphql_remaining = None
        # Set when a GraphQL request fails: the rest of the pass stores new
        # pull requests without it and leaves older rows for the next pass,
        # because refetching them could not upgrade them.
        self._graphql_down = False
        self._legacy_refreshes = 0
        # --refresh-old: refetch every older record in this pass, not a
        # bounded number. Each refetched row counts as stored, so a run
        # stopped by the rate limit resumes where it stopped.
        self.refresh_old = False

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
            # The lifecycle event list: one row per event, in order.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pr_timeline (
                    repo TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    at TEXT,
                    kind TEXT,
                    label TEXT,
                    actor TEXT,
                    detail TEXT,
                    url TEXT,
                    since_open_s INTEGER,
                    PRIMARY KEY (repo, number, seq)
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
            # Columns of the later record formats. A row stored before keeps
            # NULL ("not measured") until the backfill refetches it.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(pr_events)")}
            for column, kind in PR_EVENT_ADDED_COLUMNS:
                if column not in columns:
                    conn.execute(f"ALTER TABLE pr_events ADD COLUMN {column} {kind}")

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

    def _graphql(self, query, variables):
        """One GraphQL request: (data, errors). data is None when the
        request failed; GitHub can also answer with part of the data and an
        error for each field it would not resolve (a token that may not read
        teams, say). GraphQL has its own hourly budget; its remaining points
        are tracked apart from the REST counter."""
        post = getattr(self.api, "_post", None)
        if post is None:
            return None, [{"message": "this API object cannot post"}]
        try:
            resp = post(GRAPHQL_URL, {"query": query, "variables": variables})
        except requests.RequestException as exc:
            logger.warning("GitHub GraphQL request failed: %s", exc)
            return None, [{"message": str(exc)}]
        self.graphql_calls += 1
        if not resp.ok:
            logger.warning("GitHub GraphQL returned %s: %s", resp.status_code, resp.text[:200])
            return None, [{"message": f"HTTP {resp.status_code}"}]
        try:
            payload = resp.json()
        except ValueError:
            return None, [{"message": "not JSON"}]
        errors = payload.get("errors") or []
        if errors:
            logger.warning("GitHub GraphQL errors: %s", str(errors)[:300])
        data = payload.get("data")
        remaining = ((data or {}).get("rateLimit") or {}).get("remaining")
        if remaining is not None:
            self.graphql_remaining = int(remaining)
        return data, errors

    def _fetch_lifecycle(self, repo, number):
        """The GraphQL half of a pull request's history: size, code-owner
        teams, and the merge-queue and auto-merge events. None when GitHub
        did not answer; the record is then stored in the older format and
        read again by a later pass.

        An answer with some fields refused keeps the rest: refused teams
        make the working groups unknown ("not measured", never "none"),
        and refused queue events fall back to the merge_group runs."""
        if self._graphql_down:
            return None
        nodes = []
        first = None
        after = None
        refused = []
        for _ in range(LIFECYCLE_QUERY_MAX_PAGES):
            data, errors = self._graphql(
                LIFECYCLE_QUERY, {"owner": ORG, "name": repo, "number": number, "after": after}
            )
            pull = ((data or {}).get("repository") or {}).get("pullRequest")
            paths = [error.get("path") for error in errors]
            if pull is None or any(not path for path in paths):
                if data is None:
                    self._graphql_down = True
                return None
            refused += [part for path in paths for part in path if isinstance(part, str)]
            first = first or pull
            items = pull.get("timelineItems") or {}
            nodes.extend(items.get("nodes") or [])
            page = items.get("pageInfo") or {}
            if not page.get("hasNextPage") or not page.get("endCursor"):
                break
            after = page["endCursor"]
        graph = dict(first)
        graph["timeline"] = None if "timelineItems" in refused else nodes
        graph["teams_unknown"] = bool({"reviews", "onBehalfOf", "reviewRequests", "requestedReviewer"} & set(refused))
        return graph

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
        self.graphql_calls = 0
        self.graphql_remaining = None
        self._graphql_down = False
        self._legacy_refreshes = 0

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
            "graphql_calls": 0,
            "graphql_remaining": None,
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
                elif self.graphql_remaining is not None and self.graphql_remaining < MIN_GRAPHQL_REMAINING:
                    summary["stopped_early"] = (
                        f"stopped in {repo} after {stored} pull request(s): GraphQL points remaining "
                        f"{self.graphql_remaining} is below {MIN_GRAPHQL_REMAINING}. Re-run to resume."
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
        summary["graphql_calls"] = self.graphql_calls
        summary["graphql_remaining"] = self.graphql_remaining
        # ok says the pass stored everything it listed. Coverage advances
        # only when the listing window was read completely, so a pass that
        # stopped early leaves coverage where it was: the pull requests it
        # missed then read "not fetched yet", not "missing".
        summary["ok"] = summary["listing_complete"] and not summary["stopped_early"] and summary["prs_failed"] == 0
        self._record_run(summary, now)
        self._prune_runs(now - timedelta(days=days))
        logger.info(
            "Pull-request backfill (%s): %d stored, %d already present, %d attempts, %d API calls, "
            "%d GraphQL calls",
            resolved_mode,
            summary["prs_stored"],
            summary["prs_skipped"],
            summary["attempts_stored"],
            summary["api_calls"],
            summary["graphql_calls"],
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
        """Whether the pull request is stored in the current record format.

        A row of an older format counts as stored once this pass has spent
        its LEGACY_REFRESH_PER_PASS refetches, so the upgrade spreads over a
        few passes instead of draining the shared token in one."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT COALESCE(record_version, 1) FROM pr_events WHERE repo = ? AND number = ?", (repo, number)
            ).fetchone()
        if row is None:
            return False
        if row[0] >= RECORD_VERSION:
            return True
        if self._graphql_down:
            # Refetching it now would store the same older format again.
            return True
        if not self.refresh_old and self._legacy_refreshes >= LEGACY_REFRESH_PER_PASS:
            return True
        self._legacy_refreshes += 1
        return False

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

        graph = self._fetch_lifecycle(repo, number)
        record = self._build_pr_record(repo, pr, timeline, reviews, commits)
        attempts = self._build_attempts(repo, number, merge_runs, timeline)
        events = self._apply_lifecycle(record, pr, timeline, reviews, commits, graph, attempts)
        self._write_pr(record, attempts, events)
        return len(attempts)

    def _apply_lifecycle(self, record, pr, timeline, reviews, commits, graph, attempts, now=None):
        """Fill the lifecycle columns and return the event rows.

        Without the GraphQL answer the lifecycle is read from the REST
        history alone: the queue entries from the merge_group runs, no
        size and no code-owner teams (NULL, "not measured"). The record is
        then stored in the previous format, so a later pass reads it
        again."""
        events = lifecycle_events(pr, timeline, reviews, commits, graph, attempts)
        end_at = record.get("merged_at") or record.get("closed_at") or _iso(now or datetime.now(UTC))
        columns, rows = lifecycle_columns(events, end_at, graph)
        if graph is None:
            record["record_version"] = COUNTS_RECORD_VERSION
        record.update(columns)
        return rows

    # -- record building ----------------------------------------------------

    def _build_pr_record(self, repo, pr, timeline, reviews, commits, now=None):
        number = pr["number"]
        merged_at = pr.get("merged_at")
        closed_at = pr.get("closed_at")
        if merged_at:
            state = "merged"
        elif closed_at:
            state = "closed"
        else:
            state = "open"
        now = now or datetime.now(UTC)

        author = (pr.get("user") or {}).get("login") or ""
        # The author's own "review" is a reply in a review thread; it does
        # not end the wait for a reviewer.
        human_reviews = [
            r
            for r in reviews
            if r.get("submitted_at")
            and not is_bot(r.get("user"))
            and not (author and (r.get("user") or {}).get("login") == author)
            and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
        ]
        human_reviews.sort(key=lambda r: r["submitted_at"])
        first_review_at = human_reviews[0]["submitted_at"] if human_reviews else None
        # A dismissed review reads DISMISSED; the dismissal event keeps what
        # it was. An approval dismissed by a later push was still the first
        # approval.
        dismissed_as = {
            (event.get("dismissed_review") or {}).get("review_id"):
                ((event.get("dismissed_review") or {}).get("state") or "").upper()
            for event in timeline
            if event.get("event") == "review_dismissed"
        }
        approvals = [
            r for r in human_reviews
            if r.get("state") == "APPROVED"
            or (r.get("state") == "DISMISSED" and dismissed_as.get(r.get("id")) == "APPROVED")
        ]
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

        # A draft pull request is not waiting for review. It is ready from
        # its first ready_for_review event, or from the start when it was
        # never a draft.
        ready_events = sorted(
            event.get("created_at") for event in timeline
            if event.get("event") == "ready_for_review" and event.get("created_at")
        )
        ready_at = ready_events[0] if ready_events else pr.get("created_at")
        force_pushes = sum(1 for event in timeline if event.get("event") == "head_ref_force_pushed")
        e2e = self._e2e_stats(repo, number, opened_at=pr.get("created_at"))

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
            "total_seconds": seconds_between(pr.get("created_at"), merged_at or closed_at or _iso(now)),
            "commit_count": len(commits),
            "review_count": len(reviews),
            "review_request_count": len(review_requesters),
            "retest_count": e2e["retests"],
            "final_stage": final_stage(state, first_review_at, first_approval_at, None),
            "fetched_at": fetched_at,
            "ready_at": ready_at,
            # Ready for review to the first human review: the wait for a
            # reviewer, without the draft time Authoring includes.
            "review_wait_seconds": seconds_between(ready_at, first_review_at),
            # A force push is how a rebase reaches GitHub.
            "force_push_count": force_pushes,
            "changes_requested_count": sum(1 for r in human_reviews if r.get("state") == "CHANGES_REQUESTED"),
            "human_review_count": len(human_reviews),
            "e2e_runs": e2e["runs"],
            "e2e_failures": e2e["failures"],
            "e2e_retests": e2e["retests"],
            "e2e_versions": e2e["versions"],
            "record_version": RECORD_VERSION,
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

    def _e2e_stats(self, repo, number, opened_at=None):
        """The pull request's VMaaS, CaaS and BMaaS E2E history from the
        exporter's job history and the 90-day run archive in trends.db: runs
        (every suite run, pull request and merge queue), failures, retests
        (runs beyond the first of each suite) and versions (the distinct
        commits its pull-request runs tested; a merge-queue run tests a
        temporary merge commit, so it is not a version). Read-only, no API
        call.

        Every value is None when neither store is there, or when the stores
        start after the pull request was opened: its early runs are gone, and
        "unknown" must never render as "no retests"."""
        empty = {"runs": None, "failures": None, "retests": None, "versions": None}
        directory = os.path.dirname(self.db_file)
        condition, args = pr_runs_where(number)
        rows = {}
        oldest = []
        read = False
        for path, table in (
            (os.path.join(directory, "workflow-exporter.db"), "jobs"),
            (os.path.join(directory, "trends.db"), "suite_runs"),
        ):
            if not os.path.exists(path):
                continue
            try:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
                    if not conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
                    ).fetchone():
                        continue
                    category = " AND category = 'e2e'" if table == "jobs" else ""
                    for row in conn.execute(
                        f"SELECT id, workflow, conclusion, event, head_sha FROM {table} WHERE repo = ?{category}"
                        f" AND {condition} AND LOWER(workflow) NOT LIKE '%-gate'"
                        " AND COALESCE(LOWER(failure_reason), '') != 'gate'",
                        [repo, *args],
                    ):
                        # The VMaaS, CaaS and BMaaS suites only: the archive
                        # keeps nothing else, so the 30 days of jobs must not
                        # add Netris or stretched runs to the count.
                        if phases.suite_key(row[1]):
                            rows.setdefault(row[0], row[1:])
                    read = True
                    columns = {column[1] for column in conn.execute(f"PRAGMA table_info({table})")}
                    first = (
                        conn.execute(f"SELECT MIN(created_at) FROM {table}").fetchone()[0]
                        if "created_at" in columns else None
                    )
                    if first:
                        oldest.append(first)
            except sqlite3.Error:
                continue
        if not read:
            return empty
        if opened_at and oldest and min(oldest) > opened_at:
            return empty
        suites = set()
        versions = set()
        failures = 0
        for workflow, conclusion, event, head_sha in rows.values():
            suites.add(phases.suite_key(workflow))
            if phases.result_key(conclusion) == "failed":
                failures += 1
            if event != "merge_group" and phases.trigger_key(event, workflow) != "merge_queue":
                sha = phases.tested_sha(workflow, head_sha)
                if sha:
                    versions.add(sha)
        return {
            "runs": len(rows),
            "failures": failures,
            "retests": max(0, len(rows) - len(suites)),
            "versions": len(versions),
        }

    def _count_retests(self, repo, number):
        """E2E runs of this pull request beyond the first of each suite.
        None when the job history is not there."""
        return self._e2e_stats(repo, number)["retests"]

    def _write_pr(self, record, attempts, events=()):
        """One transaction: the PR row, then its attempts, their stage
        timings and the event list. Replacing by key makes a --force re-run
        idempotent."""
        if attempts or record.get("enqueued_at"):
            completed = [a["completed_at"] for a in attempts if a["completed_at"]]
            record["last_attempt_at"] = max(completed) if completed else None
            # GitHub's own added_to_merge_queue time when the lifecycle has
            # it; before that, the start of the first merge_group run.
            record["queued_at"] = record.get("enqueued_at") or attempts[0]["started_at"]
            # An approval after the queue entry (a repository that allows
            # it, an admin) is not a zero wait: it is no wait to measure.
            record["waiting_seconds"] = ordered_seconds(record["first_approval_at"], record["queued_at"])
            # The queue ends when its last attempt ends. The merge then runs
            # from that moment to merged_at; using merged_at for both would
            # count the same time twice.
            record["queue_seconds"] = seconds_between(record["queued_at"], record["last_attempt_at"])
            record["merged_seconds"] = seconds_between(record["last_attempt_at"], record["merged_at"])
            record["final_stage"] = final_stage(
                record["state"], record["first_review_at"], record["first_approval_at"], record["queued_at"]
            )
        if record.get("last_approval_at") and record.get("queued_at"):
            record["last_approval_wait_seconds"] = ordered_seconds(record["last_approval_at"], record["queued_at"])

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
            conn.execute("DELETE FROM pr_timeline WHERE repo = ? AND number = ?", (record["repo"], record["number"]))
            conn.executemany(
                "INSERT INTO pr_timeline (repo, number, seq, at, kind, label, actor, detail, url, since_open_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (record["repo"], record["number"], seq, event["at"], event["kind"], event["label"],
                     event["actor"], event["detail"], event["url"], event["since_open_s"])
                    for seq, event in enumerate(events)
                ],
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
                conn.execute("DELETE FROM pr_timeline WHERE repo = ? AND number = ?", (repo, row["number"]))
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
        repo = _param(params, "repo")
        # The Overview page's Repository filter sends "all" for both.
        if repo.lower() == "all":
            repo = ""
        author = _param(params, "author")
        if author.lower() in ("all", "$__all", "*"):
            author = ""
        wgs = wgs_filter(_param(params, "wgs"))
        return self._cached_index(
            (days, repo, author, wgs, int(now.timestamp() // INDEX_CACHE_SECONDS)),
            lambda: self.index_document(days, now, repo or None, author=author or None, wgs=wgs or None),
        )

    def _cached_index(self, key, build):
        """Every panel of the Pull requests page asks for the same index
        document at the same moment. The first request builds it; the
        others wait for it and reuse it for a few seconds. The key carries
        the store's size and modification time, so a backfill write is
        never hidden."""
        try:
            stat = os.stat(self.db_file)
        except OSError:
            return build()
        # prs.db is in WAL mode: a write lands in the -wal file first.
        try:
            wal = os.stat(f"{self.db_file}-wal")
            wal_stamp = (wal.st_mtime_ns, wal.st_size)
        except OSError:
            wal_stamp = None
        key = (os.path.abspath(self.db_file), stat.st_mtime_ns, stat.st_size, wal_stamp, *key)
        with _INDEX_CACHE_LOCK:
            cached = _INDEX_CACHE.get(key)
            if cached is not None and time.monotonic() - cached[0] < INDEX_CACHE_SECONDS:
                return cached[1]
            building = _INDEX_BUILDING.setdefault(key, threading.Lock())
        with building:
            with _INDEX_CACHE_LOCK:
                cached = _INDEX_CACHE.get(key)
                if cached is not None and time.monotonic() - cached[0] < INDEX_CACHE_SECONDS:
                    return cached[1]
            document = build()
            with _INDEX_CACHE_LOCK:
                moment = time.monotonic()
                for old in [old for old, (built, _) in _INDEX_CACHE.items() if moment - built >= INDEX_CACHE_SECONDS]:
                    del _INDEX_CACHE[old]
                _INDEX_CACHE[key] = (moment, document)
                _INDEX_BUILDING.pop(key, None)
        return document

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
            "summary": [],
            "chart": [],
            "lifecycle": [],
            "events": [],
            "facts": [],
            "attempts": [],
            "pr_state": state,
            "pr_state_message": reason,
            "status_message": reason,
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
            "author": "",
            "wgs": "",
            "authors": [],
            "prs": [],
            "funnel": _funnel([]),
            "kpis": _kpis([], {}),
            "weekly": [],
            **across_prs([], now, days),
            # The detail panels query this same endpoint with an empty
            # number. Present keys let them render "enter a number" instead
            # of a root-not-found error.
            "pr": None,
            "stages": [],
            "summary": [],
            "chart": [],
            "attempts": [],
            # The detail panels query this endpoint with an empty number.
            # These keys let the resolved-state panel say "enter a number"
            # instead of showing a root-not-found error.
            "pr_state": NO_SELECTION_STATE,
            "pr_state_message": NO_SELECTION_MESSAGE,
            "status_message": "",
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

    def _live_detail(self, repo, number, runs, now):
        """A cached, read-only look at one pull request the backfill has not
        stored.

        The pull request gives the identity; its timeline, reviews and
        commits give the same stages the backfill writes. That is what lets
        an open pull request show when it was approved and when it entered
        the queue. One minute of cache keeps a dashboard refresh from
        spending the shared GitHub budget twice.
        """
        key = (repo, number, "detail")
        cached = _LIVE_PR_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[0] < cached[3]:
            return cached[1], cached[2]
        live, error = self._live_pr(repo, number)
        if live is None:
            return self._remember_live(key, None, error, LIVE_FAILURE_CACHE_TTL_SECONDS)
        timeline = self._get_all(f"{API_URL}/repos/{ORG}/{repo}/issues/{number}/timeline?per_page={PAGE_SIZE}")
        reviews = self._get_all(f"{API_URL}/repos/{ORG}/{repo}/pulls/{number}/reviews?per_page={PAGE_SIZE}")
        commits = self._get_all(f"{API_URL}/repos/{ORG}/{repo}/pulls/{number}/commits?per_page={PAGE_SIZE}")
        if timeline is None or reviews is None or commits is None:
            record = self._live_record(repo, number, live)
            record["history_unavailable"] = True
            return self._remember_live(
                key,
                record,
                "the pull request was found, but its review history could not be read, so only its identity is shown",
            )
        record = self._build_pr_record(repo, live, timeline, reviews, commits, now)
        # A live pull request is still moving. The page needs one "now" so an
        # open stage ends where its stat says it ends.
        record["as_of"] = _iso(now)
        merge_runs = [run for run in runs if run.get("event") == "merge_group"]
        if merge_runs:
            record["queued_at"] = min(run["created_at"] for run in merge_runs)
            record["waiting_seconds"] = seconds_between(record["first_approval_at"], record["queued_at"])
            done = max((run.get("done_at_ms") or 0) for run in merge_runs)
            record["last_attempt_at"] = _iso_from_ms(done) if done else None
            # A pull request still in the queue runs to now; an ended one
            # stops at its last attempt, leaving the merge as its own stage.
            queue_end = _iso(now) if record["state"] == "open" else record["last_attempt_at"]
            record["queue_seconds"] = seconds_between(record["queued_at"], queue_end)
        elif record["state"] == "open" and record["first_approval_at"]:
            # Approved but not queued yet: the wait is still running.
            record["waiting_seconds"] = seconds_between(record["first_approval_at"], _iso(now))
        if record["state"] == "merged" and record["last_attempt_at"] and record["merged_at"]:
            record["merged_seconds"] = seconds_between(record["last_attempt_at"], record["merged_at"])
        record["final_stage"] = final_stage(
            record["state"], record["first_review_at"], record["first_approval_at"], record["queued_at"]
        )
        # The lifecycle of a pull request still moving ends now. One GraphQL
        # query when the API object can post; without it the merge-queue
        # entries come from the queue runs the job history holds.
        graph = self._fetch_lifecycle(repo, number)
        live_attempts = self._attempts_from_runs(repo, number, runs)
        record["events"] = self._apply_lifecycle(record, live, timeline, reviews, commits, graph, live_attempts, now)
        record["last_approval_wait_seconds"] = ordered_seconds(
            record.get("last_approval_at"), record.get("enqueued_at") or record.get("queued_at")
        )
        record["source"] = "live"
        return self._remember_live(key, record, "")

    def _attempts_from_runs(self, repo, number, runs):
        """Queue history for a pull request the backfill has not stored,
        derived from the merge_group runs the job history already holds.

        The run conclusion is the only ejection signal here; the comment
        GitHub leaves on the pull request is read when the backfill stores
        it after the pull request ends.
        """
        groups = {}
        for run in runs:
            if run.get("event") != "merge_group":
                continue
            branch = run.get("branch") or ""
            head_sha = run.get("head_sha") or ""
            # One queue entry starts several workflows at the same branch and
            # commit. The run id only separates a run that has neither.
            key = (branch, head_sha) if branch and head_sha else (branch, head_sha, run.get("run_id") or "")
            groups.setdefault(key, []).append(run)
        ordered = sorted(groups.values(), key=lambda group: min(run.get("created_at") or "" for run in group))
        attempts = []
        for group in ordered:
            group = sorted(group, key=lambda run: run.get("created_at") or "")
            conclusion = _entry_conclusion(group)
            ejected = bool(conclusion) and conclusion not in ("success", "skipped", "neutral")
            done = max((run.get("done_at_ms") or 0) for run in group)
            run_ids = [str(run.get("run_id") or "") for run in group if run.get("run_id")]
            attempts.append(
                {
                    "repo": repo,
                    "number": number,
                    "run_id": int(run_ids[0]) if run_ids else 0,
                    "run_number": None,
                    "run_attempt": group[0].get("run_attempt") or 1,
                    "branch": group[0].get("branch") or "",
                    "head_sha": group[0].get("head_sha") or "",
                    "run_ids": json.dumps(run_ids),
                    "conclusion": conclusion,
                    "started_at": group[0].get("created_at") or "",
                    "completed_at": _iso_from_ms(done) if done else "",
                    "duration_seconds": seconds_between(
                        group[0].get("created_at"), _iso_from_ms(done) if done else None
                    ),
                    "url": group[0].get("url") or "",
                    "ejected": int(ejected),
                    "ejection_reason": f"merge queue run concluded {conclusion}" if ejected else "",
                    "ejection_kind": ejection_kind(conclusion, "") if ejected else "",
                    "reason_source": "run" if ejected else "",
                    "comment_url": "",
                    "comment_author": "",
                    "comment_at": "",
                    "fetched_at": _utc_now(),
                }
            )
        return attempts

    def _jobs_db_file(self):
        """The exporter's job history, next to prs.db. Read-only."""
        return os.path.join(os.path.dirname(self.db_file), "workflow-exporter.db")

    def _runs_for_pr(self, repo, number, days, now):
        """Every E2E run this pull request made in the retention window.

        One row per stored E2E row: a full-install run is one row, a
        pull-request-flow suite is one row per suite. Each distinct head SHA
        is a code version, so a rebase shows as a new version even when the
        suite name is the same.
        """
        jobs_db = self._jobs_db_file()
        if not repo:
            return [], "no repository was given, so the runs could not be looked up"
        if not os.path.exists(jobs_db):
            return [], "the job history has not been written yet, so this pull request has no recorded runs"
        days = max(1, days)
        cutoff = _iso(now - timedelta(days=days))
        condition, condition_args = pr_runs_where(number)
        try:
            with sqlite3.connect(jobs_db, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                stored = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, run_id, workflow, branch, status, conclusion, event, trigger,"
                        " duration_s, url, created_at, updated_at, run_attempt, head_sha,"
                        " failed_step, failure_reason"
                        " FROM jobs"
                        f" WHERE repo = ? AND {condition} AND category = 'e2e' AND run_id != ''"
                        "   AND created_at >= ?"
                        "   AND LOWER(workflow) NOT LIKE '%-gate'"
                        "   AND COALESCE(LOWER(failure_reason), '') != 'gate'"
                        " ORDER BY created_at, id",
                        (repo, *condition_args, cutoff),
                    )
                ]
        except sqlite3.Error as exc:
            return [], f"the job history could not be read: {exc}"
        if not stored:
            return [], f"no E2E run for this pull request in the last {days} days"
        versions = {}
        rows = []
        for row in stored:
            # A pull-request suite run starts from the default branch; the
            # commit it tested is in its run name, not in head_sha.
            sha = phases.tested_sha(row.get("workflow"), row.get("head_sha"))
            is_queue = row.get("event") == "merge_group"
            # A merge-queue run tests a temporary merge commit, not the pull
            # request head. It is not a code version of the pull request.
            if sha and not is_queue and sha not in versions:
                versions[sha] = len(versions) + 1
            started = parse_iso(row.get("created_at"))
            done = parse_iso(row.get("updated_at"))
            failure_area = (row.get("failure_reason") or "").strip().lower()
            failed_step_name = _failed_step_name(row.get("failed_step"), failure_area)
            conclusion = row.get("conclusion") or ""
            status = row.get("status") or ""
            status_display = conclusion
            if not status_display and status in ("in_progress", "running"):
                status_display = "Running"
            elif not status_display and status in ("queued", "waiting", "pending"):
                status_display = "Queued"
            rows.append(
                {
                    "id": row.get("id"),
                    "run_id": str(row.get("run_id") or ""),
                    "run_attempt": row.get("run_attempt") or 1,
                    "workflow": row.get("workflow") or "",
                    "suite": _suite_label(row.get("workflow") or ""),
                    "suite_key": _suite_key(row.get("workflow") or ""),
                    "branch": row.get("branch") or "",
                    "status": status,
                    "conclusion": conclusion,
                    "status_display": status_display,
                    "event": row.get("event") or "",
                    "trigger": row.get("trigger") or "",
                    "created_at": row.get("created_at") or "",
                    "created_display": _short_moment(row.get("created_at")),
                    "duration_s": row.get("duration_s"),
                    "duration_display": human_duration(row.get("duration_s")),
                    "head_sha": sha,
                    "head": "" if is_queue else sha[:12],
                    "commit_url": "" if is_queue or not sha else f"https://github.com/{ORG}/{repo}/commit/{sha}",
                    "version": "" if is_queue else versions.get(sha, ""),
                    "code_display": (
                        f"v{versions[sha]} · {sha[:12]}" if not is_queue and sha in versions else ""
                    ),
                    "failed_step": row.get("failed_step") or "",
                    "failed_step_name": failed_step_name,
                    "failure_area": failure_area,
                    "url": row.get("url") or "",
                    "run_display": str(row.get("run_id") or ""),
                    # The exporter replaces this with the run page when it has
                    # a capture; until then GitHub is the honest entry point.
                    "run_link": row.get("url") or "",
                    "started_at_ms": int(started.timestamp() * 1000) if started else None,
                    "done_at_ms": int(done.timestamp() * 1000) if done else None,
                    # The exporter fills these from the capture registry; the
                    # module never talks to Tempo.
                    "capture_id": "",
                    "recorded": "",
                    "recorded_reason": "",
                    "run_url": "",
                }
            )
        # The newest run is the one a reader wants first.
        rows.reverse()
        return rows, ""

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
            stored_events = []
            if row is not None:
                attempts = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM pr_queue_attempts WHERE repo = ? AND number = ? ORDER BY started_at",
                        (row["repo"], number),
                    )
                ]
                stored_events = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM pr_timeline WHERE repo = ? AND number = ? ORDER BY seq",
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

        repo_name = row["repo"] if row is not None else repo
        runs, runs_note = self._runs_for_pr(repo_name, number, days, now) if repo_name else ([], "")

        source = "store" if row is not None else "none"
        pr_state = "stored" if row is not None else ""
        pr_state_message = ""
        live = None
        live_error = ""
        if row is None:
            if repo:
                live, live_error = self._live_detail(repo, number, runs, now)
            else:
                live, live_error = None, "no repository was given, so there was no live lookup"
            if live is not None:
                source = "live"
            if live is not None and live_error:
                # The identity was readable but the review history was not.
                # Say so instead of letting the stages claim what never
                # happened.
                notes.append(live_error)
            pr_state, pr_state_message = self._miss_state(number, live, live_error, freshness, now)
            notes.append(pr_state_message)

        # A copy: the live record is cached, and the page reshapes this one.
        pr = dict(row) if row is not None else (dict(live) if live is not None else None)
        if pr is not None:
            pr["url"] = pr.get("url") or pr_url(pr["repo"], pr["number"])
            author = pr.get("author") or ""
            pr["author_url"] = github_profile_url(author)
            pr["repo_url"] = f"https://github.com/{ORG}/{pr['repo']}"
            pr["dates_display"] = dates_display(
                pr.get("state"), pr.get("opened_at"), pr.get("merged_at"), pr.get("closed_at")
            )
            pr["age_seconds"] = pr.get("total_seconds")
            if pr["age_seconds"] is None:
                pr["age_seconds"] = seconds_between(pr["opened_at"], _iso(now))
            if row is None:
                attempts = self._attempts_from_runs(repo, number, runs)
                pr["queue_history"] = "present" if attempts else "none"
                pr["fetched_at"] = _utc_now()
                if pr.get("state") == "open":
                    notes.append(
                        "The timeline is read from GitHub when the page opens; the saved history starts "
                        "after the pull request merges or closes."
                    )
            pr["queue_attempt_count"] = len(attempts)
            pr["ejected_attempt_count"] = sum(1 for attempt in attempts if queue_outcome(attempt) == "ejected")
            pr["requeued_attempt_count"] = sum(1 for attempt in attempts if queue_outcome(attempt) == "requeued")
            pr["queue_history"] = "present" if attempts else "none"
            pr["total_display"] = human_duration(pr.get("total_seconds"))
            if row is not None:
                pr_state_message = f"Stored from the backfill; fetched at {pr['fetched_at']}."
                if not attempts:
                    notes.append(
                        "No merge_group attempt is stored for this pull request. That is 'no queue history', "
                        "not 'no ejections': queue runs before the backfill window are gone from this file."
                    )
                if any(queue_outcome(attempt) != "passed" for attempt in attempts):
                    notes.append(DERIVATION["ejections"])

        if runs_note:
            notes.append(runs_note)

        status_message = ""
        if pr is None:
            status_message = " ".join(
                part for part in (pr_state_message, runs_note) if part
            )
        elif pr.get("history_unavailable"):
            status_message = live_error
        elif runs_note.startswith("the job history"):
            status_message = runs_note

        stages = _stages(pr) if pr is not None and not pr.get("history_unavailable") else []
        if row is None:
            stored_events = (pr or {}).pop("events", None) or []
        if pr is not None and pr.get("lifecycle") is None and not pr.get("history_unavailable"):
            notes.append(
                "This pull request was stored before the lifecycle was read: the backfill reads it again "
                "and fills in the lifecycle, the event list and the review numbers."
            )
        return {
            "mode": "pr",
            "generated_at": _utc_now(),
            "days": days,
            "pr": pr,
            "stages": stages,
            "summary": _summary(pr, stages),
            "chart": _chart(pr, stages),
            "lifecycle": lifecycle_chart(pr),
            "events": events_document(stored_events),
            "facts": facts_document(pr),
            "runs": runs,
            "attempts": [_attempt_out(attempt) for attempt in attempts],
            "pr_state": pr_state,
            "pr_state_message": pr_state_message,
            "status_message": status_message,
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
                f"Pull request #{number} is open. The page is reading it from GitHub now, so the dates "
                "are current. The saved history starts after it merges or closes."
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

    def index_document(self, days, now=None, repo=None, author=None, wgs=None):
        """Every pull request of the window, and what they have in common.

        `author` keeps one author's pull requests; `wgs` keeps the ones that
        needed that many working groups ("0", "1", "2", "3plus", or
        "unknown" for rows not read with the lifecycle yet). `authors`
        lists every author of the window and repository before either
        filter, so the author drop-down never empties itself."""
        now = now or datetime.now(UTC)
        cutoff = _iso(now - timedelta(days=days))
        with self._db() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pr_events WHERE COALESCE(merged_at, closed_at, opened_at) >= ? "
                    + ("AND repo = ? " if repo else "")
                    + "ORDER BY COALESCE(merged_at, closed_at, opened_at) DESC",
                    (cutoff, repo) if repo else (cutoff,),
                )
            ]
            attempts = {}
            for attempt in conn.execute("SELECT * FROM pr_queue_attempts WHERE started_at >= ?", (cutoff,)):
                attempts.setdefault((attempt["repo"], attempt["number"]), []).append(dict(attempt))

        authors = _authors(rows)
        if author:
            rows = [row for row in rows if (row.get("author") or "") == author]
        if wgs:
            rows = [
                row for row in rows
                if (wg_group(row.get("wgs_required")) or "unknown") == wgs
            ]
        funnel = _funnel(rows)
        kpis = _kpis(rows, attempts)
        weekly = _weekly(rows)
        prs = []
        for row in rows:
            row_attempts = attempts.get((row["repo"], row["number"]), [])
            row["url"] = row.get("url") or pr_url(row["repo"], row["number"])
            author = row.get("author") or ""
            row["author_url"] = github_profile_url(author)
            row["repo_url"] = f"https://github.com/{ORG}/{row['repo']}"
            row["dates_display"] = dates_display(
                row.get("state"), row.get("opened_at"), row.get("merged_at"), row.get("closed_at")
            )
            row["age_seconds"] = row.get("total_seconds")
            if row["age_seconds"] is None:
                row["age_seconds"] = seconds_between(row["opened_at"], _iso(now))
            row["queue_attempt_count"] = len(row_attempts)
            row["ejected_attempt_count"] = sum(1 for a in row_attempts if queue_outcome(a) == "ejected")
            row["requeued_attempt_count"] = sum(1 for a in row_attempts if queue_outcome(a) == "requeued")
            row["age_display"] = human_duration(row["age_seconds"])
            # The Pull requests page opens this pull request's own section.
            # The filters ride along, so opening a pull request keeps the
            # list above it as it was.
            row["detail_url"] = (
                "/d/ci-observe-pull-requests/ci-observe-e28094-pull-requests"
                f"?var-pr_repo={quote(row['repo'], safe='')}&var-pr_number={row['number']}&var-days={days}"
                f"&var-pr_author={quote(author, safe='') if author else '$__all'}&var-wgs={wgs or 'all'}"
            )
            # The Pull requests page turns this list into a drop-down. The
            # label carries the three things a reader needs to choose.
            row["option_label"] = f"#{row['number']} · {row['final_stage']} · {row['title']}"
            teams = _json_value(row.get("owner_teams"))
            row["teams_display"] = wgs_display(teams) if teams is not None else ""
            if row.get("files_changed") is not None:
                row["lines_display"] = f"+{row.get('additions') or 0} -{row.get('deletions') or 0}"
            else:
                row["lines_display"] = ""
            prs.append(row)

        views = across_prs(prs, now, days)
        # The raw lifecycle columns are the views' input; the list does not
        # show them, and every panel of the page fetches this document.
        for row in prs:
            for column in INDEX_UNSHOWN_COLUMNS:
                row.pop(column, None)
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
        if author or wgs:
            notes.append(
                "Filtered: "
                + ", ".join(part for part in (
                    f"author {author}" if author else "",
                    f"working groups {dict(WG_GROUPS).get(wgs, wgs)}" if wgs else "",
                ) if part)
                + "."
            )
        return {
            "mode": "index",
            "generated_at": _utc_now(),
            "days": days,
            "author": author or "",
            "wgs": wgs or "",
            "authors": authors,
            "prs": prs,
            "funnel": funnel,
            "kpis": kpis,
            "weekly": weekly,
            **views,
            "pr": None,
            "stages": [],
            "summary": [],
            "chart": [],
            "runs": [],
            "attempts": [],
            # No number selected: the resolved-state panel says so.
            "pr_state": NO_SELECTION_STATE,
            "pr_state_message": NO_SELECTION_MESSAGE,
            "status_message": "",
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
    # An open pull request is still moving. Its current stage ends at the
    # as-of moment, the same moment its stat measures to.
    as_of = pr.get("as_of") if pr.get("state") == "open" else None
    waiting_end = pr.get("queued_at")
    if as_of and pr.get("first_approval_at") and not pr.get("queued_at"):
        waiting_end = as_of
    queue_end = as_of if as_of and pr.get("queued_at") else pr.get("last_attempt_at")
    boundaries = {
        "authoring": (pr.get("opened_at"), pr.get("first_review_at"), pr.get("authoring_seconds")),
        "review": (pr.get("first_review_at"), pr.get("first_approval_at"), pr.get("review_seconds")),
        "waiting": (pr.get("first_approval_at"), waiting_end, pr.get("waiting_seconds")),
        "queue": (pr.get("queued_at"), queue_end, pr.get("queue_seconds")),
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
                "time_display": human_duration(seconds),
                "state": state,
                "missing_reason": "" if state == "measured" else reasons[key],
            }
        )
    return stages


def _summary(pr: dict | None, stages: list[dict]) -> list[dict]:
    """The five numbers at the top of the page: each in seconds (`<name>_s`,
    which the page shows as every other duration) and as a display string.

    A number the data cannot support is null and says "not measured". A zero
    would claim a stage took no time, which is a different and false statement.
    """
    if pr is None:
        return []
    by_stage = {stage["stage"]: stage for stage in stages}

    def seconds(stage_key: str) -> float | None:
        stage = by_stage.get(stage_key)
        if stage is None or stage["state"] != "measured" or stage["seconds"] is None:
            return None
        return stage["seconds"]

    values = {"total": pr.get("total_seconds")}
    values.update({key: seconds(key) for key in ("authoring", "review", "waiting", "queue")})
    row: dict[str, str | float | None] = {}
    for key, value in values.items():
        row[key] = human_duration(value) if value is not None else "not measured"
        row[f"{key}_s"] = round(value) if value is not None else None
    return [row]


def _chart(pr: dict | None, stages: list[dict]) -> list[dict]:
    """The stacked bar's one row: each measured stage in seconds, which the
    page shows as hh:mm:ss like every other duration.

    The chart is a duration axis, so the whole pull request fits whatever its
    length. A stage the data cannot measure stays null and is absent from
    the bar; it is never drawn as zero.
    """
    if pr is None:
        return []
    by_stage = {stage["stage"]: stage for stage in stages}
    row: dict[str, str | float | None] = {"category": f"PR #{pr['number']}"}
    for key, _ in STAGES:
        stage = by_stage.get(key)
        if stage is None or stage["state"] != "measured" or stage["seconds"] is None:
            row[key] = None
        else:
            row[key] = round(stage["seconds"])
    return [row]


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


def _median(values):
    values = sorted(value for value in values if value is not None)
    return percentile(values, 50) if values else None


def _share(flags):
    flags = [flag for flag in flags if flag is not None]
    return round(sum(1 for flag in flags if flag) / len(flags), 4) if flags else None


def _kpis(rows, attempts):
    """The one row of headline numbers for the pull requests in the window.

    Lead times use the merged pull requests only: a closed pull request has
    no merge to measure to. A value no row could measure is None ("not
    measured"), never 0. Rows stored before the second record format have
    no rebase or E2E counts, so those numbers cover the rows that do, and
    `measured_prs` says how many.
    """
    merged = [row for row in rows if row.get("state") == "merged"]
    measured = [row for row in merged if (row.get("record_version") or 1) >= COUNTS_RECORD_VERSION]
    lifecycle = _measured_lifecycle(rows)

    def outcomes(row):
        return [queue_outcome(a) for a in attempts.get((row["repo"], row["number"]), [])]

    def ejected(row):
        return "ejected" in outcomes(row)

    def queued(row):
        return bool(attempts.get((row["repo"], row["number"])))

    lead = sorted(row["total_seconds"] for row in merged if row.get("total_seconds") is not None)
    ran_e2e = [row for row in measured if (row.get("e2e_runs") or 0) > 0]
    review_wait = [
        row.get("review_wait_seconds") if row.get("review_wait_seconds") is not None else row.get("authoring_seconds")
        for row in merged
    ]
    return [{
        "prs": len(rows),
        "merged": len(merged),
        "closed": sum(1 for row in rows if row.get("state") == "closed"),
        "lead_median_s": percentile(lead, 50) if lead else None,
        "lead_p90_s": percentile(lead, 90) if lead else None,
        "review_wait_median_s": _median(review_wait),
        "review_median_s": _median(row.get("review_seconds") for row in merged),
        "waiting_median_s": _median(row.get("waiting_seconds") for row in merged),
        "queue_median_s": _median(row.get("queue_seconds") for row in merged),
        "merge_median_s": _median(row.get("merged_seconds") for row in merged),
        "queued_share": _share(queued(row) for row in merged),
        "ejected_share": _share(ejected(row) for row in merged if queued(row)),
        "ejections": sum(outcomes(row).count("ejected") for row in merged),
        "requeues": sum(outcomes(row).count("requeued") for row in merged),
        "commits_median": _median(row.get("commit_count") for row in merged),
        "rebased_share": _share(
            (row.get("force_push_count") or 0) > 0 for row in measured if row.get("force_push_count") is not None
        ),
        "force_pushes_mean": (
            round(sum(row["force_push_count"] for row in measured if row.get("force_push_count") is not None)
                  / len([row for row in measured if row.get("force_push_count") is not None]), 2)
            if any(row.get("force_push_count") is not None for row in measured) else None
        ),
        "changes_requested_share": _share(
            (row.get("changes_requested_count") or 0) > 0 for row in measured
            if row.get("changes_requested_count") is not None
        ),
        # Most merged pull requests change nothing an E2E suite tests, so a
        # median over all of them reads 0. The E2E numbers describe the pull
        # requests that ran at least one suite run; e2e_ran_share says how many.
        "e2e_ran_share": _share((row.get("e2e_runs") or 0) > 0 for row in measured if row.get("e2e_runs") is not None),
        "e2e_runs_median": _median(row["e2e_runs"] for row in ran_e2e),
        "e2e_retests_median": _median(row.get("e2e_retests") for row in ran_e2e),
        "e2e_retests_mean": (
            round(sum(row["e2e_retests"] for row in measured if row.get("e2e_retests") is not None)
                  / len([row for row in measured if row.get("e2e_retests") is not None]), 2)
            if any(row.get("e2e_retests") is not None for row in measured) else None
        ),
        "e2e_failed_share": _share((row.get("e2e_failures") or 0) > 0 for row in ran_e2e),
        "measured_prs": len(measured),
        # The lifecycle numbers cover the merged rows read with the lifecycle
        # (record format 3); lifecycle_prs says how many.
        "review_rounds_median": _median(row.get("review_rounds") for row in lifecycle),
        "reapproved_share": _share((row.get("reapprovals") or 0) > 0 for row in lifecycle),
        "rebase_rereview_share": _share((row.get("rebase_rereviews") or 0) > 0 for row in lifecycle),
        "rereview_median_s": _median(row.get("rereview_seconds") for row in lifecycle),
        "last_approval_wait_median_s": _median(row.get("last_approval_wait_seconds") for row in lifecycle),
        "files_median": _median(row.get("files_changed") for row in merged),
        "multi_wg_share": _share(
            row["wgs_required"] >= 2 for row in merged if row.get("wgs_required") is not None
        ),
        "lifecycle_prs": len(lifecycle),
    }]


# How many working groups had to approve: the filter values and the
# across-PR groups. "3plus" because a "+" in a query string reads as a space.
WG_GROUPS = (
    ("0", "No code owner"),
    ("1", "1 WG"),
    ("2", "2 WGs"),
    ("3plus", "3+ WGs"),
)
FILE_BUCKETS = (
    (2, "up to 2 files"),
    (5, "3 to 5 files"),
    (10, "6 to 10 files"),
    (25, "11 to 25 files"),
    (None, "26 files or more"),
)
# Time to merge, for the weekly heatmap; fastest first, so it is the bottom row.
MERGE_TIME_BUCKETS = (
    ("under_1h", 3600, "under 1h"),
    ("h1_4", 4 * 3600, "1 to 4h"),
    ("h4_24", 24 * 3600, "4 to 24h"),
    ("d1_3", 3 * 86400, "1 to 3 days"),
    ("d3_7", 7 * 86400, "3 to 7 days"),
    ("d7_plus", None, "7 days or more"),
)
# The across-PR lifecycle bars keep the order of events by cycles: a cycle
# runs through the states in their order, and a state that comes back
# starts the next one. After this many cycles the rest is summed into the
# last one.
LIFECYCLE_CYCLES = 8
LONGEST_PRS = 20


def across_prs(rows, now, days):
    """The across-PR views of the index, and the empty one-PR lists the
    detail panels read when no pull request is chosen."""
    return {
        **_scatter(rows),
        "time_by_wgs": _time_by_wgs(rows),
        "time_by_files": _time_by_files(rows),
        "rounds_histogram": _rounds_histogram(rows),
        "queue_histogram": _queue_histogram(rows),
        "wgs_histogram": _wgs_histogram(rows),
        "merge_time_heatmap": _merge_time_heatmap(rows, now, days),
        "lifecycles": _longest_lifecycles(rows),
        "lifecycle": [],
        "events": [],
        "facts": [],
    }


def _authors(rows):
    """The authors of the rows, for the author drop-down: login and a
    label with the count, alphabetical."""
    counts = {}
    for row in rows:
        login = row.get("author") or ""
        if login:
            counts[login] = counts.get(login, 0) + 1
    return [
        {"author": login, "label": f"{login} ({counts[login]})"}
        for login in sorted(counts, key=str.lower)
    ]


def wg_group(value):
    """The WG group key of a wgs_required value; None when not measured."""
    if value is None:
        return None
    return "3plus" if value >= 3 else str(max(0, value))


def wgs_filter(value):
    """The wgs= parameter as a group key, or "" for every pull request.
    "3+" arrives as "3" when Grafana does not encode the plus."""
    text = (value or "").strip().lower()
    if text in ("", "all", "$__all", "any", "*"):
        return ""
    if text in ("3", "3+", "3plus"):
        return "3plus"
    if text in ("0", "1", "2", "unknown"):
        return text
    return ""


def _json_value(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _lifecycle_totals(row):
    """Seconds per lifecycle state, or None for a row stored before the
    lifecycle existed."""
    totals = _json_value(row.get("lifecycle_seconds"))
    return totals if isinstance(totals, dict) else None


def _measured_lifecycle(rows):
    return [row for row in rows if row.get("state") == "merged" and _lifecycle_totals(row) is not None]


def file_bucket(files):
    if files is None:
        return None
    for limit, label in FILE_BUCKETS:
        if limit is None or files <= limit:
            return label
    return FILE_BUCKETS[-1][1]


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _mean_time_per_state(label, rows):
    """One bar of "where a pull request's time goes": the mean seconds in
    each state over the rows, which add up to their mean time to merge."""
    totals = [_lifecycle_totals(row) for row in rows]
    totals = [total for total in totals if total is not None]
    entry = {"group": f"{label} · {_plural(len(totals), 'PR')}", "prs": len(totals)}
    for state, _ in LIFECYCLE_STATES:
        entry[state] = round(sum(total.get(state) or 0 for total in totals) / len(totals)) if totals else None
    return entry


def _time_by_wgs(rows):
    measured = _measured_lifecycle(rows)
    return [
        _mean_time_per_state(label, [row for row in measured if wg_group(row.get("wgs_required")) == key])
        for key, label in WG_GROUPS
    ]


def _time_by_files(rows):
    measured = _measured_lifecycle(rows)
    return [
        _mean_time_per_state(label, [row for row in measured if file_bucket(row.get("files_changed")) == label])
        for _, label in FILE_BUCKETS
    ]


def _histogram(values, buckets):
    """[(label, test)] over the values: one row per bucket, empty buckets
    included so the axis does not jump."""
    return [{"bucket": label, "prs": sum(1 for value in values if test(value))} for label, test in buckets]


def _count_buckets(top):
    """0, 1, ... top-1 and "top+"."""
    buckets = [(str(n), lambda value, n=n: value == n) for n in range(top)]
    buckets.append((f"{top}+", lambda value: value >= top))
    return buckets


def _rounds_histogram(rows):
    values = [row["review_rounds"] for row in _measured_lifecycle(rows) if row.get("review_rounds") is not None]
    return _histogram(values, _count_buckets(5))


def _queue_histogram(rows):
    values = [row["queue_entries"] for row in _measured_lifecycle(rows) if row.get("queue_entries") is not None]
    return _histogram(values, _count_buckets(4))


def _wgs_histogram(rows):
    groups = [wg_group(row.get("wgs_required")) for row in _measured_lifecycle(rows)]
    return [{"bucket": label, "prs": groups.count(key)} for key, label in WG_GROUPS]


def _monday(moment):
    day = moment - timedelta(days=moment.weekday())
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _merge_time_heatmap(rows, now, days):
    """Merged pull requests per week of the merge and time to merge. Every
    week of the window is a row, an empty one too, so the weeks stay evenly
    spaced."""
    first = _monday(now - timedelta(days=days))
    weeks = {}
    week = first
    while week <= now:
        weeks[week.strftime("%Y-%m-%d")] = {key: 0 for key, _, _ in MERGE_TIME_BUCKETS}
        week += timedelta(days=7)
    for row in rows:
        merged = parse_iso(row.get("merged_at")) if row.get("state") == "merged" else None
        seconds = row.get("total_seconds")
        if merged is None or seconds is None:
            continue
        counts = weeks.get(_monday(merged).strftime("%Y-%m-%d"))
        if counts is None:
            continue
        for key, limit, _ in MERGE_TIME_BUCKETS:
            if limit is None or seconds < limit:
                counts[key] += 1
                break
    return [{"week": week, **counts} for week, counts in sorted(weeks.items())]


def _scatter(rows):
    """Every merged pull request as a point: merge time against hours to
    merge, one list per WG group for the chart's colors."""
    groups = {f"scatter_wg{key}": [] for key, _ in WG_GROUPS}
    groups["scatter_unknown"] = []
    for row in rows:
        merged = parse_iso(row.get("merged_at")) if row.get("state") == "merged" else None
        if merged is None or row.get("total_seconds") is None:
            continue
        key = wg_group(row.get("wgs_required"))
        teams = _json_value(row.get("owner_teams"))
        groups[f"scatter_wg{key}" if key else "scatter_unknown"].append({
            "merged_at": _iso(merged),
            "hours": round(row["total_seconds"] / 3600, 2),
            "pull_request": f"#{row['number']} {row.get('title') or ''}".strip(),
            "author": row.get("author") or "",
            "teams": wgs_display(teams) if teams is not None else "not measured yet",
            "files": row.get("files_changed"),
            "detail_url": row.get("detail_url") or "",
        })
    return groups


def _cycle_fields():
    """The field names of the across-PR lifecycle bars, in stacking order."""
    return [f"{state}_{cycle}" for cycle in range(1, LIFECYCLE_CYCLES + 1) for state, _ in LIFECYCLE_STATES]


def lifecycle_cycles(segments):
    """Seconds per (state, cycle). A cycle runs through the states in their
    order; a state at or before the previous one starts the next cycle, so
    stacking the fields in order keeps the events in order."""
    row = dict.fromkeys(_cycle_fields())
    cycle = 1
    last = -1
    for segment in segments or []:
        order = LIFECYCLE_ORDER.get(segment.get("state"))
        if order is None:
            continue
        if order <= last and cycle < LIFECYCLE_CYCLES:
            cycle += 1
        last = order
        field = f"{segment['state']}_{cycle}"
        row[field] = (row[field] or 0) + (segment.get("seconds") or 0)
    return row


def _longest_lifecycles(rows):
    """The longest merged pull requests, each as one bar of its lifecycle."""
    measured = sorted(
        (row for row in _measured_lifecycle(rows) if row.get("total_seconds") is not None),
        key=lambda row: row["total_seconds"],
        reverse=True,
    )[:LONGEST_PRS]
    out = []
    for row in measured:
        title = row.get("title") or ""
        if len(title) > 48:
            title = title[:47].rstrip() + "…"
        out.append({
            "pull_request": f"#{row['number']} {title}".strip(),
            "detail_url": row.get("detail_url") or "",
            **lifecycle_cycles(_json_value(row.get("lifecycle"), [])),
        })
    return out


def lifecycle_chart(pr):
    """The one-PR lifecycle bar: one field per stretch, numbered in order,
    so the legend reads as the story and each stretch wears its state's
    color."""
    segments = _json_value(pr.get("lifecycle"), []) if pr else []
    if not segments:
        return []
    row = {"category": f"PR #{pr['number']}"}
    for index, segment in enumerate(segments, start=1):
        row[f"{index:02d} · {segment['label']}"] = segment["seconds"]
    return [row]


def _when(moment):
    dt = parse_iso(moment)
    return dt.strftime("%d %b %H:%M") if dt else ""


def events_document(events):
    """The event list as the page reads it."""
    return [
        {
            "at": event["at"],
            "when": _when(event["at"]),
            "since_open_s": event.get("since_open_s"),
            "what": event["label"],
            "who": event.get("actor") or "",
            "detail": event.get("detail") or "",
            "url": event.get("url") or "",
            "kind": event.get("kind") or "",
        }
        for event in events
    ]


def facts_document(pr):
    """The pull request in numbers, one fact per row, as text."""
    if pr is None:
        return []
    not_yet = "not measured yet: the backfill reads it again"
    measured = pr.get("lifecycle") is not None
    teams = _json_value(pr.get("owner_teams"))
    if teams is None:
        wgs = not_yet
    else:
        count = wgs_required(teams)
        wgs = f"{count} ({wgs_display(teams)})" if count else "none: no code owner"
    if pr.get("files_changed") is None:
        size = not_yet
    else:
        size = (f"{_plural(pr['files_changed'], 'file')}, "
                f"+{pr.get('additions') or 0} -{pr.get('deletions') or 0} lines")

    def count(field):
        value = pr.get(field)
        return not_yet if value is None or not measured else str(value)

    if measured and pr.get("reapprovals") is not None:
        reapproved = str(pr["reapprovals"])
        if pr["reapprovals"]:
            reapproved += f" ({pr.get('rebase_rereviews') or 0} after a rebase)"
            if pr.get("rereview_seconds") is not None:
                reapproved += f"; the longest took {human_duration(pr['rereview_seconds'])}"
    else:
        reapproved = not_yet
    if measured and pr.get("queue_entries") is not None:
        reasons = _json_value(pr.get("removal_reasons"), []) or []
        queue = f"{pr['queue_entries']} {'entry' if pr['queue_entries'] == 1 else 'entries'}"
        if pr.get("queue_removals"):
            times = "once" if pr["queue_removals"] == 1 else f"{pr['queue_removals']} times"
            queue += f", removed {times} ({'; '.join(reason for reason in reasons if reason)})"
    else:
        queue = not_yet
    last_wait = pr.get("last_approval_wait_seconds")
    rows = [
        ("Author", pr.get("author") or "unknown"),
        ("Size", size),
        ("Working groups that had to approve", wgs),
        ("Review rounds", count("review_rounds")),
        ("Approvals", count("approval_count")),
        ("Re-approved after the code changed", reapproved),
        ("Approvals dismissed", count("approvals_dismissed")),
        ("Force pushes", str(pr.get("force_push_count")) if pr.get("force_push_count") is not None else not_yet),
        ("Base branch merged in", count("merge_commits")),
        ("Last approval to the queue", human_duration(last_wait) if last_wait is not None else "not measured"),
        ("Merge queue", queue),
        ("Merge when ready", ("used" if pr.get("auto_merge") else "not used") if measured else not_yet),
    ]
    return [{"fact": fact, "value": value} for fact, value in rows]


def _weekly(rows):
    """Per ISO week of the merge: merged pull requests and the median of
    each stage, oldest week first. `week` is the Monday, as a date the
    dashboards read as a time axis."""
    weeks = {}
    for row in rows:
        merged = parse_iso(row.get("merged_at")) if row.get("state") == "merged" else None
        if merged is None:
            continue
        monday = (merged - timedelta(days=merged.weekday())).strftime("%Y-%m-%d")
        weeks.setdefault(monday, []).append(row)
    out = []
    for week in sorted(weeks):
        group = weeks[week]
        out.append({
            "week": week,
            "merged": len(group),
            "lead_median_s": _median(row.get("total_seconds") for row in group),
            "review_wait_median_s": _median(
                row.get("review_wait_seconds") if row.get("review_wait_seconds") is not None
                else row.get("authoring_seconds") for row in group
            ),
            "review_median_s": _median(row.get("review_seconds") for row in group),
            "waiting_median_s": _median(row.get("waiting_seconds") for row in group),
            "queue_median_s": _median(row.get("queue_seconds") for row in group),
        })
    return out


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
    attempt["started_display"] = _short_moment(attempt.get("started_at"))
    outcome = queue_outcome(attempt)
    attempt["ejected"] = outcome == "ejected"
    attempt["ejection"] = "ejected" if attempt["ejected"] else "not ejected"
    attempt["queue_outcome"] = QUEUE_OUTCOME_LABELS[outcome]
    attempt["why"] = _short_reason(attempt.get("ejection_reason") or attempt.get("ejection_kind") or "")
    attempt["why_url"] = attempt.get("comment_url") or attempt["run_url"]
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
    parser.add_argument("--days", type=int, default=PRS_HISTORY_DAYS,
                        help=f"window in days (default {PRS_HISTORY_DAYS})")
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
    parser.add_argument(
        "--refresh-old",
        action="store_true",
        help=(
            "a one-time catch-up: refetch every row stored in an older record format in this pass instead of "
            f"{LEGACY_REFRESH_PER_PASS} per pass. Lists the whole window (--mode auto becomes full). A pass "
            "stopped by the rate limit resumes on the next run."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    service = CIObservePRs(db_file=args.db, api=StandaloneAPI(), repos=args.repo)
    service.refresh_old = args.refresh_old
    # An incremental pass lists only what changed lately; the catch-up
    # needs every pull request of the window.
    mode = "full" if args.refresh_old and args.mode == "auto" else args.mode
    summary = service.backfill(days=args.days, force=args.force, limit=args.limit, mode=mode)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

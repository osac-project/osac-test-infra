"""Check the ci-observe pages against GitHub, independently.

The pages' numbers come from three SQLite stores the exporter and its timers
fill from GitHub (workflow-exporter.db, trends.db, prs.db) and from the API
that summarizes them. This script checks both against GitHub itself, with its
own small reading of the raw data: it imports nothing from the exporter or
the backfills, so a bug there is not repeated here. Keep it that way.

E2E runs (Overview, Where the time goes, Why runs fail, All runs):

- every completed run of every E2E workflow in the window, listed from
  GitHub, is stored, with GitHub's conclusion and attempt;
- for a random sample of stored suite runs, failures first: the conclusion,
  the commit, every job's and step's times and the failed step, against the
  run's jobs on GitHub;
- the Overview's runs, passes and failures per suite, against a recount of
  the stored rows by the scope written in ci-observe/model.md.

Pull requests:

- for a random sample of the window's merged pull requests (and any --pr):
  every stored time and count against the pull request's raw GitHub record
  (the pull request, its reviews, the comments on the code and on the page,
  its timeline, its merge-queue events);
- the merged count against GitHub's search, the API's numbers against the
  rows they summarize, the order of every row's timestamps;
- how review happens: every human review and comment of the sample, and the
  review time under the page's definition and the alternatives.

ci-observe-audit.timer runs it daily in the workflow exporter container,
which has the token, the stores and the API, and --out writes the result the
pages show next to "Data as of" and under "Missing or partial data". By hand:

  podman exec workflow-exporter python3 /app/ci_observe_audit.py --days 30
  podman exec workflow-exporter python3 /app/ci_observe_audit.py --part prs --pr 2061 --sample 0

It is read-only; a 30-day run makes several hundred GitHub calls. Exit status
1 when a check found a mismatch, unless --out is given: then the result file
records it and the exit status is 0.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

API = os.getenv("API_URL", "https://api.github.com")
ORG = os.getenv("GITHUB_ORG", "osac-project")
# Accounts GitHub does not mark as bots that are machines all the same. The
# pull request backfill reads the same variable with the same default; a test
# keeps the two defaults equal.
DEFAULT_MACHINE_ACCOUNTS = "osac-ci-bot,redhat-chai-bot,openshift-ci-robot,openshift-cherrypick-robot"
MACHINE_ACCOUNTS = frozenset(
    login.strip().lower()
    for login in os.getenv("CI_OBSERVE_MACHINE_ACCOUNTS", DEFAULT_MACHINE_ACCOUNTS).split(",")
    if login.strip()
)
QUEUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(first: 100, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) {
        nodes {
          __typename
          ... on AddedToMergeQueueEvent { createdAt }
          ... on RemovedFromMergeQueueEvent { createdAt reason }
        }
      }
    }
  }
}
"""


class GitHub:
    def __init__(self, token, fetch=None):
        self.token = token
        self.calls = 0
        self._fetch = fetch or self._urlopen

    def _urlopen(self, url, body=None):
        headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github+json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response), response.headers.get("Link") or ""

    def get(self, path):
        self.calls += 1
        return self._fetch(f"{API}{path}")[0]

    def get_all(self, path):
        url = f"{API}{path}{'&' if '?' in path else '?'}per_page=100"
        items = []
        while url:
            self.calls += 1
            page, link = self._fetch(url)
            items += page
            url = next((part.split(";")[0].strip(" <>") for part in link.split(",") if 'rel="next"' in part), None)
        return items

    def graphql(self, query, variables):
        self.calls += 1
        payload = self._fetch(f"{API}/graphql", {"query": query, "variables": variables})[0]
        return payload.get("data") or {}


def ts(value):
    """GitHub and prs.db timestamps as aware datetimes."""
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def seconds(start, end):
    if start is None or end is None:
        return None
    return round((end - start).total_seconds())


def is_person(user, author):
    """A person other than the author: not an app, not a [bot] login, not one
    of MACHINE_ACCOUNTS."""
    if not user:
        return False
    login = user.get("login") or ""
    return (user.get("type") != "Bot" and not login.endswith("[bot]") and login.lower() not in MACHINE_ACCOUNTS
            and login != author)


def truth(github, repo, number):
    """What GitHub says about one pull request, read here and nowhere else."""
    pull = github.get(f"/repos/{ORG}/{repo}/pulls/{number}")
    author = (pull.get("user") or {}).get("login") or ""
    reviews = github.get_all(f"/repos/{ORG}/{repo}/pulls/{number}/reviews")
    code_comments = github.get_all(f"/repos/{ORG}/{repo}/pulls/{number}/comments")
    page_comments = github.get_all(f"/repos/{ORG}/{repo}/issues/{number}/comments")
    timeline = github.get_all(f"/repos/{ORG}/{repo}/issues/{number}/timeline")
    queue = (((github.graphql(QUEUE_QUERY, {"owner": ORG, "name": repo, "number": number}).get("repository") or {})
              .get("pullRequest") or {}).get("timelineItems") or {}).get("nodes") or []

    opened = ts(pull.get("created_at"))
    merged = ts(pull.get("merged_at"))
    ready_events = sorted(ts(e["created_at"]) for e in timeline if e.get("event") == "ready_for_review")
    ready = ready_events[0] if ready_events else opened
    dismissed_as = {}
    for event in timeline:
        if event.get("event") == "review_dismissed":
            review = event.get("dismissed_review") or {}
            dismissed_as[review.get("review_id")] = (review.get("state") or "").upper()

    def state_of(review):
        """What the review was: a dismissed one reads DISMISSED, its dismissal
        event keeps the state it had."""
        state = review.get("state")
        if state == "DISMISSED":
            return dismissed_as.get(review.get("id")) or state
        return state

    submitted = [r for r in reviews if r.get("submitted_at") and r.get("state") != "PENDING"]
    people_reviews = sorted((r for r in submitted if is_person(r.get("user"), author)), key=lambda r: r["submitted_at"])
    approvals = [r for r in people_reviews if state_of(r) == "APPROVED"]
    enqueued = sorted(ts(n["createdAt"]) for n in queue if n and n.get("__typename") == "AddedToMergeQueueEvent")
    first_queue = enqueued[0] if enqueued else None
    # The final approval: the last one before the pull request first entered
    # the merge queue, or before it merged or closed when it never queued.
    gate = first_queue or merged or ts(pull.get("closed_at"))
    before_gate = [r for r in approvals if gate is None or ts(r["submitted_at"]) <= gate]
    activity = []
    for review in people_reviews:
        activity.append((ts(review["submitted_at"]), review["user"]["login"], f"review {state_of(review)}",
                         (review.get("body") or "").strip()[:80], review.get("html_url") or ""))
    for comment in code_comments:
        if is_person(comment.get("user"), author):
            activity.append((ts(comment["created_at"]), comment["user"]["login"], f"code comment {comment.get('path')}",
                             (comment.get("body") or "").strip()[:80], comment.get("html_url") or ""))
    for comment in page_comments:
        if is_person(comment.get("user"), author):
            activity.append((ts(comment["created_at"]), comment["user"]["login"], "page comment",
                             (comment.get("body") or "").strip()[:80], comment.get("html_url") or ""))
    activity.sort(key=lambda item: item[0])
    # The author's own replies in review threads: GitHub stores each as a
    # review by the author. They are not reviews.
    author_replies = sorted(r["submitted_at"] for r in submitted if (r.get("user") or {}).get("login") == author)
    first_review = ts(people_reviews[0]["submitted_at"]) if people_reviews else None
    first_approval = ts(approvals[0]["submitted_at"]) if approvals else None
    return {
        "number": number,
        "url": pull.get("html_url"),
        "title": pull.get("title"),
        "author": author,
        "base": (pull.get("base") or {}).get("ref"),
        "merged_by": (pull.get("merged_by") or {}).get("login"),
        "opened_at": iso(opened),
        "ready_at": iso(ready),
        "merged_at": iso(merged),
        "first_review_at": iso(first_review),
        "first_review_state": state_of(people_reviews[0]) if people_reviews else None,
        "first_approval_at": iso(first_approval),
        "final_approval_at": iso(ts(before_gate[-1]["submitted_at"])) if before_gate else None,
        "enqueued_at": iso(first_queue),
        "queue_entries": len(enqueued),
        "first_activity_at": iso(activity[0][0]) if activity else None,
        "files_changed": pull.get("changed_files"),
        "force_pushes": sum(1 for e in timeline if e.get("event") == "head_ref_force_pushed"),
        # A round is one version of the code a person reviewed.
        "review_rounds": len({r.get("commit_id") for r in people_reviews}),
        "people_reviews": len(people_reviews),
        "author_replies": len(author_replies),
        "first_author_reply_at": author_replies[0] if author_replies else None,
        # Reviewers named like a bot that MACHINE_ACCOUNTS does not list:
        # counted as people here and on the page, so worth a look.
        "bot_like_people": sorted({r["user"]["login"] for r in people_reviews if "bot" in r["user"]["login"].lower()}),
        "code_comments": sum(1 for c in code_comments if is_person(c.get("user"), author)),
        "page_comments": sum(1 for c in page_comments if is_person(c.get("user"), author)),
        "activity": [[iso(when), who, what, text, url] for when, who, what, text, url in activity],
    }


# The stored field, the truth field, and how they compare.
CHECKS = (
    ("opened_at", "opened_at"),
    ("ready_at", "ready_at"),
    ("merged_at", "merged_at"),
    ("first_review_at", "first_review_at"),
    ("first_approval_at", "first_approval_at"),
    ("last_approval_at", "final_approval_at"),
    ("enqueued_at", "enqueued_at"),
    ("files_changed", "files_changed"),
    ("force_push_count", "force_pushes"),
    ("author", "author"),
    ("review_rounds", "review_rounds"),
    ("queue_entries", "queue_entries"),
)


def compare(stored, real):
    problems = []
    for column, key in CHECKS:
        if column not in stored:
            continue
        mine, theirs = stored.get(column), real.get(key)
        if column in ("enqueued_at", "queue_entries") and not theirs:
            continue  # GitHub has no queue event: the row may use its merge_group runs instead
        if mine is None and column in ("review_rounds", "queue_entries", "files_changed", "enqueued_at",
                                       "last_approval_at"):
            continue  # a row stored before the lifecycle: not measured, nothing to compare
        if isinstance(mine, str) and isinstance(theirs, str) and mine.endswith("Z"):
            mine, theirs = iso(ts(mine)), iso(ts(theirs))
        if mine != theirs:
            problems.append({"field": column, "stored": mine, "github": theirs})
    # The page's review stage: the first human review to the final approval.
    expected = seconds(ts(real["first_review_at"]), ts(real["final_approval_at"]))
    if expected is not None and expected < 0:
        expected = 0
    lifecycle = stored.get("last_approval_at") is not None or stored.get("review_seconds") is not None
    if "review_seconds" in stored and lifecycle and stored.get("review_seconds") != expected:
        problems.append({"field": "review_seconds", "stored": stored.get("review_seconds"), "github": expected})
    return problems


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def human(value):
    if value is None:
        return "-"
    value = int(value)
    days, rest = divmod(value, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return (f"{days}d " if days else "") + f"{hours:02d}:{minutes:02d}"


def review_definitions(reals):
    """Review time under the page's definition and the alternatives."""
    merged = [r for r in reals if r["merged_at"]]
    with_review = [r for r in merged if r["first_review_at"]]

    def measure(start_key, end_key, rows):
        return [seconds(ts(r[start_key]), ts(r[end_key])) for r in rows if r[start_key] and r[end_key]]

    return {
        "merged in the sample": len(merged),
        "no review by a person": sum(1 for r in merged if not r["first_review_at"]),
        "first review was the approval": sum(1 for r in with_review if r["first_review_state"] == "APPROVED"),
        "reviewers named like a bot, counted as people": sorted({x for r in merged for x in r["bot_like_people"]}),
        "the author replied in a review thread before any other person reviewed": sum(
            1 for r in merged
            if r["first_author_reply_at"]
            and (not r["first_review_at"] or r["first_author_reply_at"] < r["first_review_at"])
        ),
        "merged without the merge queue": sum(1 for r in merged if not r["queue_entries"]),
        "merged into a branch other than main": sum(1 for r in merged if r["base"] and r["base"] != "main"),
        "a comment or review came before the first approval": sum(
            1 for r in with_review
            if r["first_activity_at"] and r["first_approval_at"] and r["first_activity_at"] < r["first_approval_at"]
        ),
        "medians": {
            "the page: first review -> final approval": median(
                [max(0, s) for s in measure("first_review_at", "final_approval_at", with_review)]),
            "ready -> final approval (the page's Ready to final approval)": median(
                [max(0, s) for s in measure("ready_at", "final_approval_at", merged)]),
            "first review -> first approval": median(
                [max(0, s) for s in measure("first_review_at", "first_approval_at", with_review)]),
            "first comment or review by a person -> final approval": median(
                [max(0, s) for s in measure("first_activity_at", "final_approval_at", merged)]),
        },
    }


def read_api(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def window_checks(db, github, api, repo, days, now, api_get=read_api):
    """The window as a whole: listing, the API's arithmetic, row order."""
    cutoff = now - timedelta(days=days)
    rows = [dict(r) for r in db.execute(
        "SELECT * FROM pr_events WHERE repo = ? AND state = 'merged' AND merged_at >= ?", (repo, iso(cutoff)))]
    found = []
    search = github.get("/search/issues?q=" + urllib.parse.quote(
        f"repo:{ORG}/{repo} is:pr is:merged merged:>={cutoff:%Y-%m-%dT%H:%M:%S}+00:00") + "&per_page=1")
    found.append({"check": "merged pull requests: prs.db vs GitHub search", "prs.db": len(rows),
                  "github": search.get("total_count"), "ok": len(rows) == search.get("total_count")})
    served = api_get(f"{api}/api/ci-observe/prs?days={days}&repo={repo}")["kpis"][0]
    everything = [dict(r) for r in db.execute(
        "SELECT * FROM pr_events WHERE repo = ? AND COALESCE(merged_at, closed_at, opened_at) >= ?",
        (repo, iso(cutoff)))]
    merged = [r for r in everything if r["state"] == "merged"]
    for key, column in (("review_median_s", "review_seconds"), ("waiting_median_s", "waiting_seconds"),
                        ("lead_median_s", "total_seconds")):
        mine = median([r.get(column) for r in merged])
        theirs = served.get(key)
        agree = (mine is None and theirs is None) or (
            mine is not None and theirs is not None and abs(mine - theirs) <= 1)
        found.append({"check": f"API {key} vs the median of its rows", "rows": mine, "api": theirs, "ok": agree})
    mine = median([seconds(ts(r.get("ready_at") or r.get("opened_at")), ts(r.get("last_approval_at")))
                   for r in merged if r.get("lifecycle_seconds")])
    theirs = served.get("ready_to_approval_median_s")
    found.append({"check": "API ready_to_approval_median_s vs the median of its rows", "rows": mine, "api": theirs,
                  "ok": (mine is None and theirs is None)
                  or (mine is not None and theirs is not None and abs(mine - theirs) <= 1)})
    reviewed = [r for r in merged if r.get("first_review_at")]
    mine = round(sum(1 for r in reviewed if r.get("first_approval_at") == r["first_review_at"]) / len(reviewed), 4) \
        if reviewed else None
    found.append({"check": "API approved_first_review_share vs its rows", "rows": mine,
                  "api": served.get("approved_first_review_share"),
                  "ok": mine == served.get("approved_first_review_share")})
    found.append({"check": "API merged vs rows", "rows": len(merged), "api": served.get("merged"),
                  "ok": len(merged) == served.get("merged")})
    # Orders that must hold. A review may come before "ready" (a draft can
    # be reviewed) and an approval after the queue entry (an admin), so
    # those pairs are not here.
    must = (("opened_at", "ready_at"), ("opened_at", "first_review_at"), ("first_review_at", "first_approval_at"),
            ("first_approval_at", "last_approval_at"), ("opened_at", "queued_at"), ("queued_at", "merged_at"),
            ("opened_at", "merged_at"))
    broken = []
    for row in merged:
        for earlier, later in must:
            x, y = ts(row.get(earlier)), ts(row.get(later))
            if x and y and y < x:
                broken.append(f"#{row['number']} {later} {iso(y)} before {earlier} {iso(x)}")
        for column in ("authoring_seconds", "review_seconds", "waiting_seconds", "queue_seconds", "total_seconds"):
            if (row.get(column) or 0) < 0:
                broken.append(f"#{row['number']} {column} {row[column]}")
    found.append({"check": "every merged row: timestamps in order, no negative duration", "violations": len(broken),
                  "examples": broken[:10], "ok": not broken})
    found.append({"check": "information: merged rows approved at the first review",
                  "count": sum(1 for r in reviewed if r.get("first_approval_at") == r["first_review_at"]),
                  "of": len(reviewed), "ok": True})
    return rows, found


# -- E2E runs ------------------------------------------------------------------

SUITES = ("vmaas", "caas", "bmaas")
# ci-observe/model.md: "Netris, stretched, CaaS agentless-net, full regression
# and the named installs (...) test other scenarios and are never counted."
NEVER_COUNTED = ("netris", "stretched", "agentless", "regression", "production install", "staging install",
                 "chart packaging", "ci large")
# model.md: "Pass rate: Passed / (passed + failed). Cancelled runs are left
# out. GitHub's conclusion decides."
RESULTS = {"success": "passed", "failure": "failed", "timed_out": "failed", "startup_failure": "failed",
           "cancelled": "cancelled"}
FAILED_JOB = ("failure", "timed_out")
# A gate or precondition job decides whether the tests run; it fails when they
# did. Not a test: model.md's cause is the first failed test step.
GATE_JOB_NAMES = ("changes", "e2e-readiness")


def is_gate_job(name):
    last = (name or "").rsplit(" / ", 1)[-1].strip().lower()
    return last.endswith("-gate") or last in GATE_JOB_NAMES


def documented_suite(workflow):
    """The suite a stored row counts for, by the scope table of
    ci-observe/model.md rather than by the exporter's code; "" for none."""
    name = " ".join((workflow or "").lower().split())
    if any(word in name for word in NEVER_COUNTED):
        return ""
    for suite in SUITES:
        if name in (f"e2e {suite}", f"e2e {suite} full install", f"e2e {suite} pr suite"):
            return suite
        if name.startswith((f"e2e suite · {suite} ·", f"e2e {suite} (")):
            return suite
    return ""


def is_gate(row):
    """A run that failed only in its readiness gate never ran a suite. The
    code leaves it out of every count; model.md does not say so."""
    return (row.get("failure_reason") or "").lower() == "gate" or (row.get("workflow") or "").lower().endswith("-gate")


def workflow_family(name):
    """A run name without its per-run part: "E2E suite · vmaas · #12 · abc1234" is "E2E suite · vmaas"."""
    parts = (name or "").split(" · ")
    return " · ".join(parts[:2]) if len(parts) > 2 else (name or "")


def open_ro(path):
    if not path or not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def stored_e2e_rows(jobs_db, trends_db, since, until):
    """The rows the run pages read: the exporter's E2E rows, then the
    archive's rows the exporter no longer holds."""
    rows, live = [], set()
    for path, table, extra in ((jobs_db, "jobs", " AND category = 'e2e'"), (trends_db, "suite_runs", "")):
        conn = open_ro(path)
        if conn is None:
            continue
        try:
            found = conn.execute(f"SELECT * FROM {table} WHERE created_at >= ? AND created_at < ?{extra}",
                                 (iso(since), iso(until))).fetchall()
        except sqlite3.Error:
            found = []
        finally:
            conn.close()
        for row in found:
            row = dict(row)
            if table == "suite_runs" and row["id"] in live:
                continue
            row["store"] = table
            rows.append(row)
            if table == "jobs":
                live.add(row["id"])
    return rows


def e2e_recount(rows, served):
    """The Overview's runs, passes, failures and cancellations per suite,
    against the stored rows counted by the documented scope. Also returns
    every stored workflow with the suite the documentation gives it, so a
    difference can be traced to the names that cause it."""
    mine = {suite: Counter() for suite in (*SUITES, "all")}
    families = {}
    for row in rows:
        suite = documented_suite(row.get("workflow"))
        gate = is_gate(row)
        key = (row.get("repo") or "", workflow_family(row.get("workflow")))
        family = families.setdefault(key, {"repo": key[0], "workflow": key[1], "suite": suite, "rows": 0,
                                           "gate_rows": 0, "conclusions": Counter()})
        family["rows"] += 1
        family["gate_rows"] += int(gate)
        family["conclusions"][row.get("conclusion") or "(none)"] += 1
        result = RESULTS.get((row.get("conclusion") or "").lower())
        if not suite or gate or not result:
            continue
        for name in (suite, "all"):
            mine[name][result] += 1
            mine[name]["runs"] += 1
    by_suite = {entry.get("suite"): entry for entry in served.get("suites") or []}
    checks = []
    for suite in (*SUITES, "all"):
        theirs = by_suite.get(suite) or {}
        pairs = {key: (mine[suite][key], theirs.get(key)) for key in ("runs", "passed", "failed", "cancelled")}
        checks.append({
            "check": f"Overview {suite}: runs, passed, failed, cancelled vs the stored rows counted as model.md says",
            "api": "/".join(str(b) for _, b in pairs.values()),
            "recount": "/".join(str(a) for a, _ in pairs.values()),
            "ok": all(a == b for a, b in pairs.values()),
        })
    names = sorted(families.values(), key=lambda f: (f["suite"] == "", f["suite"], -f["rows"]))
    for family in names:
        family["conclusions"] = dict(family["conclusions"])
    return checks, names


# GitHub's run lists stop near 1,000 results whatever total_count says. A
# range with more than this many runs is split in two until each half fits.
LIST_SAFE_LIMIT = 900


def listed_runs(github, repo, workflow_id, since, until):
    """Every completed run of one workflow created in [since, until), and
    whether a range could not be listed whole (one hour that still holds more
    than LIST_SAFE_LIMIT runs)."""
    url = (f"/repos/{ORG}/{repo}/actions/workflows/{workflow_id}/runs?status=completed"
           f"&created={iso(since)}..{iso(until)}&per_page=100")
    first = github.get(f"{url}&page=1")
    total = first.get("total_count") or 0
    if total > LIST_SAFE_LIMIT and until - since > timedelta(hours=1):
        middle = since + (until - since) / 2
        early, capped_early = listed_runs(github, repo, workflow_id, since, middle)
        late, capped_late = listed_runs(github, repo, workflow_id, middle, until)
        merged = {run["id"]: run for run in early + late}
        return list(merged.values()), capped_early or capped_late
    runs, page, batch = {}, 1, first.get("workflow_runs") or []
    while True:
        for run in batch:
            created = ts(run.get("created_at"))
            # created=a..b includes both ends; the next range starts at b.
            if created and since <= created < until:
                runs[run["id"]] = run
        if len(batch) < 100 or page >= 10:
            break
        page += 1
        batch = github.get(f"{url}&page={page}").get("workflow_runs") or []
    return list(runs.values()), total > LIST_SAFE_LIMIT


# The flows whose runs hold suite rows of their own: a run missing here is
# suite runs missing on the pages.
SUITE_FLOWS = ("e2e for pull requests", "e2e for the merge queue")


def counted_workflow(name):
    """Whether a workflow's runs count on the run pages: one of the suites'
    workflows, or a flow whose runs hold suite rows."""
    return bool(documented_suite(name)) or " ".join((name or "").lower().split()) in SUITE_FLOWS


def e2e_completeness(github, jobs_db, repos, since, until, now):
    """Every completed run of every E2E workflow, listed from GitHub, is in
    the exporter's job history with GitHub's conclusion and attempt."""
    conn = open_ro(jobs_db)
    if conn is None:
        return [{"check": "E2E workflows: GitHub's runs vs the job history", "ok": False,
                 "note": f"{jobs_db} is not readable"}]
    checks = []
    try:
        for repo in repos:
            workflows = github.get(f"/repos/{ORG}/{repo}/actions/workflows?per_page=100").get("workflows") or []
            for workflow in sorted(workflows, key=lambda w: w.get("name") or ""):
                if not (workflow.get("name") or "").lower().startswith("e2e"):
                    continue
                runs, capped = listed_runs(github, repo, workflow["id"], since, until)
                # The exporter skips "dynamic" runs (code scanning, Dependabot),
                # and a run that ended minutes ago may not be stored yet.
                runs = [r for r in runs if r.get("event") != "dynamic"
                        and (ts(r.get("updated_at")) or now) <= now - timedelta(minutes=30)]
                if not runs:
                    continue
                stored = {}
                ids = [r["id"] for r in runs]
                for start in range(0, len(ids), 500):
                    part = ids[start:start + 500]
                    query = ("SELECT id, conclusion, run_attempt FROM jobs "
                             f"WHERE id IN ({','.join('?' * len(part))})")
                    for row in conn.execute(query, part):
                        stored[row["id"]] = dict(row)
                missing, wrong, behind = [], [], []
                for run in runs:
                    row = stored.get(run["id"])
                    if row is None:
                        missing.append(run)
                    elif (row["run_attempt"] or 1) < (run.get("run_attempt") or 1):
                        behind.append(f"{run['html_url']} stored attempt {row['run_attempt']} ({row['conclusion']}), "
                                      f"GitHub attempt {run['run_attempt']} ({run['conclusion']})")
                    elif (row["conclusion"] or "") != (run.get("conclusion") or ""):
                        wrong.append(f"{run['html_url']} stored {row['conclusion']!r}, GitHub {run['conclusion']!r}")
                examples = ([f"not stored: {r['html_url']} ({r['conclusion']}, {r['created_at']})" for r in missing[:5]]
                            + wrong[:5] + behind[:5])
                checks.append({
                    "check": f"{repo} / {workflow['name']}: GitHub's completed runs vs the job history",
                    "github": len(runs), "stored": len(runs) - len(missing), "missing": len(missing),
                    "other_conclusion": len(wrong), "older_attempt": len(behind),
                    "counted": counted_workflow(workflow["name"]),
                    "ok": not (missing or wrong or behind or capped), "examples": examples,
                    **({"note": "an hour held more runs than GitHub lists; some runs were not compared"}
                       if capped else {}),
                })
    finally:
        conn.close()
    return checks


def run_jobs(github, repo, run_id):
    """Every job of every attempt of one run."""
    jobs, page = [], 1
    while True:
        data = github.get(f"/repos/{ORG}/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}")
        batch = data.get("jobs") or []
        jobs += batch
        if len(batch) < 100:
            return jobs
        page += 1


def e2e_truth(github, row):
    """GitHub's record of the run a stored row describes."""
    repo = row["repo"]
    run_id = str(row.get("run_id") or row["id"])
    run = github.get(f"/repos/{ORG}/{repo}/actions/runs/{run_id}")
    attempt = int(row.get("run_attempt") or 1)
    jobs = run_jobs(github, repo, run_id)
    if str(row["id"]) != run_id:
        # A suite row: one suite of a pull-request or merge-queue run, keyed
        # by the id of the suite's test job.
        return {"run": run, "jobs": jobs, "suite_row": True,
                "own_job": next((j for j in jobs if str(j.get("id")) == str(row["id"])), None)}
    conclusion = run.get("conclusion")
    if (run.get("run_attempt") or 1) != attempt:
        conclusion = github.get(f"/repos/{ORG}/{repo}/actions/runs/{run_id}/attempts/{attempt}").get("conclusion")
    return {"run": run, "jobs": jobs, "suite_row": False, "attempt_conclusion": conclusion}


def _same_time(a, b):
    return iso(ts(a)) == iso(ts(b))


def e2e_compare(row, real):
    """Where a stored row and GitHub disagree."""
    problems = []

    def differ(field, mine, theirs):
        problems.append({"field": field, "stored": mine, "github": theirs})

    run = real["run"]
    attempt = int(row.get("run_attempt") or 1)
    if not _same_time(row.get("created_at"), run.get("created_at")):
        differ("created_at", row.get("created_at"), run.get("created_at"))
    if row.get("head_sha") and run.get("head_sha") and row["head_sha"] != run["head_sha"]:
        differ("head_sha", row["head_sha"], run["head_sha"])
    try:
        timeline = json.loads(row.get("timeline_json") or "[]") or []
    except ValueError:
        timeline = []
        differ("timeline_json", "unreadable", "")
    matched = []
    for stored_job in timeline:
        name = stored_job.get("name")
        candidates = [j for j in real["jobs"] if j.get("name") == name]
        same = next((j for j in candidates if _same_time(j.get("created_at"), stored_job.get("created_at"))), None)
        if same is None:
            differ(f"job {name!r} created_at", stored_job.get("created_at"),
                   [j.get("created_at") for j in candidates] or "no job of that name")
            continue
        matched.append(same)
        for key in ("started_at", "completed_at", "conclusion"):
            mine, theirs = stored_job.get(key) or None, same.get(key) or None
            if key.endswith("_at") and _same_time(mine, theirs):
                continue
            if mine != theirs:
                differ(f"job {name!r} {key}", mine, theirs)
        steps = {step.get("number"): step for step in same.get("steps") or []}
        for stored_step in stored_job.get("steps") or []:
            theirs = steps.get(stored_step.get("number"))
            label = f"step {name} → {stored_step.get('name')}"
            if theirs is None or theirs.get("name") != stored_step.get("name"):
                differ(label, "stored", "not on GitHub")
                continue
            for key in ("started_at", "completed_at", "conclusion"):
                mine, other = stored_step.get(key) or None, theirs.get(key) or None
                if key.endswith("_at") and _same_time(mine, other):
                    continue
                if mine != other:
                    differ(f"{label} {key}", mine, other)
    stored_conclusion = (row.get("conclusion") or "").lower()
    if real["suite_row"]:
        own = real["own_job"]
        if own is None:
            differ("the suite's test job", row["id"], "not a job of this run")
        elif int(own.get("run_attempt") or 1) != attempt:
            differ("run_attempt (the suite job's own)", attempt, own.get("run_attempt"))
        if any((j.get("conclusion") or "") in FAILED_JOB for j in matched):
            expected = "failure"
        else:
            tests = [j for j in matched if (j.get("name") or "").rstrip().endswith(" / e2e")] or matched
            expected = "success" if tests and all(j.get("conclusion") == "success" for j in tests) else "(no result)"
        failed_jobs = matched
    else:
        expected = (real.get("attempt_conclusion") or "").lower()
        if (run.get("run_attempt") or 1) > attempt:
            differ("run_attempt (GitHub has a newer one)", attempt, run.get("run_attempt"))
        failed_jobs = [j for j in real["jobs"] if int(j.get("run_attempt") or 1) == attempt
                       and not is_gate_job(j.get("name"))]
    if stored_conclusion != expected:
        differ("conclusion", stored_conclusion, expected)
    if expected == "failure":
        theirs = sorted(f"{j.get('name')} → {s.get('name')}" for j in failed_jobs if j.get("conclusion") == "failure"
                        for s in j.get("steps") or [] if s.get("conclusion") == "failure")
        mine = sorted(part.strip() for part in (row.get("failed_step") or "").split(";") if part.strip())
        if mine != theirs:
            differ("failed_step", mine, theirs)
    return problems


def e2e_sample(rows, size, rng):
    """In-scope stored rows, failures first: half the sample is failed runs."""
    counted = [r for r in rows if documented_suite(r.get("workflow")) and not is_gate(r)]
    failed = [r for r in counted if RESULTS.get((r.get("conclusion") or "").lower()) == "failed"]
    picked = rng.sample(failed, min(len(failed), size // 2))
    chosen = {id(r) for r in picked}
    rest = [r for r in counted if id(r) not in chosen]
    picked += rng.sample(rest, min(len(rest), size - len(picked)))
    return sorted(picked, key=lambda r: r.get("created_at") or "")


def e2e_audit(github, args, now, api_get, rng):
    served = api_get(f"{args.api}/api/ci-observe/health?days={args.days}&suite=all&repo=all&trigger=all")
    bounds = (served.get("window") or [{}])[0]
    since = ts(bounds.get("window_from")) or now - timedelta(days=args.days)
    until = ts(bounds.get("window_to")) or now
    rows = stored_e2e_rows(args.jobs_db, args.trends_db, since, until)
    recount, names = e2e_recount(rows, served)
    # The exporter keeps its job history for 30 days; older runs live in
    # the archive only, which does not keep the runs that are not E2E rows.
    listed_from = max(since, now - timedelta(days=28))
    repos = [r.strip() for r in args.e2e_repos.split(",") if r.strip()]
    completeness = e2e_completeness(github, args.jobs_db, repos, listed_from, until, now)
    audited = []
    for row in e2e_sample(rows, args.e2e_sample, rng):
        real = e2e_truth(github, row)
        audited.append({
            "id": row["id"], "run_id": str(row.get("run_id") or row["id"]), "repo": row["repo"],
            "workflow": row.get("workflow"), "conclusion": row.get("conclusion"), "store": row["store"],
            "url": real["run"].get("html_url"), "problems": e2e_compare(row, real),
        })
    return {"window_from": iso(since), "window_to": iso(until), "rows": len(rows), "recount": recount,
            "workflows": names, "completeness": completeness, "completeness_from": iso(listed_from),
            "sample": audited}


def summarize(result):
    """The few numbers the pages show: what was compared and what differed.
    Every count here is the audit's; the pages only print them."""
    summary = {"checked_at": result["now"], "days": result["days"], "e2e": None, "prs": None}
    e2e = result["e2e"]
    if e2e is not None:
        counted = [check for check in e2e["completeness"] if check.get("counted")]
        summary["e2e"] = {
            "listed_from": e2e["completeness_from"],
            "runs_listed": sum(check.get("github", 0) for check in counted),
            "runs_missing": sum(check.get("missing", 0) for check in counted),
            "runs_other_result": sum(check.get("other_conclusion", 0) + check.get("older_attempt", 0)
                                     for check in counted),
            "lists_incomplete": sum(1 for check in counted if check.get("note")),
            "sample": len(e2e["sample"]),
            "sample_differ": sum(1 for audit in e2e["sample"] if audit["problems"]),
            "recount_ok": all(check["ok"] for check in e2e["recount"]),
        }
    if result["review_definitions"] is not None:
        checks = {check["check"]: check for check in result["window"]}
        order = checks.get("every merged row: timestamps in order, no negative duration") or {}
        summary["prs"] = {
            "repo": result["repo"],
            "sample": len(result["pull_requests"]),
            "sample_differ": sum(1 for audit in result["pull_requests"] if audit["problems"]),
            "merged_ok": (checks.get("merged pull requests: prs.db vs GitHub search") or {}).get("ok", False),
            "api_ok": all(check["ok"] for name, check in checks.items() if name.startswith("API ")),
            "order_violations": order.get("violations", 0),
            "order_examples": order.get("examples", [])[:3],
        }
    parts = [part for part in (summary["e2e"], summary["prs"]) if part]
    summary["ok"] = all(
        not part.get("runs_missing") and not part.get("runs_other_result") and not part.get("sample_differ")
        and part.get("recount_ok", True) and part.get("merged_ok", True) and part.get("api_ok", True)
        and not part.get("order_violations") and not part.get("lists_incomplete")
        for part in parts
    )
    return summary


def write_result(path, result):
    """Write the result where the pages read it, whole or not at all."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    partial = f"{path}.partial"
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(result, handle, default=str)
    os.replace(partial, path)


def main(argv=None, github=None, now=None, api_get=read_api):
    cache = os.getenv("CACHE_DIR", "/data")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--part", choices=("all", "e2e", "prs"), default="all")
    parser.add_argument("--seed", type=int, default=None, help="the samples' random seed (default: today's date)")
    parser.add_argument("--api", default=f"http://127.0.0.1:{os.getenv('PORT', '9103')}")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--e2e-sample", type=int, default=30, help="stored suite runs to check, half of them failed")
    parser.add_argument("--e2e-repos", default="osac,osac-test-infra")
    parser.add_argument("--jobs-db", default=os.path.join(cache, "workflow-exporter.db"))
    parser.add_argument("--trends-db", default=os.path.join(cache, "trends.db"))
    parser.add_argument("--repo", default="osac", help="the pull requests' repository")
    parser.add_argument("--sample", type=int, default=30, help="merged pull requests to check")
    parser.add_argument("--pr", type=int, action="append", default=[], help="also audit this pull request")
    parser.add_argument("--db", default=os.path.join(cache, "prs.db"))
    parser.add_argument("--out", help="also write the result here, for the pages (ci-observe-audit.json); "
                                      "the exit status is then 0 when the audit ran")
    parser.add_argument("--quiet", action="store_true", help="print nothing")
    args = parser.parse_args(argv)
    now = now or datetime.now(UTC)
    if github is None:
        token = os.getenv("PRIVATE_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
        if not token:
            raise SystemExit("set PRIVATE_GITHUB_TOKEN (the workflow exporter container has it)")
        github = GitHub(token)
    seed = args.seed if args.seed is not None else int(now.strftime("%Y%m%d"))
    result = {"days": args.days, "now": iso(now), "seed": seed, "repo": args.repo, "e2e": None,
              "window": [], "pull_requests": [], "review_definitions": None}
    if args.part in ("all", "e2e"):
        result["e2e"] = e2e_audit(github, args, now, api_get, random.Random(seed))
    if args.part in ("all", "prs"):
        db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows, result["window"] = window_checks(db, github, args.api, args.repo, args.days, now, api_get)
        rng = random.Random(seed)
        numbers = sorted(set(rng.sample([r["number"] for r in rows], min(args.sample, len(rows)))) | set(args.pr))
        for number in numbers:
            stored = db.execute("SELECT * FROM pr_events WHERE repo = ? AND number = ?", (args.repo, number)).fetchone()
            real = truth(github, args.repo, number)
            result["pull_requests"].append({
                "number": number, "github": real, "stored": dict(stored) if stored else None,
                "problems": compare(dict(stored), real) if stored else [{"field": "row", "stored": None}]})
        result["review_definitions"] = review_definitions([a["github"] for a in result["pull_requests"]])
    result["github_calls"] = github.calls
    result["summary"] = summarize(result)
    if args.out:
        write_result(args.out, result)
    if args.quiet:
        pass
    elif args.json:
        print(json.dumps(result, indent=1, default=str))
    else:
        print(report(result))
    e2e = result["e2e"] or {}
    failed = (any(not check["ok"] for check in result["window"])
              or any(a["problems"] for a in result["pull_requests"])
              or any(not check["ok"] for check in e2e.get("recount", []) + e2e.get("completeness", []))
              or any(a["problems"] for a in e2e.get("sample", [])))
    return 1 if failed and not args.out else 0


def _check_lines(checks):
    lines = []
    for check in checks:
        verdict = "ok      " if check["ok"] else "MISMATCH"
        detail = ", ".join(f"{k}={v}" for k, v in check.items() if k not in ("check", "ok", "examples"))
        lines.append(f"  {verdict} {check['check']}: {detail}")
        for example in check.get("examples") or []:
            lines.append(f"             {example}")
    return lines


def e2e_report(e2e):
    lines = [f"E2E runs, created {e2e['window_from']} to {e2e['window_to']} ({e2e['rows']} stored E2E rows)", "",
             "Overview counts against the stored rows, counted the way ci-observe/model.md says"]
    lines += _check_lines(e2e["recount"])
    lines += ["  (a difference of a run or two can be a run stored in the 2 minutes the API caches its numbers;"
              " run again to confirm)", "", "Stored E2E workflows and the suite model.md gives them"]
    for family in e2e["workflows"]:
        conclusions = ", ".join(f"{k} {v}" for k, v in sorted(family["conclusions"].items()))
        gate = f", {family['gate_rows']} gate" if family["gate_rows"] else ""
        lines.append(f"  {family['suite'] or 'not counted':<12} {family['repo']} / {family['workflow']}: "
                     f"{family['rows']} rows{gate} ({conclusions})")
    lines += ["", f"Every completed run of the E2E workflows since {e2e['completeness_from']}, from GitHub"]
    lines += _check_lines(e2e["completeness"])
    bad = [a for a in e2e["sample"] if a["problems"]]
    lines += ["", f"The sample: {len(e2e['sample'])} stored suite runs (failures first), "
              f"{len(bad)} that differ from GitHub"]
    for audit in bad:
        lines.append(f"  {audit['repo']} / {audit['workflow']} ({audit['conclusion']}, {audit['store']})  "
                     f"{audit['url']}")
        for problem in audit["problems"][:12]:
            lines.append(f"      {problem['field']}: stored {problem.get('stored')}  github {problem.get('github')}")
        if len(audit["problems"]) > 12:
            lines.append(f"      ... {len(audit['problems']) - 12} more")
    return lines


def report(result):
    lines = [f"Audit of the ci-observe pages against GitHub, last {result['days']} days, at {result['now']}",
             f"({result['github_calls']} GitHub calls, sample seed {result['seed']}; "
             f"{'everything agrees' if result['summary']['ok'] else 'see MISMATCH and the samples below'})", ""]
    if result["e2e"]:
        lines += [*e2e_report(result["e2e"]), ""]
    if result["review_definitions"] is None:
        return "\n".join(lines)
    lines += [f"Pull requests of {ORG}/{result['repo']}", ""]
    lines += _check_lines(result["window"])
    bad = [a for a in result["pull_requests"] if a["problems"]]
    lines += ["", f"The sample: {len(result['pull_requests'])} pull requests, {len(bad)} with a stored value "
              "that differs from GitHub"]
    for audit in bad:
        lines.append(f"  #{audit['number']} {audit['github']['url']}")
        for problem in audit["problems"]:
            lines.append(f"      {problem['field']}: stored {problem.get('stored')}  github {problem.get('github')}")
    definitions = result["review_definitions"]
    lines += ["", "How review happens, and the review time under each definition (the sample's merged pull requests)"]
    for key, value in definitions.items():
        if key != "medians":
            lines.append(f"  {key}: {value}")
    for key, value in definitions["medians"].items():
        lines.append(f"  median {human(value):>10}  {key}")
    lines += ["", "Every human review and comment of the sample, to check by hand on GitHub"]
    for audit in result["pull_requests"]:
        real = audit["github"]
        stored = audit["stored"] or {}
        lines.append(
            f"  #{audit['number']} by {real['author']} into {real['base']}, merged by {real['merged_by']}: "
            f"page review {human(stored.get('review_seconds'))}, {real['people_reviews']} reviews, "
            f"{real['code_comments']} code comments, {real['page_comments']} page comments, "
            f"{real['author_replies']} author replies  {real['url']}"
        )
        if real["bot_like_people"]:
            lines.append(f"      counted as people, named like bots: {', '.join(real['bot_like_people'])}")
        for when, who, what, text, _ in real["activity"][:8]:
            lines.append(f"      {when}  {who:<20} {what:<28} {text[:60]!r}")
        if len(real["activity"]) > 8:
            lines.append(f"      ... {len(real['activity']) - 8} more")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

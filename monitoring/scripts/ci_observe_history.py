"""The history half of ci-observe: one test or one job over a window.

GET /api/ci-observe/history answers "is this unit failing more often or
getting slower?" for one pytest node id or one job identity
(`repo` + `display_name`), over 7 or 30 days. It returns a summary, a daily
series and the recent runs. GET /api/ci-observe/jobs returns the job names
that match a substring, so the page can offer a list instead of a blank box.

Two stores, both read-only here:

- trends.db: the daily per-test JUnit rollup and the raw per-test rows
  behind it (`test_rollup`, `test_runs`), written by ci_observe_trends.py.
- workflow-exporter.db: the job history (`jobs`), owned by the exporter.

A percentile over a window cannot be built from daily percentiles: a
percentile of percentiles is not a percentile. So the summary uses the raw
per-run rows when they exist. A window rolled up before `test_runs` existed
still has counts and its daily values; it says the window percentile was not
measured and shows the daily-derived number only where it is honest to name
it. Nothing here turns a missing duration into zero.

Everything degrades to a note in a 200: a missing DB, an unknown unit, an
ambiguous name and a window with no rows are data gaps the page must show,
not errors it would hide.
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_observe_phases as phases  # noqa: E402

ORG = os.getenv("GITHUB_ORG", "osac-project")

DEFAULT_DAYS = 30
MAX_DAYS = 30
DEFAULT_RECENT = 50
DEFAULT_MATCHES = 25
MAX_MATCHES = 100


# GitHub's own failed conclusions. A cancelled or skipped run counts in
# Runs but is neither a pass nor a failure, so passes + failures can be
# smaller than runs.
FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}


def percentile(values, fraction):
    """The nearest-rank percentile of values, None for an empty list.

    The rollup uses the same rule, so a daily value and a window value of
    one run agree. See ci_observe_trends.percentile.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    index = min(max(rank, 1), len(ordered)) - 1
    return round(ordered[index], 3)


def mean_value(values):
    """The arithmetic mean, None when nothing was measured."""
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def history_url(kind, name, days=DEFAULT_DAYS, repo="", workflow=""):
    """The page that answers "is this failing more often or getting slower?"
    for one unit.

    A test opens its Test history page, from its node id or JUnit id. A job
    opens Where the time goes for its suite in its repository: that page has
    the suite's phases and steps per day and how they changed. The workflow
    names the suite; without it the last part of the display name does. A job
    outside the three suites opens the page on all of them.
    """
    if kind == "test":
        return phases.link_to_test(name, days)
    suite = phases.suite_key(workflow) or phases.suite_key((name or "").rsplit(" / ", 1)[-1])
    return phases.link_to_suite(phases.TIME_PAGE, suite or "all", days, repo)


def _int_param(params, key, default, low, high):
    try:
        value = int((params.get(key, [""])[0] or default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def _str_param(params, key):
    return (params.get(key, [""])[0] or "").strip()


def _window(days, now):
    today = (now or datetime.now(UTC)).date()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _job_bounds(window_from, window_to):
    """The job history's created_at bounds: the two window dates, widened to
    whole UTC days. The document keeps the dates; a timestamp column cannot
    compare against a bare date at the upper bound."""
    return f"{window_from}T00:00:00Z", f"{window_to}T23:59:59Z"


def _pass_rate(passes, runs):
    if not runs:
        return None
    return round(passes / runs, 4)


def _connect_read_only(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _table_columns(conn, table):
    """The column names of a table, empty when the table does not exist.

    The read path must survive a trends.db written before a column or a
    table was added: the rollup migrates the file on its next run, and the
    page may be opened before that. A missing column reads as NULL ("not
    measured"), not as an error.
    """
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _document(kind, days, window_from, window_to, population, source):
    return {
        "kind": kind,
        "name": "",
        "repo": "",
        "days": days,
        "window_from": window_from,
        "window_to": window_to,
        "population": population,
        "source": source,
        "summary": [],
        "daily": [],
        "recent": [],
        "matches": [],
        "truncated": False,
        "duration_basis": "",
        "notes": [],
    }


def _summary_row(totals):
    runs = totals["runs"]
    passes = totals["passes"]
    failures = totals["failures"]
    return {
        "runs": runs,
        "passes": passes,
        "failures": failures,
        "other": runs - passes - failures,
        "pass_rate": _pass_rate(passes, runs),
        "p50_duration_s": totals["p50_duration_s"],
        "p95_duration_s": totals["p95_duration_s"],
        "p99_duration_s": totals["p99_duration_s"],
        "mean_duration_s": totals["mean_duration_s"],
        "measured_runs": totals["measured_runs"],
    }


def _daily_row(day, runs, passes, failures, p50, p95, p99, mean):
    return {
        "day": day,
        "runs": runs,
        "passes": passes,
        "failures": failures,
        "pass_rate": _pass_rate(passes, runs),
        "p50_duration_s": p50,
        "p95_duration_s": p95,
        "p99_duration_s": p99,
        "mean_duration_s": mean,
    }


# -- tests ------------------------------------------------------------------


def _resolve_test(conn, name, window_from, notes):
    """The exact test id to read, or None with a reason.

    An exact node id wins. Otherwise the name is a substring, the same
    search the page already offered: one match is used and named in a note,
    several are returned as candidates so the reader picks one instead of
    reading a total that mixes tests.
    """
    exact = conn.execute(
        "SELECT COUNT(*) FROM test_rollup WHERE test_id = ? AND day >= ?",
        (name, window_from),
    ).fetchone()[0]
    if exact:
        return name
    matches = [
        row["test_id"] for row in conn.execute(
            "SELECT DISTINCT test_id FROM test_rollup"
            " WHERE test_id LIKE ? AND day >= ?"
            " ORDER BY test_id LIMIT ?",
            (f"%{name}%", window_from, MAX_MATCHES + 1),
        )
    ]
    if len(matches) > MAX_MATCHES:
        notes.append(
            f"more than {MAX_MATCHES} tests match {name!r}; narrow the name"
        )
        return None
    if len(matches) == 1:
        notes.append(f"{name!r} is not an exact test id; using {matches[0]!r}")
        return matches[0]
    if matches:
        notes.append(
            f"{len(matches)} tests match {name!r}; choose one from the list"
        )
        return None
    notes.append(f"the rollup has no test whose id matches {name!r} in this window")
    return None


def _test_daily(rows):
    """One row per day from the day's stored rollup rows.

    Counts are sums. Percentiles are the nearest-rank value across the
    day's rows (repository, workflow, suite and source SHA): with one row
    it is that row's value, and with several it is the median of them, not
    a percentile of every run. The raw rows behind it are what the summary
    uses when they exist.
    """
    by_day = {}
    for row in rows:
        day = by_day.setdefault(
            row["day"],
            {"runs": 0, "passes": 0, "failures": 0, "p50": [], "p95": [], "p99": [], "mean": []},
        )
        day["runs"] += row["runs"]
        day["passes"] += row["passes"]
        day["failures"] += row["failures"]
        for column, key in (
            ("p50_duration_s", "p50"),
            ("p95_duration_s", "p95"),
            ("p99_duration_s", "p99"),
        ):
            if row[column] is not None:
                day[key].append(row[column])
        if row["mean_duration_s"] is not None:
            # Keep the day's mean weighted by that row's runs.
            day["mean"].append((row["mean_duration_s"], row["runs"]))
    daily = []
    for day in sorted(by_day):
        values = by_day[day]
        weighted = values["mean"]
        mean = (
            round(sum(value * runs for value, runs in weighted) / sum(runs for _, runs in weighted), 3)
            if weighted
            else None
        )
        daily.append(_daily_row(
            day,
            values["runs"],
            values["passes"],
            values["failures"],
            percentile(values["p50"], 0.50),
            percentile(values["p95"], 0.95),
            percentile(values["p99"], 0.99),
            mean,
        ))
    return daily


def _test_summary(totals, daily, durations):
    """The window summary for one test.

    Raw per-run durations give true percentiles. Without them (a window
    rolled up before test_runs existed) p50 and p95 are the median of the
    daily values; p99 and mean are NULL, because a median of daily p99s or a
    run-weighted mean of daily means is not a percentile or mean over the
    window's runs. duration_basis states which numbers are real.

    A partial raw window (some runs have a row, some do not) keeps the true
    percentiles over the measured runs and reports how many of the summary's
    runs were measured, so the reader can see the coverage.
    """
    summary = dict(totals)
    if durations:
        measured = len(durations)
        summary.update({
            "p50_duration_s": percentile(durations, 0.50),
            "p95_duration_s": percentile(durations, 0.95),
            "p99_duration_s": percentile(durations, 0.99),
            "mean_duration_s": mean_value(durations),
            "measured_runs": measured,
        })
        basis = (
            f"true percentiles over {measured} of {totals['runs']} run(s) with a "
            "raw duration in test_runs; a run without one is left out"
        )
        return _summary_row(summary), basis
    p50_values = [row["p50_duration_s"] for row in daily if row["p50_duration_s"] is not None]
    p95_values = [row["p95_duration_s"] for row in daily if row["p95_duration_s"] is not None]
    summary.update({
        "p50_duration_s": percentile(p50_values, 0.50),
        "p95_duration_s": percentile(p95_values, 0.50),
        "p99_duration_s": None,
        "mean_duration_s": None,
        "measured_runs": 0,
    })
    basis = (
        "no raw per-run durations are stored for this window: p50 and p95 are the "
        "median of the daily values, not percentiles over every run; p99 and mean "
        "are not measured"
    )
    return _summary_row(summary), basis


def _test_recent(rows, limit):
    recent = []
    for row in rows[:limit]:
        conclusion = "success" if row["passed"] else "failure"
        recent.append({
            "created_at": row["created_at"],
            "day": row["day"],
            "conclusion": conclusion,
            "duration_s": row["duration_s"],
            "commit": row["source_sha"],
            "run_id": str(row["run_id"]),
            "run_url": f"https://github.com/{ORG}/{row['repo']}/actions/runs/{row['run_id']}",
        })
    return recent


def _test_history(document, trends_db, name, repo, window_from, notes, days=DEFAULT_DAYS):
    if not name:
        notes.append(
            "type a test name, or choose one from the matching tests list"
        )
        return document
    if not os.path.exists(trends_db):
        notes.append(
            "no per-test rollup has been written on this machine yet, so there "
            "is no test history here"
        )
        return document
    try:
        conn = _connect_read_only(trends_db)
    except sqlite3.Error as exc:
        notes.append(f"the rollup DB could not be opened: {exc}")
        return document
    try:
        conn.row_factory = sqlite3.Row
        resolved = _resolve_test(conn, name, window_from, notes)
        if resolved is None:
            document["matches"] = _test_matches(conn, name, window_from, days)
            return document
        document["name"] = resolved
        where = "test_id = ? AND day >= ?"
        args = [resolved, window_from]
        if repo:
            where += " AND repo = ?"
            args.append(repo)
        columns = _table_columns(conn, "test_rollup")
        optional = {
            column: (column if column in columns else f"NULL AS {column}")
            for column in ("p99_duration_s", "mean_duration_s")
        }
        rollup_rows = [
            dict(row) for row in conn.execute(
                "SELECT day, repo, workflow, suite, test_id, source_sha, runs,"
                " passes, failures, p50_duration_s, p95_duration_s,"
                f" {optional['p99_duration_s']}, {optional['mean_duration_s']}"
                f" FROM test_rollup WHERE {where} ORDER BY day",
                args,
            )
        ]
        totals = {
            "runs": sum(row["runs"] for row in rollup_rows),
            "passes": sum(row["passes"] for row in rollup_rows),
            "failures": sum(row["failures"] for row in rollup_rows),
            "p50_duration_s": None,
            "p95_duration_s": None,
            "p99_duration_s": None,
            "mean_duration_s": None,
            "measured_runs": None,
        }
        run_rows = []
        if _table_columns(conn, "test_runs"):
            run_rows = [
                dict(row) for row in conn.execute(
                    "SELECT day, repo, test_id, source_sha, run_id, created_at,"
                    " duration_s, passed"
                    f" FROM test_runs WHERE {where}"
                    " ORDER BY created_at DESC, run_id DESC",
                    args,
                )
            ]
    except sqlite3.Error as exc:
        notes.append(f"the rollup DB could not be read: {exc}")
        return document
    finally:
        conn.close()

    document["daily"] = _test_daily(rollup_rows)
    durations = [row["duration_s"] for row in run_rows if row["duration_s"] is not None]
    summary, basis = _test_summary(totals, document["daily"], durations)
    document["summary"] = [summary | {"duration_basis": basis}]
    document["duration_basis"] = basis
    document["recent"] = _test_recent(run_rows, DEFAULT_RECENT)
    document["truncated"] = len(run_rows) > DEFAULT_RECENT
    if document["truncated"]:
        notes.append(
            f"the recent-run table shows the newest {DEFAULT_RECENT} of "
            f"{len(run_rows)} run(s)"
        )
    if not totals["runs"]:
        notes.append(f"the rollup has no rows for {resolved!r} on or after {window_from}")
    if not durations:
        notes.append(
            "no raw per-run durations are stored for this window; the window "
            "percentiles were not measured from individual runs"
        )
    return document


def _test_matches(conn, name, window_from, days=DEFAULT_DAYS):
    """The tests whose id contains name, worst first, for the picker."""
    rows = conn.execute(
        "SELECT test_id, suite, repo, SUM(runs) AS runs,"
        " SUM(failures) AS failures"
        " FROM test_rollup WHERE test_id LIKE ? AND day >= ?"
        " GROUP BY test_id, suite, repo"
        " ORDER BY failures DESC, runs DESC, test_id LIMIT ?",
        (f"%{name}%", window_from, DEFAULT_MATCHES),
    )
    return [
        dict(row) | {"history_url": history_url("test", row["test_id"], days, row["repo"])}
        for row in rows
    ]


# -- jobs -------------------------------------------------------------------


JOB_COLUMNS = "id, repo, display_name, created_at, duration_s, conclusion, run_id, url, head_sha"


def _job_conclusion_counts(rows):
    passes = sum(1 for row in rows if (row["conclusion"] or "").lower() == "success")
    failures = sum(
        1 for row in rows if (row["conclusion"] or "").lower() in FAILED_CONCLUSIONS
    )
    return passes, failures


def _job_daily(rows):
    by_day = {}
    for row in rows:
        day = by_day.setdefault(row["created_at"][:10], [])
        day.append(row)
    daily = []
    for day in sorted(by_day):
        day_rows = by_day[day]
        passes, failures = _job_conclusion_counts(day_rows)
        durations = [
            row["duration_s"] for row in day_rows
            if row["duration_s"] is not None and row["duration_s"] > 0
        ]
        daily.append(_daily_row(
            day,
            len(day_rows),
            passes,
            failures,
            percentile(durations, 0.50),
            percentile(durations, 0.95),
            percentile(durations, 0.99),
            mean_value(durations),
        ))
    return daily


def _job_history(document, jobs_db, name, repo, window_from, window_to, notes, days=DEFAULT_DAYS):
    if not name:
        notes.append(
            "type a job name, or choose one from the matching jobs list"
        )
        return document
    if not os.path.exists(jobs_db):
        notes.append(
            "the workflow exporter DB does not exist on this machine, so there "
            "is no job history here"
        )
        return document
    try:
        conn = _connect_read_only(jobs_db)
    except sqlite3.Error as exc:
        notes.append(f"the job history DB could not be opened: {exc}")
        return document
    try:
        conn.row_factory = sqlite3.Row
        resolved = _resolve_job(conn, name, repo, window_from, window_to, document, notes, days)
        if resolved is None:
            return document
        repo, display_name = resolved
        document["repo"] = repo
        document["name"] = display_name
        created_from, created_until = _job_bounds(window_from, window_to)
        where = "created_at >= ? AND created_at < ? AND display_name = ?"
        args = [created_from, created_until, display_name]
        if repo:
            where += " AND repo = ?"
            args.append(repo)
        rows = [
            dict(row) for row in conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE {where}"
                " ORDER BY created_at DESC, id DESC",
                args,
            )
        ]
    except sqlite3.Error as exc:
        notes.append(f"the job history DB could not be read: {exc}")
        return document
    finally:
        conn.close()

    passes, failures = _job_conclusion_counts(rows)
    durations = [
        row["duration_s"] for row in rows
        if row["duration_s"] is not None and row["duration_s"] > 0
    ]
    document["summary"] = [_summary_row({
        "runs": len(rows),
        "passes": passes,
        "failures": failures,
        "p50_duration_s": percentile(durations, 0.50),
        "p95_duration_s": percentile(durations, 0.95),
        "p99_duration_s": percentile(durations, 0.99),
        "mean_duration_s": mean_value(durations),
        "measured_runs": len(durations),
    })]
    document["duration_basis"] = (
        f"true percentiles over {len(durations)} of {len(rows)} run(s) with a stored "
        "duration; a run without one is left out, never counted as zero. Pass rate "
        "is passes divided by all stored runs, including cancelled"
    )
    document["summary"][0]["duration_basis"] = document["duration_basis"]
    document["daily"] = _job_daily(rows)
    document["recent"] = _job_recent(rows, DEFAULT_RECENT)
    document["truncated"] = len(rows) > DEFAULT_RECENT
    if document["truncated"]:
        notes.append(
            f"the recent-run table shows the newest {DEFAULT_RECENT} of "
            f"{len(rows)} run(s)"
        )
    if not rows:
        notes.append(
            f"the job history has no row for {display_name!r} on or after {window_from}"
        )
    if len(durations) < len(rows):
        notes.append(
            f"{len(rows) - len(durations)} run(s) have no stored duration and are "
            "excluded from every duration statistic"
        )
    if passes + failures < len(rows):
        notes.append(
            f"{len(rows) - passes - failures} run(s) ended with a conclusion that is "
            "neither pass nor fail (for example cancelled); they count in Runs and in "
            "the pass rate denominator"
        )
    return document


def _job_recent(rows, limit):
    recent = []
    for row in rows[:limit]:
        duration = row["duration_s"] if row["duration_s"] else None
        run_id = row["run_id"] or str(row["id"])
        url = row["url"] or f"https://github.com/{ORG}/{row['repo']}/actions/runs/{run_id}"
        recent.append({
            "created_at": row["created_at"],
            "day": (row["created_at"] or "")[:10],
            "conclusion": row["conclusion"] or "",
            "duration_s": duration,
            "commit": row["head_sha"] or "",
            "run_id": str(run_id),
            "run_url": url,
        })
    return recent


def _job_matches(conn, name, repo, window_from, window_to, limit=MAX_MATCHES, days=DEFAULT_DAYS):
    created_from, created_until = _job_bounds(window_from, window_to)
    where = "created_at >= ? AND created_at < ? AND display_name LIKE ?"
    args = [created_from, created_until, f"%{name}%"]
    if repo:
        where += " AND repo = ?"
        args.append(repo)
    rows = conn.execute(
        "SELECT repo, display_name, COUNT(*) AS runs, MAX(created_at) AS last_seen"
        f" FROM jobs WHERE {where}"
        " GROUP BY repo, display_name"
        " ORDER BY runs DESC, display_name LIMIT ?",
        [*args, limit],
    )
    return [
        dict(row) | {"history_url": history_url("job", row["display_name"], days, row["repo"])}
        for row in rows
    ]


def _resolve_job(conn, name, repo, window_from, window_to, document, notes, days=DEFAULT_DAYS):
    """The one (repo, display_name) to read, or None with a reason."""
    created_from, created_until = _job_bounds(window_from, window_to)
    exact = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE created_at >= ? AND created_at < ?"
        " AND display_name = ?" + (" AND repo = ?" if repo else ""),
        [created_from, created_until, name] + ([repo] if repo else []),
    ).fetchone()[0]
    if exact:
        if repo:
            return repo, name
        matched = conn.execute(
            "SELECT repo FROM jobs WHERE created_at >= ? AND created_at < ?"
            " AND display_name = ? LIMIT 1",
            (created_from, created_until, name),
        ).fetchone()
        return (matched["repo"] if matched else ""), name
    matches = _job_matches(conn, name, repo, window_from, window_to, MAX_MATCHES + 1, days)
    if len(matches) > MAX_MATCHES:
        notes.append(
            f"more than {MAX_MATCHES} jobs match {name!r}; narrow the name"
        )
        return None
    if len(matches) == 1:
        notes.append(
            f"{name!r} is not an exact job name; using {matches[0]['display_name']!r}"
        )
        return matches[0]["repo"], matches[0]["display_name"]
    if matches:
        document["matches"] = matches[:DEFAULT_MATCHES]
        notes.append(
            f"{len(matches)} jobs match {name!r}; choose one from the list"
        )
        return None
    notes.append(f"the job history has no job whose name matches {name!r} in this window")
    return None


# -- public -----------------------------------------------------------------


def history_document(trends_db, jobs_db, params, now=None):
    """The /api/ci-observe/history document.

    Query params: kind -- "test" (default) or "job"; name -- a pytest node
    id or a job display name, exact or a substring; days -- 1..30, default
    30; repo -- optional repository filter. Returns kind, name, repo, days,
    window_from, window_to, population, source, summary (a one-row list),
    daily, recent, matches, truncated, duration_basis and notes.
    """
    kind = (_str_param(params, "kind") or "test").lower()
    name = _str_param(params, "name")
    repo = _str_param(params, "repo")
    days = _int_param(params, "days", DEFAULT_DAYS, 1, MAX_DAYS)
    window_from, window_to = _window(days, now)
    notes = []
    if kind not in ("test", "job"):
        notes.append(f"kind {kind!r} is neither test nor job; showing test history")
        kind = "test"
    if kind == "test":
        document = _document(
            kind, days, window_from, window_to,
            (
                f"every stored run of one pytest node id in the {days}-day window "
                f"from {window_from} (all jobs, not captured runs)"
            ),
            (
                "trends.db: the daily per-test JUnit rollup and the raw per-run rows "
                "written by ci_observe_trends.py"
            ),
        )
        document["notes"].extend(notes)
        return _test_history(
            document, trends_db, name, repo, window_from, document["notes"], days
        )
    document = _document(
        kind, days, window_from, window_to,
        (
            f"every stored job record of one repo and display name in the {days}-day "
            f"window from {window_from} (all jobs, not captured runs)"
        ),
        (
            "workflow-exporter.db: the job history the exporter polls from GitHub "
            "(one row per job record, 30 days)"
        ),
    )
    document["notes"].extend(notes)
    return _job_history(
        document, jobs_db, name, repo, window_from, window_to, document["notes"], days
    )


def jobs_document(jobs_db, params, now=None):
    """The /api/ci-observe/jobs document: the job names that match a query.

    Query params: days -- 1..30, default 30; q -- substring of the display
    name (empty lists every job); repo -- optional filter; limit -- 1..200,
    default 50. Each row carries repo, display_name, runs, last_seen and a
    history_url that opens Where the time goes on that job's suite.
    """
    days = _int_param(params, "days", DEFAULT_DAYS, 1, MAX_DAYS)
    limit = _int_param(params, "limit", 50, 1, 200)
    query = _str_param(params, "q")
    repo = _str_param(params, "repo")
    window_from, window_to = _window(days, now)
    document = {
        "days": days,
        "q": query,
        "repo": repo,
        "window_from": window_from,
        "window_to": window_to,
        "source": (
            "workflow-exporter.db: the job history the exporter polls from GitHub "
            "(one row per job record, 30 days)"
        ),
        "jobs": [],
        "truncated": False,
        "notes": [],
    }
    if not os.path.exists(jobs_db):
        document["notes"].append(
            "the workflow exporter DB does not exist on this machine, so there "
            "are no job names here"
        )
        return document
    try:
        conn = _connect_read_only(jobs_db)
    except sqlite3.Error as exc:
        document["notes"].append(f"the job history DB could not be opened: {exc}")
        return document
    try:
        conn.row_factory = sqlite3.Row
        document["jobs"] = _job_matches(
            conn, query, repo, window_from, window_to, limit + 1, days
        )
    except sqlite3.Error as exc:
        document["notes"].append(f"the job history DB could not be read: {exc}")
        return document
    finally:
        conn.close()
    document["truncated"] = len(document["jobs"]) > limit
    document["jobs"] = document["jobs"][:limit]
    if document["truncated"]:
        document["notes"].append(
            f"the job limit ({limit}) was reached; type more of the name to narrow it"
        )
    if not document["jobs"]:
        document["notes"].append(
            f"no job name on or after {window_from} matches {query!r}"
        )
    return document

"""Index the failed runs' artifact logs into Loki, and every run's error
signatures into trends.db.

The artifact bundles (osac-logs-[<suite>-]full-install-<run id>) hold the
only error text an install or infrastructure failure leaves behind: the
gather script writes pod logs, AAP job outputs, events and a
failure-summary.txt into them, and nobody read them. The bundles live for
30 days on GitHub (see ci_observe_run_evidence.find_artifacts for the two
places). This job reads them and answers two different questions:

"What failed in this run?" -- Loki holds the failed runs' error lines and
their failure-summary context, queryable per run, outliving the artifacts.
Stream labels stay few, because Loki keeps one index entry and one chunk
per label set:
    source="artifact"      these lines came from a bundle, not the live
                           capture (the live lane pushes without it)
    repo, suite            the run's repository and suite (vmaas, caas or
                           bmaas, from the artifact name)
    kind                   "error" for an error line, "summary" for the
                           gather script's failure-summary context
    bucket                 the UTC day of the line's stamp: Loki refuses
                           an entry more than max_chunk_age/2 (84 h here)
                           behind its stream's newest one, so without the
                           bucket one stream per (repo, suite, kind)
                           would lose every run of a backfill older than
                           that; with it a stream's entries are at most a
                           day apart, and thirty buckets a month per
                           (repo, suite, kind) stay bounded
Every line also carries structured metadata, which LogQL filters on after
the selector without an index entry per value:
    run_id, attempt        the run the bundle belongs to
    signature              only on error lines: the message with its
                           instance-specific parts and log prefixes
                           replaced (ci_observe_phases.message_signature),
                           so the same error in two runs counts as one
so the lines of one run are {source="artifact"} | run_id="<id>". (Before
2026-11-22 run_id, attempt and signature were stream labels: one stream
per run and signature, tens of thousands a week. A label filter after the
selector matches both kinds of line.) Lines are stored verbatim, without a
file label. A line's own RFC 3339 timestamp is kept when it falls inside
the run (an hour of slack each side, never in the future); any other line
is stamped with the run's end, so a query over the run's time range finds
all of its lines and Loki never refuses a stamp as too old or too new.

"What makes failed runs different from passing ones?" -- trends.db (the
ci-observe store) holds one row per (run, signature): how often each
signature occurred, in failed and in passing runs alike. Passing runs
contribute table rows only, no Loki lines: a signature that appears in
both answers nothing, and only the comparison can tell a cause from the
ambient noise every cluster logs (informer timeouts, libvirt races).

Population: the in-scope suites (ci_observe_phases.SUITE_WORKFLOWS), every
trigger, the latest attempt of each run. Failed runs are indexed fully;
passing runs contribute their signatures.

State lives in trends.db:
    artifact_logs          per (run, suite), with the attempt it read:
                          "indexed"; "missing" (no bundle in either place
                          yet: a collect run can trail its source run by
                          minutes, so it is asked for again every pass for
                          MISSING_RETRY_HOURS after the run ended, then
                          recorded "unavailable"); "unavailable" (expired,
                          over the bound, not a zip, or never published:
                          the run's own fact, said once); "rejected" (Loki
                          refused the run's own lines: the run's fact, the
                          reason kept, the pass goes on). A newer attempt of
                          the run is read again.
    artifact_log_signatures per (run, suite, signature): occurrence count,
                          of the attempt the state names
    rollup_passes          the job "logs": when a pass last ran to its end,
                          which the pages and CiObserveHourlyStoreStale read,
                          and how many runs it left for the next passes

The lookups share one GitHub token and one hourly budget with the exporter's
polling, the run pages and the other rollups. A pass stops looking up when
it reaches --limit, when the budget falls to GITHUB_RESERVE, or when GitHub
says the budget is spent; what it read is recorded and the runs it left are
counted on the pages. A pass that read nothing it had to read is not a pass.

Usage:
    ci_observe_logs.py [--days N] [--limit N] [--force] [--dry-run]
                        [--failures-only] [--resign]

Exit status: 0 when the pass ran and every run's bundle was read; 1 when
GitHub or Loki refused the pass (it stops: the refusal is the same for
every following run, and the next hour retries) or when a run's bundle
could not be read or was rejected (the pass goes on; the failure must be
visible, never silent).
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests

import ci_observe_phases as phases
from ci_observe_run_evidence import (
    SUITE_ARTIFACTS,
    EvidenceError,
    GitHubBudget,
    GitHubRateLimited,
    GitHubRefused,
    artifact_places,
    find_artifacts,
    rate_limit_refusal,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
TOKEN = os.getenv("PRIVATE_GITHUB_TOKEN")
LOKI_URL = os.getenv("CI_OBSERVE_LOKI_URL", "http://127.0.0.1:3100").rstrip("/")
CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
EXPORTER_DB = os.path.join(CACHE_DIR, "workflow-exporter.db")
TRENDS_DB = os.path.join(CACHE_DIR, "trends.db")

# The same window as the trends job: past it the artifacts are gone.
ARTIFACT_RETENTION_DAYS = 30
# A bundle larger than this is refused, not truncated: a truncated log is a
# lie about what was collected. The largest observed bundle is 6 MB zipped.
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
# Files above this are skipped and counted, not read: the cluster-state
# text dumps can dwarf the logs, and Loki lines are not the place for them.
MAX_FILE_BYTES = 2 * 1024 * 1024
# Loki refuses entries over 262144 bytes (its max entry size). A longer
# line is skipped and counted, never split: half a line says something its
# author never said.
MAX_LINE_BYTES = 262144
# One Loki push carries at most this many lines: keeps request bodies small
# enough for the LGTM defaults.
PUSH_BATCH_LINES = 500
# A line's own timestamp is kept only inside the run, with this slack each
# side: a stamp outside it (a skewed clock, a local time read as UTC, an
# old date quoted in a message) would put the line where no query over the
# run looks, or be refused by Loki as too new or too old.
LINE_TIME_SLACK = timedelta(hours=1)
# A bundle not found in either place is asked for again every pass for this
# long after the run ended (a collect run can trail its source run), then
# recorded unavailable: a bundle that never came is the run's own fact.
MISSING_RETRY_HOURS = 6
# What a pass leaves of the shared token's hourly GitHub API budget for the
# exporter's polling and the run pages; below it the pass stops looking up
# bundles and the next pass goes on (see ci_observe_run_evidence.GitHubBudget).
GITHUB_RESERVE = int(os.getenv("CI_OBSERVE_LOGS_GITHUB_RESERVE", "1000"))

# The strong error patterns the gather script's own summary greps for,
# minus its noise filter: a zero counter is not an error.
ERROR_LINE_RE = re.compile(r"(?i)\b(errors?|panick?|fatal|fail(ed|ure)?|unreachable|exception|traceback)\b")
NOT_ERROR_RE = re.compile(r"(?i)\b(fail(ed|ure)?|unreachable|errors?)\s*[=:]\s*0\b")
# A line's message begins after these prefixes, stripped before the
# signature is computed: the gather's "N-" line number before an RFC 3339
# timestamp (bare or bracketed), Kubernetes' "I1121 12:00:00.000 1
# file.go:42]" log prefix, and a syslog-style "Nov 21 12:00:00.000" date.
# Applied in rounds, because a logger can stack them ("N-" then a
# timestamp then a k8s prefix); three rounds cover every observed shape.
STAMP_PREFIX_RE = re.compile(
    r"^\s*(?:\d+[-:])?"
    r"\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s*")
K8S_PREFIX_RE = re.compile(r"^\s*[IWEF]\d{4} \d{2}:\d{2}:\d{2}\.\d+ +\d+ [^\s]+:\d+\]\s*")
SYSLOG_PREFIX_RE = re.compile(r"^\s*[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s*")
# A JSON log line's message field, when the line is one: the signature
# groups on what was said, not on the logger's envelope.
JSON_MESSAGE_RE = re.compile(r'"(?:msg|message)"\s*:\s*"([^"]*)"')
LINE_TIME_RE = re.compile(
    r"^\s*(?:\d+[-:])?"
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
# A JSON log line's time field, for lines whose timestamp is not first.
JSON_TIME_RE = re.compile(r'"(?:time|timestamp)"\s*:\s*"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^"]*)"')

# Bundle areas, in match order: the first pattern that fits the path wins.
KIND_PATTERNS = (
    (re.compile(r"(^|/)aap-jobs/"), "aap-jobs"),
    (re.compile(r"(^|/)failures/"), "test-failure"),
    (re.compile(r"events"), "events"),
    (re.compile(r"(^|/)namespaces/|(^|/)pods?[-/]|(^|/)pod-"), "pod"),
)
TEXT_SUFFIXES = (".log", ".txt", ".out", ".err")


def line_message(line):
    """The line with its log prefixes stripped and, for a JSON log line,
    its message field: what the signature is computed on, so the same
    error in two logs groups together however each logger prefixed it."""
    text = line
    for _ in range(3):
        stripped = SYSLOG_PREFIX_RE.sub(
            "", K8S_PREFIX_RE.sub("", STAMP_PREFIX_RE.sub("", text, count=1), count=1), count=1)
        if stripped == text:
            break
        text = stripped
    if text.lstrip().startswith("{"):
        match = JSON_MESSAGE_RE.search(text)
        if match:
            return match.group(1)
    return text.strip()


def parse_line_time(line, fallback, earliest=None, latest=None):
    """The line's own timestamp when it falls in [earliest, latest], else
    `fallback` (the run's end, itself held to `latest`): a stamp outside
    the run would put the line where no query over the run looks, or be
    refused by Loki. The fallback is clamped too, so a run whose stored
    end is ahead of the local clock cannot stamp lines into the future."""
    if latest is not None and fallback > latest:
        fallback = latest
    match = LINE_TIME_RE.match(line) or JSON_TIME_RE.search(line[:200])
    if not match:
        return fallback
    text = match.group(1).replace(" ", "T")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if (earliest is not None and moment < earliest) or (latest is not None and moment > latest):
        return fallback
    return moment


def is_error_line(line):
    stripped = line.strip()
    # The gather script's own markers: the summary file's header and its
    # per-file separators ("--- path ---", "PLAY RECAP"). They carry the
    # words the error patterns grep for, and they say nothing failed.
    if stripped.startswith("===") or stripped.startswith("---") or stripped == "PLAY RECAP":
        return False
    return bool(ERROR_LINE_RE.search(line)) and not NOT_ERROR_RE.search(line)


def file_kind(path):
    for pattern, kind in KIND_PATTERNS:
        if pattern.search(path):
            return kind
    return "other"


def run_phase(run):
    """The failing step's phase, the vocabulary the pages already use (the
    first failed step in run order decides; see
    ci_observe_phases.failure_cause)."""
    return phases.failure_cause(run.get("conclusion"), run["failed_step"], None)["phase"] or "other"


def open_state(db_path):
    """The trends DB with this job's tables (it is the ci-observe store:
    one place for what was read and what was not). The connection waits
    up to a minute for the write lock: the exporter's document reads and
    the other rollups share this file, and a locked-out write must wait
    its turn, not fail the pass (the DB runs in WAL mode, so readers do
    not block writers; the timeout covers the checkpoint moments)."""
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_logs ("
        " run_id TEXT NOT NULL, suite TEXT NOT NULL, repo TEXT NOT NULL,"
        " state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
        " files INTEGER NOT NULL DEFAULT 0, lines INTEGER NOT NULL DEFAULT 0,"
        " error_lines INTEGER NOT NULL DEFAULT 0, skipped_files INTEGER NOT NULL DEFAULT 0,"
        " skipped_lines INTEGER NOT NULL DEFAULT 0,"
        " ingested_at TEXT NOT NULL, attempt TEXT NOT NULL DEFAULT '', PRIMARY KEY (run_id, suite))"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(artifact_logs)")}
    if "skipped_lines" not in columns:
        conn.execute("ALTER TABLE artifact_logs ADD COLUMN skipped_lines INTEGER NOT NULL DEFAULT 0")
    if "attempt" not in columns:
        # Rows from before carry no attempt; they were read at the attempt
        # stored then, which is the first for nearly every run (see
        # stored_attempt).
        conn.execute("ALTER TABLE artifact_logs ADD COLUMN attempt TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_log_signatures ("
        " run_id TEXT NOT NULL, suite TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL,"
        " outcome TEXT NOT NULL, phase TEXT NOT NULL,"
        " lines INTEGER NOT NULL, PRIMARY KEY (run_id, suite, kind, signature))"
    )
    return conn


def stored_attempt(row):
    """The attempt a state row was read at: rows recorded before the attempt
    was kept read the first."""
    return (row["attempt"] if "attempt" in row.keys() else "") or "1"


def runs_in_window(days, now, failures_only):
    """The in-scope suite runs inside the window, newest first: [{run_id,
    repo, trigger, workflow, suite, conclusion, failed_step, attempt,
    created_at, finished_at, outcome}], one per (run, suite) at its latest
    attempt. A merge-queue run appears once per suite (it holds three
    bundles); every other run once for its own suite. A re-run leaves a
    suite row per attempt behind; only the latest attempt's is the run's
    result, and its bundle is the newest of that name. The trigger is the
    taxonomy word (pr, merge_queue, scheduled, manual) the artifact places
    route on, not the raw stored text ("merge queue (PR #2180)"): the
    stored text names the run, the taxonomy names where its artifact
    lives."""
    conn = sqlite3.connect(f"file:{EXPORTER_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conclusion = "AND conclusion IN ('success', 'failure')"
    try:
        rows = conn.execute(
            "SELECT run_id, repo, workflow, event, trigger, failed_step, conclusion, created_at, updated_at,"
            f" run_attempt FROM jobs WHERE category = 'e2e' {conclusion}"
            " AND updated_at >= ? ORDER BY updated_at DESC", (since,)).fetchall()
    finally:
        conn.close()
    latest = {}
    for row in rows:
        suite = phases.suite_key(row["workflow"])
        if not suite:
            continue
        finished = phases.parse_time(row["updated_at"]) or now
        run = {
            "run_id": str(row["run_id"]), "repo": row["repo"],
            "trigger": phases.trigger_key(row["event"], row["workflow"]),
            "workflow": row["workflow"], "failed_step": row["failed_step"], "suite": suite,
            "conclusion": row["conclusion"], "outcome": "success" if row["conclusion"] == "success" else "failure",
            "attempt": str(row["run_attempt"] or 1),
            "created_at": phases.parse_time(row["created_at"]) or finished,
            "finished_at": finished,
        }
        key = (run["run_id"], suite)
        held = latest.get(key)
        if held is None or (int(run["attempt"]), run["finished_at"]) > (int(held["attempt"]), held["finished_at"]):
            latest[key] = run
    # --failures-only keeps the failed results after the latest attempt is
    # picked: filtering in the SQL would make a run whose re-run passed
    # look like its attempt-1 failure, and its newest bundle would be
    # recorded under that old attempt.
    picked = sorted(latest.values(), key=lambda run: run["finished_at"], reverse=True)
    if failures_only:
        return [run for run in picked if run["conclusion"] == "failure"]
    return picked


def list_bundle(run, headers, budget=None):
    """The run's own-suite bundle: {suite, artifact, label, private}, or
    None when neither place has it. GitHub refusing raises (a spent budget
    as GitHubRateLimited). `budget` reads what is left off every answer."""
    name = SUITE_ARTIFACTS[run["suite"]].format(run_id=run["run_id"])
    observe = budget.observe if budget is not None else (lambda response: response)
    found = find_artifacts(
        lambda url: observe(requests.get(url, headers=headers, timeout=60)),
        run["trigger"], run["repo"], run["run_id"], [name], org=ORG, api_url=API_URL)
    for artifact_name, artifact, label, private in found:
        if artifact_name == name:
            return {"suite": run["suite"], "artifact": artifact, "label": label, "private": private}
    return None


def download_bundle(artifact, headers, budget=None):
    """The bundle as a zip, or (None, reason) for the run's own facts
    (expired, over the bound, not a zip). GitHub refusing raises (a spent
    budget as GitHubRateLimited)."""
    if artifact.get("expired"):
        return None, f"the artifact expired on {artifact.get('expires_at') or 'an unknown date'}"
    size = artifact.get("size_in_bytes") or 0
    if size > MAX_BUNDLE_BYTES:
        return None, f"the artifact is {size} bytes, over the {MAX_BUNDLE_BYTES}-byte bound"
    url = artifact.get("archive_download_url") or ""
    try:
        response = requests.get(url, headers=headers, timeout=300)
    except requests.exceptions.RequestException as error:
        raise GitHubRefused(f"downloading the artifact failed: {error}") from None
    if budget is not None:
        budget.observe(response)
    limited = rate_limit_refusal(response, "GitHub", "the artifact download")
    if limited is not None:
        raise limited
    if not response.ok:
        raise GitHubRefused(f"the artifact download answered HTTP {response.status_code}")
    try:
        return zipfile.ZipFile(io.BytesIO(response.content)), ""
    except zipfile.BadZipFile:
        return None, "the artifact is not a zip archive"


def read_bundle(archive, run, now=None):
    """(streams, signatures, counts) of one bundle:
    streams -- {labels tuple: [(ns, line, metadata)]} of the lines to push
        to Loki (failed runs only: every error line, and the failure
        summary's context lines); the labels are few (see the module
        docstring), the run and the signature ride as structured metadata
    signatures -- [(signature, kind, lines)] for the presence table, the
        same for failed and passing runs
    counts -- files, error_lines, skipped_files, lines
    A file over MAX_FILE_BYTES is skipped and counted, never truncated."""
    now = now or datetime.now(timezone.utc)
    streams = {}
    signatures = {}
    counts = {"files": 0, "lines": 0, "error_lines": 0, "skipped_files": 0, "skipped_lines": 0}
    failed = run["outcome"] == "failure"
    finished = run["finished_at"]
    earliest = (run.get("created_at") or finished) - LINE_TIME_SLACK
    latest = min(finished + LINE_TIME_SLACK, now)
    for info in archive.infolist():
        if info.is_dir() or not info.filename.endswith(TEXT_SUFFIXES):
            continue
        is_summary = info.filename.rsplit("/", 1)[-1] == "failure-summary.txt"
        # The summary is read whatever its size up to the bundle's own
        # bound: it is the gather script's digest, and a bundle may not
        # declare a summary bigger than itself (a zip bomb would).
        bound = MAX_BUNDLE_BYTES if is_summary else MAX_FILE_BYTES
        if info.file_size > bound:
            counts["skipped_files"] += 1
            continue
        counts["files"] += 1
        kind = "summary" if is_summary else file_kind(info.filename)
        try:
            text = archive.read(info).decode("utf-8", "replace")
        except (OSError, zipfile.BadZipFile) as error:
            raise EvidenceError(f"{info.filename} could not be read: {error}") from None
        for line in text.splitlines():
            line = line.rstrip("\r")
            if not line.strip():
                continue
            counts["lines"] += 1
            if len(line.encode("utf-8", "replace")) > MAX_LINE_BYTES:
                counts["skipped_lines"] += 1
                continue
            error_line = is_error_line(line)
            if error_line:
                counts["error_lines"] += 1
                signature = phases.message_signature(line_message(line))
                entry = signatures.setdefault((signature, kind), 0)
                signatures[(signature, kind)] = entry + 1
            if not failed or not (error_line or is_summary):
                continue
            labels = {"source": "artifact", "repo": run["repo"], "suite": run["suite"],
                      "kind": "error" if error_line else "summary"}
            metadata = {"run_id": run["run_id"], "attempt": run["attempt"]}
            if error_line:
                metadata["signature"] = signature
            moment = parse_line_time(line, finished, earliest, latest)
            # The day of the line's own stamp, clamped inside the run: a
            # stream's entries are at most a day apart, however far apart
            # the runs that wrote them are. Loki refuses an entry more
            # than max_chunk_age/2 (84 h here) behind its stream's newest
            # one, so one stream per (repo, suite, kind) would lose every
            # run older than that to a 400 while a backfill works through
            # the backlog newest first -- and a rejected run's lines and
            # signatures are never retried. Thirty buckets a month per
            # (repo, suite, kind) is bounded; queries select
            # {source="artifact"} and filter after the selector, so the
            # bucket changes nothing for them.
            labels["bucket"] = moment.astimezone(timezone.utc).strftime("%Y-%m-%d")
            streams.setdefault(tuple(sorted(labels.items())), []).append(
                (int(moment.timestamp() * 1e9), line, metadata))
    return streams, signatures, counts


class LokiRefused(RuntimeError):
    """Loki did not take a push for a reason every following run shares
    (it is down, overloaded, or refusing this client): the pass stops."""


class LokiRejected(EvidenceError):
    """Loki answered 400 to this run's own lines (their content, not the
    service): the run's fact, recorded with Loki's reason; the pass goes
    on. The lines it accepted before stay; the run's signatures are
    recorded whatever Loki said; the lines are not pushed again unless
    forced, so nothing is stored twice."""


def push_streams(streams, session):
    """Push the stream groups to Loki in batches. Loki refusing anything
    raises with its answer; nothing is silently dropped."""
    for labels, values in streams.items():
        group_labels = dict(labels)
        for start in range(0, len(values), PUSH_BATCH_LINES):
            batch = values[start:start + PUSH_BATCH_LINES]
            payload = {"streams": [{
                "stream": group_labels,
                "values": [[str(when), line, metadata] for when, line, metadata in batch],
            }]}
            try:
                response = session.post(f"{LOKI_URL}/loki/api/v1/push", json=payload, timeout=120)
            except requests.exceptions.RequestException as error:
                raise LokiRefused(f"Loki did not answer a push of {len(batch)} lines: {error}") from None
            if response.status_code == 204:
                continue
            answer = f"Loki answered HTTP {response.status_code} to a push of {len(batch)} lines: " \
                     f"{(response.text or '')[:300]}"
            if response.status_code == 400:
                raise LokiRejected(answer)
            raise LokiRefused(answer)


def flush_loki(session):
    """Old-timestamped entries stay invisible to queries until their chunk
    is flushed (see the loki override's comment); the pass flushes at the
    end so a backfill is queryable at once."""
    try:
        response = session.post(f"{LOKI_URL}/flush", timeout=60)
        if response.status_code not in (200, 204):
            logger.warning("Loki answered HTTP %s to the flush", response.status_code)
    except requests.exceptions.RequestException as error:
        logger.warning("the Loki flush failed: %s", error)


NO_COUNTS = {"files": 0, "lines": 0, "error_lines": 0, "skipped_files": 0, "skipped_lines": 0}


def record_run(state, run, state_name, reason, counts, now):
    state.execute(
        "INSERT OR REPLACE INTO artifact_logs (run_id, suite, repo, state, reason, files, lines, error_lines,"
        " skipped_files, skipped_lines, ingested_at, attempt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run["run_id"], run["suite"], run["repo"], state_name, reason,
         counts["files"], counts["lines"], counts["error_lines"], counts["skipped_files"],
         counts["skipped_lines"], now.strftime("%Y-%m-%dT%H:%M:%SZ"), run["attempt"]))


def record_signatures(state, run, signatures):
    state.execute("DELETE FROM artifact_log_signatures WHERE run_id = ? AND suite = ?",
                  (run["run_id"], run["suite"]))
    state.executemany(
        "INSERT INTO artifact_log_signatures VALUES (?,?,?,?,?,?,?)",
        [(run["run_id"], run["suite"], kind, signature, run["outcome"], run_phase(run), lines)
         for (signature, kind), lines in signatures.items()])
    state.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index the artifact logs of ci-observe's suite runs.")
    parser.add_argument("--days", type=int, default=1, help="read runs updated in the last N days")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after looking up N runs' bundles (indexed, missing or unreadable alike:"
                             " each costs GitHub calls); the next pass goes on")
    parser.add_argument("--force", action="store_true", help="re-index runs the guard already recorded")
    parser.add_argument("--dry-run", action="store_true", help="resolve and read, push and record nothing")
    parser.add_argument("--failures-only", action="store_true",
                        help="skip passing runs (their signatures are the noise baseline; "
                             "the hourly pass wants them, a backfill may not)")
    parser.add_argument("--resign", action="store_true",
                        help="recompute the signature table of runs already recorded as indexed"
                             " or rejected, pushing nothing to Loki (their lines are already there,"
                             " all or in part). For repairing the table after the signature"
                             " normalization changes, or for runs rejected before 2026-11-22, whose"
                             " signatures were not recorded; a plain re-index would duplicate"
                             " every line in Loki.")
    args = parser.parse_args(argv)

    if not TOKEN:
        logger.error("PRIVATE_GITHUB_TOKEN is not set; the artifacts cannot be listed")
        return 1
    now = datetime.now(timezone.utc)
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
    session = requests.Session()

    state = open_state(TRENDS_DB)
    runs = runs_in_window(args.days, now, args.failures_only)
    logger.info("%d in-scope suite run(s) in the last %d day(s)", len(runs), args.days)

    indexed = skipped = failed = missing = unavailable = 0
    looked_up = 0
    budget = GitHubBudget(GITHUB_RESERVE)
    stopped = ""
    left = 0
    for position, run in enumerate(runs):
        row = state.execute("SELECT * FROM artifact_logs WHERE run_id = ? AND suite = ?",
                            (run["run_id"], run["suite"])).fetchone()
        if not needs_lookup(row, run, args):
            skipped += 1
            continue
        # "missing": a collect run can trail its source run by minutes, so a
        # missing bundle is asked for again every pass while the run is young.
        if args.limit and looked_up >= args.limit:
            stopped = f"a pass looks up at most {args.limit} runs"
        elif budget.low():
            stopped = budget.describe()
        if stopped:
            left = pending_lookups(state, runs[position:], args)
            logger.info("the pass stops (%s); %d run(s) are left for the next passes", stopped, left)
            break
        looked_up += 1
        try:
            bundle = list_bundle(run, headers, budget)
            if bundle is None:
                expected, other = artifact_places(run["trigger"], run["repo"], run["run_id"], ORG)
                reason = (f"no {SUITE_ARTIFACTS[run['suite']].format(run_id=run['run_id'])} artifact"
                          f" in {expected} or in {other}")
                hours = (now - run["finished_at"]).total_seconds() / 3600
                final = hours > MISSING_RETRY_HOURS
                if final:
                    reason += f" {hours:.0f} hours after the run ended"
                if not args.dry_run:
                    record_run(state, run, "unavailable" if final else "missing", reason, NO_COUNTS, now)
                    state.commit()
                if final:
                    unavailable += 1
                else:
                    missing += 1
                logger.info("  %s run %s (%s, %s): %s", run["repo"], run["run_id"], run["suite"],
                            run["outcome"], reason)
                continue
            archive, reason = download_bundle(bundle["artifact"], headers, budget)
            if archive is None:
                if not args.dry_run:
                    record_run(state, run, "unavailable", reason, NO_COUNTS, now)
                    state.commit()
                unavailable += 1
                logger.info("  %s run %s (%s, %s): %s", run["repo"], run["run_id"], run["suite"],
                            run["outcome"], reason)
                continue
            streams, signatures, counts = read_bundle(archive, run, now)
            if not args.dry_run and streams and not args.resign:
                try:
                    push_streams(streams, session)
                except LokiRejected as error:
                    # Recorded before the next run, so a later pass does not
                    # push the lines Loki already took a second time. The
                    # signatures are bundle facts, not Loki's, so they are
                    # recorded (and counted by the pages) whatever Loki said
                    # about the lines.
                    record_run(state, run, "rejected", str(error), counts, now)
                    record_signatures(state, run, signatures)
                    state.commit()
                    raise
            if not args.dry_run:
                # A repair keeps the run's state: a rejected run's lines are
                # in Loki only in part, and the run rows say so.
                kept = row["state"] if args.resign else "indexed"
                record_run(state, run, kept, row["reason"] if args.resign else "", counts, now)
                record_signatures(state, run, signatures)
            indexed += 1
            logger.info("  %s run %s (%s, %s): %d file(s), %d error line(s), %d signature(s)%s",
                        run["repo"], run["run_id"], run["suite"], run["outcome"], counts["files"],
                        counts["error_lines"], len(signatures),
                        f", {counts['skipped_files']} file(s) skipped" if counts["skipped_files"] else "")
            if counts["skipped_lines"]:
                logger.info("  %s run %s: %d line(s) over Loki's entry bound were not stored",
                            run["repo"], run["run_id"], counts["skipped_lines"])
        except GitHubRateLimited as error:
            # The shared budget is spent: not this run's fact and not a
            # fault. What was read is recorded; the rest waits for the next
            # pass, after the budget resets.
            looked_up -= 1
            stopped = str(error)
            left = pending_lookups(state, runs[position:], args)
            logger.warning("  %s run %s (%s): %s", run["repo"], run["run_id"], run["suite"], error)
            break
        except (GitHubRefused, LokiRefused) as error:
            # A refusal is the same for every following run: stop the pass,
            # say why, exit non-zero. The guard retries next hour. No pass
            # is recorded, so the store's freshness ages and alerts.
            logger.error("  %s run %s (%s): %s", run["repo"], run["run_id"], run["suite"], error)
            logger.error("the artifact log pass stopped, nothing more was stored")
            state.close()
            return 1
        except EvidenceError as error:
            # This run's own unreadable or rejected bundle; the pass continues.
            failed += 1
            logger.error("  %s run %s (%s): %s", run["repo"], run["run_id"], run["suite"], error)

    summary = (f"{indexed} indexed, {missing} missing, {unavailable} unavailable, {skipped} already read,"
               f" {failed} unreadable or rejected")
    if left:
        summary += f", {left} left for the next passes ({stopped})"
    if not args.dry_run:
        flush_loki(session)
        if left and not looked_up:
            # It read none of what it had to read: not a pass. The pages and
            # CiObserveHourlyStoreStale say the signatures are stale.
            logger.error("the artifact log pass read none of the %d run(s) it has to read: %s", left, stopped)
            state.close()
            return 1
        phases.record_pass(state, "logs", now, summary, pending=left)
    state.close()
    logger.info("%s%s", summary, " (dry run, nothing pushed or recorded)" if args.dry_run else "")
    return 1 if failed else 0


def needs_lookup(row, run, args):
    """Whether this pass looks up the run's bundle, given its artifact_logs
    row (None when never read)."""
    same_attempt = row is not None and stored_attempt(row) == run["attempt"]
    if args.resign:
        # A repair pass recomputes what was read: indexed, or rejected by
        # Loki (whose signatures were not recorded before 2026-11-22). A run
        # with no recorded bundle has nothing to recompute.
        return row is not None and row["state"] in ("indexed", "rejected") and same_attempt
    # The run's own fact at this attempt is not read again unless forced; a
    # newer attempt is a new bundle and is read.
    return not (row is not None and same_attempt and not args.force
                and row["state"] in ("indexed", "unavailable", "rejected"))


def pending_lookups(state, runs, args):
    """How many of `runs` still need a lookup: what a stopped pass leaves."""
    count = 0
    for run in runs:
        row = state.execute("SELECT * FROM artifact_logs WHERE run_id = ? AND suite = ?",
                            (run["run_id"], run["suite"])).fetchone()
        count += needs_lookup(row, run, args)
    return count


if __name__ == "__main__":
    sys.exit(main())

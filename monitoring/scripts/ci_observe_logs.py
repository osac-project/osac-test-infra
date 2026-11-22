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
Labels on every line:
    source="artifact"      these lines came from a bundle, not the live
                           capture (the live lane pushes without it)
    repo, run_id, attempt  the run the bundle belongs to
    suite                  vmaas, caas or bmaas, from the artifact name
    phase                  the failing step's phase (ci_observe_phases:
                           prepare, build, install, cluster, test, collect,
                           other)
    kind                   the bundle area: aap-jobs (the install
                           pipeline's job outputs), test-failure (the
                           per-test captured logs), pod, events, summary
                           (the gather script's own digest), other
    signature              only on error lines: the message with its
                           instance-specific parts and log prefixes
                           replaced (ci_observe_phases.message_signature),
                           so the same error in two runs counts as one
Lines are stored verbatim, without a file label: a per-file label would
multiply the stream count by the bundle's file count. Timestamps are
parsed from the line when it carries an RFC 3339 one, else the run's end
time, so time-range queries stay honest.

"What makes failed runs different from passing ones?" -- trends.db (the
ci-observe store) holds one row per (run, signature): how often each
signature occurred, in failed and in passing runs alike. Passing runs
contribute table rows only, no Loki lines: a signature that appears in
both answers nothing, and only the comparison can tell a cause from the
ambient noise every cluster logs (informer timeouts, libvirt races).

Population: the in-scope suites (ci_observe_phases.SUITE_WORKFLOWS), every
trigger. Failed runs are indexed fully; passing runs contribute their
signatures.

State lives in trends.db:
    artifact_logs          per (run, suite): "indexed", "missing" (no
                          bundle in either place; retried every pass,
                          because a collect run can trail its source run by
                          minutes) or "unavailable" (expired or unreadable;
                          the run's own fact, said once)
    artifact_log_signatures per (run, suite, signature): occurrence count

Usage:
    ci_observe_logs.py [--days N] [--limit N] [--force] [--dry-run]
                        [--failures-only]

Exit status: 0 when the pass ran, 1 when Loki or GitHub refused anything
(fail fast: the guard retries next hour, but a refusal must be visible,
never silent).
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
    GitHubRefused,
    artifact_places,
    find_artifacts,
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


def parse_line_time(line, fallback):
    """The line's own timestamp, or the run's end time when it has none."""
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
    one place for what was read and what was not)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_logs ("
        " run_id TEXT NOT NULL, suite TEXT NOT NULL, repo TEXT NOT NULL,"
        " state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
        " files INTEGER NOT NULL DEFAULT 0, lines INTEGER NOT NULL DEFAULT 0,"
        " error_lines INTEGER NOT NULL DEFAULT 0, skipped_files INTEGER NOT NULL DEFAULT 0,"
        " skipped_lines INTEGER NOT NULL DEFAULT 0,"
        " ingested_at TEXT NOT NULL, PRIMARY KEY (run_id, suite))"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(artifact_logs)")}
    if "skipped_lines" not in columns:
        conn.execute("ALTER TABLE artifact_logs ADD COLUMN skipped_lines INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifact_log_signatures ("
        " run_id TEXT NOT NULL, suite TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL,"
        " outcome TEXT NOT NULL, phase TEXT NOT NULL,"
        " lines INTEGER NOT NULL, PRIMARY KEY (run_id, suite, kind, signature))"
    )
    return conn


def runs_in_window(days, now, failures_only):
    """The in-scope suite runs inside the window, newest first: [{run_id,
    repo, trigger, workflow, suite, conclusion, failed_step, attempt,
    finished_at, outcome}]. A merge-queue run appears once per suite (it
    holds three bundles); every other run once for its own suite. The
    trigger is the taxonomy word (pr, merge_queue, scheduled, manual) the
    artifact places route on, not the raw stored text ("merge queue (PR
    #2180)"): the stored text names the run, the taxonomy names where its
    artifact lives."""
    conn = sqlite3.connect(f"file:{EXPORTER_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conclusion = "AND conclusion = 'failure'" if failures_only else "AND conclusion IN ('success', 'failure')"
    try:
        rows = conn.execute(
            "SELECT run_id, repo, workflow, event, trigger, failed_step, conclusion, updated_at, run_attempt"
            f" FROM jobs WHERE category = 'e2e' {conclusion}"
            " AND updated_at >= ? ORDER BY updated_at DESC", (since,)).fetchall()
    finally:
        conn.close()
    runs = []
    for row in rows:
        suite = phases.suite_key(row["workflow"])
        if not suite:
            continue
        runs.append({
            "run_id": str(row["run_id"]), "repo": row["repo"],
            "trigger": phases.trigger_key(row["event"], row["workflow"]),
            "workflow": row["workflow"], "failed_step": row["failed_step"], "suite": suite,
            "conclusion": row["conclusion"], "outcome": "success" if row["conclusion"] == "success" else "failure",
            "attempt": str(row["run_attempt"] or 1),
            "finished_at": phases.parse_time(row["updated_at"]) or now,
        })
    return runs


def list_bundle(run, headers):
    """The run's own-suite bundle: {suite, artifact, label, private}, or
    None when neither place has it. GitHub refusing raises."""
    name = SUITE_ARTIFACTS[run["suite"]].format(run_id=run["run_id"])
    found = find_artifacts(
        lambda url: requests.get(url, headers=headers, timeout=60),
        run["trigger"], run["repo"], run["run_id"], [name], org=ORG, api_url=API_URL)
    for artifact_name, artifact, label, private in found:
        if artifact_name == name:
            return {"suite": run["suite"], "artifact": artifact, "label": label, "private": private}
    return None


def download_bundle(artifact, headers):
    """The bundle as a zip, or (None, reason) for the run's own facts
    (expired, over the bound, not a zip). GitHub refusing raises."""
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
    if not response.ok:
        raise GitHubRefused(f"the artifact download answered HTTP {response.status_code}")
    try:
        return zipfile.ZipFile(io.BytesIO(response.content)), ""
    except zipfile.BadZipFile:
        return None, "the artifact is not a zip archive"


def read_bundle(archive, run):
    """(streams, signatures, counts) of one bundle:
    streams -- {labels tuple: [(ns, line)]} of the lines to push to Loki
        (failed runs only: every error line, and the failure summary's
        context lines)
    signatures -- [(signature, kind, lines)] for the presence table, the
        same for failed and passing runs
    counts -- files, error_lines, skipped_files, lines
    A file over MAX_FILE_BYTES is skipped and counted, never truncated."""
    streams = {}
    signatures = {}
    counts = {"files": 0, "lines": 0, "error_lines": 0, "skipped_files": 0, "skipped_lines": 0}
    failed = run["outcome"] == "failure"
    for info in archive.infolist():
        if info.is_dir() or not info.filename.endswith(TEXT_SUFFIXES):
            continue
        is_summary = info.filename.rsplit("/", 1)[-1] == "failure-summary.txt"
        if not is_summary and info.file_size > MAX_FILE_BYTES:
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
            # Per-line labels stay small: repo, run, suite and, on error
            # lines, the signature the queries aggregate by. Kind and phase
            # are per-run facts and live in the signature table; a label
            # per line would multiply the stream count by nothing useful.
            labels = {
                "source": "artifact", "repo": run["repo"], "run_id": run["run_id"],
                "attempt": run["attempt"], "suite": run["suite"],
            }
            if error_line:
                labels["signature"] = signature
            else:
                labels["kind"] = "summary"
            moment = parse_line_time(line, run["finished_at"])
            streams.setdefault(tuple(sorted(labels.items())), []).append(
                (int(moment.timestamp() * 1e9), line))
    return streams, signatures, counts


def push_streams(streams, session):
    """Push the stream groups to Loki in batches. Loki refusing anything
    raises with its answer; nothing is silently dropped."""
    for labels, values in streams.items():
        group_labels = dict(labels)
        for start in range(0, len(values), PUSH_BATCH_LINES):
            batch = values[start:start + PUSH_BATCH_LINES]
            payload = {"streams": [{
                "stream": group_labels,
                "values": [[str(when), line] for when, line in batch],
            }]}
            response = session.post(f"{LOKI_URL}/loki/api/v1/push", json=payload, timeout=120)
            if response.status_code != 204:
                raise RuntimeError(
                    f"Loki answered HTTP {response.status_code} to a push of {len(batch)} lines: "
                    f"{response.text[:300]}")


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


def record_run(state, run, state_name, reason, counts, now):
    state.execute("INSERT OR REPLACE INTO artifact_logs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (run["run_id"], run["suite"], run["repo"], state_name, reason,
                   counts["files"], counts["lines"], counts["error_lines"], counts["skipped_files"],
                   counts["skipped_lines"], now.strftime("%Y-%m-%dT%H:%M:%SZ")))


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
    parser.add_argument("--limit", type=int, default=None, help="stop after N indexed bundles")
    parser.add_argument("--force", action="store_true", help="re-index runs the guard already recorded")
    parser.add_argument("--dry-run", action="store_true", help="resolve and read, push and record nothing")
    parser.add_argument("--failures-only", action="store_true",
                        help="skip passing runs (their signatures are the noise baseline; "
                             "the hourly pass wants them, a backfill may not)")
    args = parser.parse_args(argv)

    if not TOKEN:
        logger.error("PRIVATE_GITHUB_TOKEN is not set; the artifacts cannot be listed")
        return 1
    now = datetime.now(timezone.utc)
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
    session = requests.Session()
    session.headers.update(headers)

    state = open_state(TRENDS_DB)
    runs = runs_in_window(args.days, now, args.failures_only)
    logger.info("%d in-scope suite run(s) in the last %d day(s)", len(runs), args.days)

    indexed = skipped = failed = missing = 0
    done = 0
    for run in runs:
        row = state.execute("SELECT state FROM artifact_logs WHERE run_id = ? AND suite = ?",
                            (run["run_id"], run["suite"])).fetchone()
        if row and not args.force:
            if row["state"] == "indexed":
                skipped += 1
                continue
            if row["state"] == "unavailable":
                # The run's own fact (expired, unreadable); only a forced
                # pass says it again.
                skipped += 1
                continue
            # "missing": a collect run can trail its source run by minutes,
            # so a missing bundle is asked for again every pass.
        try:
            bundle = list_bundle(run, headers)
            if bundle is None:
                expected, other = artifact_places(run["trigger"], run["repo"], run["run_id"], ORG)
                reason = (f"no {SUITE_ARTIFACTS[run['suite']].format(run_id=run['run_id'])} artifact"
                          f" in {expected} or in {other}")
                if not args.dry_run:
                    record_run(state, run, "missing", reason,
                               {"files": 0, "lines": 0, "error_lines": 0, "skipped_files": 0,
                                "skipped_lines": 0}, now)
                    state.commit()
                missing += 1
                logger.info("  %s run %s (%s, %s): %s", run["repo"], run["run_id"], run["suite"],
                            run["outcome"], reason)
                continue
            archive, reason = download_bundle(bundle["artifact"], headers)
            if archive is None:
                if not args.dry_run:
                    record_run(state, run, "unavailable", reason,
                               {"files": 0, "lines": 0, "error_lines": 0, "skipped_files": 0,
                                "skipped_lines": 0}, now)
                    state.commit()
                missing += 1
                logger.info("  %s run %s (%s, %s): %s", run["repo"], run["run_id"], run["suite"],
                            run["outcome"], reason)
                continue
            streams, signatures, counts = read_bundle(archive, run)
            if not args.dry_run:
                if streams:
                    push_streams(streams, session)
                record_run(state, run, "indexed", "", counts, now)
                record_signatures(state, run, signatures)
            indexed += 1
            done += 1
            logger.info("  %s run %s (%s, %s): %d file(s), %d error line(s), %d signature(s)%s",
                        run["repo"], run["run_id"], run["suite"], run["outcome"], counts["files"],
                        counts["error_lines"], len(signatures),
                        f", {counts['skipped_files']} file(s) skipped" if counts["skipped_files"] else "")
            if counts["skipped_lines"]:
                logger.info("  %s run %s: %d line(s) over Loki's entry bound were not stored",
                            run["repo"], run["run_id"], counts["skipped_lines"])
            if args.limit and done >= args.limit:
                break
        except (GitHubRefused, RuntimeError) as error:
            # A refusal is the same for every following run: stop the pass,
            # say why, exit non-zero. The guard retries next hour.
            logger.error("  %s run %s (%s): %s", run["repo"], run["run_id"], run["suite"], error)
            logger.error("the artifact log pass stopped, nothing more was stored")
            state.close()
            return 1
        except EvidenceError as error:
            # This run's own unreadable bundle; the pass continues.
            failed += 1
            logger.error("  %s run %s (%s): %s", run["repo"], run["run_id"], run["suite"], error)

    if not args.dry_run:
        flush_loki(session)
    state.close()
    logger.info("%d bundle(s) indexed, %d missing, %d skipped, %d unreadable%s",
                indexed, missing, skipped, failed,
                " (dry run, nothing pushed or recorded)" if args.dry_run else "")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

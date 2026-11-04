"""The runs half of ci-observe: /api/ci-observe/runs, the E2E run list with a
per-run capture verdict.

The workflow exporter knows every E2E run (SQLite job history, 60 days) but
stores no capture id. Tempo knows every capture that arrived (30 days) but
not the runs that never sent one. Neither store can answer "which runs have
no capture" alone, so this endpoint joins them on the GitHub run id, which
the capture id embeds.

The capture set comes from Tempo's tag-values endpoint, which lists the
distinct values of the `resource.osac.ci.capture_id` tag in a window. One
capture produces hundreds of spans, so a span search needs a trace limit and
silently loses captures past it; the tag list has one entry per capture and
no such per-capture cap. Tempo rejects any range over 168h, and the entry
page offers 7, 30 and 60 days, so the window is queried in chunks of at most
seven days and the answers are merged.

Nothing is faked. A run is `not collected` only when every chunk answered
completely and the run is old enough that its capture would have arrived.
Every other case is `unknown` with a reason: Tempo down, a chunk that failed
or came back capped, a tag whose values are not capture ids, a run newer than
the cached lookup, or a run older than the capture retention.

The capture map is cached for 60 seconds per window length. The exporter
polls GitHub every 90 seconds and this endpoint is called on every dashboard
refresh, while the Pull requests page asks for 7 days and the entry page for
30, so one cache entry per length is what keeps a reader from re-querying
Tempo on every alternating call. The lookup is bounded by the chunk size and
a request timeout, and is read-only: it never writes the job history, the
rollup or Tempo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Tempo 2.10 (classic) serves every trace recorded since the 2026-11-01
# cutover. The router exports to it on the host's loopback and Grafana reads
# it the same way (datasources.yml, uid ci-observe-traces). The exporter
# container runs with host networking for exactly this reason (see
# quadlet/workflow-exporter.container); CI_OBSERVE_TEMPO_URL overrides the
# address for any other network layout.
DEFAULT_TEMPO_URL = "http://127.0.0.1:3210"
TEMPO_URL_ENV = "CI_OBSERVE_TEMPO_URL"

# The template blocks are kept 720h (monitoring/config/ci-observe/tempo210/
# tempo.yaml), so a capture older than this cannot be found however healthy
# Tempo is. A run older than the retention is `unknown`, never `not
# collected`.
TEMPO_RETENTION_DAYS = 30

# Live ingest moved from the LGTM bundle's Tempo 3.1.1 (port 3200) to this
# 2.10 instance on 2026-11-01. Traces recorded before that are in the
# archive, which this endpoint does not query, so runs from before the
# cutover are `unknown` rather than `not collected`.
TEMPO_LIVE_SINCE = datetime(2026, 11, 1, tzinfo=UTC)

# Tempo rejects a range longer than 168h ("range specified by start and end
# exceeds 168h0m0s"), so a 30- or 60-day window is queried in one chunk per
# seven days and the answers are merged. Exactly 168h is allowed; only a
# longer range is refused.
TEMPO_MAX_RANGE_HOURS = 168

CAPTURE_ID_ATTRIBUTE = "osac.ci.capture_id"

# The tag-values endpoint takes the scoped tag name; the bare attribute name
# is rejected ("invalid tag name"). Scope and name are kept in one constant so
# the endpoint path and the tests cannot drift.
CAPTURE_TAG = f"resource.{CAPTURE_ID_ATTRIBUTE}"

# A capture id is self-describing: "<suite>-<prN|main>-<run id>-a<n>" (see
# ci_observe_run_evidence.CAPTURE_ID_RE). The run id is the join key, and it
# comes out of the id with this pattern -- the tag list carries capture ids
# only, not their attributes.
CAPTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-(?:pr\d+|main)-(\d+)-a(\d+)$")


class CIObserveRuns:
    """The /api/ci-observe/runs endpoint, held by WorkflowExporter as
    self._runs. The exporter argument provides the SQLite job history
    (get_jobs_json) and is the only source of the run list; Tempo is reached
    over HTTP with the standard library, read-only."""

    # The capture set is a tag list, not a span search: one entry per capture,
    # so nothing caps it per capture. Tempo imposes no count limit on tag
    # values, only a byte limit on the response, which it reports as an HTTP
    # error -- treated as a failed chunk, never as "no captures". A response
    # whose own metrics say it did not finish every job is treated the same
    # way (see _tag_values_complete).
    TEMPO_TIMEOUT_SECONDS = 5
    # Tempo answers only for spans it has indexed. A capture that closed
    # seconds ago may not be searchable yet, so a run younger than this is
    # `unknown`: too new to judge.
    FRESH_SECONDS = 120
    CAPTURE_CACHE_TTL_SECONDS = 60
    DEFAULT_DAYS = 7
    MAX_DAYS = 60
    DEFAULT_LIMIT = 200
    MAX_LIMIT = 2000

    # One chunk of a window is at most Tempo's 168h range limit. Chunks are
    # whole seconds so the boundary arithmetic matches what Tempo is sent.
    TEMPO_CHUNK_SECONDS = TEMPO_MAX_RANGE_HOURS * 3600

    # A pull-request-flow run adds one e2e row per suite (up to four), and each
    # of those is a job, not a run. Asking the history for five times `limit`
    # rows keeps the run list full when suite rows dominate the newest window.
    # The fetch stays bounded by MAX_LIMIT.
    FETCH_ROWS_PER_RUN = 5

    # A run-level row's url has no /job/ segment; a suite row's does.
    RUN_URL_RE = re.compile(r"/actions/runs/(\d+)$")

    def __init__(self, exporter, tempo_url=None):
        self.exporter = exporter
        self.tempo_url = (tempo_url or os.getenv(TEMPO_URL_ENV) or DEFAULT_TEMPO_URL).rstrip("/")
        # {days: {"expires_at": monotonic, "captures": {...}, "verdict": {...}}}
        # One entry per window length, because the entry page asks for 30 days
        # while the Pull requests read side asks for 7: a single slot would
        # drop one of them on every alternating call and re-query Tempo.
        # Entries are replaced whole, never mutated, so a reader never sees a
        # half-built map.
        self._cache = {}
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------
    def get_json(self, params):
        """The /api/ci-observe/runs document.

        Query params: days -- how many days of runs to list (default 7,
        clamped to 1..60), limit -- how many runs to list (default 200,
        clamped to 1..2000).

        Returns days, window_from, window_to, tempo, runs, notes. Every run
        row carries capture_state ("capture", "not collected" or "unknown"),
        capture_ids and capture_reason (empty unless the state is unknown).
        """
        days = self._int_param(params, "days", self.DEFAULT_DAYS, 1, self.MAX_DAYS)
        limit = self._int_param(params, "limit", self.DEFAULT_LIMIT, 1, self.MAX_LIMIT)
        now = datetime.now(UTC)
        window_from = now - timedelta(days=days)
        doc = {
            "days": days,
            "window_from": window_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tempo": {
                "url": self.tempo_url,
                "tag": CAPTURE_TAG,
                "chunks": 0,
                "cached": False,
                "captures": 0,
                "state": "ok",
                "note": "",
            },
            "runs": [],
            "notes": [],
        }

        runs = self._run_rows(window_from, now, limit)
        if not runs:
            doc["notes"].append(
                f"the job history has no E2E runs on or after {doc['window_from']}"
            )
            return doc

        captures, verdict = self._capture_map(days, window_from, now)
        doc["tempo"].update(
            chunks=verdict["chunks"],
            cached=verdict["cached"],
            captures=len(captures),
            state=verdict["state"],
            note=verdict["note"],
        )
        if verdict["note"]:
            doc["notes"].append(verdict["note"])

        judgeable_from = max(
            window_from, now - timedelta(days=TEMPO_RETENTION_DAYS), TEMPO_LIVE_SINCE
        )
        judgeable_until = verdict["fetched_at"] - timedelta(seconds=self.FRESH_SECONDS)

        for run in runs:
            capture_ids = captures.get(run["id"], [])
            state, reason = self._state(run, capture_ids, verdict, judgeable_from, judgeable_until)
            doc["runs"].append(
                run | {
                    "capture_state": state,
                    "capture_ids": capture_ids,
                    "capture_reason": reason,
                }
            )
        return doc

    def capture_ids_by_run(self, days=DEFAULT_DAYS):
        """{run id: [capture id, ...]} for the window, from the run cache.

        The Pull requests read side uses it to link a queue attempt to the
        capture its run produced. A missing entry means Tempo has no capture
        for that run in the window, or that part of the window could not be
        read; it is not a claim that the run had none.
        """
        now = datetime.now(UTC)
        captures, _ = self._capture_map(days, now - timedelta(days=days), now)
        return captures

    # -- run list ----------------------------------------------------------
    def _run_rows(self, window_from, until, limit):
        """One row per run, not one per job.

        A pull-request-flow run's suite rows share the flow's id but are jobs,
        and a job id never appears in a capture id; keeping them would invent
        `not collected` rows. A run-level row's url has no /job/ segment.
        """
        jobs = self.exporter.get_jobs_json({
            "category": ["e2e"],
            "since": [window_from.strftime("%Y-%m-%dT%H:%M:%SZ")],
            "until": [until.strftime("%Y-%m-%dT%H:%M:%SZ")],
            "limit": [str(min(limit * self.FETCH_ROWS_PER_RUN, self.MAX_LIMIT))],
        })
        rows = []
        seen = set()
        for job in jobs:
            run_id = str(job.get("id", ""))
            if not run_id or run_id in seen:
                continue
            if not self.RUN_URL_RE.search(job.get("url") or ""):
                continue
            seen.add(run_id)
            rows.append({
                "id": run_id,
                "workflow": job.get("workflow", ""),
                "trigger": job.get("trigger", ""),
                "branch": job.get("branch", ""),
                "conclusion": job.get("conclusion", ""),
                "created_at": job.get("created_at", ""),
                "url": job.get("url", ""),
            })
            if len(rows) >= limit:
                break
        return rows

    # -- the capture join --------------------------------------------------
    def _capture_map(self, days, window_from, until):
        """({run id: [capture id, ...]}, verdict) for the window.

        The window is queried in chunks of at most seven days, because Tempo
        refuses a range over 168h and the entry page offers 30 and 60. The
        answers are merged; any failed or incomplete chunk marks the answer
        incomplete, so an absent capture is `unknown`, never `not collected`.

        Cached for CAPTURE_CACHE_TTL_SECONDS under the window length, not the
        exact seconds, so repeated dashboard loads hit it. A run newer than
        the cache's fetch time is `unknown`, never `not collected` (see
        FRESH_SECONDS).
        """
        with self._lock:
            cached = self._cache.get(days)
            if cached and time.monotonic() < cached["expires_at"]:
                logger.debug("capture map cache hit for days=%s", days)
                return cached["captures"], cached["verdict"] | {"cached": True}

        verdict = {
            "state": "ok",
            "note": "",
            "complete": True,
            "chunks": 0,
            "fetched_at": until,
            "cached": False,
        }
        captures = {}
        failures = []
        capped = []
        values_seen = 0
        capture_ids_seen = 0
        for start, end in self._window_chunks(window_from, until):
            verdict["chunks"] += 1
            try:
                values, complete = self._tempo_capture_ids(start, end)
            except (OSError, ValueError) as exc:
                logger.debug("Tempo tag values failed for %s..%s: %s", start, end, exc)
                failures.append(f"{self._iso(start)}: {exc}")
                continue
            if not complete:
                capped.append(self._iso(start))
            values_seen += len(values)
            for capture_id in values:
                match = CAPTURE_ID_RE.match(capture_id)
                if not match:
                    continue
                capture_ids_seen += 1
                bucket = captures.setdefault(match.group(1), [])
                if capture_id not in bucket:
                    bucket.append(capture_id)

        if failures or capped:
            verdict.update(complete=False)
            if len(failures) == verdict["chunks"]:
                verdict.update(
                    state="unknown",
                    note=(
                        f"Tempo at {self.tempo_url} did not answer ({failures[0]}); every run "
                        "is reported as unknown, never as not collected"
                    ),
                )
            else:
                reasons = failures or [f"an incomplete answer for {', '.join(capped)}"]
                verdict.update(
                    state="truncated",
                    note=(
                        f"the capture lookup did not complete ({reasons[0]}); a missing capture "
                        "is reported as unknown, not as not collected"
                    ),
                )
        elif values_seen and not capture_ids_seen:
            verdict.update(
                state="unreadable",
                complete=False,
                note=(
                    "Tempo returned capture tag values but none of them is a capture id; "
                    "the id format may have changed, so a missing capture is unknown"
                ),
            )

        with self._lock:
            now = time.monotonic()
            self._cache = {
                key: entry for key, entry in self._cache.items() if entry["expires_at"] > now
            }
            self._cache[days] = {
                "expires_at": now + self.CAPTURE_CACHE_TTL_SECONDS,
                "captures": captures,
                "verdict": verdict,
            }
        return captures, verdict

    @classmethod
    def _window_chunks(cls, window_from, until):
        """(start, end) whole-second pairs covering the window, oldest first.

        No chunk is longer than TEMPO_CHUNK_SECONDS, so every request is
        inside Tempo's 168h limit. Boundaries are half-open on the wire (a
        chunk's end is the next chunk's start); Tempo treats both ends as
        inclusive, so a capture exactly on a boundary can appear in two
        chunks, and the merge deduplicates it.
        """
        start = int(window_from.timestamp())
        end = int(until.timestamp())
        while start < end:
            chunk_end = min(start + cls.TEMPO_CHUNK_SECONDS, end)
            yield start, chunk_end
            start = chunk_end

    def _tempo_capture_ids(self, start, end):
        """([capture id, ...], complete) for one chunk of the window.

        Raises on any transport or decoding failure so the caller can answer
        `unknown` instead of guessing. `complete` is False when Tempo's own
        metrics say the query did not finish every job: a partial answer must
        not become a `not collected` verdict.
        """
        tag = urllib.parse.quote(CAPTURE_TAG, safe=".")
        query = urllib.parse.urlencode({"start": start, "end": end})
        url = f"{self.tempo_url}/api/v2/search/tag/{tag}/values?{query}"
        try:
            with urllib.request.urlopen(url, timeout=self.TEMPO_TIMEOUT_SECONDS) as response:
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A tag-values response that is too large comes back as an error,
            # not a truncated list. Carry the server's words into the note so
            # the reason is visible on the page.
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
            raise ValueError(f"HTTP {exc.code}: {detail or exc.reason}") from None
        values = document.get("tagValues") if isinstance(document, dict) else None
        if not isinstance(values, list):
            raise ValueError("the tag-values response has no tagValues list")
        capture_ids = []
        for entry in values:
            value = entry.get("value") if isinstance(entry, dict) else None
            if isinstance(value, str) and value and value not in capture_ids:
                capture_ids.append(value)
        return capture_ids, self._tag_values_complete(document)

    @staticmethod
    def _tag_values_complete(document):
        """False when the response says it did not finish the query.

        Tempo reports search metrics on the answer. A response that finished
        fewer jobs than it started covered only part of the window, whatever
        the reason (a per-tenant cap, a timeout, a byte limit), so its capture
        list cannot prove a capture is missing.
        """
        metrics = document.get("metrics") if isinstance(document, dict) else None
        if not isinstance(metrics, dict):
            return True
        try:
            total = int(metrics.get("totalJobs") or 0)
            completed = int(metrics.get("completedJobs") or 0)
        except (TypeError, ValueError):
            return True
        return completed >= total

    # -- the verdict -------------------------------------------------------
    def _state(self, run, capture_ids, verdict, judgeable_from, judgeable_until):
        """("capture" | "not collected" | "unknown", reason).

        `not collected` needs all of these: every chunk of the window answered
        completely, the run is inside the capture retention, the run is old
        enough that its capture would have arrived, and the lookup that
        produced the map was fresh enough to cover the run.
        """
        if capture_ids:
            return "capture", ""
        created = self._parse_time(run.get("created_at"))
        if created is None:
            return "unknown", "the run has no created_at, so its capture window cannot be judged"
        if created < judgeable_from:
            return "unknown", (
                "the run predates the capture retention (or the live Tempo cutover), "
                "so a missing capture proves nothing"
            )
        if created > judgeable_until:
            return "unknown", "the run is newer than the capture lookup; too early to tell"
        if not verdict["complete"]:
            return "unknown", verdict["note"]
        return "not collected", ""

    @staticmethod
    def _iso(seconds):
        """The chunk boundary as the ISO string the note shows."""
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _int_param(params, name, default, low, high):
        try:
            value = int(params.get(name, [""])[0] or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

"""The runs half of ci-observe: /api/ci-observe/runs, the E2E run list with a
per-run capture verdict.

The workflow exporter knows every E2E run (SQLite job history, 60 days) but
stores no capture id. Tempo knows every capture that arrived (30 days) but
not the runs that never sent one. Neither store can answer "which runs have
no capture" alone, so this endpoint joins them on the GitHub run id: the
capture id embeds it, and Tempo keeps `osac.ci.run_id` on the same spans.

Nothing is faked. A run is `not collected` only when Tempo answered, its
answer was complete, and the run is old enough that its capture would have
arrived. Every other case is `unknown` with a reason: Tempo down, Tempo
truncated by its own trace limit, a response this code could not read, a run
newer than the cached lookup, or a run older than the capture retention.

The capture map is cached for 60 seconds. The exporter polls GitHub every 90
seconds and this endpoint is called on every dashboard refresh, so the cache
keeps one Tempo query per minute instead of one per reader. The lookup is
bounded by a trace limit and a request timeout, and is read-only: it never
writes the job history, the rollup or Tempo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

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
TEMPO_LIVE_SINCE = datetime(2026, 11, 1, tzinfo=timezone.utc)

CAPTURE_ID_ATTRIBUTE = "osac.ci.capture_id"
RUN_ID_ATTRIBUTE = "osac.ci.run_id"

# A capture id is self-describing: "<suite>-<prN|main>-<run id>-a<n>" (see
# ci_observe_run_evidence.CAPTURE_ID_RE). Matching the pattern is how the run
# id is recovered when Tempo's response does not carry the attribute under the
# name this code looks for first.
CAPTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-(?:pr\d+|main)-(\d+)-a(\d+)$")


def _attribute_name(key):
    """The ci-observe attribute a Tempo key names, or "".

    Tempo returns a matched span's attributes either bare
    ("osac.ci.capture_id") or scoped ("resource.osac.ci.capture_id"), and the
    scope prefix has changed between versions, so both are accepted.
    """
    for suffix in (CAPTURE_ID_ATTRIBUTE, RUN_ID_ATTRIBUTE):
        if key == suffix or key.endswith("." + suffix):
            return suffix
    return ""


class CIObserveRuns:
    """The /api/ci-observe/runs endpoint, held by WorkflowExporter as
    self._runs. The exporter argument provides the SQLite job history
    (get_jobs_json) and is the only source of the run list; Tempo is reached
    over HTTP with the standard library, read-only."""

    # One capture produces many spans; Tempo's search limit counts traces, so
    # 1000 is 1000 captures. A 7-day window holds a few dozen E2E runs; the
    # headroom covers a busy merge queue. Reaching the limit is reported and
    # turns an absent capture into `unknown`, not `not collected`.
    TEMPO_MAX_TRACES = 1000
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

    # The query is deliberately broad. Selecting only the session span
    # (name = "test run") would hide exactly the partial captures this page
    # exists to show, and those are `capture`, not `not collected`. Any span
    # carrying a capture id proves the capture arrived.
    CAPTURE_QUERY = '{ resource."osac.ci.capture_id" =~ ".+" }'

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
        # {"days": int, "expires_at": monotonic, "captures": {...}, "verdict": {...}}
        # Replaced whole, never mutated, so a reader never sees a half-built map.
        self._cache = None
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
        now = datetime.now(timezone.utc)
        window_from = now - timedelta(days=days)
        doc = {
            "days": days,
            "window_from": window_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tempo": {
                "url": self.tempo_url,
                "query": self.CAPTURE_QUERY,
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

        The verdict says whether the map can support a `not collected` answer
        at all. Cached for CAPTURE_CACHE_TTL_SECONDS and keyed by the window
        length, not by the exact seconds, so repeated dashboard loads hit it.
        A run newer than the cache's fetch time is `unknown`, never `not
        collected` (see FRESH_SECONDS).
        """
        with self._lock:
            cached = self._cache
            if cached and cached["days"] == days and time.monotonic() < cached["expires_at"]:
                logger.debug("capture map cache hit for days=%s", days)
                return cached["captures"], cached["verdict"] | {"cached": True}

        verdict = {
            "state": "ok",
            "note": "",
            "truncated": False,
            "complete": True,
            "fetched_at": until,
            "cached": False,
        }
        captures = {}
        try:
            traces = self._tempo_search(int(window_from.timestamp()), int(until.timestamp()))
        except (OSError, ValueError) as exc:
            logger.debug("Tempo search failed: %s", exc)
            verdict.update(
                state="unknown",
                complete=False,
                note=(
                    f"Tempo at {self.tempo_url} did not answer ({exc}); every run is "
                    "reported as unknown, never as not collected"
                ),
            )
            traces = []
        else:
            if len(traces) >= self.TEMPO_MAX_TRACES:
                verdict.update(
                    state="truncated",
                    complete=False,
                    truncated=True,
                    note=(
                        f"the capture query reached its {self.TEMPO_MAX_TRACES}-trace limit; "
                        "a missing capture is reported as unknown, not as not collected"
                    ),
                )
            readable = 0
            for trace in traces:
                run_id, capture_ids = self._read_trace(trace)
                if capture_ids:
                    readable += 1
                if not run_id:
                    continue
                bucket = captures.setdefault(run_id, [])
                for capture_id in capture_ids:
                    if capture_id not in bucket:
                        bucket.append(capture_id)
            if traces and not readable:
                verdict.update(
                    state="unreadable",
                    complete=False,
                    note=(
                        "Tempo returned traces but no capture id could be read from them; "
                        "the response shape may have changed, so a missing capture is unknown"
                    ),
                )

        with self._lock:
            self._cache = {
                "days": days,
                "expires_at": time.monotonic() + self.CAPTURE_CACHE_TTL_SECONDS,
                "captures": captures,
                "verdict": verdict,
            }
        return captures, verdict

    def _tempo_search(self, start, end):
        """GET /api/search on the live Tempo.

        Raises on any transport or decoding failure so the caller can answer
        `unknown` instead of guessing.
        """
        query = urllib.parse.urlencode({
            "q": self.CAPTURE_QUERY,
            "start": start,
            "end": end,
            "limit": self.TEMPO_MAX_TRACES,
        })
        url = f"{self.tempo_url}/api/search?{query}"
        with urllib.request.urlopen(url, timeout=self.TEMPO_TIMEOUT_SECONDS) as response:
            document = json.loads(response.read().decode("utf-8"))
        traces = document.get("traces") if isinstance(document, dict) else None
        if not isinstance(traces, list):
            raise ValueError("the search response has no traces list")
        return traces

    def _read_trace(self, trace):
        """(run id, capture ids) for one search hit.

        Each matched span's attributes are read first, under either name
        spelling. If no capture id is among them, the trace's own strings are
        matched against the capture-id pattern: the id is self-describing and
        unique, and it carries the run id.
        """
        run_id = ""
        capture_ids = []
        for span_set in trace.get("spanSets") or []:
            for span in span_set.get("spans") or []:
                for key, value in self._attributes(span):
                    if key == RUN_ID_ATTRIBUTE and not run_id:
                        run_id = value
                    elif key == CAPTURE_ID_ATTRIBUTE and value not in capture_ids:
                        capture_ids.append(value)
        if not capture_ids:
            for value in self._strings(trace):
                if CAPTURE_ID_RE.match(value) and value not in capture_ids:
                    capture_ids.append(value)
        if not run_id and capture_ids:
            match = CAPTURE_ID_RE.match(capture_ids[0])
            if match:
                run_id = match.group(1)
        return run_id, capture_ids

    @staticmethod
    def _attributes(span):
        for attribute in span.get("attributes") or []:
            value = attribute.get("value") or {}
            text = value.get("stringValue", value.get("string_value", ""))
            name = _attribute_name(attribute.get("key", ""))
            if name and text:
                yield name, text

    def _strings(self, value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from self._strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from self._strings(item)

    # -- the verdict -------------------------------------------------------
    def _state(self, run, capture_ids, verdict, judgeable_from, judgeable_until):
        """("capture" | "not collected" | "unknown", reason).

        `not collected` needs all of these: Tempo answered, the answer was not
        truncated or unreadable, the run is inside the capture retention, the
        run is old enough that its capture would have arrived, and the lookup
        that produced the map was fresh enough to cover the run.
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
    def _parse_time(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _int_param(params, name, default, low, high):
        try:
            value = int((params.get(name, [""])[0] or default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

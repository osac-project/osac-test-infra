"""The runs half of ci-observe: /api/ci-observe/runs, the E2E run list with a
per-run capture verdict.

The workflow exporter knows every E2E run (SQLite job history, 30 days) and,
since the capture registry landed, every capture that arrived (SQLite
`captures`). Both answers are one SQL statement joined on the GitHub run id
that the capture id embeds, so this endpoint never calls Tempo: its
tag-values scan took 21-29s for a 7-day window against 17ms for Prometheus
label values, and the three stores disagreed by a handful of captures.

`not collected` is the only verdict that blames CI, so it needs every one of
these: the run finished, at least 15 minutes have passed, the registry has a
complete and fresh answer, that answer is newer than the run, and the run is
inside the registry's coverage. Anything else is `unknown` with a reason.
There is no exception path that produces `not collected`.

The registry itself is polled by the exporter every 60 seconds (see
CaptureRegistry in workflow-exporter.py); this module only reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# A run that ended seconds ago may still have its capture in flight: the
# action posts telemetry after the deploy and again in verify, and the OTLP
# batch can lag the run's completion. Until this has passed, a missing
# capture proves nothing.
JUST_ENDED_SECONDS = 15 * 60

# Values of `status` that mean the run is over. `conclusion` is set on every
# finished run, so it is the primary signal; these cover rows stored before
# a conclusion was recorded.
FINISHED_STATUSES = {
    "completed", "success", "failure", "cancelled", "skipped", "timed_out",
    "action_required", "neutral", "stale",
}

# The registry's coverage is aggregate across its sources; when it has no
# answer this is the reason shown per run.
NO_ANSWER = "the capture registry has no complete answer yet"


class CIObserveRuns:
    """The /api/ci-observe/runs endpoint, held by WorkflowExporter as
    self._runs. The exporter provides the SQLite run page (joined to the
    capture registry) and the registry itself; this class applies the
    verdict."""

    DEFAULT_DAYS = 30
    MAX_DAYS = 30
    DEFAULT_LIMIT = 200
    MAX_LIMIT = 2000

    def __init__(self, exporter):
        self.exporter = exporter

    @property
    def registry(self):
        """The capture registry, read fresh on each request so a test or a
        restart cannot leave a stale object behind."""
        return getattr(self.exporter, "_captures", None)

    # -- public ------------------------------------------------------------
    def get_json(self, params):
        """The /api/ci-observe/runs document.

        Query params: days -- how many days of runs to list (default 30,
        clamped to 1..30); limit -- how many runs to list (default 200,
        clamped to 1..2000); before -- the id of the last run on the previous
        page (keyset paging); run -- an exact run id, which ignores the
        window.

        Returns days, window_from, window_to, source, runs, truncated, shown,
        total, counts, next_before and notes. shown is the number of rows on
        this page; total is how many runs match the window. Every run row
        carries capture_state ("capture", "not collected" or "unknown"),
        capture_ids, capture_id (the first one, for a Capture link),
        capture_reason (empty unless unknown), identity_source, started_at
        and finished_at.
        """
        now = datetime.now(UTC)
        days = self._int_param(params, "days", self.DEFAULT_DAYS, 1, self.MAX_DAYS)
        limit = self._int_param(params, "limit", self.DEFAULT_LIMIT, 1, self.MAX_LIMIT)
        before = (params.get("before", [""])[0] or "").strip()
        run_id = (params.get("run", [""])[0] or "").strip()
        notes = []
        if before and not before.isdigit():
            notes.append(
                f"before={before!r} is not a run id; the page starts from the "
                "newest run"
            )
            before = ""

        # An explicit but empty run= is the Find box with nothing typed. It
        # must answer "no rows", not fall back to the whole window.
        blank_run_lookup = "run" in params and not run_id
        if blank_run_lookup:
            page = {
                "rows": [],
                "matched": 0,
                "truncated": False,
                "window_from": self._iso(now - timedelta(days=days)),
                "window_to": self._iso(now),
            }
        else:
            page = self.exporter.get_ci_observe_run_page(
                days=days, limit=limit, before=before, run_id=run_id
            )
        source = self._source(now)
        # The cards count the whole window, not the returned page. A run
        # lookup counts only the runs it returns.
        counts = self._verdict_counts(now, days if not run_id else None, source, page["rows"])
        counts["returned"] = len(page["rows"])
        counts["matched"] = page["matched"]
        doc = {
            "days": days,
            "window_from": page["window_from"],
            "window_to": page["window_to"],
            "source": source,
            "runs": [],
            "truncated": page["truncated"],
            "shown": len(page["rows"]),
            "total": page["matched"],
            "counts": counts,
            "next_before": None,
            "notes": notes,
        }
        if page["truncated"] and page["rows"]:
            doc["next_before"] = page["rows"][-1]["id"]
            doc["notes"].append(
                f"this page is capped at {limit} runs; {page['matched']} match the "
                "window. Pass before=<next_before> for the next page."
            )
        if blank_run_lookup:
            doc["notes"].append(
                "the Find box is empty; enter a run id, or clear it to see the window"
            )
        elif not page["rows"] and run_id:
            doc["notes"].append(f"the job history has no run with id {run_id}")
        elif not page["rows"]:
            doc["notes"].append(
                f"the job history has no E2E runs on or after {doc['window_from']}"
            )
        if source["state"] != "ok":
            doc["notes"].append(self._source_note(source))

        for run in page["rows"]:
            ended = self._ended(run)
            finished = self._finished(run) if ended else None
            state, reason = self._state(run, ended, finished, source, now)
            capture_ids = run["capture_ids"]
            doc["runs"].append(
                run | {
                    # The Capture dashboard links with an explicit window, so
                    # both ends are named: the run's creation is the earliest
                    # bound that contains the capture, its last update the
                    # finish. started_at is created_at under the name the
                    # dashboard reads.
                    "started_at": run.get("created_at", ""),
                    # Grafana data links take epoch milliseconds: a 20-char
                    # "...Z" value is not a format its URL parser accepts, so
                    # the range would be ignored. An unfinished run still
                    # needs a `to`, and now is the honest end.
                    "started_at_ms": self._epoch_ms(run.get("created_at")),
                    "done_at_ms": self._epoch_ms(finished) or int(now.timestamp() * 1000),
                    "finished": ended,
                    "finished_at": self._iso(finished),
                    "capture_state": state,
                    "capture_id": capture_ids[0] if capture_ids else "",
                    "capture_reason": reason,
                }
            )
        return doc

    def _verdict_counts(self, now, days, source, rows=None):
        """{"capture": n, "not collected": n, "unknown": n} over the window.

        `days` is None when the caller already has the rows (a run lookup).
        The rules are `_state` and nothing else, so the cards and the table
        can never disagree about a run.
        """
        if days is not None:
            rows = self.exporter.get_ci_observe_run_window(days=days)
        totals = {"capture": 0, "not collected": 0, "unknown": 0}
        for run in rows or []:
            ended = self._ended(run)
            finished = self._finished(run) if ended else None
            state, _ = self._state(run, ended, finished, source, now)
            totals[state] = totals.get(state, 0) + 1
        return totals

    def capture_ids_by_run(self, days=DEFAULT_DAYS):
        """{run id: [capture id, ...]} from the registry, for the Pull
        requests read side.

        Read-only SQLite. A missing entry means the registry has no capture
        for that run in the window, or that the registry could not answer; it
        is not a claim that the run had none.
        """
        registry = self.registry
        if registry is None:
            return {}
        return registry.capture_ids_by_run(days=days)

    def verdict(self, run, now=None):
        """("capture" | "not collected" | "unknown", reason) for one run.

        The Pull requests page uses the same rules as the run list: a missing
        capture is only 'not collected' when the registry has a complete,
        fresh answer that covers the run. Everything else is unknown with a
        reason, so the page never blames CI for an unproven absence.
        """
        now = now or datetime.now(UTC)
        ended = self._ended(run)
        finished = self._finished(run) if ended else None
        return self._state(run, ended, finished, self._source(now), now)

    def parse_capture_id(self, capture_id):
        """The capture id's suite, run and attempt, or None for a junk id.

        The Pull requests page needs the suite and attempt to pick its own
        capture; the parser stays on the capture registry.
        """
        registry = self.registry
        if registry is None:
            return None
        return registry.parse_capture_id(capture_id)

    # -- the verdict -------------------------------------------------------
    def _state(self, run, ended, finished, source, now):
        """("capture" | "not collected" | "unknown", reason).

        The checks run in this order, so a data gap is never mistaken for a
        CI failure:
        capture -> the run has not finished -> the run just ended -> the
        registry has no answer -> that answer predates the run -> the answer
        is stale -> the run predates coverage -> not collected.
        """
        if run["capture_ids"]:
            return "capture", ""
        if not ended:
            return "unknown", "the run has not finished"
        if finished is None:
            return "unknown", "the run has no finish time, so its capture window cannot be judged"
        if now - finished < timedelta(seconds=JUST_ENDED_SECONDS):
            return "unknown", "the run just ended; telemetry may still be arriving"
        last_ok = self._parse_time(source["last_ok_at"])
        if last_ok is None:
            return "unknown", NO_ANSWER
        if finished > last_ok:
            return "unknown", (
                "no complete discovery since "
                f"{source['last_ok_at']}; the run finished after it"
            )
        coverage_from = self._parse_time(source["coverage_from"])
        if coverage_from is None:
            return "unknown", "the capture registry has no coverage window yet"
        if now - last_ok > timedelta(seconds=self._stale_seconds(source)):
            return "unknown", (
                "the capture registry's last complete answer is "
                f"{source['last_ok_at']}; it is stale"
            )
        created = self._parse_time(run.get("created_at"))
        if created is None:
            return "unknown", "the run has no created_at, so its capture window cannot be judged"
        if created < coverage_from:
            return "unknown", "the run predates capture coverage"
        return "not collected", ""

    def _source(self, now):
        registry = self.registry
        if registry is None:
            return {
                "state": "unknown",
                "last_ok_at": "",
                "coverage_from": "",
                "captures": 0,
                "rejected": {"count": 0, "ids": []},
                "sources": [],
            }
        return registry.source_summary(now)

    def _stale_seconds(self, source):
        registry = self.registry
        return getattr(registry, "STALE_SECONDS", 900)

    @staticmethod
    def _source_note(source):
        if source["state"] == "unknown":
            return (
                "the capture registry has no complete answer from any source; "
                "every run without a capture is reported as unknown, never as "
                "not collected"
            )
        failed = [
            row for row in source.get("sources", []) if row.get("last_error")
        ]
        if failed:
            detail = "; ".join(
                f"{row['source']}: {row['last_error']}" for row in failed
            )
            return (
                f"the capture registry's last answer is partial ({detail}); a "
                "missing capture is reported as unknown, not as not collected"
            )
        return (
            "the capture registry's last answer is partial or stale; a missing "
            "capture is reported as unknown, not as not collected"
        )

    @staticmethod
    def _ended(run):
        """True once GitHub finished the run.

        A stored row has `conclusion` only once GitHub finished the run. A
        row without one may still be queued or in progress, so it is not
        judgeable: reporting `not collected` for a running run would blame CI
        for a capture that cannot exist yet.
        """
        conclusion = (run.get("conclusion") or "").strip()
        status = (run.get("status") or "").strip().lower()
        return bool(conclusion) or status in FINISHED_STATUSES

    @staticmethod
    def _finished(run):
        """The run's finish time, or None when the row has none."""
        return CIObserveRuns._parse_time(run.get("updated_at"))

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
    def _iso(value):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""

    @classmethod
    def _epoch_ms(cls, value):
        """Unix milliseconds for a timestamp, or None. Grafana links use it."""
        parsed = value if isinstance(value, datetime) else cls._parse_time(value)
        return int(parsed.timestamp() * 1000) if parsed else None

    @staticmethod
    def _int_param(params, name, default, low, high):
        try:
            value = int(params.get(name, [""])[0] or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

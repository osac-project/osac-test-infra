#!/usr/bin/env python3
"""Live acceptance harness for the ci-observe read path.

Run it on the central host, after the registry-backed endpoints are deployed:

    python3 monitoring/scripts/ci_observe_read_path_test.py

It measures the read endpoints, checks them against the budgets in
`ci-observe read path: accepted design`, and prints PASS/FAIL per budget. It
is read-only, sends no writes, and is not part of CI: Tempo, Loki and
Prometheus must be reachable, so only the central host can run it.

Two rules keep the measurement honest without disturbing production readers.

* Private `days` keys. The exporter caches per window length and the
  dashboards own 7, 30 and 60, so the harness measures 13 and discovers data
  under 17. A dashboard reader never lands on a harness cache entry, and a
  harness "cold" sample is a real cache miss.
* No restart. The harness cannot flush the exporter's in-process cache, so a
  "cold" sample is the first request for a `days` value in this process. A
  "warm" sample is the same URL again, after the cache is populated.

The completeness check is the exception: it asks `/captures` for the
registry's own 30-day window, because Prometheus, Loki and Tempo still hold
30 days of ids and a shorter window would report false gaps. That answer is
a SQLite read, so unlike the per-window capture cache it cannot warm or
evict an entry the dashboards use.

Percentiles are nearest-rank: the p95 of n samples is the value at position
ceil(0.95 * n) in sorted order. With a handful of samples that is the second
largest, which is what a reader feels.

What it cannot prove from outside: that an endpoint made no Tempo call. The
hermetic tests in monitoring/tests/test_ci_observe_read_path.py pin that.
This harness proves the visible consequences: the time, the states and the
completeness.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# The exporter's own API, the three stores it must agree with, and the
# Grafana proxy timeout that bounds every panel request.
API_BASE_DEFAULT = "http://127.0.0.1:9103"
PROMETHEUS_URL_DEFAULT = "http://127.0.0.1:9092"
LOKI_URL_DEFAULT = "http://127.0.0.1:3100"
TEMPO_URL_DEFAULT = "http://127.0.0.1:3210"
PROXY_TIMEOUT_SECONDS = 30.0

# The capture id is one label in Prometheus and Loki and one resource
# attribute in Tempo. The scoped name is what Tempo's tag-values API takes.
CAPTURE_LABEL = "osac_ci_capture_id"
CAPTURE_TAG = "resource.osac.ci.capture_id"

# Dashboard window lengths. The harness must not use these, or it would
# evict the entry page's cache entry and change the number it is measuring.
DASHBOARD_DAYS = (7, 30, 60)
PERF_DAYS = 13
WARM_DAYS = 17
EXACT_LOOKUP_RUN_ID = "99999999999999"

# A verdict word, not an endpoint state. No read path may produce it while a
# source is incomplete, slow or dead.
NOT_COLLECTED = "not collected"

# The states the Capture view may report. `not collected` is not one of them:
# a capture that is not indexed is a Capture-page state, not a run verdict.
CAPTURE_VIEW_STATES = {"ok", "not_indexed", "missing", "busy", "too_large", "unknown"}

# A run younger than this cannot be judged: its telemetry may still arrive.
VERDICT_GRACE_SECONDS = 15 * 60

# The spec's budget matrix, in seconds. `cold` is the first request for a
# private `days` key; `warm` is any later request for the same URL.
# /captures and /cohorts are not named in the spec's matrix. They are indexes
# over the same SQLite tables as /runs, so they get the /runs budget. /trends
# is not named either; it reads a bounded SQLite table like Compare, so it
# gets Compare's budget.
BUDGETS = {
    "runs": (0.400, 0.200),
    "captures": (0.400, 0.200),
    "cohorts": (0.400, 0.200),
    "capture": (1.500, 0.400),
    "trends": (0.500, 0.500),
    "compare": (0.500, 0.500),
    "prs": (0.500, 0.500),
}

# Page budgets from the spec, as the sum of the endpoint requests that page
# fires. A page is not a single request, so this is a deliberate upper bound:
# if the sum fits, the page fits.
PAGE_BUDGETS = {
    "start_here": ((1.000, 0.400), ("runs", "prs")),
    "capture_page": ((2.000, 0.800), ("capture", "captures")),
}

# A single request may not exceed twice its budget. Four readers may not
# push p95 above twice the single-reader p95.
#
# The ratio needs a floor: two samples of a fast endpoint both land under a
# millisecond, and timer jitter alone can double that. Fifty milliseconds is
# well inside every budget here and well above that noise.
MAX_BUDGET_FACTOR = 2.0
CONCURRENCY_P95_FACTOR = 2.0
CONCURRENCY_MIN_SECONDS = 0.050


@dataclass
class Result:
    """One HTTP GET: what came back and how long it took."""

    url: str
    status: int
    document: dict | None
    elapsed: float
    error: str = ""


@dataclass
class Load:
    """The results of one or more readers hitting one URL."""

    results: list[Result] = field(default_factory=list)

    @property
    def latencies(self) -> list[float]:
        return [result.elapsed for result in self.results]

    @property
    def statuses(self) -> list[int]:
        return [result.status for result in self.results]

    def failures(self) -> list[Result]:
        return [result for result in self.results if result.status != 200]

    def rate_limited(self) -> list[Result]:
        return [result for result in self.results if result.status == 429]


@dataclass
class Check:
    """One PASS, FAIL or SKIP line. `ok` is None for a case the live data
    cannot exercise, such as the dead-source rule when every source is
    healthy."""

    name: str
    ok: bool | None
    detail: str

    def render(self) -> str:
        label = "SKIP" if self.ok is None else ("PASS" if self.ok else "FAIL")
        return f"{label}  {self.name:<26} {self.detail}"


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile. None for no samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[rank]


def fetch(url: str, timeout: float) -> Result:
    """GET one JSON document. A non-200 is a result, not an exception: the
    harness must measure and name a 429 or a 500, not stop at the first one."""
    started = time.monotonic()
    status = 0
    document = None
    error = ""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
            status = response.status
        document = json.loads(body)
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        error = str(exc)
    except ValueError as exc:
        error = f"the response is not JSON: {exc}"
    return Result(url=url, status=status, document=document, elapsed=time.monotonic() - started, error=error)


def load(url: str, readers: int, per_reader: int, timeout: float) -> Load:
    """Hit one URL from `readers` threads, `per_reader` times each."""
    combined = Load()
    lock = threading.Lock()

    def read() -> None:
        local = [fetch(url, timeout) for _ in range(per_reader)]
        with lock:
            combined.results.extend(local)

    threads = [threading.Thread(target=read, daemon=True) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return combined


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_budget(name: str, cold: float | None, warm: list[float]) -> list[Check]:
    """The cold sample, the warm p95, and the no-request-over-2x rule."""
    budget_cold, budget_warm = BUDGETS[name]
    checks = []
    if cold is None:
        checks.append(Check(f"{name}/cold", None, "not measured"))
    else:
        checks.append(
            Check(f"{name}/cold", cold <= budget_cold, f"{cold:.3f}s <= {budget_cold:.3f}s")
        )
    warm_p95 = percentile(warm, 0.95)
    if warm_p95 is None:
        checks.append(Check(f"{name}/warm p95", None, "not measured"))
        return checks
    checks.append(
        Check(f"{name}/warm p95", warm_p95 <= budget_warm, f"{warm_p95:.3f}s <= {budget_warm:.3f}s")
    )
    if cold is not None:
        worst = max([cold, *warm])
        limit = MAX_BUDGET_FACTOR * max(budget_cold, budget_warm)
        checks.append(
            Check(f"{name}/max", worst <= limit, f"{worst:.3f}s <= {limit:.3f}s (2x budget)")
        )
    return checks


def check_page(name: str, endpoint_samples: dict[str, tuple[float | None, list[float]]]) -> Check:
    """A page budget, as the sum of the endpoints that page fires.

    Cold sums the cold samples. Warm sums the warm p95 of each endpoint, an
    upper bound: the page's own p95 cannot exceed the sum of its parts' p95s.
    """
    budget_cold, budget_warm = PAGE_BUDGETS[name][0]
    endpoints = PAGE_BUDGETS[name][1]
    missing = [endpoint for endpoint in endpoints if endpoint not in endpoint_samples]
    if missing:
        return Check(f"page/{name}", None, f"not measured: {', '.join(missing)} missing")
    cold = 0.0
    warm = 0.0
    for endpoint in endpoints:
        endpoint_cold, endpoint_warm = endpoint_samples[endpoint]
        if endpoint_cold is None:
            return Check(f"page/{name}", None, f"not measured: {endpoint} has no cold sample")
        cold += endpoint_cold
        warm += percentile(endpoint_warm, 0.95) or 0.0
    ok = cold <= budget_cold and warm <= budget_warm
    return Check(
        f"page/{name}",
        ok,
        f"cold {cold:.3f}s <= {budget_cold:.3f}s, warm {warm:.3f}s <= {budget_warm:.3f}s",
    )


def check_clean(name: str, samples: Load) -> list[Check]:
    """Zero non-200 and zero 429. A 429 is named separately because the
    Capture page's old TraceQL fan-out produced exactly that."""
    failures = samples.failures()
    statuses = ", ".join(sorted({str(result.status) if result.status else "no response" for result in failures}))
    checks = [
        Check(
            f"{name}/status",
            not failures,
            f"{len(failures)} of {len(samples.results)} not 200"
            + (f" ({statuses})" if failures else ""),
        )
    ]
    limited = samples.rate_limited()
    checks.append(
        Check(f"{name}/429", not limited, f"{len(limited)} rate limited" if limited else "none")
    )
    return checks


def check_concurrency(name: str, single: Load, concurrent: Load) -> list[Check]:
    """Four readers: p95 no worse than twice one reader.

    Both loads carry successful requests only. Statuses and 429s are checked
    by the caller against the unfiltered load: passing only the 200s here
    would make those checks unable to fail.
    """
    checks: list[Check] = []
    single_p95 = percentile(single.latencies, 0.95)
    concurrent_p95 = percentile(concurrent.latencies, 0.95)
    if single_p95 is None or concurrent_p95 is None:
        checks.append(Check(f"{name}/4-readers p95", None, "not measured"))
        return checks
    limit = max(CONCURRENCY_P95_FACTOR * single_p95, CONCURRENCY_MIN_SECONDS)
    checks.append(
        Check(
            f"{name}/4-readers p95",
            concurrent_p95 <= limit,
            f"{concurrent_p95:.3f}s <= {limit:.3f}s (2x single {single_p95:.3f}s, 50ms floor)",
        )
    )
    return checks


def check_shape(name: str, document: dict | None) -> list[Check]:
    """The fields the dashboards read. A missing field is a shape mismatch,
    named here rather than left for a panel to render empty."""
    if not isinstance(document, dict):
        return [Check(f"{name}/shape", False, "no JSON document")]
    checks = []
    if name == "runs":
        required = ["days", "runs", "source", "truncated"]
        checks.append(_required(name, document, required))
        runs = document.get("runs")
        if isinstance(runs, list) and runs:
            row_fields = ["capture_state", "capture_ids", "capture_reason", "identity_source"]
            missing = [field_name for field_name in row_fields if field_name not in runs[0]]
            checks.append(
                Check(
                    "runs/row shape",
                    not missing,
                    "every row has the verdict fields" if not missing else f"missing {', '.join(missing)}",
                )
            )
        sources = normalize_sources(document)
        def _count(value):
            if isinstance(value, int):
                return value
            if isinstance(value, dict) and isinstance(value.get("count"), int):
                return value["count"]
            return None

        incomplete = [
            name
            for name, row in sources.items()
            if _count(row.get("captures")) is None or _count(row.get("rejected")) is None
        ]
        checks.append(
            Check(
                "runs/source shape",
                bool(sources) and not incomplete,
                "every source row has captures and rejected counts"
                if not incomplete
                else f"no counts for {', '.join(incomplete) or 'no source row'}",
            )
        )
    elif name == "captures":
        rows = document.get("captures", document.get("rows"))
        checks.append(
            Check(
                "captures/shape",
                isinstance(rows, list),
                "captures list present" if isinstance(rows, list) else "no captures list",
            )
        )
    elif name == "cohorts":
        checks.append(_required("cohorts", document, ["cohorts", "ungrouped"]))
        cohorts = document.get("cohorts")
        if isinstance(cohorts, list):
            unknown = [row for row in cohorts if contains_value(row, "unknown")]
            checks.append(
                Check(
                    "cohorts/no-unknown",
                    not unknown,
                    "no cohort value is unknown" if not unknown else f"{len(unknown)} cohort rows mention unknown",
                )
            )
    elif name == "capture":
        checks.append(
            _required(
                "capture",
                document,
                ["capture_id", "state", "session", "coverage", "identity", "tests", "waits",
                 "fixtures", "fixture_uses", "failed_tests", "notes"],
            )
        )
        state = document.get("state")
        checks.append(
            Check(
                "capture/state",
                state in CAPTURE_VIEW_STATES,
                f"state {state!r} is {'known' if state in CAPTURE_VIEW_STATES else 'not one of the six'}",
            )
        )
    return checks


def contains_value(value: object, needle: object) -> bool:
    """True when a scalar in the document equals `needle`.

    Checking values, not the serialized row: a field called `unknown_runs`
    is not an unknown cohort value.
    """
    if isinstance(value, dict):
        return any(contains_value(item, needle) for item in value.values())
    if isinstance(value, list):
        return any(contains_value(item, needle) for item in value)
    return value == needle


def _required(name: str, document: dict, fields: list[str]) -> Check:
    missing = [field_name for field_name in fields if field_name not in document]
    return Check(
        f"{name}/keys",
        not missing,
        "all required keys present" if not missing else f"missing {', '.join(missing)}",
    )


def total_and_shown(document: dict, shown: int) -> tuple[int | None, str | None]:
    """(total, key) from the document's own integer counts.

    The spec says `truncated` plus "both counts" without naming them. Any
    top-level integer at least as large as the row count can be the total;
    a key that says total or count is preferred, and the window metadata
    (`days`, `limit`) is never a count.
    """
    candidates = []
    for key, value in document.items():
        if key in ("days", "limit") or not isinstance(value, int) or isinstance(value, bool):
            continue
        if value < shown:
            continue
        if "total" in key:
            rank = 0
        elif any(word in key for word in ("count", "shown", "returned", "listed")):
            rank = 1
        else:
            rank = 2
        candidates.append((rank, key, value))
    if not candidates:
        return None, None
    candidates.sort()
    return candidates[0][2], candidates[0][1]


def check_index(name: str, document: dict | None, limit: int | None) -> list[Check]:
    """`truncated` must agree with the counts, not be a decorative flag."""
    if not isinstance(document, dict):
        return [Check(f"{name}/truncated", False, "no JSON document")]
    rows = document.get("runs")
    if not isinstance(rows, list):
        return [Check(f"{name}/truncated", False, "no runs list")]
    shown = len(rows)
    checks = []
    if limit is not None:
        checks.append(
            Check(
                f"{name}/limit",
                shown <= limit,
                f"{shown} rows <= limit {limit}" if shown <= limit else f"{shown} rows exceed limit {limit}",
            )
        )
    truncated = document.get("truncated")
    total, key = total_and_shown(document, shown)
    if total is None:
        return [
            *checks,
            Check(
                f"{name}/truncated",
                False,
                "no integer total count next to the rows; keys: "
                + ", ".join(sorted(document)),
            ),
        ]
    checks.append(
        Check(
            f"{name}/truncated",
            truncated is (total > shown),
            f"truncated={truncated!r}, shown {shown} of {key}={total}",
        )
    )
    return checks


def check_coverage_window(document: dict, limit: int) -> list[Check]:
    """The completeness answer must cover the whole window. A capped or
    truncated registry answer cannot prove a superset, so it fails here."""
    rows = document.get("captures", document.get("rows"))
    if not isinstance(rows, list):
        return [Check("completeness/answer", False, "no captures list in the registry answer")]
    checks = [
        Check(
            "completeness/answer",
            len(rows) < limit,
            f"{len(rows)} rows, limit {limit}"
            + ("" if len(rows) < limit else "; the answer is capped, so a missing id proves nothing"),
        )
    ]
    if "truncated" in document:
        checks.append(
            Check(
                "completeness/not-truncated",
                document["truncated"] is False,
                f"truncated={document['truncated']!r}",
            )
        )
    return checks


def normalize_sources(document: dict) -> dict[str, dict]:
    """{source name: source row} from the `source` block.

    The spec names the row fields but not the container, so both shapes are
    accepted: a mapping of name to row, or a list of rows that carry their
    own name.
    """
    block = document.get("source")
    sources: dict[str, dict] = {}
    if isinstance(block, dict):
        if any(field_name in block for field_name in ("state", "last_ok_at", "coverage_from")):
            # A single aggregate row, not a row per source.
            return {"registry": block}
        for key, value in block.items():
            if isinstance(value, dict) and any(
                field_name in value for field_name in ("state", "last_ok_at", "coverage_from")
            ):
                sources[key] = value
    elif isinstance(block, list):
        for row in block:
            if not isinstance(row, dict):
                continue
            name = row.get("source") or row.get("name")
            if name:
                sources[str(name)] = row
    return sources


def parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def check_verdicts(document: dict, now: datetime) -> list[Check]:
    """The verdict rules, checked against the live answer.

    Four rules are checked from outside:

    * A run younger than 15 minutes is never `not collected`.
    * When any source is unhealthy (no complete answer, an error, or a
      run that finished after the source's last complete answer), no run is
      `not collected`.
    * A run created before a source's coverage start is never
      `not collected`.
    * A capture that never existed is never `not collected`; that one is
      checked on the capture view, where the word is not a valid state.
    """
    if not isinstance(document, dict):
        return [Check("verdicts", False, "no JSON document")]
    runs = [run for run in document.get("runs") or [] if isinstance(run, dict)]
    sources = normalize_sources(document)
    checks: list[Check] = []

    young = []
    for run in runs:
        created = parse_time(run.get("created_at"))
        if created is not None and now - created < timedelta(seconds=VERDICT_GRACE_SECONDS):
            young.append(run)
    bad = [run for run in young if run.get("capture_state") == NOT_COLLECTED]
    checks.append(
        Check(
            "verdicts/grace",
            not bad,
            f"{len(young)} runs under 15m, {len(bad)} marked {NOT_COLLECTED!r}",
        )
    )

    unhealthy = [
        name
        for name, row in sources.items()
        if row.get("state") not in ("ok", None)
        or row.get("last_ok_at") in (None, "")
        or row.get("last_error")
    ]
    if not sources:
        checks.append(Check("verdicts/dead-source", False, "the response has no source block"))
    elif unhealthy:
        affected = [run for run in runs if run.get("capture_state") == NOT_COLLECTED]
        checks.append(
            Check(
                "verdicts/dead-source",
                not affected,
                f"{', '.join(unhealthy)} unhealthy, {len(affected)} runs marked {NOT_COLLECTED!r}",
            )
        )
    else:
        checks.append(
            Check(
                "verdicts/dead-source",
                None,
                "every source is healthy; the dead-source rule was not exercised live",
            )
        )

    stale = []
    for run in runs:
        if run.get("capture_state") != NOT_COLLECTED:
            continue
        finished = parse_time(run.get("updated_at")) or parse_time(run.get("created_at"))
        if finished is None:
            continue
        for name, row in sources.items():
            last_ok = parse_time(row.get("last_ok_at"))
            if last_ok is None or finished > last_ok:
                stale.append((name, run.get("id")))
                break
    if not sources:
        checks.append(Check("verdicts/freshness", None, "not measured: no source block"))
    else:
        checks.append(
            Check(
                "verdicts/freshness",
                not stale,
                f"{len(stale)} runs finished after their source's last complete answer"
                if stale
                else "every verdict is backed by a discovery after the run ended",
            )
        )

    coverage_starts = [
        parse_time(row.get("coverage_from")) for row in sources.values() if row.get("coverage_from")
    ]
    coverage_starts = [start for start in coverage_starts if start is not None]
    if coverage_starts:
        coverage_from = max(coverage_starts)
        outside = [
            run
            for run in runs
            if (created := parse_time(run.get("created_at"))) is not None
            and created < coverage_from
            and run.get("capture_state") == NOT_COLLECTED
        ]
        checks.append(
            Check(
                "verdicts/coverage",
                not outside,
                f"{len(outside)} runs before coverage {coverage_from:%Y-%m-%d} marked {NOT_COLLECTED!r}",
            )
        )
    return checks


def capture_ids_from_registry(document: dict | None) -> set[str]:
    """The capture ids in a /captures answer, by the keys the row may use."""
    if not isinstance(document, dict):
        return set()
    rows = document.get("captures", document.get("rows"))
    if not isinstance(rows, list):
        return set()
    ids = set()
    for row in rows:
        if isinstance(row, dict):
            for key in ("capture_id", "id"):
                if isinstance(row.get(key), str) and row[key]:
                    ids.add(row[key])
                    break
    return ids


def check_superset(registry: set[str], store: set[str], name: str) -> Check:
    """The registry must list every capture a store still holds."""
    missing = sorted(store - registry)
    detail = f"{len(registry)} registry, {len(store)} {name}, {len(missing)} missing"
    if missing:
        detail += f": {', '.join(missing[:5])}"
    return Check(f"completeness/{name}", not missing, detail)


# ---------------------------------------------------------------------------
# Store label values
# ---------------------------------------------------------------------------
def prometheus_capture_ids(base_url: str, days: int, timeout: float) -> set[str]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    query = urllib.parse.urlencode({"start": start.timestamp(), "end": end.timestamp()})
    url = f"{base_url.rstrip('/')}/api/v1/label/{CAPTURE_LABEL}/values?{query}"
    document = _store_json(url, timeout)
    return {value for value in document.get("data") or [] if isinstance(value, str) and value}


def loki_capture_ids(base_url: str, days: int, timeout: float) -> set[str]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    query = urllib.parse.urlencode({"start": int(start.timestamp() * 1e9), "end": int(end.timestamp() * 1e9)})
    url = f"{base_url.rstrip('/')}/loki/api/v1/label/{CAPTURE_LABEL}/values?{query}"
    document = _store_json(url, timeout)
    return {value for value in document.get("data") or [] if isinstance(value, str) and value}


def tempo_capture_ids(base_url: str, days: int, timeout: float) -> set[str]:
    """Tempo rejects a range over 168h, so the window is read in chunks."""
    base_url = base_url.rstrip("/")
    tag = urllib.parse.quote(CAPTURE_TAG, safe=".")
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    cursor = int(start.timestamp())
    stop = int(end.timestamp())
    ids: set[str] = set()
    while cursor < stop:
        chunk_end = min(cursor + 168 * 3600, stop)
        query = urllib.parse.urlencode({"start": cursor, "end": chunk_end})
        url = f"{base_url}/api/v2/search/tag/{tag}/values?{query}"
        document = _store_json(url, timeout)
        for entry in document.get("tagValues") or []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if isinstance(value, str) and value:
                ids.add(value)
        cursor = chunk_end
    return ids


def _store_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


# ---------------------------------------------------------------------------
# Measurement plan
# ---------------------------------------------------------------------------
def targets(api_base: str, days: int, capture_id: str, base_sha: str, head_sha: str) -> list[tuple[str, str]]:
    """(name, url) in measurement order. A missing capture id or SHA becomes
    an empty URL, which the caller skips with a reason."""
    api_base = api_base.rstrip("/")
    quoted_capture = urllib.parse.quote(capture_id)
    compare = urllib.parse.urlencode({"base": base_sha, "head": head_sha, "days": days})
    plan = [
        ("runs", f"{api_base}/api/ci-observe/runs?days={days}&limit=200"),
        ("captures", f"{api_base}/api/ci-observe/captures?days={days}&limit=200"),
        ("cohorts", f"{api_base}/api/ci-observe/cohorts?days={days}&limit=50"),
        ("capture", f"{api_base}/api/ci-observe/capture?capture_id={quoted_capture}" if capture_id else ""),
        ("trends", f"{api_base}/api/ci-observe/trends?days={days}"),
        ("compare", f"{api_base}/api/ci-observe/trends/compare?{compare}" if base_sha and head_sha else ""),
        ("prs", f"{api_base}/api/ci-observe/prs?days={days}"),
    ]
    return plan


def resolve_capture_id(api_base: str, timeout: float) -> str:
    """The first capture id the runs index reports, for the Capture view.

    Asked under WARM_DAYS, not PERF_DAYS: the discovery request must not
    populate the window the harness is about to measure cold.
    """
    document = fetch(f"{api_base.rstrip('/')}/api/ci-observe/runs?days={WARM_DAYS}&limit=50", timeout).document
    if not isinstance(document, dict):
        return ""
    for run in document.get("runs") or []:
        ids = run.get("capture_ids") or []
        if ids:
            return str(ids[0])
    return ""


def resolve_shas(api_base: str, days: int, timeout: float) -> tuple[str, str]:
    """Two source SHAs from the trends rollup, for the Compare endpoint.

    Reads the per-day rows, not the summary: only a row carries source_sha,
    and the summary is grouped by test.
    """
    document = fetch(f"{api_base.rstrip('/')}/api/ci-observe/trends?days={days}", timeout).document
    if not isinstance(document, dict):
        return "", ""
    counts: dict[str, int] = {}
    for row in document.get("rows") or []:
        sha = row.get("source_sha") if isinstance(row, dict) else None
        if isinstance(sha, str) and sha:
            counts[sha] = counts.get(sha, 0) + 1
    ordered = sorted(counts, key=lambda sha: counts[sha], reverse=True)
    if len(ordered) < 2:
        return "", ""
    return ordered[1], ordered[0]


def measure(args: argparse.Namespace) -> list[Check]:
    checks: list[Check] = []
    api_base = args.api_base
    capture_id = args.capture_id or resolve_capture_id(api_base, args.timeout)
    base_sha, head_sha = args.base, args.head
    if not base_sha or not head_sha:
        base_sha, head_sha = resolve_shas(api_base, WARM_DAYS, args.timeout)

    samples: dict[str, tuple[float | None, list[float]]] = {}
    for name, url in targets(api_base, PERF_DAYS, capture_id, base_sha, head_sha):
        if not url:
            if name == "capture":
                reason = "not measured: the runs index lists no capture id"
            elif name == "compare":
                reason = "not measured: the trends rollup has fewer than two source SHAs"
            else:
                reason = "not measured: no URL"
            checks.append(Check(f"{name}/cold", None, reason))
            samples[name] = (None, [])
            continue
        cold = fetch(url, args.timeout)
        checks.extend(check_clean(f"{name}/cold", Load([cold])))
        checks.extend(check_shape(name, cold.document))
        warm = load(url, 1, args.warm_samples, args.timeout)
        checks.extend(check_clean(f"{name}/warm", warm))
        four = load(url, 4, args.per_request, args.timeout)
        checks.extend(check_clean(f"{name}/4-readers", four))
        # A budget is a claim about a successful answer. A dead endpoint is
        # already a status failure; its absence must not read as a fast pass.
        answered = [result for result in warm.results if result.status == 200]
        four_answered = [result for result in four.results if result.status == 200]
        checks.extend(check_budget(name, cold.elapsed if cold.status == 200 else None, [r.elapsed for r in answered]))
        checks.extend(check_concurrency(name, Load(answered), Load(four_answered)))
        samples[name] = (cold.elapsed if cold.status == 200 else None, [r.elapsed for r in answered])
        if name == "runs":
            checks.extend(check_index(name, cold.document, 200))
            checks.extend(check_verdicts(cold.document, datetime.now(UTC)))

    for page in PAGE_BUDGETS:
        checks.append(check_page(page, {
            endpoint: samples[endpoint] for endpoint in PAGE_BUDGETS[page][1] if endpoint in samples
        }))

    checks.extend(check_index_limits(api_base, args.timeout))
    checks.extend(check_exact_lookup(api_base, args.timeout))
    checks.extend(check_synthetic_capture(api_base, args.timeout))
    checks.extend(check_completeness(api_base, args))
    return checks


def check_index_limits(api_base: str, timeout: float) -> list[Check]:
    """A small limit must truncate, and a large one must not hide rows."""
    small = fetch(f"{api_base.rstrip('/')}/api/ci-observe/runs?days={PERF_DAYS}&limit=5", timeout)
    large = fetch(f"{api_base.rstrip('/')}/api/ci-observe/runs?days={PERF_DAYS}&limit=2000", timeout)
    checks = check_index("runs/limit-5", small.document, 5)
    checks.extend(check_index("runs/limit-2000", large.document, 2000))
    return checks


def _exact_lookup_detail(status: int, runs: list) -> str:
    if status != 200:
        return "not measured: no answer"
    if runs:
        return f"{len(runs)} rows returned for an unknown id"
    return "an unknown run id returns no rows"


def check_exact_lookup(api_base: str, timeout: float) -> list[Check]:
    """`run=` is the exact lookup: an unknown id is an empty answer, never an
    error and never a fabricated row."""
    result = fetch(
        f"{api_base.rstrip('/')}/api/ci-observe/runs?days={PERF_DAYS}&run={EXACT_LOOKUP_RUN_ID}", timeout
    )
    document = result.document if isinstance(result.document, dict) else {}
    runs = document.get("runs") or []
    return [
        Check("runs/exact-status", result.status == 200, f"HTTP {result.status}"),
        Check(
            "runs/exact-empty",
            None if result.status != 200 else runs == [],
            _exact_lookup_detail(result.status, runs),
        ),
    ]


def check_synthetic_capture(api_base: str, timeout: float) -> list[Check]:
    """A capture that never existed, and a source that cannot answer, must
    both come back fast with a state and a reason: an empty answer is not
    `not collected`, and a slow one is not a timeout."""
    prefix = "vmaas-main-99999999999-a1"
    result = fetch(f"{api_base.rstrip('/')}/api/ci-observe/capture?capture_id={prefix}", timeout)
    document = result.document if isinstance(result.document, dict) else {}
    state = document.get("state")
    budget_cold, _ = BUDGETS["capture"]
    notes = document.get("notes")
    return [
        Check("capture/synthetic-status", result.status == 200, f"HTTP {result.status}"),
        Check(
            "capture/synthetic-state",
            state in CAPTURE_VIEW_STATES and state != NOT_COLLECTED,
            f"state {state!r} is one of the six" if state in CAPTURE_VIEW_STATES
            else f"state {state!r} is not one of the six",
        ),
        Check(
            "capture/synthetic-fast",
            result.elapsed <= budget_cold,
            f"{result.elapsed:.3f}s <= {budget_cold:.3f}s (empty is fast, not a hang)",
        ),
        Check(
            "capture/synthetic-reason",
            bool(notes),
            "the answer carries a note" if notes else "no note explains the empty answer",
        ),
    ]


def check_completeness(api_base: str, args: argparse.Namespace) -> list[Check]:
    """The registry must be a superset of every store's still-held captures."""
    registry_result = fetch(
        f"{api_base.rstrip('/')}/api/ci-observe/captures"
        f"?days={args.coverage_days}&limit={args.coverage_limit}",
        args.timeout,
    )
    if registry_result.status != 200:
        return [Check("completeness/registry", False, f"HTTP {registry_result.status}")]
    document = registry_result.document or {}
    checks = check_coverage_window(document, args.coverage_limit)
    registry = capture_ids_from_registry(document)
    readers = (
        # Prometheus keeps 15 days, so a narrower --coverage-days must narrow
        # it too; otherwise the registry looks like it is missing captures
        # that Prometheus was never asked about.
        ("prometheus", prometheus_capture_ids, args.prometheus_url, min(args.coverage_days, 15)),
        ("loki", loki_capture_ids, args.loki_url, min(args.coverage_days, 30)),
        ("tempo", tempo_capture_ids, args.tempo_url, min(args.coverage_days, 30)),
    )
    for name, reader, base_url, days in readers:
        try:
            store = reader(base_url, days, args.store_timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            checks.append(Check(f"completeness/{name}", False, f"the store did not answer: {exc}"))
            continue
        checks.append(check_superset(registry, store, name))
    return checks


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-base", default=API_BASE_DEFAULT, help="the workflow exporter's API")
    parser.add_argument("--capture-id", default="", help="the capture to open; default: first in /runs")
    parser.add_argument("--base", default="", help="Compare base SHA; default: from the trends rollup")
    parser.add_argument("--head", default="", help="Compare head SHA; default: from the trends rollup")
    parser.add_argument("--warm-samples", type=int, default=3, help="single-reader repeat requests")
    parser.add_argument("--per-request", type=int, default=2, help="requests per concurrent reader")
    parser.add_argument("--timeout", type=float, default=PROXY_TIMEOUT_SECONDS, help="per-request timeout")
    parser.add_argument("--store-timeout", type=float, default=180.0, help="a store label-values timeout")
    parser.add_argument("--coverage-days", type=int, default=30, help="registry window for completeness")
    parser.add_argument("--coverage-limit", type=int, default=20000, help="registry rows for completeness")
    parser.add_argument("--prometheus-url", default=PROMETHEUS_URL_DEFAULT)
    parser.add_argument("--loki-url", default=LOKI_URL_DEFAULT)
    parser.add_argument("--tempo-url", default=TEMPO_URL_DEFAULT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    started = time.monotonic()
    print(f"ci-observe read path acceptance: {args.api_base}")
    print(
        f"private days keys: {PERF_DAYS} measured, {WARM_DAYS} for discovery"
        f" (dashboards use {', '.join(map(str, DASHBOARD_DAYS))})"
    )
    print(f"read-only; {args.timeout:.0f}s request timeout, Grafana proxy is {PROXY_TIMEOUT_SECONDS:.0f}s")
    print()

    checks = measure(args)
    for check in checks:
        print(check.render())

    failed = [check for check in checks if check.ok is False]
    skipped = [check for check in checks if check.ok is None]
    passed = len(checks) - len(failed) - len(skipped)
    print()
    print(f"SUMMARY: {passed} passed, {len(failed)} failed, {len(skipped)} skipped, {time.monotonic() - started:.1f}s")
    if failed:
        print("FAILED: " + ", ".join(check.name for check in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

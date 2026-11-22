"""Run-evidence half of the workflow exporter's /api/ci-observe/run endpoint.

Sibling module of workflow-exporter.py (which imports it; the exporter's
file name has a hyphen, so it cannot import this module's classes from
anywhere else by package path). A ci-observe capture id embeds a GitHub
run id and attempt, and CIObserveRunEvidence ties such a capture to the
GitHub-side evidence the exporter already stores (job history in SQLite)
and, for /api/ci-observe/failed-tests, to the JUnit file inside the logs
artifact of the capture's suite, downloaded from GitHub on demand, bounded
and cached. Same exporter, same token, same data -- no second poller.

The artifact is read from one place, decided by the run's trigger (see
artifact_place), under one name, decided by the capture's suite (see
SUITE_ARTIFACTS). Anything that stops that read is an EvidenceError whose
message says what failed, where, and what to fix: there is no second place
and no second source.

CIObserveRunEvidence holds an exporter reference and calls back into it
for everything shared: _db(), _row_to_job(), _get(), _update_rate_limit(),
headers, and _github_token() (the token is a module-level global in
workflow-exporter.py that its tests patch after constructing the
exporter, so it must be read live rather than copied). The four bounds
the tests read or monkeypatch on WorkflowExporter are likewise read
through the exporter at call time; every other constant lives here.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

import ci_observe_phases

logger = logging.getLogger(__name__)

# GitHub access mirrors workflow-exporter.py's module-level config: the
# same environment variables, read once at import there and here, so the
# two modules always agree (the token is the exception -- see
# WorkflowExporter._github_token, which reads it live).
ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
JOBS_HISTORY_DAYS = int(os.getenv("JOBS_HISTORY_DAYS", "30"))
# Where an E2E run's logs artifact (with the JUnit file) is. Two eras:
# until 2026-11-14, pull-request and merge-queue runs uploaded the artifact
# to their own run (e2e-pr-*.yml, e2e-queue.yml passed no app key on
# purpose) and scheduled and manual runs published it to the private
# repository (the e2e-*-caller.yml workflows pass the OSAC CI app key).
# Since osac commit c39048bb96 (2026-11-14) every trigger publishes to the
# private repository, whose collect runs are named
# "collect <repo>#<run id>.<attempt>" and whose artifact names embed the
# source run id, so a name lookup finds them regardless of trigger. An
# artifact therefore lives in exactly one of two known places and the
# reader checks both: the place the run's trigger says first, then the
# other one.
PRIVATE_ARTIFACT_REPO = os.getenv("PRIVATE_ARTIFACT_REPO", "osac-ci-artifacts")
ON_THE_RUN = frozenset({"pr", "merge_queue"})
IN_THE_PRIVATE_REPO = frozenset({"scheduled", "manual"})
# The artifact each suite's full-install job uploads. VMaaS's name has no
# suite in it. A merge-queue run holds all three, each with its own capture.
SUITE_ARTIFACTS = {
    "vmaas": "osac-logs-full-install-{run_id}",
    "caas": "osac-logs-caas-full-install-{run_id}",
    "bmaas": "osac-logs-bmaas-full-install-{run_id}",
}
TOKEN_FIX = (
    "PRIVATE_GITHUB_TOKEN needs read access to the Actions artifacts of {org}/{repo}: "
    "the repo scope on a classic token, or {repo} with Actions read-only on a fine-grained one"
)


class EvidenceError(Exception):
    """A run's JUnit evidence cannot be read. The message says what failed,
    where, and, when the fix is known, what to change."""


class GitHubRefused(EvidenceError):
    """GitHub did not answer with an artifact listing or an artifact: an
    HTTP error, a network error or a body that is not one. Unlike a run's
    own facts (no artifact where its trigger says), this is the same for
    every run, so a batch reader stops on it."""


def artifact_place(trigger, repo, run_id, org=ORG, private=None):
    """(listing path, label, private) of one place a run's artifact can be:
    its own run for a pull-request or merge-queue run, the private artifact
    repository for a scheduled or manual one. `private` explicitly selects
    the place for the second lookup (see find_artifacts). Raises
    EvidenceError for any other trigger."""
    if private is True or (private is None and trigger in IN_THE_PRIVATE_REPO):
        return f"/repos/{org}/{PRIVATE_ARTIFACT_REPO}/actions/artifacts", f"{org}/{PRIVATE_ARTIFACT_REPO}", True
    if private is False or (private is None and trigger in ON_THE_RUN):
        return (f"/repos/{org}/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
                f"run {run_id} of {org}/{repo}", False)
    raise EvidenceError(
        f"run {run_id} is a {trigger or 'unknown'}-triggered run, and only pull-request and merge-queue runs "
        f"(artifact on the run) and scheduled and manual runs (artifact in {org}/{PRIVATE_ARTIFACT_REPO}) "
        "have a known artifact place"
    )


def artifact_places(trigger, repo, run_id, org=ORG):
    """(label, label) of the two places a run's artifact can be, the
    trigger's expected one first. For the not-found reason: a missing
    artifact was looked for in both."""
    first = artifact_place(trigger, repo, run_id, org)
    second = artifact_place(trigger, repo, run_id, org, private=not first[2])
    return first[1], second[1]


def find_artifacts(get, trigger, repo, run_id, names, org=ORG, api_url=API_URL):
    """The artifacts named in `names`, newest first per name:
    [(name, artifact, label, private)]. An artifact lives in exactly one of
    two places, its own run or the private artifact repository (see the
    comment at ON_THE_RUN), and both are checked: the place the run's
    trigger says first, then the other. A name with no artifact in either
    is left out and the caller decides what that means. Any answer from
    GitHub other than a listing raises EvidenceError: a refusal from the
    private repository names the token's missing access. `get` is a
    function from a URL to a response."""
    found = {}
    label, private = "", False
    for place in (None, trigger in ON_THE_RUN):
        path, label, private = artifact_place(trigger, repo, run_id, org, place)
        urls = ([f"{api_url}{path}?name={name}&per_page=100" for name in names] if private
                else [f"{api_url}{path}"])
        for url in urls:
            try:
                response = get(url)
            except requests.exceptions.RequestException as error:
                raise GitHubRefused(f"listing the artifacts of {label} failed: {error}") from None
            if private and response.status_code in (401, 403, 404):
                body = (response.text or "")[:200]
                if "rate limit" in body.lower():
                    raise GitHubRefused(
                        f"{label} answered HTTP {response.status_code} to the artifact listing: the token's"
                        " GitHub API rate limit is spent; retry after the limit resets")
                raise GitHubRefused(
                    f"{label} answered HTTP {response.status_code} to the artifact listing: the token cannot read it. "
                    + TOKEN_FIX.format(org=org, repo=PRIVATE_ARTIFACT_REPO)
                )
            if not response.ok:
                raise GitHubRefused(f"{label} answered HTTP {response.status_code} to the artifact listing")
            try:
                listing = response.json()
            except ValueError:
                listing = None
            artifacts = listing.get("artifacts") if isinstance(listing, dict) else None
            if not isinstance(artifacts, list) or not all(isinstance(a, dict) for a in artifacts):
                raise GitHubRefused(f"{label} answered something that is not an artifact listing")
            for artifact in artifacts:
                name = artifact.get("name")
                if name in names:
                    found.setdefault(name, []).append(artifact)
        if found:
            break
    return [
        (name, max(found[name], key=lambda a: a.get("created_at") or ""), label, private)
        for name in names if name in found
    ]


def artifact_page_url(artifact, repo, run_id, private, org=ORG):
    """The page an artifact is on: its run's, or the private repository run's
    that published it."""
    if private:
        published_by = (artifact.get("workflow_run") or {}).get("id")
        return f"https://github.com/{org}/{PRIVATE_ARTIFACT_REPO}/actions/runs/{published_by}/artifacts/{artifact.get('id')}"
    return f"https://github.com/{org}/{repo}/actions/runs/{run_id}/artifacts/{artifact.get('id')}"


class BoundedCache:
    """A small thread-safe TTL cache for the run-evidence endpoint: at most
    max_entries keys, each expiring its own ttl after it was put. Parsed
    results are put with the full ttl; failed lookups with a short one, so
    they retry soon without hammering the GitHub API on every dashboard
    refresh. Plain dict-of-tuples rather than functools/caching libraries --
    per-entry TTLs and a size cap are all that is needed."""

    def __init__(self, max_entries):
        self._entries = {}  # key -> (expires_at, value)
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() >= expires_at:
                self._entries.pop(key, None)
                return None
            return value

    def put(self, key, value, ttl):
        with self._lock:
            self._entries[key] = (time.monotonic() + ttl, value)
            while len(self._entries) > self._max_entries:
                self._entries.pop(next(iter(self._entries)))


class CIObserveRunEvidence:
    """The /api/ci-observe/run endpoint's implementation, held by
    WorkflowExporter as self._run_evidence. The exporter argument is a
    WorkflowExporter instance (workflow-exporter.py cannot be imported
    here -- hyphenated file name -- and importing it would be circular
    anyway); it provides the SQLite job history, the GitHub API access and
    the auth headers, so this class adds no second poller and no separate
    credentials."""

    # Capture ids as built by the e2e-*-full-install workflows'
    # ci-observe-capture step: "<suite>-<prN|main>-<run id>-a<attempt>",
    # e.g. "vmaas-main-41310072671-a1". Older captures used
    # "run-<run id>-attempt-<attempt>". A bare run id also identifies a
    # run, but says nothing about the attempt.
    CAPTURE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-(?:pr\d+|main)-(\d+)-a(\d+)$")
    LEGACY_CAPTURE_ID_RE = re.compile(r"^run-(\d+)-attempt-(\d+)$")
    BARE_RUN_ID_RE = re.compile(r"^\d+$")

    # Bounds for the on-demand native (JUnit) lookup. The artifact zip is
    # read streamed and capped; a larger artifact is reported as too_large,
    # never downloaded unbounded. Parsed results are cached for 15 minutes
    # so repeated dashboard loads do not re-download; an error is cached for
    # 2 minutes so a dashboard left open asks again soon without hammering
    # the GitHub API.
    NATIVE_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
    NATIVE_MAX_JUNIT_FILES = 16
    NATIVE_MAX_JUNIT_FILE_BYTES = 32 * 1024 * 1024
    NATIVE_MAX_MESSAGE_CHARS = 2000
    NATIVE_CACHE_TTL_SECONDS = 900
    NATIVE_FAILURE_CACHE_TTL_SECONDS = 120
    NATIVE_CACHE_MAX_ENTRIES = 32
    EVIDENCE_MAX_STEPS = 500
    # Control characters never belong in a failure message shown on a
    # dashboard; XML 1.0 forbids most of them anyway.
    CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    # XML 1.0 also forbids these in the raw document, but JUnit writers
    # sometimes emit them anyway (terminal escapes inside failure
    # messages), which would make the whole file unparseable. They are
    # deleted from the raw bytes before parsing: a well-formed document
    # contains none of them, so this can only rescue a file, never corrupt
    # one. UTF-16 documents (BOM-prefixed) are left alone -- their bytes
    # are not character-aligned.
    ILLEGAL_XML_CONTROL_BYTES = (
        bytes(range(0x00, 0x09)) + bytes((0x0B, 0x0C)) + bytes(range(0x0E, 0x20))
    )
    # The capture ids /api/ci-observe/failed-tests reads: the suite names the
    # artifact. A legacy or bare id names none.
    SUITE_CAPTURE_ID_RE = re.compile(r"^(vmaas|caas|bmaas)-(?:pr\d+|main)-(\d+)-a(\d+)$")

    def __init__(self, exporter):
        self.exporter = exporter
        # Run-evidence cache: failed-test documents (or their error) and run
        # metadata fetched on demand from GitHub, keyed by (kind, run id[,
        # run attempt, suite]).
        self._cache = BoundedCache(self.NATIVE_CACHE_MAX_ENTRIES)

    @staticmethod
    def _parse_capture_id(capture_id):
        """Split a capture id into (parsed, run_id, run_attempt).

        parsed is "exact" for the two capture-id formats (both carry the
        run id and the attempt), "inferred" for a bare run id (the attempt
        comes from stored data instead), and "invalid" for anything else.
        No run association is invented for an invalid id.
        """
        cid = (capture_id or "").strip()
        match = CIObserveRunEvidence.CAPTURE_ID_RE.match(cid) \
            or CIObserveRunEvidence.LEGACY_CAPTURE_ID_RE.match(cid)
        if match:
            return "exact", int(match.group(1)), int(match.group(2))
        if CIObserveRunEvidence.BARE_RUN_ID_RE.match(cid):
            return "inferred", int(cid), None
        return "invalid", None, None

    def _ci_observe_run_rows(self, run_id):
        """Every stored jobs row of one GitHub run: the run's own row (its
        id is the run id) plus the pull-request-flow suite rows that belong
        to it. A suite row's id is a job id, not the run id; its url column
        (the job's .../actions/runs/<run id>/job/<job id> link) is the only
        persisted trace of the parent run, so suite rows are matched on it.
        The /job/ suffix keeps a shorter run id from prefix-matching a
        longer one. Returns [] when the exporter has no row for the run.
        """
        with self.exporter._db() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE id = :run_id OR url LIKE :job_url",
                {"run_id": run_id, "job_url": f"%/actions/runs/{run_id}/job/%"},
            ).fetchall()
        jobs = []
        for row in rows:
            job = self.exporter._row_to_job(row)
            # The job and step timeline is not part of the job document the
            # exporter serves; the run page reads it as the run's phases.
            job["timeline"] = ci_observe_phases.load_timeline(dict(row).get("timeline_json"))
            jobs.append(job)
        return jobs

    @staticmethod
    def _failed_step_names(job):
        """The step names in a row's failed_step text ("job → step; ...") --
        the only per-step conclusion signal that is stored."""
        names = set()
        for entry in (job.get("failed_step") or "").split("; "):
            step = entry.rsplit(" → ", 1)[-1]
            if step:
                names.add(step)
        return names

    def _ci_observe_steps(self, jobs):
        """Flattened step rows for the jobs list, capped at
        EVIDENCE_MAX_STEPS. A step's conclusion comes from the job timeline
        when the row has one: "failure" when any step of that name failed,
        otherwise the conclusion its steps share. Without a timeline it is
        "failure" when the name appears in the row's failed_step text and
        None (not stored) otherwise."""
        steps = []
        for job in jobs:
            failed = self._failed_step_names(job)
            reported = {}
            for entry in job.get("timeline") or []:
                for step in entry.get("steps") or []:
                    if step.get("name") and step.get("conclusion"):
                        reported.setdefault(step["name"], set()).add(step["conclusion"])
            for step in job.get("steps", []):
                name = step.get("name", "")
                if not name:
                    continue
                seen = reported.get(name, set())
                if "failure" in seen or name in failed:
                    conclusion = "failure"
                elif len(seen) == 1:
                    conclusion = next(iter(seen))
                else:
                    conclusion = None
                steps.append({
                    "job_name": job.get("display_name") or job.get("workflow", ""),
                    "step_name": name,
                    "duration_s": step.get("duration_s"),
                    "conclusion": conclusion,
                })
                if len(steps) >= CIObserveRunEvidence.EVIDENCE_MAX_STEPS:
                    return steps
        return steps

    @staticmethod
    def _ci_observe_timing(jobs):
        """Where the run's time went: one `phases` row per suite row (the
        bar the run page draws), the run's `timing` (time to result, wait for
        a runner, failure cause) and its step `timeline`, each step with its
        start as seconds after the first job was created."""
        phase_rows = []
        timing = []
        timeline_rows = []
        for job in jobs:
            timeline = job.get("timeline") or []
            result = ci_observe_phases.run_phases(timeline, job.get("steps") or [])
            if not result["measured"]:
                continue
            suite = ci_observe_phases.suite_key(job.get("workflow"))
            label = ci_observe_phases.suite_label(suite) if suite else (job.get("workflow") or "This run")
            phase_rows.append({"category": label, **result["phases"]})
            cause = ci_observe_phases.failure_cause(job.get("conclusion"), job.get("failed_step"), timeline)
            timing.append({
                "suite": label,
                "total_s": result["total_s"] if result["total_s"] is not None else job.get("duration_s"),
                "queue_s": result["queue_s"],
                "source": result["source"],
                "cause_label": cause["label"],
                "cause_area": cause["area"],
                "failed_step": cause["step"],
            })
            created = [ci_observe_phases.parse_time(entry.get("created_at")) for entry in timeline]
            created = [moment for moment in created if moment]
            origin = min(created) if created else None
            for entry in timeline:
                for step in entry.get("steps") or []:
                    start = ci_observe_phases.parse_time(step.get("started_at"))
                    seconds = ci_observe_phases.seconds_between(step.get("started_at"), step.get("completed_at"))
                    if start is None or seconds is None:
                        continue
                    phase = ci_observe_phases.step_phase(step.get("name"), entry.get("name"))
                    timeline_rows.append({
                        "suite": label,
                        "job": entry.get("name") or "",
                        "step": step.get("name") or "",
                        "phase": phase,
                        "phase_label": ci_observe_phases.PHASE_LABELS[phase],
                        "start_s": round((start - origin).total_seconds()) if origin else None,
                        "duration_s": round(seconds),
                        "conclusion": step.get("conclusion") or "",
                    })
        timeline_rows.sort(key=lambda row: (row["suite"], row["start_s"] if row["start_s"] is not None else 0))
        return {"phases": phase_rows, "timing": timing, "timeline": timeline_rows}

    def _ci_observe_run_summary(self, rows, run_id, run_attempt):
        """The run-level summary. rows are the stored rows matching the
        requested attempt: the run's own row when its attempt matches,
        otherwise its suite rows (which carry the run's repo, branch, PR,
        actor and created_at, copied at ingestion -- see _suite_records).
        """
        conclusions = sorted({r.get("conclusion") for r in rows if r.get("conclusion")})
        if "failure" in conclusions or "timed_out" in conclusions:
            conclusion = "failure"
        elif conclusions == ["success"]:
            conclusion = "success"
        else:
            conclusion = ", ".join(conclusions)
        repo = rows[0].get("repo", "")
        url = next(
            (r.get("url") for r in rows
             if (r.get("url") or "").endswith(f"/actions/runs/{run_id}")),
            "",
        )
        runners = sorted({
            name for r in rows for name in (r.get("runner_name") or "").split(", ") if name
        })
        return {
            "repo": repo,
            "workflow": ", ".join(sorted({
                r.get("workflow", "") for r in rows if r.get("workflow")
            })),
            "conclusion": conclusion,
            "status": next((r.get("status") for r in rows if r.get("status")),
                           conclusion or "unknown"),
            "duration_s": max((r.get("duration_s") for r in rows if r.get("duration_s") is not None),
                              default=None),
            "url": url or (f"https://github.com/{ORG}/{repo}/actions/runs/{run_id}" if repo else ""),
            "runner_name": ", ".join(runners),
            # Stored with the job row (the exporter's run payload carries
            # it), so most runs need no extra GitHub call. The caller falls
            # back to the API when it is empty (a row stored before the
            # column existed).
            "head_sha": next((r.get("head_sha") for r in rows if r.get("head_sha")), ""),
            # Same for every row of one run; first non-empty wins because
            # the pull-request flow's own row never resolves a PR while its
            # suite rows do.
            "branch": next((r.get("branch") for r in rows if r.get("branch")), ""),
            "pr_display": next((r.get("pr_display") for r in rows if r.get("pr_display")), ""),
            "created_at": min((r.get("created_at") or "" for r in rows), default=""),
            # The requested attempt, repeated here so the run summary
            # carries the whole run identity.
            "run_attempt": run_attempt,
        }

    def _ci_observe_head_sha(self, repo, run_id):
        """A run's head_sha from the GitHub API, used when the stored job
        row has none (a row stored before the column existed), "" when the
        fetch fails -- the endpoint must still serve the job data.
        Cached like the native data: failures briefly, successes for the
        full TTL."""
        key = ("head_sha", run_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        sha = ""
        if self.exporter._github_token():
            try:
                resp = self.exporter._get(f"{API_URL}/repos/{ORG}/{repo}/actions/runs/{run_id}")
                if resp.ok:
                    sha = resp.json().get("head_sha") or ""
            except requests.exceptions.RequestException:
                logger.debug("Run metadata fetch failed for run %s", run_id)
        self._cache.put(
            key, sha,
            CIObserveRunEvidence.NATIVE_CACHE_TTL_SECONDS if sha
            else CIObserveRunEvidence.NATIVE_FAILURE_CACHE_TTL_SECONDS,
        )
        return sha

    def _download_artifact(self, url):
        """Download an artifact zip, reading at most NATIVE_MAX_DOWNLOAD_BYTES
        (streamed; a larger artifact is abandoned mid-read, never buffered
        whole). Returns (data, reason): (bytes, None) on success, or
        (None, reason) with reason "too_large" or an error message."""
        try:
            resp = requests.get(url, headers=self.exporter.headers, timeout=120, stream=True)
        except requests.exceptions.RequestException:
            return None, "artifact download failed (network error)"
        try:
            self.exporter._update_rate_limit(resp)
            if not resp.ok:
                return None, f"artifact download failed with HTTP {resp.status_code}"
            chunks = []
            read = 0
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                read += len(chunk)
                # Read through the exporter (not this class's own value):
                # the tests monkeypatch the bound on WorkflowExporter.
                if read > self.exporter.NATIVE_MAX_DOWNLOAD_BYTES:
                    return None, "too_large"
                chunks.append(chunk)
            return b"".join(chunks), None
        finally:
            resp.close()

    def _extract_junit_files(self, data):
        """The junit.xml (plus any junit-*.xml) entries of an artifact zip,
        capped at NATIVE_MAX_JUNIT_FILES files and
        NATIVE_MAX_JUNIT_FILE_BYTES decompressed bytes each (a zip entry
        can decompress far larger than it compresses; the read is capped,
        not the compressed size). Returns (files, reason): a list of
        (name, bytes) with reason None, or (None, reason) when the zip or
        its entries are unusable."""
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return None, "artifact is not a valid zip archive"
        names = []
        for name in archive.namelist():
            base = name.rsplit("/", 1)[-1]
            if base == "junit.xml" or (base.startswith("junit-") and base.endswith(".xml")):
                names.append(name)
                if len(names) >= CIObserveRunEvidence.NATIVE_MAX_JUNIT_FILES:
                    break
        if not names:
            return None, "artifact has no junit.xml"
        files = []
        for name in names:
            try:
                with archive.open(name) as entry:
                    content = entry.read(CIObserveRunEvidence.NATIVE_MAX_JUNIT_FILE_BYTES + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                return None, f"could not read {name} from the artifact"
            if len(content) > CIObserveRunEvidence.NATIVE_MAX_JUNIT_FILE_BYTES:
                return None, f"{name} is larger than the per-file bound"
            files.append((name, content))
        return files, None

    @classmethod
    def _bound_junit_message(cls, raw):
        """(message, truncated) for a failure/error message attribute:
        control characters stripped, capped at NATIVE_MAX_MESSAGE_CHARS.
        Stack traces (the text inside <failure>/<error>) never reach this
        -- only the bounded message attribute is kept."""
        text = cls.CONTROL_CHARS_RE.sub("", raw or "")
        return text[: cls.NATIVE_MAX_MESSAGE_CHARS], len(text) > cls.NATIVE_MAX_MESSAGE_CHARS

    @staticmethod
    def _junit_time(element):
        """An element's time attribute as a rounded float, or None when it
        is missing, not a number, or not finite -- inf/nan would serialize
        as invalid JSON (Python's json writes a bare Infinity/NaN), so they
        get the same missing-value sentinel as unparseable text."""
        try:
            value = float(element.get("time") or 0)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value):
            return None
        return round(value, 3)

    def _parse_junit_files(self, files, artifact_url):
        """Aggregate JUnit XML files into counts and failed-test rows. Every
        count is a count of <testcase> elements (a <failure>, <error> or
        <skipped> child), so the counts and the rows always agree. Only each
        <failure>/<error> element's type and bounded message attribute are
        kept, never its stack-trace text. Returns (fields, reason): fields
        is None with a reason when a file does not parse."""
        totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        time_s = 0.0
        failed_tests = []
        notes = []
        for name, content in files:
            if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
                cleaned = content
            else:
                cleaned = content.translate(None, CIObserveRunEvidence.ILLEGAL_XML_CONTROL_BYTES)
            try:
                root = ET.fromstring(cleaned)
            except ET.ParseError:
                return None, f"{name} is not valid XML"
            suites = list(root.iter("testsuite"))
            if not suites:
                return None, f"{name} has no testsuite element"
            for suite in suites:
                cases = suite.findall("testcase")
                totals["tests"] += len(cases)
                totals["failures"] += sum(1 for c in cases if c.find("failure") is not None)
                totals["errors"] += sum(1 for c in cases if c.find("error") is not None)
                totals["skipped"] += sum(1 for c in cases if c.find("skipped") is not None)
                time_s += sum(
                    measured for case in cases
                    if (measured := self._junit_time(case)) is not None
                )
                for case in cases:
                    problem = case.find("failure")
                    if problem is None:
                        problem = case.find("error")
                    if problem is None:
                        continue
                    message, truncated = self._bound_junit_message(problem.get("message"))
                    if truncated:
                        notes.append(
                            f"a failure message was truncated at "
                            f"{CIObserveRunEvidence.NATIVE_MAX_MESSAGE_CHARS} characters"
                        )
                    failed_tests.append({
                        "classname": case.get("classname", ""),
                        "name": case.get("name", ""),
                        "type": problem.get("type") or problem.tag,
                        "message": message,
                        "time_s": self._junit_time(case),
                        "artifact_url": artifact_url,
                    })
        return {
            "tests": totals["tests"],
            "failures": totals["failures"],
            "errors": totals["errors"],
            "skipped": totals["skipped"],
            "time_s": round(time_s, 3),
            "failed_tests": failed_tests,
            "notes": notes,
        }, None

    def get_ci_observe_run_json(self, params):
        """The /api/ci-observe/run document: everything known about one
        ci-observe capture's GitHub run.

        Query params: capture_id -- a capture id such as
        "vmaas-main-41310072671-a1", a legacy "run-<id>-attempt-<n>", or a
        bare run id.

        Returns a bounded document:
          capture_id, parsed ("exact" | "inferred" | "invalid"), run_id,
          run_attempt -- the capture id's run identity;
          run -- repo, workflow, conclusion, status, duration_s, url,
              runner_name, head_sha, branch, pr_display, created_at,
              run_attempt;
          jobs -- the run's own stored row plus its suite rows for that
              attempt (a run can have several; all are returned);
          steps -- flattened step rows (job_name, step_name, duration_s,
              conclusion);
          coverage -- where each part comes from and what is missing.

        The run's failed tests are not part of it: they come from an
        artifact, which /api/ci-observe/failed-tests reads, and its errors
        must not take the job history's panels down with them.
        """
        capture_id = (params.get("capture_id", [""])[0] or "").strip()
        parsed, run_id, run_attempt = self._parse_capture_id(capture_id)
        notes = []
        if parsed == "invalid":
            notes.append(
                "capture_id query parameter is missing or empty"
                if not capture_id
                else f"capture id {capture_id!r} matches no capture-id format, "
                     "so no run association was made"
            )
        if parsed == "inferred":
            identity = f"a bare run id: run {run_id}, attempt not stated in the id"
        elif CIObserveRunEvidence.LEGACY_CAPTURE_ID_RE.match(capture_id):
            identity = f"legacy capture id format: run {run_id} attempt {run_attempt}"
        elif run_id is not None:
            identity = (f"capture id format <suite>-<prN|main>-<run id>-a<attempt>: "
                        f"run {run_id} attempt {run_attempt}")
        else:
            identity = "the capture id does not identify a run"
        coverage = {
            "job_data": (
                "GitHub job history stored by this workflow exporter "
                f"(SQLite, {JOBS_HISTORY_DAYS}-day retention)"
            ),
            "capture_identity": identity,
            "notes": notes,
        }
        doc = {
            "capture_id": capture_id,
            "parsed": parsed,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "run": None,
            "jobs": [],
            "steps": [],
            "phases": [],
            "timing": [],
            "timeline": [],
            "coverage": coverage,
        }
        if run_id is None:
            return doc

        rows = self._ci_observe_run_rows(run_id)
        if not rows:
            notes.append(
                f"run {run_id} is not in the exporter's job history (it was never "
                f"polled, or it is older than the {JOBS_HISTORY_DAYS}-day retention)"
            )
            return doc

        if run_attempt is None:
            # A bare run id: show the newest attempt the exporter has.
            run_attempt = max(r.get("run_attempt") or 1 for r in rows)
            doc["run_attempt"] = run_attempt
            notes.append(
                f"the capture id states no attempt; showing the latest stored "
                f"attempt ({run_attempt})"
            )

        run_row = next((r for r in rows if r["id"] == run_id), None)
        suite_rows = [
            r for r in rows
            if r["id"] != run_id and (r.get("run_attempt") or 1) == run_attempt
        ]
        run_row_matches = run_row is not None and run_row.get("run_attempt") == run_attempt
        if run_row is not None and not run_row_matches:
            notes.append(
                f"the exporter keeps only a run's latest attempt; the stored run row "
                f"is attempt {run_row.get('run_attempt')}, the capture says {run_attempt}"
            )
        jobs = ([run_row] if run_row_matches else []) + suite_rows
        if not jobs:
            notes.append(f"no stored rows match attempt {run_attempt}")
        if len(suite_rows) > 1:
            notes.append(
                f"this run has {len(suite_rows)} suite rows for attempt {run_attempt}; "
                "all are listed"
            )

        summary_rows = jobs or ([run_row] if run_row is not None else [])
        if summary_rows:
            doc["run"] = self._ci_observe_run_summary(summary_rows, run_id, run_attempt)
            if not doc["run"]["head_sha"]:
                doc["run"]["head_sha"] = self._ci_observe_head_sha(doc["run"]["repo"], run_id)
            if not doc["run"]["head_sha"]:
                notes.append("head_sha could not be fetched from GitHub")

        import ci_observe_history

        doc["jobs"] = [
            {
                "name": j.get("display_name") or j.get("workflow", ""),
                "display_name": j.get("display_name") or j.get("workflow", ""),
                "repo": j.get("repo", ""),
                "workflow": j.get("workflow", ""),
                "conclusion": j.get("conclusion", ""),
                "duration_s": j.get("duration_s", 0),
                "failed_step": j.get("failed_step", ""),
                "failure_reason": j.get("failure_reason") or "",
                # The cause the other ci-observe pages give this failure
                # (the phase of its first failed step), not the exporter's
                # older failure_reason, so the run page and Why runs fail agree.
                "cause_label": (
                    ci_observe_phases.failure_cause(j.get("conclusion"), j.get("failed_step"), j.get("timeline"))[
                        "label"]
                    if ci_observe_phases.result_key(j.get("conclusion")) == "failed" else ""
                ),
                "steps": j.get("steps", []),
                "url": j.get("url", ""),
                # The job name opens its suite's Where the time goes page, not
                # GitHub: the question after a run is "is this job getting
                # slower, and where?" The GitHub link stays on the url field.
                "history_url": ci_observe_history.history_url(
                    "job", j.get("display_name") or j.get("workflow", ""),
                    repo=j.get("repo", ""), workflow=j.get("workflow", ""),
                ),
            }
            for j in jobs
        ]
        doc["steps"] = self._ci_observe_steps(jobs)
        doc.update(self._ci_observe_timing(jobs))
        return doc

    # -- /api/ci-observe/failed-tests -------------------------------------

    @staticmethod
    def run_trigger(rows, run_id):
        """Why a run ran ("pr", "merge_queue", "scheduled", "manual", ...),
        from its stored rows: the run's own row says it for a flow run (a
        merge group, a pull request); a suite row says it otherwise."""
        own = next((row for row in rows if row.get("id") == run_id), None)
        ordered = ([own] if own else []) + [row for row in rows if row is not own]
        for row in ordered:
            trigger = ci_observe_phases.trigger_key(row.get("event"), row.get("workflow"))
            if trigger != "other":
                return trigger
        return "other"

    def get_ci_observe_failed_tests_json(self, params):
        """The /api/ci-observe/failed-tests document for ?capture_id=: the
        failed tests of one capture, from the JUnit file in its suite's
        artifact, read from the one place the run's trigger says.

        Returns {capture_id, run_id, run_attempt, suite, trigger, artifact:
        {name, place, url}, tests, failures, errors, skipped, failed_tests:
        [{name, classname, type, message, time_s, artifact_url}], notes}.
        failed_tests is empty only when the JUnit file lists no failed test.
        Raises EvidenceError, with what failed, where and what to fix, for
        everything else; the error is cached briefly too."""
        capture_id = (params.get("capture_id", [""])[0] or "").strip()
        match = CIObserveRunEvidence.SUITE_CAPTURE_ID_RE.match(capture_id)
        if match is None:
            raise EvidenceError(
                f"capture id {capture_id!r} is not <vmaas|caas|bmaas>-<prN|main>-<run id>-a<attempt>, "
                "so it names no suite artifact"
            )
        suite, run_id, run_attempt = match.group(1), int(match.group(2)), int(match.group(3))
        key = ("failed_tests", run_id, run_attempt, suite)
        cached = self._cache.get(key)
        if cached is not None:
            kind, value = cached
            if kind == "error":
                raise EvidenceError(value)
            return value
        try:
            document = self._failed_tests_uncached(capture_id, suite, run_id, run_attempt)
        except EvidenceError as error:
            logger.error("failed tests of %s cannot be read: %s", capture_id, error)
            self._cache.put(key, ("error", str(error)), CIObserveRunEvidence.NATIVE_FAILURE_CACHE_TTL_SECONDS)
            raise
        self._cache.put(key, ("ok", document), CIObserveRunEvidence.NATIVE_CACHE_TTL_SECONDS)
        return document

    def _no_upload_step_ever_ran(self, rows):
        """Whether the suite's stored steps name no upload step at all. A
        step that never ran stores no timestamps, so it is absent from the
        stored steps: a cancelled or lost job leaves no upload step behind,
        while a job whose upload ran (always() && !cancelled()) keeps it.
        No claim when the steps were never read or hold nothing."""
        for row in rows:
            steps = row.get("steps")
            if not isinstance(steps, list) or not steps:
                continue
            names = [str(step.get("name") or "") if isinstance(step, dict) else "" for step in steps]
            if any(name.startswith("Scan and upload artifacts") for name in names):
                return False
            return True
        return False

    def _failed_tests_uncached(self, capture_id, suite, run_id, run_attempt):
        if not self.exporter._github_token():
            raise EvidenceError("no GitHub token is configured (PRIVATE_GITHUB_TOKEN), so no artifact can be read")
        rows = self._ci_observe_run_rows(run_id)
        if not rows:
            raise EvidenceError(
                f"run {run_id} is not in the exporter's job history (never polled, or older than "
                f"{JOBS_HISTORY_DAYS} days), so its repository and trigger are unknown"
            )
        repo = next((row.get("repo") for row in rows if row.get("repo")), "")
        trigger = self.run_trigger(rows, run_id)
        # The capture's own rows: its suite's in a run that holds several
        # suites (the merge queue), the run's own row in a run of one.
        own = [row for row in rows if ci_observe_phases.suite_key(row.get("workflow")) == suite]
        own = own or [row for row in rows if row.get("id") == run_id]
        if not own:
            raise EvidenceError(f"run {run_id} has no stored row for its {suite} suite")
        latest = max(row.get("run_attempt") or 1 for row in own)
        if run_attempt != latest:
            raise EvidenceError(
                f"the capture is attempt {run_attempt}, and the job history holds attempt {latest} of this suite: "
                f"an artifact does not say which attempt uploaded it, so attempt {run_attempt}'s failed tests "
                "cannot be told apart from the later attempt's"
            )
        name = SUITE_ARTIFACTS[suite].format(run_id=run_id)
        found = find_artifacts(self.exporter._get, trigger, repo, run_id, [name])
        if not found:
            expected, other = artifact_places(trigger, repo, run_id)
            reason = f"no {name} artifact in {expected} or in {other}"
            if self._no_upload_step_ever_ran(own):
                reason += ("; the suite's stored steps include no upload step, so no artifact was ever"
                           " uploaded (the job log says why it never ran)")
            raise EvidenceError(reason)
        _, artifact, place, private = found[0]
        artifact_url = artifact_page_url(artifact, repo, run_id, private)
        if artifact.get("expired"):
            raise EvidenceError(
                f"{name} in {place} expired on {artifact.get('expires_at') or 'an unknown date'} "
                "(GitHub keeps artifacts 30 days)"
            )
        size = artifact.get("size_in_bytes") or 0
        # Read through the exporter (not this class's own value): the tests
        # monkeypatch the bound on WorkflowExporter.
        if size > self.exporter.NATIVE_MAX_DOWNLOAD_BYTES:
            raise EvidenceError(
                f"{name} in {place} is {size} bytes, over the {self.exporter.NATIVE_MAX_DOWNLOAD_BYTES}-byte "
                "download bound"
            )
        data, reason = self._download_artifact(artifact.get("archive_download_url", ""))
        if data is None and reason == "too_large":
            raise EvidenceError(f"downloading {name} from {place} stopped: it is over the download bound")
        if data is None:
            raise GitHubRefused(f"downloading {name} from {place} failed: {reason}")
        files, reason = self._extract_junit_files(data)
        if files is None:
            raise EvidenceError(f"{name} from {place}: {reason}")
        fields, reason = self._parse_junit_files(files, artifact_url)
        if fields is None:
            raise EvidenceError(f"{name} from {place}: {reason}")
        failed_test_steps = [
            step for row in own for job, step in ci_observe_phases.failed_steps(row.get("failed_step"))
            if ci_observe_phases.step_phase(step, job) == "test"
        ]
        if failed_test_steps and not fields["failed_tests"]:
            raise EvidenceError(
                f"the test step failed ({failed_test_steps[0]}), but the junit.xml of {name} lists no failed "
                "test: pytest stopped before it reported one (a collection or internal error, an interrupt, "
                "or no test collected). The job log has the cause"
            )
        return {
            "capture_id": capture_id,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "suite": suite,
            "trigger": trigger,
            "artifact": {"name": name, "place": place, "url": artifact_url},
            "tests": fields["tests"],
            "failures": fields["failures"],
            "errors": fields["errors"],
            "skipped": fields["skipped"],
            "time_s": fields["time_s"],
            "failed_tests": fields["failed_tests"],
            "notes": fields["notes"],
        }

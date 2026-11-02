"""Run-evidence half of the workflow exporter's /api/ci-observe/run endpoint.

Sibling module of workflow-exporter.py (which imports it; the exporter's
file name has a hyphen, so it cannot import this module's classes from
anywhere else by package path). A ci-observe capture id embeds a GitHub
run id and attempt, and CIObserveRunEvidence ties such a capture to the
GitHub-side evidence the exporter already stores (job history in SQLite)
and to the native JUnit results inside the run's
osac-logs-[<suite>-]full-install-<run id> artifact, downloaded from
GitHub on demand, bounded and cached. Same exporter, same token, same
data -- no second poller, no new artifact.

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

logger = logging.getLogger(__name__)

# GitHub access mirrors workflow-exporter.py's module-level config: the
# same environment variables, read once at import there and here, so the
# two modules always agree (the token is the exception -- see
# WorkflowExporter._github_token, which reads it live).
ORG = os.getenv("GITHUB_ORG", "osac-project")
API_URL = os.getenv("API_URL", "https://api.github.com")
JOBS_HISTORY_DAYS = int(os.getenv("JOBS_HISTORY_DAYS", "60"))


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
    # so repeated dashboard loads do not re-download; failed lookups are
    # cached for 2 minutes so a dashboard left open retries soon without
    # hammering the GitHub API.
    #
    # Artifact names are not uniform across suites: VMaaS full-install runs
    # upload "osac-logs-full-install-<run id>" while BMaaS and CaaS insert
    # the suite ("osac-logs-bmaas-full-install-<run id>",
    # "osac-logs-caas-full-install-<run id>" -- confirmed live). Any name
    # matching NATIVE_ARTIFACT_RE is accepted; the run-id anchor rejects
    # another run's similarly-named artifact, and anything else is not an
    # artifact of this run at all. The exact plain name is preferred when
    # several match.
    NATIVE_ARTIFACT_RE = re.compile(r"^osac-logs-(?:[a-z0-9-]+-)?full-install-(\d+)$")
    NATIVE_ARTIFACT_PREFIX = "osac-logs-full-install-"
    NATIVE_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
    NATIVE_MAX_JUNIT_FILES = 16
    NATIVE_MAX_JUNIT_FILE_BYTES = 32 * 1024 * 1024
    NATIVE_MAX_FAILED_TESTS = 100
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

    def __init__(self, exporter):
        self.exporter = exporter
        # Run-evidence cache: parsed native JUnit results and run metadata
        # fetched on demand from GitHub, keyed by (kind, run id[, run
        # attempt]) -- see get_ci_observe_run_json.
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
        return [self.exporter._row_to_job(r) for r in rows]

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
        EVIDENCE_MAX_STEPS. conclusion is "failure" when the step name
        appears in that job's failed_step text, None otherwise: per-step
        conclusions are not stored, and identically named steps of
        different jobs in one row cannot be told apart from the stored
        text alone."""
        steps = []
        for job in jobs:
            failed = self._failed_step_names(job)
            for step in job.get("steps", []):
                name = step.get("name", "")
                if not name:
                    continue
                steps.append({
                    "job_name": job.get("display_name") or job.get("workflow", ""),
                    "step_name": name,
                    "duration_s": step.get("duration_s", 0),
                    "conclusion": "failure" if name in failed else None,
                })
                if len(steps) >= CIObserveRunEvidence.EVIDENCE_MAX_STEPS:
                    return steps
        return steps

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
            "duration_s": max((r.get("duration_s") or 0 for r in rows), default=0),
            "url": url or f"https://github.com/{ORG}/{repo}/actions/runs/{run_id}",
            "runner_name": ", ".join(runners),
            # Not stored in SQLite; filled in from the GitHub API by the
            # caller (see _ci_observe_head_sha).
            "head_sha": "",
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
        """A run's head_sha from the GitHub API (not stored in SQLite), ""
        when the fetch fails -- the endpoint must still serve the job data.
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

    @staticmethod
    def _native_doc(status, artifact_name, artifact_url, notes, **counts):
        """One native-evidence document. Counts stay None (never 0) when
        the JUnit data was not parsed, so a missing or expired artifact can
        never look like a green zero."""
        return {
            "status": status,
            "artifact_name": artifact_name,
            "artifact_url": artifact_url,
            "tests": counts.get("tests"),
            "failures": counts.get("failures"),
            "errors": counts.get("errors"),
            "skipped": counts.get("skipped"),
            "time_s": counts.get("time_s"),
            "failed_tests": counts.get("failed_tests", []),
            "notes": notes,
        }

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
    def _junit_count(suite, attr, fallback):
        """A <testsuite> count attribute as an int, falling back to counting
        the matching <testcase> children when the attribute is missing, not
        a number, or not finite. A crafted tests="inf" must not raise out
        of the endpoint: float("inf") parses fine and only int() then
        raises OverflowError, and float("1e400") parses straight to inf, so
        finiteness is checked before the int() and OverflowError (a huge
        digit string) is caught too."""
        raw = suite.get(attr)
        if raw is not None:
            try:
                value = float(raw)
                if math.isfinite(value):
                    return int(value)
            except (TypeError, ValueError, OverflowError):
                pass
        return fallback

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
        """Aggregate JUnit XML files into the native doc's counts and
        failed-test rows. Counts come from the <testsuite> attributes,
        counting <testcase> children when an attribute is absent. Failed
        tests are capped at NATIVE_MAX_FAILED_TESTS entries; only each
        <failure>/<error> element's type and bounded message attribute are
        kept, never its stack-trace text. Returns (fields, reason):
        fields is None with a reason when no file parses."""
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
                totals["tests"] += self._junit_count(suite, "tests", len(cases))
                totals["failures"] += self._junit_count(
                    suite, "failures",
                    sum(1 for c in cases if c.find("failure") is not None))
                totals["errors"] += self._junit_count(
                    suite, "errors",
                    sum(1 for c in cases if c.find("error") is not None))
                totals["skipped"] += self._junit_count(
                    suite, "skipped",
                    sum(1 for c in cases if c.find("skipped") is not None))
                time_s += self._junit_time(suite) or 0
                for case in cases:
                    problem = case.find("failure")
                    if problem is None:
                        problem = case.find("error")
                    if problem is None:
                        continue
                    if len(failed_tests) >= CIObserveRunEvidence.NATIVE_MAX_FAILED_TESTS:
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
        if len(failed_tests) >= CIObserveRunEvidence.NATIVE_MAX_FAILED_TESTS:
            notes.append(
                f"only the first {CIObserveRunEvidence.NATIVE_MAX_FAILED_TESTS} failed tests are listed"
            )
        return {
            "tests": totals["tests"],
            "failures": totals["failures"],
            "errors": totals["errors"],
            "skipped": totals["skipped"],
            "time_s": round(time_s, 3),
            "failed_tests": failed_tests,
            "notes": notes,
        }, None

    @staticmethod
    def _is_native_artifact(name, run_id):
        """Whether an artifact name is this run's native-evidence artifact:
        the plain "osac-logs-full-install-<run id>" (VMaaS) or a
        suite-qualified "osac-logs-<suite>-full-install-<run id>" (BMaaS,
        CaaS -- see NATIVE_ARTIFACT_RE). Anything else -- including another
        run's similarly-named artifact, rejected by the run-id anchor --
        is not accepted."""
        match = CIObserveRunEvidence.NATIVE_ARTIFACT_RE.match(name or "")
        return match is not None and int(match.group(1)) == run_id

    def _ci_observe_native_uncached(self, run_id, repo):
        """The native JUnit evidence of one run, straight from GitHub. Every
        failure path returns a document whose status explains the gap; this
        must never raise into the endpoint. "not_requested" covers the
        cases where no lookup happened (no token, or the lookup itself
        failed) -- the notes say which."""
        artifact_name = f"{CIObserveRunEvidence.NATIVE_ARTIFACT_PREFIX}{run_id}"
        if not self.exporter._github_token():
            return self._native_doc(
                "not_requested", artifact_name, "",
                ["no GitHub token is configured, so the artifact was not looked up"],
            )
        try:
            resp = self.exporter._get(
                f"{API_URL}/repos/{ORG}/{repo}/actions/runs/{run_id}"
                f"/artifacts?per_page=100"
            )
        except requests.exceptions.RequestException:
            return self._native_doc(
                "not_requested", artifact_name, "",
                ["artifact lookup failed (network error)"],
            )
        if not resp.ok:
            return self._native_doc(
                "not_requested", artifact_name, "",
                [f"artifact lookup failed with HTTP {resp.status_code}"],
            )
        # The run's own artifacts only (the run-scoped listing), filtered to
        # the bounded name pattern. Expired matches sort last so a live one
        # is chosen whenever both exist; the exact plain name beats
        # suite-qualified ones, and ties break lexically -- a deterministic
        # choice whenever several match.
        matches = [
            a for a in resp.json().get("artifacts") or []
            if self._is_native_artifact(a.get("name", ""), run_id)
        ]
        if not matches:
            return self._native_doc(
                "missing", artifact_name, "",
                [f"no osac-logs-[<suite>-]full-install-{run_id} artifact "
                 f"exists in {ORG}/{repo}"],
            )
        matches.sort(key=lambda a: (bool(a.get("expired")),
                                    a.get("name") != artifact_name,
                                    a.get("name") or ""))
        artifact = matches[0]
        artifact_name = artifact.get("name") or artifact_name
        notes = []
        if len(matches) > 1:
            notes.append(
                f"{len(matches)} artifacts matched; used {artifact_name}"
            )
        artifact_url = (
            f"https://github.com/{ORG}/{repo}/actions/runs/{run_id}"
            f"/artifacts/{artifact.get('id')}"
        )
        if artifact.get("expired"):
            return self._native_doc(
                "expired", artifact_name, artifact_url,
                notes + [f"artifact {artifact_name} has passed its 7-day retention"
                         f" (expired {artifact.get('expires_at') or 'date unknown'})"],
            )
        # Read through the exporter (not this class's own value): the tests
        # monkeypatch the bound on WorkflowExporter.
        if (artifact.get("size_in_bytes") or 0) > self.exporter.NATIVE_MAX_DOWNLOAD_BYTES:
            return self._native_doc(
                "too_large", artifact_name, artifact_url,
                notes + [f"artifact is {artifact.get('size_in_bytes')} bytes, over the "
                         f"{self.exporter.NATIVE_MAX_DOWNLOAD_BYTES}-byte download bound"],
            )
        data, reason = self._download_artifact(artifact.get("archive_download_url", ""))
        if data is None:
            status = "too_large" if reason == "too_large" else "not_requested"
            return self._native_doc(status, artifact_name, artifact_url, notes + [reason])
        files, reason = self._extract_junit_files(data)
        if files is None:
            return self._native_doc("invalid", artifact_name, artifact_url, notes + [reason])
        fields, reason = self._parse_junit_files(files, artifact_url)
        if fields is None:
            return self._native_doc("invalid", artifact_name, artifact_url, notes + [reason])
        return self._native_doc(
            "available", artifact_name, artifact_url, notes + fields.pop("notes"), **fields
        )

    def _ci_observe_native(self, run_id, run_attempt, repo):
        """Cached native evidence for one (run, attempt). A successful parse
        is cached for NATIVE_CACHE_TTL_SECONDS so repeated dashboard loads
        do not re-download the artifact; anything else is cached briefly so
        an open dashboard retries soon without hammering the GitHub API."""
        key = ("native", run_id, run_attempt)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self._ci_observe_native_uncached(run_id, repo)
        ttl = (
            CIObserveRunEvidence.NATIVE_CACHE_TTL_SECONDS
            if result["status"] == "available"
            else CIObserveRunEvidence.NATIVE_FAILURE_CACHE_TTL_SECONDS
        )
        self._cache.put(key, result, ttl)
        return result

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
          native -- JUnit evidence parsed from the run's
              osac-logs-[<suite>-]full-install-<run id> artifact (the
              plain name for VMaaS runs, a suite-qualified one for BMaaS
              and CaaS), or a status (missing / expired / too_large /
              invalid / not_requested) explaining the gap;
          coverage -- where each part comes from and what is missing.

        A missing or malformed capture id, an unknown run, or a failed
        native lookup never fails the request: the document says what is
        missing instead.
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
            "native_data": (
                "JUnit XML parsed on demand from the run's "
                f"osac-logs-[<suite>-]full-install-<run id> workflow artifact "
                "(GitHub artifact retention is 7 days)"
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
            "native": None,
            "coverage": coverage,
        }
        if run_id is None:
            doc["native"] = self._native_doc("not_requested", "", "", list(notes))
            return doc

        rows = self._ci_observe_run_rows(run_id)
        if not rows:
            notes.append(
                f"run {run_id} is not in the exporter's job history (it was never "
                f"polled, or it is older than the {JOBS_HISTORY_DAYS}-day retention)"
            )
            doc["native"] = self._native_doc(
                "not_requested", f"{CIObserveRunEvidence.NATIVE_ARTIFACT_PREFIX}{run_id}", "",
                ["the run's repo is unknown, so the artifact was not looked up"],
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
            notes.append(
                "re-runs overwrite the artifact, so the native results may be from "
                "a newer attempt than the capture"
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
            doc["run"]["head_sha"] = self._ci_observe_head_sha(doc["run"]["repo"], run_id)
            if not doc["run"]["head_sha"]:
                notes.append("head_sha could not be fetched from GitHub")

        doc["jobs"] = [
            {
                "name": j.get("display_name") or j.get("workflow", ""),
                "workflow": j.get("workflow", ""),
                "conclusion": j.get("conclusion", ""),
                "duration_s": j.get("duration_s", 0),
                "failed_step": j.get("failed_step", ""),
                "failure_reason": j.get("failure_reason") or "",
                "steps": j.get("steps", []),
                "url": j.get("url", ""),
            }
            for j in jobs
        ]
        doc["steps"] = self._ci_observe_steps(jobs)

        repo = (doc["run"] or {}).get("repo") or rows[0].get("repo", "")
        doc["native"] = self._ci_observe_native(run_id, run_attempt, repo)
        return doc

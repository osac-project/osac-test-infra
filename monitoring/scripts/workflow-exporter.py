#!/usr/bin/env python3
"""OSAC Workflow Exporter — GitHub Actions job queue and history metrics for Prometheus.

Polls the GitHub API for workflow run status across all repos in the org and exposes:
  - Queue depth (queued + waiting runs)
  - In-progress runs
  - Completed run counts by conclusion
  - Run duration histogram
  - JSON API at /api/jobs with detailed recent job info for Grafana Infinity
"""

import os
import sys
import time
import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests
from prometheus_client import (
    generate_latest,
    CONTENT_TYPE_LATEST,
    Gauge,
    Counter,
    Histogram,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ORG = os.getenv("GITHUB_ORG", "osac-project")
TOKEN = os.getenv("PRIVATE_GITHUB_TOKEN")
API_URL = os.getenv("API_URL", "https://api.github.com")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "90"))
PORT = int(os.getenv("PORT", "9103"))
# Comma-separated list of repos to monitor. Empty = auto-discover active repos.
REPOS_FILTER = [r.strip() for r in os.getenv("REPOS", "").split(",") if r.strip()]
# How many recent jobs to keep in memory for the JSON API
JOBS_HISTORY_SIZE = int(os.getenv("JOBS_HISTORY_SIZE", "500"))
# Cache file for persistence across restarts
CACHE_DIR = os.getenv("CACHE_DIR", os.path.expanduser("~/.monitoring-server/data"))
CACHE_FILE = os.path.join(CACHE_DIR, "workflow-exporter-cache.json")
# Max age of cache file before falling back to API fetch (seconds)
CACHE_MAX_AGE = int(os.getenv("CACHE_MAX_AGE", "3600"))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
queued_runs = Gauge(
    "github_actions_queued_runs",
    "Queued workflow runs per repo",
    ["org", "repo"],
)
in_progress_runs = Gauge(
    "github_actions_in_progress_runs",
    "In-progress workflow runs per repo",
    ["org", "repo"],
)
queued_total = Gauge(
    "github_actions_queued_runs_org",
    "Total queued workflow runs across all repos",
    ["org"],
)
in_progress_total = Gauge(
    "github_actions_in_progress_runs_org",
    "Total in-progress workflow runs across all repos",
    ["org"],
)
completed_runs = Counter(
    "github_actions_completed_runs_total",
    "Completed workflow runs",
    ["org", "repo", "workflow", "conclusion"],
)
run_duration = Histogram(
    "github_actions_run_duration_seconds",
    "Workflow run duration in seconds",
    ["org", "repo", "conclusion"],
    buckets=[60, 120, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200],
)
failed_step_total = Counter(
    "github_actions_failed_step_total",
    "Failed workflow run steps",
    ["org", "workflow", "step"],
)
api_remaining = Gauge(
    "github_actions_api_rate_limit_remaining",
    "GitHub API rate limit remaining (workflow exporter)",
    ["org"],
)


# ---------------------------------------------------------------------------
# Exporter logic
# ---------------------------------------------------------------------------
class WorkflowExporter:
    # Ordered category mapping — first match wins (case-insensitive substring)
    WORKFLOW_CATEGORIES = {
        "e2e":        ["e2e"],
        "lint":       ["pre-commit", "lint", "checklist", "kustomize", "check image"],
        "ci":         ["ci", "test", "check", "build"],
        "release":    ["publish", "container image", "mirror"],
        "automation": ["bump", "dependabot", "copilot", "slash"],
    }

    @staticmethod
    def _categorize_workflow(name):
        """Categorize a workflow name using substring matching.

        Iterates WORKFLOW_CATEGORIES in order, returns first match.
        Defaults to 'ci' for unknown workflows.
        """
        lower = name.lower()
        for category, patterns in WorkflowExporter.WORKFLOW_CATEGORIES.items():
            for pattern in patterns:
                if pattern in lower:
                    return category
        return "ci"

    def __init__(self):
        self.headers = {
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._repos_cache = []
        self._repos_cache_ts = 0
        self._active_repos = None
        self._active_repos_ts = 0
        self._seen_run_ids = set()
        # Recent jobs for JSON API — thread-safe via GIL for reads
        self.recent_jobs = deque(maxlen=JOBS_HISTORY_SIZE)
        # Current in-flight runs (queued + in_progress)
        self.active_runs = []
        self._lock = threading.Lock()

    # -- cache persistence ---------------------------------------------------

    def _save_cache(self):
        """Persist recent_jobs and _seen_run_ids to disk."""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            data = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "recent_jobs": list(self.recent_jobs),
                "seen_run_ids": sorted(self._seen_run_ids),
            }
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, default=str)
            os.replace(tmp, CACHE_FILE)
            logger.debug("Cache saved: %d jobs, %d seen IDs",
                         len(self.recent_jobs), len(self._seen_run_ids))
        except Exception:
            logger.exception("Failed to save cache")

    def _load_cache(self):
        """Load recent_jobs and _seen_run_ids from disk if cache is fresh.

        Returns True if cache was loaded, False if missing/stale/corrupt.
        """
        if not os.path.exists(CACHE_FILE):
            logger.info("No cache file found at %s", CACHE_FILE)
            return False

        try:
            age = time.time() - os.path.getmtime(CACHE_FILE)
            if age > CACHE_MAX_AGE:
                logger.info("Cache is %.0fs old (max %ds), will re-fetch",
                            age, CACHE_MAX_AGE)
                return False

            with open(CACHE_FILE) as f:
                data = json.load(f)

            jobs = data.get("recent_jobs", [])
            seen = data.get("seen_run_ids", [])

            # Backfill display_name and category for cached records
            for job in jobs:
                if "display_name" not in job:
                    repo = job.get("repo", "")
                    wf = job.get("workflow", "unknown")
                    job["display_name"] = f"{repo} / {wf}"
                if "category" not in job:
                    job["category"] = self._categorize_workflow(
                        job.get("workflow", "unknown"))

            self.recent_jobs = deque(jobs, maxlen=JOBS_HISTORY_SIZE)
            self._seen_run_ids = set(seen)

            logger.info("Cache loaded: %d jobs, %d seen IDs (%.0fs old)",
                         len(self.recent_jobs), len(self._seen_run_ids), age)
            return True
        except Exception:
            logger.exception("Failed to load cache, will re-fetch")
            return False

    # -- helpers -------------------------------------------------------------

    def _update_rate_limit(self, resp):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            api_remaining.labels(org=ORG).set(int(remaining))

    def _get(self, url):
        resp = requests.get(url, headers=self.headers, timeout=15)
        self._update_rate_limit(resp)
        return resp

    # -- repo listing --------------------------------------------------------

    def get_repos(self):
        """List non-archived repos in the org (cached 10 min)."""
        now = time.time()
        if self._repos_cache and now - self._repos_cache_ts < 600:
            return self._repos_cache

        repos = []
        url = f"{API_URL}/orgs/{ORG}/repos?per_page=100&type=all"
        while url:
            resp = self._get(url)
            if not resp.ok:
                logger.error("Failed to list repos: %s %s", resp.status_code, resp.text[:200])
                return self._repos_cache
            repos.extend(r["name"] for r in resp.json() if not r.get("archived"))
            url = resp.links.get("next", {}).get("url")

        self._repos_cache = repos
        self._repos_cache_ts = now
        logger.info("Cached %d repos", len(repos))
        return repos

    def get_active_repos(self):
        """Return repos that have at least one workflow run (cached 30 min)."""
        now = time.time()
        if self._active_repos is not None and now - self._active_repos_ts < 1800:
            return self._active_repos

        all_repos = self.get_repos()
        active = []
        for repo in all_repos:
            resp = self._get(
                f"{API_URL}/repos/{ORG}/{repo}/actions/runs?per_page=1"
            )
            if resp.ok and resp.json().get("total_count", 0) > 0:
                active.append(repo)
            elif resp.status_code == 409:
                continue

        self._active_repos = active
        self._active_repos_ts = now
        logger.info("Active repos (with Actions): %d / %d", len(active), len(all_repos))
        return active

    # -- helpers for detailed job info ---------------------------------------

    @staticmethod
    def _extract_trigger(run):
        """Derive a human-readable trigger label from the run."""
        event = run.get("event", "unknown")
        if event == "pull_request":
            prs = run.get("pull_requests") or []
            if prs:
                pr_num = prs[0].get("number", "?")
                return f"PR #{pr_num}"
            # head_branch may hint at the PR
            return f"PR ({run.get('head_branch', '?')})"
        if event == "push":
            return f"push ({run.get('head_branch', '?')})"
        if event == "schedule":
            return "scheduled"
        if event == "workflow_dispatch":
            return "manual"
        return event

    @staticmethod
    def _make_job_record(run, repo):
        """Build a flat dict for the JSON API from a workflow run."""
        started = run.get("run_started_at") or run.get("created_at", "")
        ended = run.get("updated_at", "")
        duration_s = 0
        if started and ended:
            try:
                t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                duration_s = max(0, (t1 - t0).total_seconds())
            except (ValueError, TypeError):
                pass

        conclusion = run.get("conclusion") or ""
        status = run.get("status", "unknown")
        display_status = conclusion if conclusion else status

        workflow_name = run.get("name", "unknown")
        return {
            "id": run.get("id"),
            "repo": repo,
            "workflow": workflow_name,
            "display_name": f"{repo} / {workflow_name}",
            "category": WorkflowExporter._categorize_workflow(workflow_name),
            "branch": run.get("head_branch", ""),
            "status": display_status,
            "conclusion": conclusion,
            "event": run.get("event", "unknown"),
            "trigger": WorkflowExporter._extract_trigger(run),
            "duration_s": round(duration_s),
            "duration": WorkflowExporter._fmt_duration(duration_s),
            "actor": run.get("actor", {}).get("login", ""),
            "url": run.get("html_url", ""),
            "created_at": run.get("created_at", ""),
            "updated_at": ended,
            "run_number": run.get("run_number", 0),
            "run_attempt": run.get("run_attempt", 1),
        }

    @staticmethod
    def _fmt_duration(seconds):
        if seconds <= 0:
            return "-"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m}m {s}s"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"

    # -- metric collection ---------------------------------------------------

    def _run_count(self, repo, status):
        """Get total_count of runs with given status (single API call)."""
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs?status={status}&per_page=1"
        )
        if not resp.ok:
            return 0
        return resp.json().get("total_count", 0)

    def _fetch_active_runs(self, repo):
        """Fetch current queued and in_progress runs for the active list."""
        runs = []
        for status in ("queued", "in_progress"):
            resp = self._get(
                f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
                f"?status={status}&per_page=20"
            )
            if resp.ok:
                for run in resp.json().get("workflow_runs", []):
                    runs.append(self._make_job_record(run, repo))
        return runs

    def _recent_completed(self, repo):
        """Fetch recently completed runs to detect new completions.

        The GitHub API returns completed runs sorted by updated_at descending,
        so the most recently finished runs come first. We fetch 50 per page
        and rely on _seen_run_ids to skip already-processed ones.
        This correctly catches long-running jobs (e.g. created 2h ago,
        just now completed) that a created-time filter would miss.
        """
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
            f"?status=completed&per_page=50"
        )
        if not resp.ok:
            return []
        return resp.json().get("workflow_runs", [])

    def _fetch_recent_history(self, repo):
        """Fetch the most recent completed runs for initial history load.

        Fetches 100 most recent completed runs per repo to seed Prometheus
        counters and the JSON API with a meaningful baseline.
        """
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs"
            f"?status=completed&per_page=100"
        )
        if not resp.ok:
            return []
        return resp.json().get("workflow_runs", [])

    def _fetch_run_jobs(self, repo, run_id):
        """Fetch job-level details for a run.

        Returns the raw jobs list from the GitHub API, or [] on error.
        """
        resp = self._get(
            f"{API_URL}/repos/{ORG}/{repo}/actions/runs/{run_id}/jobs"
            f"?filter=latest&per_page=30"
        )
        if not resp.ok:
            return []
        return resp.json().get("jobs", [])

    def _extract_failed_steps(self, jobs):
        """Extract failed step info from a list of job objects.

        Returns: [{"display": "job → step", "step": "step_name"}, ...]
        """
        failed_steps = []
        for job in jobs:
            if job.get("conclusion") != "failure":
                continue
            for step in job.get("steps", []):
                if step.get("conclusion") == "failure":
                    failed_steps.append({
                        "display": f"{job['name']} → {step['name']}",
                        "step": step["name"],
                    })
        return failed_steps

    def _extract_step_durations(self, jobs):
        """Extract step durations from a list of job objects.

        Returns: [{"name": "step_name", "duration_s": N}, ...]
        Only includes completed steps with valid timestamps.
        """
        steps = []
        for job in jobs:
            if job.get("conclusion") not in ("success", "failure"):
                continue
            for step in job.get("steps", []):
                if step.get("status") != "completed":
                    continue
                started = step.get("started_at", "")
                completed = step.get("completed_at", "")
                if not started or not completed:
                    continue
                try:
                    t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                    dur = max(0, (t1 - t0).total_seconds())
                    steps.append({"name": step["name"], "duration_s": round(dur)})
                except (ValueError, TypeError):
                    pass
        return steps

    def initial_load(self):
        """Load recent history on startup so the jobs table isn't empty.

        Tries loading from the on-disk cache first. If the cache is fresh
        (less than CACHE_MAX_AGE seconds old), uses it and skips the slow
        GitHub API fetch entirely.

        Only populates the JSON API history (recent_jobs) and marks run IDs
        as seen.  Does NOT increment Prometheus counters — those are only
        incremented for genuinely new completions detected during regular
        polling.  This prevents increase() from showing inflated numbers
        after every restart.
        """
        if self._load_cache():
            return

        repos = REPOS_FILTER if REPOS_FILTER else self.get_active_repos()
        for repo in repos:
            try:
                for run in self._fetch_recent_history(repo):
                    run_id = run["id"]
                    self._seen_run_ids.add(run_id)
                    record = self._make_job_record(run, repo)
                    conclusion = run.get("conclusion") or "unknown"

                    # Fetch job-level data for failed steps and step durations
                    jobs = self._fetch_run_jobs(repo, run_id)
                    if jobs:
                        if conclusion == "failure":
                            failed = self._extract_failed_steps(jobs)
                            if failed:
                                record["failed_step"] = "; ".join(
                                    f["display"] for f in failed
                                )
                        record["steps"] = self._extract_step_durations(jobs)

                    self.recent_jobs.appendleft(record)
            except Exception:
                logger.exception("Error loading history for %s", repo)

        logger.info("Initial load: %d jobs in history", len(self.recent_jobs))
        self._save_cache()

    def collect(self):
        repos = REPOS_FILTER if REPOS_FILTER else self.get_active_repos()
        tot_queued = 0
        tot_in_progress = 0
        current_active = []

        for repo in repos:
            try:
                q = self._run_count(repo, "queued")
                ip = self._run_count(repo, "in_progress")

                queued_runs.labels(org=ORG, repo=repo).set(q)
                in_progress_runs.labels(org=ORG, repo=repo).set(ip)
                tot_queued += q
                tot_in_progress += ip

                # Collect active (queued/in_progress) runs for the active list
                if q > 0 or ip > 0:
                    current_active.extend(self._fetch_active_runs(repo))

                # Track newly completed runs
                for run in self._recent_completed(repo):
                    run_id = run["id"]
                    if run_id in self._seen_run_ids:
                        continue
                    self._seen_run_ids.add(run_id)

                    conclusion = run.get("conclusion") or "unknown"
                    workflow_name = run.get("name", "unknown")
                    completed_runs.labels(
                        org=ORG, repo=repo, workflow=workflow_name, conclusion=conclusion
                    ).inc()

                    started = run.get("run_started_at")
                    ended = run.get("updated_at")
                    if started and ended:
                        t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                        t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                        dur = (t1 - t0).total_seconds()
                        if dur > 0:
                            run_duration.labels(
                                org=ORG, repo=repo, conclusion=conclusion
                            ).observe(dur)

                    # Add to recent jobs for JSON API
                    record = self._make_job_record(run, repo)
                    jobs = self._fetch_run_jobs(repo, run_id)
                    if jobs:
                        if conclusion == "failure":
                            failed = self._extract_failed_steps(jobs)
                            if failed:
                                record["failed_step"] = "; ".join(
                                    f["display"] for f in failed
                                )
                                for f in failed:
                                    failed_step_total.labels(
                                        org=ORG, workflow=workflow_name,
                                        step=f["step"]
                                    ).inc()
                        record["steps"] = self._extract_step_durations(jobs)
                    self.recent_jobs.appendleft(record)

            except Exception:
                logger.exception("Error collecting metrics for %s", repo)

        queued_total.labels(org=ORG).set(tot_queued)
        in_progress_total.labels(org=ORG).set(tot_in_progress)

        with self._lock:
            self.active_runs = current_active

        # Prevent unbounded memory growth — keep most recent IDs
        if len(self._seen_run_ids) > 10000:
            # Keep the most recent 5000 IDs. Run IDs are monotonically
            # increasing integers, so sorting preserves chronological order.
            self._seen_run_ids = set(sorted(self._seen_run_ids)[-5000:])

        logger.info(
            "Collected: repos=%d queued=%d in_progress=%d history=%d",
            len(repos),
            tot_queued,
            tot_in_progress,
            len(self.recent_jobs),
        )
        self._save_cache()

    # Maps job_type filter values to GitHub Actions event names
    JOB_TYPE_EVENTS = {
        "periodic":  {"schedule"},
        "presubmit": {"pull_request"},
        "manual":    {"workflow_dispatch"},
    }

    def _parse_grafana_param(self, params, key):
        """Parse a Grafana template variable query param.

        Returns the cleaned value, or None if the value is empty, "All",
        contains unresolved template syntax like "${var}", or has
        trailing colons from Grafana variable quirks.
        """
        raw = params.get(key, [None])[0]
        if not raw:
            return None
        cleaned = raw.strip().rstrip(":").strip()
        if (cleaned.lower() == "all"
                or cleaned == ""
                or "${" in cleaned):
            return None
        return cleaned

    def get_jobs_json(self, params):
        """Return jobs list as JSON, with optional filters.

        Query params:
          status    - filter by conclusion (success, failure, cancelled)
          repo      - filter by repo name
          workflow  - comma-separated workflow name substrings (case-insensitive)
          limit     - max results (default 200)
          active    - include queued/in_progress runs (true/false)
          since     - ISO 8601 timestamp, only return jobs created at or after
          until     - ISO 8601 timestamp, only return jobs created before
          job_type  - periodic, presubmit, or manual
        """
        status_filter = self._parse_grafana_param(params, "status")
        repo_filter = self._parse_grafana_param(params, "repo")
        workflow_filter = params.get("workflow", [None])[0]
        workflow_name_filter = self._parse_grafana_param(params, "workflow_name")
        job_type_filter = self._parse_grafana_param(params, "job_type")
        category_filter = self._parse_grafana_param(params, "category")
        limit = min(int(params.get("limit", [200])[0]), JOBS_HISTORY_SIZE)
        include_active = params.get("active", ["false"])[0].lower() == "true"

        # Parse time-range filters
        since_str = params.get("since", [None])[0]
        until_str = params.get("until", [None])[0]
        since_dt = None
        until_dt = None
        if since_str:
            try:
                since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        if until_str:
            try:
                until_dt = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Parse workflow filter into list of lowercase substrings
        wf_filters = []
        if workflow_filter:
            wf_filters = [w.strip().lower() for w in workflow_filter.split(",") if w.strip()]

        # Resolve job_type to a set of allowed event names
        allowed_events = None
        if job_type_filter:
            allowed_events = self.JOB_TYPE_EVENTS.get(job_type_filter.lower())

        def matches(job):
            if status_filter and job.get("conclusion") != status_filter:
                return False
            if repo_filter and job["repo"] != repo_filter:
                return False
            if category_filter and job.get("category", "").lower() != category_filter.lower():
                return False
            if wf_filters:
                wf_name = job.get("workflow", "").lower()
                if not any(f in wf_name for f in wf_filters):
                    return False
            if (workflow_name_filter
                    and workflow_name_filter.lower()
                    not in job.get("workflow", "").lower()):
                return False
            if allowed_events and job.get("event") not in allowed_events:
                return False
            if since_dt or until_dt:
                created = job.get("created_at", "")
                if created:
                    try:
                        job_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if since_dt and job_dt < since_dt:
                            return False
                        if until_dt and job_dt >= until_dt:
                            return False
                    except (ValueError, TypeError):
                        pass
            return True

        result = []

        # Add completed jobs first — snapshot to avoid concurrent mutation.
        # The limit applies only to completed jobs so active runs never
        # crowd them out.
        jobs_snapshot = list(self.recent_jobs)
        completed_count = 0
        for job in jobs_snapshot:
            if matches(job):
                result.append(job)
                completed_count += 1
                if completed_count >= limit:
                    break

        # Prepend active runs (queued/in_progress) if requested — these are
        # not counted against the limit so they never displace history.
        if include_active:
            active_matched = []
            with self._lock:
                for job in self.active_runs:
                    if matches(job):
                        active_matched.append(job)
            result = active_matched + result

        return result

    def get_counts_by_workflow_json(self, params):
        """Return per-workflow counts, with the same filters as get_jobs_json.

        Returns: [{"workflow": "...", "success": N, "failure": N,
                   "cancelled": N, "total": N, "success_rate": 0.xx}, ...]
        """
        jobs = self.get_jobs_json(params)
        merge = self._parse_grafana_param(params, "merge_similar")
        use_display = not (merge and merge.lower() in ("true", "yes", "1"))
        by_wf = {}
        for job in jobs:
            wf = (job.get("display_name") or job.get("workflow", "unknown")
                  ) if use_display else job.get("workflow", "unknown")
            c = job.get("conclusion") or job.get("status", "unknown")
            if wf not in by_wf:
                by_wf[wf] = {"workflow": wf, "success": 0, "failure": 0,
                             "cancelled": 0, "total": 0}
            by_wf[wf][c] = by_wf[wf].get(c, 0) + 1
            by_wf[wf]["total"] += 1

        result = []
        for wf_data in by_wf.values():
            decisive = wf_data["success"] + wf_data["failure"]
            wf_data["success_rate"] = (
                round(wf_data["success"] / decisive, 4) if decisive > 0 else 0
            )
            result.append(wf_data)
        return sorted(result, key=lambda x: x["workflow"])

    def get_workflows_json(self, params):
        """Return distinct workflow names from the job history.

        Accepts the same filters (workflow substring, since, until) so the
        dropdown only shows workflows relevant to the current view.

        Returns: [{"workflow": "...", "__text": "...", "__value": "..."}, ...]
        The __text/__value keys are for Grafana variable data source compatibility.
        """
        jobs = self.get_jobs_json(params)
        names = sorted(set(j.get("workflow", "unknown") for j in jobs))
        return [{"workflow": n, "__text": n, "__value": n} for n in names]

    def get_failed_steps_json(self, params):
        """Return failure counts by step name for jobs matching the filters.

        Parses the 'failed_step' field (semicolon-separated "job -> step" entries)
        from each matching failed job.

        Returns: [{"step": "step_name", "count": N}, ...] sorted by count desc.
        """
        jobs = self.get_jobs_json(params)
        step_counts = {}
        for job in jobs:
            fs = job.get("failed_step", "")
            if not fs:
                continue
            for entry in fs.split("; "):
                entry = entry.strip()
                if entry:
                    step_counts[entry] = step_counts.get(entry, 0) + 1
        result = [{"step": s, "count": c} for s, c in step_counts.items()]
        return sorted(result, key=lambda x: -x["count"])

    # Maps event names back to human-readable job type labels
    EVENT_TYPE_LABELS = {
        "schedule": "Periodic",
        "pull_request": "Presubmit",
        "workflow_dispatch": "Manual",
    }

    def get_avg_duration_by_type_json(self, params):
        """Return average run duration grouped by job type.

        Returns: [{"job_type": "Periodic", "avg_duration_s": N,
                   "avg_duration": "Xm Ys", "count": N}, ...]
        """
        jobs = self.get_jobs_json(params)
        by_type = {}
        for job in jobs:
            event = job.get("event", "unknown")
            label = self.EVENT_TYPE_LABELS.get(event, event)
            dur = job.get("duration_s", 0)
            if dur <= 0:
                continue
            if label not in by_type:
                by_type[label] = {"total_s": 0, "count": 0}
            by_type[label]["total_s"] += dur
            by_type[label]["count"] += 1

        result = []
        for label, data in sorted(by_type.items()):
            avg = round(data["total_s"] / data["count"]) if data["count"] else 0
            result.append({
                "job_type": label,
                "avg_duration_s": avg,
                "avg_duration": self._fmt_duration(avg),
                "count": data["count"],
            })
        return result

    def get_avg_step_duration_json(self, params):
        """Return average duration per step name across matching jobs.

        Returns: [{"step": "step_name", "avg_duration_s": N,
                   "avg_duration": "Xm Ys", "count": N}, ...]
        sorted by typical execution order (average position in the step list).
        """
        jobs = self.get_jobs_json(params)
        by_step = {}
        step_order = {}  # track average position for ordering
        for job in jobs:
            steps = job.get("steps", [])
            for idx, step in enumerate(steps):
                name = step.get("name", "")
                dur = step.get("duration_s", 0)
                if not name or dur <= 0:
                    continue
                if name not in by_step:
                    by_step[name] = {"total_s": 0, "count": 0}
                    step_order[name] = {"total_idx": 0, "count": 0}
                by_step[name]["total_s"] += dur
                by_step[name]["count"] += 1
                step_order[name]["total_idx"] += idx
                step_order[name]["count"] += 1

        result = []
        for name, data in by_step.items():
            avg = round(data["total_s"] / data["count"]) if data["count"] else 0
            if avg < 5:
                continue  # skip trivial steps (< 5s avg)
            avg_idx = (step_order[name]["total_idx"] /
                       step_order[name]["count"]) if step_order[name]["count"] else 0
            result.append({
                "step": name,
                "avg_duration_s": avg,
                "avg_duration": self._fmt_duration(avg),
                "count": data["count"],
                "_order": avg_idx,
            })
        # Sort by execution order, then remove the internal field
        result.sort(key=lambda x: x["_order"])
        for r in result:
            del r["_order"]
        return result

    def get_counts_json(self, params):
        """Return job counts by conclusion, with the same filters as get_jobs_json.

        Returns: {"success": N, "failure": N, "cancelled": N,
                  "queued": N, "in_progress": N, "total": N,
                  "failure_rate": 0.xx}
        """
        # Reuse the same filtering logic — just count instead of return
        jobs = self.get_jobs_json(params)
        counts = {}
        for job in jobs:
            c = job.get("conclusion") or job.get("status", "unknown")
            counts[c] = counts.get(c, 0) + 1
        total = len(jobs)
        success_count = counts.get("success", 0)
        failure_count = counts.get("failure", 0)
        decisive = success_count + failure_count  # exclude cancelled
        return {
            "success": success_count,
            "failure": failure_count,
            "cancelled": counts.get("cancelled", 0),
            "queued": counts.get("queued", 0),
            "in_progress": counts.get("in_progress", 0),
            "total": total,
            "failure_rate": round(failure_count / decisive, 4) if decisive > 0 else 0,
        }


# ---------------------------------------------------------------------------
# Custom HTTP handler: /metrics + /api/jobs
# ---------------------------------------------------------------------------
class ExporterHandler(BaseHTTPRequestHandler):
    exporter = None  # set after instantiation

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/metrics":
            output = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(output)

        elif parsed.path == "/api/jobs":
            params = parse_qs(parsed.query)
            jobs = self.exporter.get_jobs_json(params)
            payload = json.dumps(jobs, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/counts":
            params = parse_qs(parsed.query)
            # Override limit to max so we count all matching jobs
            params["limit"] = [str(JOBS_HISTORY_SIZE)]
            # Always include active runs in counts so in-progress/queued
            # periodic jobs are reflected in the totals
            params["active"] = ["true"]
            counts = self.exporter.get_counts_json(params)
            payload = json.dumps(counts, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/workflows":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_SIZE)]
            workflows = self.exporter.get_workflows_json(params)
            payload = json.dumps(workflows, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/failed-steps":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_SIZE)]
            steps = self.exporter.get_failed_steps_json(params)
            payload = json.dumps(steps, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/counts-by-workflow":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_SIZE)]
            counts = self.exporter.get_counts_by_workflow_json(params)
            payload = json.dumps(counts, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/avg-duration-by-type":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_SIZE)]
            data = self.exporter.get_avg_duration_by_type_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/avg-step-duration":
            params = parse_qs(parsed.query)
            params["limit"] = [str(JOBS_HISTORY_SIZE)]
            data = self.exporter.get_avg_step_duration_json(params)
            payload = json.dumps(data, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/active":
            with self.exporter._lock:
                active = list(self.exporter.active_runs)
            payload = json.dumps(active, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        elif parsed.path == "/api/categories":
            categories = list(WorkflowExporter.WORKFLOW_CATEGORIES.keys())
            payload = json.dumps(categories, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode())

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        logger.debug("HTTP %s", self.path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not TOKEN:
        logger.error("PRIVATE_GITHUB_TOKEN environment variable is required")
        sys.exit(1)

    exporter = WorkflowExporter()
    ExporterHandler.exporter = exporter

    logger.info("Starting workflow exporter on port %d (poll every %ds)", PORT, POLL_INTERVAL)

    server = HTTPServer(("0.0.0.0", PORT), ExporterHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Load recent job history so the table isn't empty on startup.
    # HTTP server is already running so /metrics and /api/jobs are available
    # (they'll return empty data until this finishes).
    logger.info("Loading recent job history...")
    exporter.initial_load()

    while True:
        try:
            exporter.collect()
        except Exception:
            logger.exception("Collection error")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

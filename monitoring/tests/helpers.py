"""Fakes and record builders shared by the monitoring/scripts unit tests."""

from __future__ import annotations


class FakeResponse:
    """The subset of requests.Response the scripts use."""

    def __init__(self, status=200, payload=None, headers=None, next_url=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload
        self.headers = headers or {}
        self.links = {"next": {"url": next_url}} if next_url else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGitHub:
    """Routes requests.get by URL. A route is the URL without API_URL, and
    its value is a FakeResponse, a list of them (returned in turn, the last
    one repeating), or an exception instance to raise. Unrouted URLs get a
    404."""

    def __init__(self, api_url):
        self.api_url = api_url
        self.routes = {}
        self.calls = []

    def add(self, path, *responses):
        self.routes[path] = list(responses)

    def get(self, url, headers=None, timeout=None):
        path = url.removeprefix(self.api_url)
        self.calls.append(path)
        queue = self.routes.get(path)
        if not queue:
            return FakeResponse(404, text="not routed")
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def count(self, path):
        return self.calls.count(path)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_job(**overrides):
    """A stored job record with sensible defaults, for _upsert_job."""
    job = {
        "id": 1,
        "repo": "osac",
        "workflow": "E2E VMaaS",
        "display_name": "osac / E2E VMaaS",
        "category": "e2e",
        "branch": "main",
        "pr_url": "",
        "pr_display": "",
        "status": "success",
        "conclusion": "success",
        "event": "pull_request",
        "trigger": "PR #1",
        "duration_s": 600,
        "duration": "10m 0s",
        "actor": "dev",
        "url": "https://github.com/x",
        "created_at": "2026-10-10T10:00:00Z",
        "updated_at": "2026-10-10T10:10:00Z",
        "run_number": 1,
        "run_attempt": 1,
        "failed_step": "",
        "failure_reason": None,
        "runner_name": "",
        "steps": [],
    }
    job.update(overrides)
    return job


def make_run(**overrides):
    """A raw GitHub workflow-run object."""
    run = {
        "id": 100,
        "name": "E2E VMaaS",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "feature",
        "pull_requests": [{"number": 7}],
        "actor": {"login": "dev"},
        "html_url": "https://github.com/osac-project/osac/actions/runs/100",
        "created_at": "2026-10-10T10:00:00Z",
        "run_started_at": "2026-10-10T10:00:00Z",
        "updated_at": "2026-10-10T10:30:00Z",
        "run_number": 5,
        "run_attempt": 1,
    }
    run.update(overrides)
    return run


def make_step(name, conclusion="success", start="2026-10-10T10:00:00Z", end="2026-10-10T10:01:00Z"):
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "started_at": start, "completed_at": end}


def make_gh_job(name="e2e", conclusion="success", steps=None, runner="box-runner-01"):
    return {"name": name, "conclusion": conclusion, "steps": steps or [], "runner_name": runner}

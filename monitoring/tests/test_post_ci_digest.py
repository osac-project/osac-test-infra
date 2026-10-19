"""post-ci-digest.py: data gathering, PNG and HTML rendering, Slack upload."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import requests

from helpers import FakeResponse

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def counts(success, failure, rate):
    return {"success": success, "failure": failure, "success_rate": rate}


def infra(infra_n, test_n, steps=()):
    return {"infra_total": infra_n, "test_total": test_n, "total_failures": infra_n + test_n,
            "infra_by_step": [{"step": s, "count": c} for s, c in steps]}


def merge_time(by_repo=(), outliers=(), **extra):
    data = {
        "median_approval_to_merge_display": "2h 0m 0s", "approved_count": 3, "count": 5,
        "avg_retest_count": 1.2, "by_repo": list(by_repo), "approval_outliers": list(outliers),
    }
    data.update(extra)
    return data


def repo(name, seconds):
    return {"repo": name, "median_approval_to_merge_seconds": seconds,
            "median_approval_to_merge_display": f"{seconds}s", "approved_count": 1, "count": 2}


@pytest.fixture
def full_data():
    steps = [("Setup cluster", 4), ("Teardown", 2), ("Fetch secrets", 1), ("Provision", 1)]
    return {
        "periodic_24h": counts(9, 1, 0.9),
        "periodic_72h": counts(6, 4, 0.6),
        "infra_24h": infra(2, 1, steps),
        "infra_72h": infra(3, 3, steps[:1]),
        "periodic_infra_24h": infra(1, 0),
        "periodic_infra_72h": infra(0, 2),
        "merge_time": merge_time(
            [repo("osac", 7200), repo("a-very-long-repository-name-that-overflows", 3600)],
            [{"repo": "osac", "number": n, "approval_to_merge_display": "3d"} for n in range(5)],
            median_queue_wait_display="20m", via_merge_queue_count=4,
        ),
        "merge_time_24h": merge_time(),
        "flake_rate": 0.05,
        "mttr": {"mttr_display": "1h 0m 0s"},
        "top_failing": {"workflow": "E2E CaaS", "failure": 3},
        "jobs_per_pr": {"avg_jobs_per_pr": 2.5, "median_jobs_per_pr": 2, "distinct_prs": 4,
                        "outcomes": [{"outcome": "Success", "count": 6}, {"outcome": "Cancelled", "count": 1},
                                     {"outcome": "Failure", "count": 3}]},
        "now": "2026-10-18 06:00 UTC",
    }


@pytest.fixture
def empty_data():
    return {
        "periodic_24h": counts(0, 0, 0),
        "periodic_72h": counts(0, 0, 0),
        "infra_24h": infra(0, 0),
        "infra_72h": infra(0, 0),
        "periodic_infra_24h": infra(0, 0),
        "periodic_infra_72h": infra(0, 0),
        "merge_time": merge_time([repo("osac", 0)]),
        "merge_time_24h": merge_time(),
        "flake_rate": None,
        "mttr": None,
        "top_failing": None,
        "jobs_per_pr": {"avg_jobs_per_pr": 0, "median_jobs_per_pr": 0, "distinct_prs": 0,
                        "outcomes": [{"outcome": "Success", "count": 0}]},
        "now": "2026-10-18 06:00 UTC",
    }


def test_formatting_helpers(digest):
    assert digest.pct(0.1234) == "12.3%"
    assert digest.pct(None) == "n/a"
    assert digest.status_color(None) == digest.COLOR_GRAY
    assert digest.status_color(0.3) == digest.COLOR_RED
    assert digest.status_color(0.7) == digest.COLOR_YELLOW
    assert digest.status_color(0.95) == digest.COLOR_GREEN
    assert digest.decisive_rate(counts(0, 0, 0)) is None
    assert digest.decisive_rate(counts(3, 1, 0.75)) == 0.75
    assert digest._iso(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "2026-01-02T03:04:05Z"
    assert digest._truncate("short") == "short"
    assert digest._truncate("x" * 30) == "x" * 25 + "…"


def test_get_uses_session(digest, monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse(payload={"ok": 1})

    monkeypatch.setattr(digest._SESSION, "get", fake_get)
    assert digest._get("/api/counts", category="e2e") == {"ok": 1}
    assert calls == [(f"{digest.EXPORTER_URL}/api/counts", {"category": "e2e"})]
    monkeypatch.setattr(digest._SESSION, "get", lambda *a, **k: FakeResponse(500))
    with pytest.raises(requests.exceptions.HTTPError):
        digest._get("/api/counts")


def test_gather_digest_data(digest, monkeypatch):
    calls = []
    responses = {
        "/api/counts": counts(1, 0, 1.0),
        "/api/presubmit-infra-failures": infra(1, 1),
        "/api/pr-merge-time": merge_time(),
        "/api/flake-rate": [{"flaky_passes": 1, "total_successes": 4}, {"flaky_passes": 0, "total_successes": 4}],
        "/api/mttr": {"overall": {"mttr_display": "5m"}},
        "/api/counts-by-workflow": [{"workflow": "a", "failure": 1}, {"workflow": "b", "failure": 4},
                                    {"workflow": "c"}],
        "/api/jobs-per-pr": {"distinct_prs": 0},
    }

    def fake_get(path, **params):
        calls.append((path, params))
        return responses[path]

    monkeypatch.setattr(digest, "_get", fake_get)
    data = digest.gather_digest_data(datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc))
    assert data["flake_rate"] == 0.125
    assert data["mttr"] == {"mttr_display": "5m"}
    assert data["top_failing"]["workflow"] == "b"
    assert data["now"] == "2026-10-18 06:00 UTC"
    assert ("/api/counts", {"job_type": "periodic", "category": "e2e", "since": "2026-10-17T06:00:00Z"}) in calls
    assert ("/api/presubmit-infra-failures",
            {"job_type": "periodic", "category": "e2e", "since": "2026-10-15T06:00:00Z"}) in calls

    responses["/api/flake-rate"] = []
    responses["/api/counts-by-workflow"] = [{"workflow": "a", "failure": 0}]
    assert digest.fetch_overall_flake_rate(datetime.now(timezone.utc)) is None
    assert digest.fetch_top_failing_workflow(datetime.now(timezone.utc)) is None


def test_build_digest_image(digest, full_data, empty_data):
    assert digest.build_digest_image(full_data).startswith(PNG_MAGIC)
    assert digest.build_digest_image(empty_data).startswith(PNG_MAGIC)
    empty_data["merge_time"]["by_repo"] = []
    assert digest.build_digest_image(empty_data).startswith(PNG_MAGIC)


def test_build_digest_html_escapes_payload(digest, full_data):
    full_data["top_failing"]["workflow"] = "</script><script>alert(1)</script>\u2028\u2029"
    html = digest.build_digest_html(full_data).decode("utf-8")
    assert "__DIGEST_DATA_JSON__" not in html
    assert "<script>alert(1)" not in html
    assert "\\u003c/script>" in html
    assert "\u2028" not in html and "\u2029" not in html
    assert "\\u2028\\u2029" in html


class TestUploadFiles:
    @pytest.fixture
    def posts(self, digest, monkeypatch):
        calls = []
        replies = {}

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            reply = replies.get(url, FakeResponse(payload={"ok": True}))
            # A list is a series of answers, returned in turn.
            return reply.pop(0) if isinstance(reply, list) else reply

        monkeypatch.setattr(digest.requests, "post", fake_post)
        monkeypatch.setattr(digest, "SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setattr(digest, "SLACK_CHANNEL_ID", "C123")
        return calls, replies

    def test_uploads_all_files_in_one_message(self, digest, posts):
        calls, replies = posts
        replies["https://slack.com/api/files.getUploadURLExternal"] = [
            FakeResponse(payload={"ok": True, "upload_url": "https://upload", "file_id": "F1"}),
            FakeResponse(payload={"ok": True, "upload_url": "https://upload", "file_id": "F2"}),
        ]
        digest.upload_files([("a.png", b"png", "image/png", "A"), ("b.html", b"html", "text/html", "B")], "hi")
        urls = [c[0] for c in calls]
        assert urls.count("https://upload") == 2
        complete = calls[-1]
        assert complete[0] == "https://slack.com/api/files.completeUploadExternal"
        assert complete[1]["json"] == {"files": [{"id": "F1", "title": "A"}, {"id": "F2", "title": "B"}],
                                       "channel_id": "C123", "initial_comment": "hi"}
        assert complete[1]["headers"]["Authorization"] == "Bearer xoxb-test"

    def test_get_upload_url_error_exits(self, digest, posts):
        _, replies = posts
        replies["https://slack.com/api/files.getUploadURLExternal"] = FakeResponse(
            payload={"ok": False, "error": "invalid_auth"})
        with pytest.raises(SystemExit):
            digest.upload_files([("a.png", b"png", "image/png", "A")], "hi")

    def test_complete_error_exits(self, digest, posts):
        _, replies = posts
        replies["https://slack.com/api/files.completeUploadExternal"] = FakeResponse(
            payload={"ok": False, "error": "channel_not_found"})
        with pytest.raises(SystemExit):
            digest.upload_files([], "hi")


class TestMain:
    @pytest.fixture
    def stubbed(self, digest, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(digest, "gather_digest_data", lambda now: {"now": "x", "n": 1})
        monkeypatch.setattr(digest, "build_digest_image", lambda data: b"png-bytes")
        monkeypatch.setattr(digest, "build_digest_html", lambda data: b"html-bytes")
        uploads = []
        monkeypatch.setattr(digest, "upload_files", lambda files, caption: uploads.append((files, caption)))
        return tmp_path, uploads

    def test_dry_run_writes_previews(self, digest, stubbed, monkeypatch):
        tmp_path, uploads = stubbed
        monkeypatch.setattr(digest, "DRY_RUN", True)
        digest.main()
        assert json.loads((tmp_path / "ci-digest-data.json").read_text()) == {"now": "x", "n": 1}
        assert (tmp_path / "ci-digest-preview.png").read_bytes() == b"png-bytes"
        assert (tmp_path / "ci-digest-preview.html").read_bytes() == b"html-bytes"
        assert uploads == []

    @pytest.mark.parametrize(("token", "channel"), [(None, "C1"), ("xoxb", None)])
    def test_requires_slack_settings(self, digest, stubbed, monkeypatch, token, channel):
        monkeypatch.setattr(digest, "DRY_RUN", False)
        monkeypatch.setattr(digest, "SLACK_BOT_TOKEN", token)
        monkeypatch.setattr(digest, "SLACK_CHANNEL_ID", channel)
        with pytest.raises(SystemExit):
            digest.main()

    def test_posts_to_slack(self, digest, stubbed, monkeypatch):
        _, uploads = stubbed
        monkeypatch.setattr(digest, "DRY_RUN", False)
        monkeypatch.setattr(digest, "SLACK_BOT_TOKEN", "xoxb")
        monkeypatch.setattr(digest, "SLACK_CHANNEL_ID", "C1")
        digest.main()
        files, caption = uploads[0]
        assert [f[0] for f in files] == ["ci-digest.png", "ci-digest-report.html"]
        assert caption.startswith("📊 OSAC CI Daily Digest")

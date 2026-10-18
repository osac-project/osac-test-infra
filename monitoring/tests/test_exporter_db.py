"""SQLite persistence and the startup migrations of workflow-exporter.py."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from helpers import FakeResponse, iso, make_job


def rows(exporter, sql, *args):
    with exporter._db() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def job_row(exporter, job_id):
    return rows(exporter, "SELECT * FROM jobs WHERE id = ?", job_id)[0]


def insert_pr_merge(exporter, **values):
    cols = ", ".join(values)
    marks = ", ".join(f":{c}" for c in values)
    with exporter._db() as conn:
        conn.execute(f"INSERT INTO pr_merges ({cols}) VALUES ({marks})", values)


def test_init_db_creates_schema(exporter):
    cols = {r["name"] for r in rows(exporter, "PRAGMA table_info(jobs)")}
    assert {"id", "failure_reason", "runner_name", "steps_json"} <= cols
    pr_cols = {r["name"] for r in rows(exporter, "PRAGMA table_info(pr_merges)")}
    assert {"queued_at", "via_merge_queue", "approval_to_queue_seconds"} <= pr_cols


def test_init_db_migrates_old_schema(wfe, tmp_path, monkeypatch, github):
    db = tmp_path / "workflow-exporter.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, repo TEXT, workflow TEXT, category TEXT, "
                 "event TEXT, conclusion TEXT, failed_step TEXT, steps_json TEXT, created_at TEXT, "
                 "run_attempt INTEGER)")
    conn.execute("CREATE TABLE pr_merges (id INTEGER PRIMARY KEY, repo TEXT, number INTEGER, merged_at TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(wfe, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(wfe, "DB_FILE", str(db))
    monkeypatch.setattr(wfe, "LEGACY_CACHE_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(wfe, "TOKEN", None)
    exporter = wfe.WorkflowExporter()
    cols = {r["name"] for r in rows(exporter, "PRAGMA table_info(jobs)")}
    assert {"pr_url", "pr_display", "failure_reason", "runner_name"} <= cols
    pr_cols = {r["name"] for r in rows(exporter, "PRAGMA table_info(pr_merges)")}
    assert {"first_approval_at", "queued_at", "retest_count", "via_merge_queue"} <= pr_cols


def test_upsert_job_only_overwrites_newer_attempts(exporter):
    assert exporter._upsert_job(make_job(conclusion="failure", steps=[{"name": "a", "duration_s": 3}]))
    assert json.loads(job_row(exporter, 1)["steps_json"]) == [{"name": "a", "duration_s": 3}]
    assert not exporter._upsert_job(make_job(conclusion="success"))
    assert job_row(exporter, 1)["conclusion"] == "failure"
    assert exporter._upsert_job(make_job(conclusion="success", run_attempt=2))
    assert job_row(exporter, 1)["conclusion"] == "success"


def test_needs_upsert(exporter):
    assert exporter._needs_upsert({"id": 1})
    exporter._upsert_job(make_job(run_attempt=2))
    assert not exporter._needs_upsert({"id": 1, "run_attempt": 2})
    assert exporter._needs_upsert({"id": 1, "run_attempt": 3})


def test_prune_jobs_and_cache_coverage(exporter, wfe, monkeypatch):
    assert exporter.get_cache_coverage() is None
    now = datetime.now(timezone.utc)
    exporter._upsert_job(make_job(id=1, created_at=iso(now - timedelta(days=wfe.JOBS_HISTORY_DAYS + 1))))
    exporter._upsert_job(make_job(id=2, created_at=iso(now - timedelta(days=2))))
    exporter._upsert_job(make_job(id=3, created_at=iso(now - timedelta(days=1))))
    monkeypatch.setattr(wfe, "JOBS_HISTORY_MAX_COUNT", 1)
    exporter._prune_jobs()
    assert [r["id"] for r in rows(exporter, "SELECT id FROM jobs")] == [3]
    assert exporter.get_cache_coverage() == iso(now - timedelta(days=1))


def test_prune_pr_merges(exporter, wfe, monkeypatch):
    now = datetime.now(timezone.utc)
    insert_pr_merge(exporter, id=1, merged_at=iso(now - timedelta(days=wfe.JOBS_HISTORY_DAYS + 1)))
    insert_pr_merge(exporter, id=2, merged_at=iso(now - timedelta(days=2)))
    insert_pr_merge(exporter, id=3, merged_at=iso(now - timedelta(days=1)))
    monkeypatch.setattr(wfe, "JOBS_HISTORY_MAX_COUNT", 1)
    exporter._prune_pr_merges()
    assert [r["id"] for r in rows(exporter, "SELECT id FROM pr_merges")] == [3]


def test_purge_ignored_events(exporter):
    exporter._upsert_job(make_job(id=1, event="dynamic"))
    exporter._upsert_job(make_job(id=2, event="push"))
    exporter._purge_ignored_events_if_needed()
    assert [r["id"] for r in rows(exporter, "SELECT id FROM jobs")] == [2]


def test_recategorize_jobs(exporter):
    exporter._upsert_job(make_job(id=1, workflow="Bump chart", category="ci"))
    exporter._upsert_job(make_job(id=2, workflow="E2E VMaaS", category="e2e"))
    exporter._recategorize_jobs_if_needed()
    assert job_row(exporter, 1)["category"] == "automation"
    assert job_row(exporter, 2)["category"] == "e2e"


def test_reclassify_failure_reasons(exporter):
    exporter._upsert_job(make_job(id=1, conclusion="failure", failed_step="e2e-readiness → check"))
    exporter._upsert_job(make_job(id=2, conclusion="failure", failed_step="", failure_reason="gate"))
    exporter._upsert_job(make_job(id=3, conclusion="failure", failed_step="e2e → Run E2E tests",
                                  failure_reason="infra"))
    exporter._upsert_job(make_job(id=4, conclusion="failure", failed_step="e2e → Checkout"))
    exporter._upsert_job(make_job(id=5, conclusion="failure", category="lint", failed_step="lint → ruff"))
    exporter._upsert_job(make_job(id=6, conclusion="failure", category="ai", failure_reason="ran"))
    exporter._reclassify_failure_reasons_if_needed()
    reasons = {r["id"]: r["failure_reason"] for r in rows(exporter, "SELECT id, failure_reason FROM jobs")}
    assert reasons == {1: "gate", 2: "gate", 3: "test", 4: "infra", 5: "n/a", 6: "ran"}
    exporter._reclassify_failure_reasons_if_needed()  # idempotent


def test_reclassify_ai_status(exporter, wfe):
    step = wfe.WorkflowExporter.AI_DIAGNOSIS_STEP
    exporter._upsert_job(make_job(id=1, category="ai", steps=[{"name": step, "duration_s": 9}]))
    exporter._upsert_job(make_job(id=2, category="ai", steps=[{"name": "Mint token", "duration_s": 1}]))
    exporter._upsert_job(make_job(id=3, category="ai", failure_reason="n/a"))
    exporter._upsert_job(make_job(id=4, category="ai", failure_reason="ran"))
    exporter._upsert_job(make_job(id=5, category="ai"))
    with exporter._db() as conn:
        conn.execute("UPDATE jobs SET steps_json = '{bad' WHERE id = 5")
    exporter._reclassify_ai_status_if_needed()
    reasons = {r["id"]: r["failure_reason"] for r in rows(exporter, "SELECT id, failure_reason FROM jobs")}
    assert reasons == {1: "ran", 2: "skipped", 3: "n/a", 4: "skipped", 5: "skipped"}


def test_clean_hosted_runner_noise(exporter):
    exporter._upsert_job(make_job(id=1, runner_name="GitHub Actions 3, box-runner-01"))
    exporter._upsert_job(make_job(id=2, runner_name="box-runner-02"))
    exporter._clean_hosted_runner_noise_if_needed()
    assert job_row(exporter, 1)["runner_name"] == "box-runner-01"
    assert job_row(exporter, 2)["runner_name"] == "box-runner-02"


def test_migrate_json_cache(exporter, wfe):
    legacy = wfe.LEGACY_CACHE_FILE
    with open(legacy, "w") as f:
        json.dump({"recent_jobs": [
            {"id": 1, "repo": "osac", "workflow": "pre-commit", "run_attempt": 1},
            make_job(id=2),
        ]}, f)
    exporter._migrate_json_cache_if_needed()
    assert not os.path.exists(legacy)
    assert os.path.exists(legacy + ".migrated")
    row = job_row(exporter, 1)
    assert (row["display_name"], row["category"]) == ("osac / pre-commit", "lint")
    assert len(rows(exporter, "SELECT id FROM jobs")) == 2


def test_migrate_json_cache_skips_non_empty_db(exporter, wfe):
    exporter._upsert_job(make_job(id=9))
    with open(wfe.LEGACY_CACHE_FILE, "w") as f:
        json.dump({"recent_jobs": [make_job(id=1)]}, f)
    exporter._migrate_json_cache_if_needed()
    assert os.path.exists(wfe.LEGACY_CACHE_FILE)


def test_migrate_json_cache_survives_bad_file(exporter, wfe):
    with open(wfe.LEGACY_CACHE_FILE, "w") as f:
        f.write("{not json")
    exporter._migrate_json_cache_if_needed()
    assert os.path.exists(wfe.LEGACY_CACHE_FILE)


def test_backfill_pr_data_from_legacy_cache(exporter, wfe):
    exporter._upsert_job(make_job(id=1))
    exporter._upsert_job(make_job(id=2, pr_url="kept", pr_display="#2"))
    migrated = wfe.LEGACY_CACHE_FILE + ".migrated"
    with open(migrated, "w") as f:
        json.dump({"recent_jobs": [
            {"id": 1, "pr_url": "https://pr/1", "pr_display": "#1"},
            {"id": 2, "pr_url": "https://pr/2"},
            {"id": 3},
        ]}, f)
    exporter._backfill_pr_data_from_legacy_cache()
    assert job_row(exporter, 1)["pr_url"] == "https://pr/1"
    assert job_row(exporter, 2)["pr_url"] == "kept"
    assert os.path.exists(migrated + ".pr-backfilled")


def test_backfill_pr_data_survives_bad_file(exporter, wfe):
    migrated = wfe.LEGACY_CACHE_FILE + ".migrated"
    with open(migrated, "w") as f:
        f.write("[")
    exporter._backfill_pr_data_from_legacy_cache()
    assert os.path.exists(migrated)


def test_backfill_pr_approval_data(exporter, wfe, github, monkeypatch):
    exporter._backfill_pr_approval_data_if_needed()  # no token: no-op
    assert github.calls == []
    monkeypatch.setattr(wfe, "TOKEN", "t")
    exporter._backfill_pr_approval_data_if_needed()  # no rows: no-op
    insert_pr_merge(exporter, id=1, repo="osac", number=1, merged_at="2026-10-10T12:00:00Z")
    insert_pr_merge(exporter, id=2, repo="osac", number=2, merged_at="2026-10-10T12:00:00Z")
    insert_pr_merge(exporter, id=3, repo="osac", number=3, merged_at="2026-10-10T12:00:00Z")
    insert_pr_merge(exporter, id=4, repo="osac", number=4, merged_at="bad")
    approved = [{"state": "APPROVED", "user": {"type": "User"}, "submitted_at": "2026-10-10T11:00:00Z"}]
    github.add(f"/repos/{wfe.ORG}/osac/pulls/1/reviews?per_page=100", FakeResponse(payload=approved))
    github.add(f"/repos/{wfe.ORG}/osac/pulls/2/reviews?per_page=100", FakeResponse(payload=[]))
    github.add(f"/repos/{wfe.ORG}/osac/pulls/3/reviews?per_page=100", FakeResponse(500))
    github.add(f"/repos/{wfe.ORG}/osac/pulls/4/reviews?per_page=100", FakeResponse(payload=approved))
    exporter._upsert_job(make_job(id=10, category="e2e", event="pull_request", pr_display="#1"))
    exporter._upsert_job(make_job(id=11, category="e2e", event="pull_request", pr_display="#1"))
    exporter._backfill_pr_approval_data_if_needed()
    got = {r["id"]: r for r in rows(exporter, "SELECT * FROM pr_merges")}
    assert got[1]["first_approval_at"] == "2026-10-10T11:00:00Z"
    assert got[1]["approval_to_merge_seconds"] == 3600
    assert got[1]["retest_count"] == 1
    assert got[2]["first_approval_at"] == ""
    assert got[3]["first_approval_at"] is None
    assert got[4]["approval_to_merge_seconds"] is None

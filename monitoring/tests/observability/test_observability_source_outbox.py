"""The source-facts outbox writer and its ingestion-funnel integration
(W01c): envelope shape, real-filesystem segment semantics (exclusive
append, rotation, caps, restart, per-record fsync durability),
degraded-health containment (disk full, permission, unreadable
directory), the strict JSON boundary (NaN/Infinity, non-string keys,
tuples, cycles, over-deep structures are refused, never normalized),
sanitized and guarded loss-state records, pending-obligation retries
independent of the legacy upsert gate, value-free errors, dedupe and
at-least-once delivery, ignored-event retention with unchanged legacy
rows, window coverage honesty (unknown totals are unknown, never
complete), and a real local-HTTP source-fetch smoke test.

All filesystem operations are real (tmp_path). Disk-full is injected at
the os.write syscall boundary (errno ENOSPC), fsync and directory-listing
faults at os.fsync/os.listdir, and the ignored-chmod case at os.chmod,
because those failures cannot be reproduced portably in CI; production
code always calls the real functions. The HTTP smoke test uses a real
socket and the real requests.get against a local server standing in for
the GitHub API — no credentials, no real GitHub, no live environment.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import pytest
from helpers import FakeGitHub, FakeResponse, make_gh_job, make_run, make_step
from source_outbox_helpers import kinds, of_kind, records, strict_loads

if TYPE_CHECKING:
    import workflow_exporter


def drops(wfe: ModuleType, reason: str) -> float:
    return wfe.source_facts_dropped.labels(reason=reason)._value.get()


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def install_path_tracer(wfe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[int, str], list[str]]:
    """Trace os.open and os.fsync with component-aware absolute paths.

    The producer opens one path component at a time relative to a pinned
    parent descriptor, so a bare open's path is a single component name;
    resolving it against the parent fd's recorded path reconstructs the
    absolute path and proves every open stayed inside the walked chain
    (never an absolute open that a symlinked ancestor could redirect).
    Returns (opened, fsynced): fd -> absolute path, and the fsynced
    absolute paths in call order."""
    opened: dict[int, str] = {}
    fsynced: list[str] = []
    real_open, real_fsync = os.open, os.fsync

    def tracing_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None:
            opened[fd] = str(path)
        else:
            parent = opened.get(dir_fd, f"<{dir_fd}>")
            opened[fd] = f"{parent}/{path}" if parent != "/" else f"/{path}"
        return fd

    def tracing_fsync(fd: int) -> None:
        fsynced.append(opened.get(fd, f"<closed:{fd}>"))
        real_fsync(fd)

    monkeypatch.setattr(wfe.os, "open", tracing_open)
    monkeypatch.setattr(wfe.os, "fsync", tracing_fsync)
    return opened, fsynced


@pytest.fixture
def R(wfe: ModuleType) -> Callable[[str], str]:
    return lambda path: f"/repos/{wfe.ORG}/{path}"


@pytest.fixture
def sf(
    wfe: ModuleType, exporter: workflow_exporter.WorkflowExporter, tmp_path: Path
) -> workflow_exporter.SourceFactsOutbox:
    outbox = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
    exporter._source_facts = outbox
    return outbox


class TestEnvelope:
    def test_shape_version_1(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_run(repo="osac", run=make_run(id=100, run_attempt=2), window="recent_completed")
        rec = records(outbox=sf)[0]
        assert rec["schema"] == "osac.ci-observability.source-facts"
        assert rec["version"] == 1
        assert rec["kind"] == "run"
        assert rec["repo"] == "osac"
        assert rec["run_id"] == 100
        assert rec["run_attempt"] == 2
        assert datetime.strptime(rec["fetched_at"], "%Y-%m-%dT%H:%M:%SZ")
        assert rec["page_complete"] is True
        assert rec["payload"]["window"] == "recent_completed"
        assert rec["payload"]["run"]["id"] == 100

    def test_coverage_envelope_has_no_run_identity_by_default(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        sf.observe_coverage(repo="osac", payload={"window": "active_runs", "complete": True})
        rec = records(outbox=sf)[0]
        assert rec["kind"] == "coverage"
        assert rec["run_id"] is None and rec["run_attempt"] is None
        assert rec["page_complete"] is None

    def test_metric_labels_are_bounded_enums(self, wfe: ModuleType) -> None:
        assert wfe.source_facts_records._labelnames == ("kind",)
        assert wfe.source_facts_dropped._labelnames == ("reason",)


class TestStrictJsonBoundary:
    def dropped_run(self, wfe: ModuleType, tmp_path: Path, bad_value: object) -> workflow_exporter.SourceFactsOutbox:
        """Observe a run carrying one bad value; returns the outbox."""
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        run = make_run()
        run["bad"] = bad_value
        assert not sf.observe_run(repo="osac", run=run, window="recent_completed")
        return sf

    def assert_loss_state(self, sf: workflow_exporter.SourceFactsOutbox, reason: str) -> None:
        # Nothing invalid on disk, and the consumer learns the drop from
        # the outbox itself: one small, strictly-parseable loss record.
        (loss,) = records(outbox=sf)
        assert loss["kind"] == "coverage"
        assert loss["run_id"] == 100
        assert loss["payload"] == {
            "window": "outbox_health",
            "event": "record_dropped",
            "dropped_kind": "run",
            "reason": reason,
        }

    def test_nan_refused_with_durable_loss_state(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        sf = self.dropped_run(wfe, tmp_path, float("nan"))
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_infinity_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        sf = self.dropped_run(wfe, tmp_path, float("inf"))
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_float_overflow_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        # json.loads("1e999") yields inf: a parsed-API response can carry it.
        before = drops(wfe, "serialize_error")
        sf = self.dropped_run(wfe, tmp_path, json.loads("1e999"))
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_nested_non_string_key_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        sf = self.dropped_run(wfe, tmp_path, {"ok": 1, 7: "int key"})
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_tuple_refused_not_normalized_to_a_list(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        sf = self.dropped_run(wfe, tmp_path, (1, 2))
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_over_deep_structure_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        deep: dict = {}
        node = deep
        for _ in range(wfe.SourceFactsOutbox.JSON_MAX_DEPTH + 2):
            node["a"] = {}
            node = node["a"]
        sf = self.dropped_run(wfe, tmp_path, deep)
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_cyclic_structure_refused_without_hanging(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        cyclic: dict = {}
        cyclic["self"] = cyclic
        sf = self.dropped_run(wfe, tmp_path, cyclic)
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_unserializable_object_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        before = drops(wfe, "serialize_error")
        sf = self.dropped_run(wfe, tmp_path, object())
        assert drops(wfe, "serialize_error") == before + 1
        self.assert_loss_state(sf, "serialize_error")

    def test_nan_identity_is_refused_not_lethal(self, wfe: ModuleType, tmp_path: Path) -> None:
        # A NaN run id is refused before any dedupe key exists and stated
        # as a bounded loss record with an explicit unknown identity.
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "invalid_identity")
        assert not sf.observe_run(repo="osac", run=make_run(id=float("nan")), window="recent_completed")
        assert drops(wfe, "invalid_identity") == before + 1
        (loss,) = records(outbox=sf)
        assert loss["run_id"] is None  # explicit unknown, never a faked id
        assert loss["payload"] == {
            "window": "outbox_health",
            "event": "record_dropped",
            "dropped_kind": "run",
            "reason": "invalid_identity",
        }
        # Bounded: no large uncapped fallback.
        (segment,) = (tmp_path / "sf").glob("sf-*.jsonl")
        assert len(segment.read_text()) < 512

    def test_loss_path_sanitizes_and_never_raises(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        # Even a fully unserializable identity cannot raise into collect.
        sf._record_loss_state(kind=object(), repo=object(), run_id=float("nan"), run_attempt=object(), reason=object())
        (loss,) = records(outbox=sf)
        assert loss["repo"] == "unknown"
        assert loss["run_id"] is None and loss["run_attempt"] is None
        assert loss["payload"]["dropped_kind"] == "unknown"
        assert loss["payload"]["reason"] == "unknown"

    def test_loss_path_serialize_failure_only_counts(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "serialize_error")

        def boom(*args: object, **kwargs: object) -> str:
            raise TypeError("injected")

        monkeypatch.setattr(wfe.json, "dumps", boom)
        sf._record_loss_state(
            kind="run", repo="osac", run_id=1, run_attempt=1, reason="serialize_error"
        )  # must not raise
        monkeypatch.undo()
        assert drops(wfe, "serialize_error") == before + 1
        assert records(outbox=sf) == []

    def test_valid_api_shapes_still_pass_after_bad_drops(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = self.dropped_run(wfe, tmp_path, float("nan"))
        assert sf.observe_run(repo="osac", run=make_run(id=7), window="recent_completed")
        recs = records(outbox=sf)
        assert kinds(recs=recs) == ["coverage", "run"]
        assert recs[1]["payload"]["run"]["id"] == 7

    def test_every_persisted_line_parses_strictly(self, wfe: ModuleType, tmp_path: Path) -> None:
        # Mixed good and refused writes: every complete persisted line is
        # strict JSON — no NaN token, no duplicate key, nothing normalized.
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        sf.observe_coverage(repo="osac", payload={"window": "w", "i": 1})
        self.dropped_run(wfe, tmp_path, float("nan"))
        sf.observe_coverage(repo="osac", payload={"window": "w", "i": 2})
        recs = records(outbox=sf)
        assert [
            r["payload"].get("i") for r in recs if r["kind"] == "coverage" and r["payload"].get("window") == "w"
        ] == [1, 2]


class TestIdentityValidation:
    """A malformed identity (a list id, a NaN, a str id, a non-str repo)
    is refused before any seen/pending key is built: the observer never
    raises, nothing is marked seen, and the refused fact is stated as a
    bounded loss record with an explicit unknown identity."""

    def assert_refused(self, sf: workflow_exporter.SourceFactsOutbox, kind: str) -> None:
        # The malformed part becomes an explicit unknown; a valid sibling
        # identity field may be kept, and no id is ever invented.
        (loss,) = records(outbox=sf)
        assert loss["kind"] == "coverage"
        assert loss["run_id"] is None
        assert loss["payload"] == {
            "window": "outbox_health",
            "event": "record_dropped",
            "dropped_kind": kind,
            "reason": "invalid_identity",
        }

    def test_list_valued_run_id_is_contained(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "invalid_identity")
        assert not sf.observe_run(repo="osac", run=make_run(id=[1, 2]), window="recent_completed")
        assert drops(wfe, "invalid_identity") == before + 1
        self.assert_refused(sf, "run")
        assert sf._seen_runs == set()  # nothing marked seen
        # A later valid observation of the same run still writes.
        assert sf.observe_run(repo="osac", run=make_run(id=100), window="recent_completed")
        assert len(of_kind(recs=records(outbox=sf), kind="run")) == 1

    def test_list_valued_attempt_and_bad_repo_are_contained(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert not sf.observe_run(repo="osac", run=make_run(run_attempt=[7]), window="recent_completed")
        assert not sf.observe_run(repo=None, run=make_run(), window="recent_completed")  # type: ignore[arg-type]
        losses = records(outbox=sf)
        assert len(losses) == 2
        # A malformed attempt keeps the valid run id as an explicit unknown
        # attempt; a non-str repo keeps the valid ids with repo "unknown".
        assert losses[0]["run_attempt"] is None and losses[0]["run_id"] == 100
        assert losses[1]["repo"] == "unknown"
        assert losses[1]["run_id"] == 100 and losses[1]["run_attempt"] == 1
        assert all(r["payload"]["reason"] == "invalid_identity" for r in losses)
        assert sf._seen_runs == set()

    def test_list_valued_jobs_run_id_is_contained(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "invalid_identity")
        assert not sf.observe_jobs(
            repo="osac",
            run_id=[1],
            run_attempt=1,
            jobs=[],
            complete=True,  # type: ignore[arg-type]
            window="recent_completed",
            run_status="completed",
        )
        assert drops(wfe, "invalid_identity") == before + 1
        self.assert_refused(sf, "jobs")
        assert sf._seen_attempt_jobs == set()
        assert sf.pending_facts() == []

    def test_run_attempt_and_artifact_entry_points_validate(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        bad_attempt = make_run(id=100, run_attempt=1)
        bad_attempt["run_attempt"] = [2]
        assert not sf.observe_run_attempt(repo="osac", run_id=100, attempt_run=bad_attempt, jobs=[], complete=True)
        assert not sf.observe_artifacts(
            repo="osac",
            run_id=float("nan"),
            run_attempt=[],
            artifacts=[],  # type: ignore[arg-type]
            complete=True,
            window="recent_completed",
        )
        losses = records(outbox=sf)
        assert len(losses) == 2
        assert all(r["payload"]["reason"] == "invalid_identity" for r in losses)
        assert sf._seen_attempt_jobs == set() and sf._seen_artifacts == set()

    def test_malformed_pending_identity_is_contained(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), pending_max_age=3600.0)
        before = drops(wfe, "invalid_identity")
        sf.add_pending(kind="jobs", repo="osac", run_id=[1], run_attempt=1, window="w", run_status="completed")  # type: ignore[arg-type]
        sf.add_pending(kind="run_attempt", repo="osac", run_id=100, run_attempt=[2], window="w")  # type: ignore[arg-type]
        sf.add_pending(kind="artifacts", repo=None, run_id=100, run_attempt=None, window="w")  # type: ignore[arg-type]
        assert drops(wfe, "invalid_identity") == before + 3
        assert sf.pending_facts() == []  # never keyed
        losses = records(outbox=sf)
        assert len(losses) == 3
        assert all(r["payload"]["reason"] == "invalid_identity" for r in losses)

    def test_list_status_with_pending_present_is_contained(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        # A raw list status would make the pending key unhashable and
        # raise inside the observer; with a non-empty pending set the
        # defect is reachable. The refusal is contained: no exception,
        # a bounded loss record, the legacy row stored, the unrelated
        # pending obligation intact, and nothing marked seen.
        sf.new_cycle()
        sf.add_pending(
            kind="jobs", repo="osac", run_id=999, run_attempt=1, window="recent_completed", run_status="completed"
        )
        route = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        run = make_run()
        run["status"] = ["not", "a", "status"]
        assert exporter._process_completed_run("osac", run)
        losses = [r for r in records(outbox=sf) if r["payload"].get("reason") == "invalid_status"]
        assert len(losses) == 1
        assert losses[0]["payload"]["dropped_kind"] == "jobs"
        assert of_kind(recs=records(outbox=sf), kind="jobs") == []
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert [p.run_id for p in sf.pending_facts()] == [999]
        # The refused fact poisons no seen state: a valid observation of
        # the same attempt still writes.
        assert sf.observe_jobs(
            repo="osac",
            run_id=100,
            run_attempt=1,
            jobs=[],
            complete=True,
            window="recent_completed",
            run_status="completed",
        )

    def test_add_pending_dict_status_is_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "invalid_status")
        sf.add_pending(kind="jobs", repo="osac", run_id=1, run_attempt=1, window="w", run_status={"bad": 1})  # type: ignore[arg-type]
        assert drops(wfe, "invalid_status") == before + 1
        assert sf.pending_facts() == []
        (loss,) = records(outbox=sf)
        assert loss["payload"] == {
            "window": "outbox_health",
            "event": "record_dropped",
            "dropped_kind": "jobs",
            "reason": "invalid_status",
        }

    def test_observer_leaves_legacy_state_unchanged(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        route = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run())
        with exporter._db() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        # The refused observations neither raise nor touch legacy state.
        assert not sf.observe_run(repo="osac", run=make_run(id=[1]), window="recent_completed")
        assert not sf.observe_jobs(
            repo="osac",
            run_id=[1],
            run_attempt=1,
            jobs=[],
            complete=True,  # type: ignore[arg-type]
            window="recent_completed",
            run_status="completed",
        )
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == rows
        assert len(of_kind(recs=records(outbox=sf), kind="run")) == 1  # the valid fact stands


class TestDurability:
    def test_each_record_is_fsynced_before_ack(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        calls: list[int] = []
        real_fsync = os.fsync

        def counting_fsync(fd: int) -> None:
            calls.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(wfe.os, "fsync", counting_fsync)
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        segment_fd = sf._segment_fd
        assert segment_fd is not None
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        monkeypatch.undo()
        # First record: the adopted anchor's parent entry, the new root's
        # parent entry, and the root itself at segment open (three
        # directory fsyncs), then the record; later records fsync only
        # themselves — the ack always follows the record's fsync. Closed
        # directory fds may reuse the segment's number, so order — not fd
        # identity — is the check here; the parent-fsync tracing test pins
        # the paths.
        assert len(calls) == 5
        assert calls[3:] == [segment_fd, segment_fd]
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 2]

    def test_record_fsync_failure_is_retryable(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        before = drops(wfe, "fsync_failed")
        real_fsync = os.fsync
        state = {"fail": True}

        def failing(fd: int) -> None:
            if state["fail"]:
                raise OSError(errno.EIO, "injected")
            real_fsync(fd)

        monkeypatch.setattr(wfe.os, "fsync", failing)
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "fsync_failed") == before + 1
        state["fail"] = False
        # Not acked, not marked seen: the next sighting writes it. The
        # un-acked bytes stay in the abandoned segment (at-least-once —
        # the consumer dedups by content), so both copies are on disk.
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 2, 2]

    def test_directory_fsync_failure_refuses_new_segment(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        real_fsync = os.fsync
        calls: list[int] = []

        def first_fails(fd: int) -> None:
            calls.append(fd)
            if len(calls) == 1:
                raise OSError(errno.EIO, "injected")
            real_fsync(fd)

        monkeypatch.setattr(wfe.os, "fsync", first_fails)
        before = drops(wfe, "fsync_failed")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "fsync_failed") == before + 1
        monkeypatch.undo()
        # The never-acked empty segment was removed; the retry creates a
        # fresh one and the record lands.
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]
        segments = sorted((tmp_path / "sf").glob("sf-*.jsonl"))
        assert len(segments) == 1
        assert len(segments[0].read_text().splitlines()) == 1

    def test_parent_entries_are_fsynced_before_first_ack(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A nested new root (two missing ancestors) persists every new
        # directory's parent entry before the first acknowledgement.
        root = tmp_path / "a" / "b" / "sf"
        opened, fsynced = install_path_tracer(wfe, monkeypatch)
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        monkeypatch.undo()
        assert str(tmp_path / "a") in fsynced
        assert str(tmp_path / "a" / "b") in fsynced
        assert str(root) in fsynced
        # Every traced open is one component relative to a pinned parent
        # fd (or the filesystem root): no absolute path open exists that a
        # symlinked ancestor could redirect.
        assert all("/" in path or path == "/" for path in opened.values())
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]

    def test_symlinked_root_refused_external_target_untouched(self, wfe: ModuleType, tmp_path: Path) -> None:
        external = tmp_path / "external"
        external.mkdir()
        (external / "marker").write_text("keep")
        os.chmod(external, 0o755)
        link = tmp_path / "sf-link"
        link.symlink_to(external)
        sf = wfe.SourceFactsOutbox(root=str(link))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        # The external target is neither chmod'ed nor written into.
        assert stat_mode(external) == 0o755
        assert (external / "marker").read_text() == "keep"
        assert list(external.iterdir()) == [external / "marker"]

    def test_replaced_root_is_never_acknowledged(self, wfe: ModuleType, tmp_path: Path) -> None:
        root = tmp_path / "sf"
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        # Rename the root away and put an empty replacement at the path
        # between segment open and the root fsync: the pinned identity
        # refuses to acknowledge into the replacement.
        moved = tmp_path / "sf-moved"
        os.rename(root, moved)
        root.mkdir()
        # Force the next write to open a new segment, as the packet's
        # probe does: the segment is created inside the replacement, and
        # the pinned identity must refuse to fsync or acknowledge it.
        sf._segment_size = sf.max_segment_bytes
        before = drops(wfe, "root_replaced")
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "root_replaced") == before + 1
        assert records(outbox=sf) == []  # the replacement holds no acknowledged record
        assert not sf.observe_coverage(repo="osac", payload={"w": 3})  # still refused
        # The original records survive in the renamed root.
        kept = list(moved.glob("sf-*.jsonl"))
        assert len(kept) == 1
        assert [r["payload"]["w"] for r in records(outbox=wfe.SourceFactsOutbox(root=str(moved)))] == [1]


class TestRootChain:
    """Failure and race branches of the durable root-chain creation."""

    def test_segment_root_fsync_failure_refuses_new_segment(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the root already adopted, the segment-open fsync of the
        # root itself fails (the adoption fsync of the anchor's parent
        # succeeds): the segment is abandoned, this call's own empty file
        # unlinked, and nothing acknowledged.
        root = tmp_path / "sf"
        root.mkdir(mode=0o700)
        opened: dict[int, str] = {}
        real_open, real_fsync = os.open, os.fsync

        def tracing_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is None:
                opened[fd] = str(path)
            else:
                parent = opened.get(dir_fd, f"<{dir_fd}>")
                opened[fd] = f"{parent}/{path}" if parent != "/" else f"/{path}"
            return fd

        def fail_root_fsyncs(fd: int) -> None:
            if opened.get(fd) == str(root):
                raise OSError(errno.EIO, "injected")
            real_fsync(fd)

        monkeypatch.setattr(wfe.os, "open", tracing_open)
        monkeypatch.setattr(wfe.os, "fsync", fail_root_fsyncs)
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "fsync_failed")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "fsync_failed") == before + 1
        assert list(root.glob("sf-*.jsonl")) == []
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]

    def test_segment_create_failure_is_contained(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The root is adopted cleanly first; a later segment create that
        # fails is counted and contained.
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        real_open = os.open
        before = drops(wfe, "permission")

        def deny_segments(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            if sf.SEGMENT_PREFIX in str(path):
                raise OSError(errno.EACCES, "injected")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(wfe.os, "open", deny_segments)
        sf._segment_size = sf.max_segment_bytes  # force a new segment
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "permission") == before + 1
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 2]

    def test_root_open_failure_after_chain_is_contained(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The chain creates the root and fsyncs its entry, then the
        # component open of the root itself (the pin open, relative to
        # the pinned parent) fails: refused, counted, retryable.
        root = tmp_path / "sf"
        real_open = os.open
        state = {"opens": 0}

        def fail_root_component_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            if str(path) == "sf" and flags & os.O_DIRECTORY and dir_fd is not None:
                state["opens"] += 1
                if state["opens"] == 1:  # the post-create pin open fails
                    raise OSError(errno.EACCES, "injected")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(wfe.os, "open", fail_root_component_open)
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 1})

    def test_fchmod_failure_refuses_adoption(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sf"
        root.mkdir()
        os.chmod(root, 0o777)

        def raising_fchmod(fd: int, mode: int) -> None:
            raise OSError(errno.EACCES, "injected")

        monkeypatch.setattr(wfe.os, "fchmod", raising_fchmod)
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        assert stat_mode(root) == 0o777  # untouched

    def test_concurrent_root_creation_is_adopted(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another creator wins the mkdir race: the FileExistsError is
        # tolerated and the component open below verifies the result.
        root = tmp_path / "sf"

        real_mkdir = os.mkdir

        def racing_mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            real_mkdir(path, mode, dir_fd=dir_fd)
            raise FileExistsError(17, "File exists", path)

        monkeypatch.setattr(wfe.os, "mkdir", racing_mkdir)
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert stat_mode(root) == 0o700
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]

    def test_mkdir_failure_refused(self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def denied_mkdir(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
            raise OSError(errno.EACCES, "injected")

        monkeypatch.setattr(wfe.os, "mkdir", denied_mkdir)
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1

    def test_parent_open_failure_refused(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The chain walk opens the parent cleanly and the mkdir succeeds,
        # and then the durability fsync of the new root's entry (through
        # the pinned parent descriptor) fails: refused before any
        # acknowledgement, retryable once the fsync works again.
        root = tmp_path / "sf"
        opened: dict[int, str] = {}
        real_open, real_fsync = os.open, os.fsync
        state = {"failed": False}

        def tracing_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is None:
                opened[fd] = str(path)
            else:
                parent = opened.get(dir_fd, f"<{dir_fd}>")
                opened[fd] = f"{parent}/{path}" if parent != "/" else f"/{path}"
            return fd

        def fail_entry_fsync(fd: int) -> None:
            # The fsync of the anchor holding the newly created root: the
            # new entry's durability flush, distinct from the adoption
            # fsync of the anchor's own parent one level higher.
            if not state["failed"] and opened.get(fd) == str(tmp_path):
                state["failed"] = True
                raise OSError(errno.EIO, "injected")
            real_fsync(fd)

        monkeypatch.setattr(wfe.os, "open", tracing_open)
        monkeypatch.setattr(wfe.os, "fsync", fail_entry_fsync)
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "fsync_failed")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "fsync_failed") == before + 1
        assert root.is_dir()
        assert list(root.glob("sf-*.jsonl")) == []
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]


class TestForeignSegmentPreservation:
    """A root swapped between segment creation and cleanup must never let
    cleanup delete the replacement writer's same-named file: creation and
    cleanup are relative to the pinned parent descriptor, so this writer
    only ever removes the file it created, in the directory it created
    it in."""

    def swap_during_create(
        self, wfe: ModuleType, monkeypatch: pytest.MonkeyPatch, root: Path, moved: Path, replacement: Path
    ) -> None:
        """Patch os.open so the moment this writer creates its exclusive
        segment file, the root is renamed away and `replacement` (with a
        non-empty same-named segment) takes the configured pathname —
        the reviewer's interleave, deterministically at the syscall."""
        real_open = os.open
        state = {"swapped": False}

        def swapping_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if not state["swapped"] and flags & os.O_CREAT and flags & os.O_EXCL and dir_fd is not None:
                state["swapped"] = True
                os.rename(root, moved)
                os.mkdir(root)
                foreign = replacement / "sf-0000000001.jsonl"
                foreign.write_bytes(b'{"foreign":"data","padding":"' + b"B" * 4096 + b'"}\n')
            return fd

        monkeypatch.setattr(wfe.os, "open", swapping_open)

    def test_root_replaced_preserves_foreign_same_name_segment(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sf"
        moved = tmp_path / "sf-moved"
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})  # pin the root identity
        # The next segment creation swaps the root underneath it.
        self.swap_during_create(wfe, monkeypatch, root, moved, root)
        sf._segment_size = sf.max_segment_bytes  # force a new segment
        before = drops(wfe, "root_replaced")
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "root_replaced") == before + 1
        monkeypatch.undo()
        # The foreign file in the replacement is byte-for-byte intact.
        foreign = root / "sf-0000000001.jsonl"
        assert foreign.exists()
        content = foreign.read_bytes()
        assert content.startswith(b'{"foreign":"data"')
        assert len(content) == 4096 + len(b'{"foreign":"data","padding":""}\n')
        # This writer's own empty segment was removed from the renamed
        # directory; only the first (acked) segment remains there.
        assert sorted(moved.glob("sf-*.jsonl")) == [moved / "sf-0000000001.jsonl"]
        assert [r["payload"]["w"] for r in records(outbox=wfe.SourceFactsOutbox(root=str(moved)))] == [1]

    def test_fsync_error_cleanup_preserves_foreign_segment(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ordinary refusal branch under the same interleave: the root
        # swap makes the component walk hit a symlinked replacement
        # (ELOOP), the cleanup runs through the pinned parent, and the
        # replacement's target stays untouched.
        root = tmp_path / "sf"
        moved = tmp_path / "sf-moved"
        external = tmp_path / "external"
        external.mkdir()
        foreign = external / "sf-0000000001.jsonl"
        foreign.write_bytes(b'{"foreign":"data","padding":"' + b"B" * 4096 + b'"}\n')
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        real_open = os.open
        state = {"swapped": False}

        def swapping_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if not state["swapped"] and flags & os.O_CREAT and flags & os.O_EXCL and dir_fd is not None:
                state["swapped"] = True
                os.rename(root, moved)
                os.symlink(external, root)  # no-follow open -> ELOOP
            return fd

        monkeypatch.setattr(wfe.os, "open", swapping_open)
        sf._segment_size = sf.max_segment_bytes
        before_permission = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        # The swapped-in symlinked root is refused by the component walk
        # (ELOOP), which is a permission refusal, not an fsync failure.
        assert drops(wfe, "permission") == before_permission + 1
        monkeypatch.undo()
        # The symlink and its external target are untouched.
        assert root.is_symlink()
        assert foreign.read_bytes() == b'{"foreign":"data","padding":"' + b"B" * 4096 + b'"}\n'
        assert sorted(moved.glob("sf-*.jsonl")) == [moved / "sf-0000000001.jsonl"]

    def test_ordinary_failure_removes_only_own_empty_segment(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No swap at all: a plain fsync failure still cleans exactly this
        # call's own empty file through the pinned parent.
        root = tmp_path / "sf"
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})

        def failing(fd: int) -> None:
            raise OSError(errno.EIO, "injected")

        monkeypatch.setattr(wfe.os, "fsync", failing)
        sf._segment_size = sf.max_segment_bytes
        before = drops(wfe, "fsync_failed")
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "fsync_failed") == before + 1
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 2]


class TestRootChainRetryRepair:
    """Cross-instance directory durability: a directory left behind by a
    failed writer is adopted by a NEW writer (fresh state, no history of
    the creation) only after the missed parent entry is flushed — the
    adoption fsync is unconditional and idempotent, so it needs no
    bookkeeping. Real filesystem, real writer, fd-to-path fsync tracing,
    and device/inode identity to prove adoption rather than recreation."""

    def install_tracer(
        self, wfe: ModuleType, monkeypatch: pytest.MonkeyPatch, fail_paths: set[str]
    ) -> tuple[list[str], dict[str, bool]]:
        """Patch os.fsync to fail (once per path) for `fail_paths` and to
        record the path of every successful fsync; os.open records the
        fd-to-path mapping, resolving each component open against its
        pinned parent fd so traced paths are absolute and prove the walk
        never left the checked chain. Returns (trace, per-path failure
        flags)."""
        opened: dict[int, str] = {}
        traced: list[str] = []
        failed: dict[str, bool] = {path: False for path in fail_paths}
        real_open, real_fsync = os.open, os.fsync

        def tracing_open(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if dir_fd is None:
                opened[fd] = str(path)
            else:
                parent = opened.get(dir_fd, f"<{dir_fd}>")
                opened[fd] = f"{parent}/{path}" if parent != "/" else f"/{path}"
            return fd

        def failing_fsync(fd: int) -> None:
            path = opened.get(fd, f"<closed:{fd}>")
            if path in failed and not failed[path]:
                failed[path] = True
                raise OSError(errno.EIO, "injected")
            traced.append(path)
            real_fsync(fd)

        monkeypatch.setattr(wfe.os, "open", tracing_open)
        monkeypatch.setattr(wfe.os, "fsync", failing_fsync)
        return traced, failed

    def test_failed_root_parent_fsync_new_writer_repairs_before_ack(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First writer: mkdir succeeds, the parent fsync fails — refused,
        # the root stays on disk, nothing acknowledged or marked seen.
        root = tmp_path / "sf"
        _traced, _failed = self.install_tracer(wfe, monkeypatch, {str(tmp_path)})
        first = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "fsync_failed")
        assert not first.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "fsync_failed") == before + 1
        assert root.is_dir()
        assert records(outbox=first) == []
        identity = os.stat(root).st_dev, os.stat(root).st_ino
        monkeypatch.undo()

        # A NEW writer instance — fresh state, empty history — adopts the
        # existing root by inode and flushes the missed parent entry
        # BEFORE the acknowledgement: the repair fsync is the first
        # successful one of the new writer.
        traced2, _ = self.install_tracer(wfe, monkeypatch, set())
        second = wfe.SourceFactsOutbox(root=str(root))
        assert second.observe_coverage(repo="osac", payload={"w": 1})
        monkeypatch.undo()
        assert (os.stat(root).st_dev, os.stat(root).st_ino) == identity
        assert traced2[0] == str(tmp_path)
        assert [r["payload"]["w"] for r in records(outbox=second)] == [1]

    def test_failed_anchor_parent_fsync_new_writer_repairs_before_ack(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deepest existing anchor with missing descendants: the first
        # writer creates x, then y, then fails the fsync of x (y's
        # entry). A NEW writer finds y existing, still creates sf below
        # it — and must flush BOTH the missed x and y (for sf's entry).
        root = tmp_path / "x" / "y" / "sf"
        anchor = tmp_path / "x" / "y"
        _traced, _failed = self.install_tracer(wfe, monkeypatch, {str(tmp_path / "x")})
        first = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "fsync_failed")
        assert not first.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "fsync_failed") == before + 1
        assert anchor.is_dir() and not root.exists()
        identity = os.stat(anchor).st_dev, os.stat(anchor).st_ino
        monkeypatch.undo()

        traced2, _ = self.install_tracer(wfe, monkeypatch, set())
        second = wfe.SourceFactsOutbox(root=str(root))
        assert second.observe_coverage(repo="osac", payload={"w": 1})
        monkeypatch.undo()
        # The anchor is adopted by inode, not recreated; the missed
        # anchor-parent flush (x) and the new descendant entry flush (y)
        # both happen before the record is acknowledged.
        assert (os.stat(anchor).st_dev, os.stat(anchor).st_ino) == identity
        assert str(tmp_path / "x") in traced2
        assert str(anchor) in traced2
        assert [r["payload"]["w"] for r in records(outbox=second)] == [1]

    def test_fresh_adopter_repair_failure_refuses(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A fresh adopter whose own adoption fsync fails must refuse: no
        # acknowledgement, nothing marked seen, the gap counted — until a
        # writer whose fsync works repairs and succeeds.
        root = tmp_path / "sf"
        root.mkdir(mode=0o700)  # a leftover root, creator unknown
        _traced, _failed = self.install_tracer(wfe, monkeypatch, {str(tmp_path)})
        adopter = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "fsync_failed")
        assert not adopter.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "fsync_failed") == before + 1
        assert records(outbox=adopter) == []
        monkeypatch.undo()
        healed = wfe.SourceFactsOutbox(root=str(root))
        assert healed.observe_coverage(repo="osac", payload={"w": 1})
        assert [r["payload"]["w"] for r in records(outbox=healed)] == [1]

    def test_concurrent_adoption_before_creator_fsync(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A creator has run its mkdir but not yet its parent fsync (the
        # window a concurrent adopter can hit): the adopter's own
        # unconditional adoption fsync persists the entry before its ack,
        # regardless of the creator's progress.
        root = tmp_path / "sf"
        os.mkdir(root, 0o700)  # the creator's mkdir, fsync not yet run
        identity = os.stat(root).st_dev, os.stat(root).st_ino
        traced, _ = self.install_tracer(wfe, monkeypatch, set())
        adopter = wfe.SourceFactsOutbox(root=str(root))
        assert adopter.observe_coverage(repo="osac", payload={"w": 1})
        monkeypatch.undo()
        assert (os.stat(root).st_dev, os.stat(root).st_ino) == identity
        assert traced[0] == str(tmp_path)  # the entry the creator owed
        assert [r["payload"]["w"] for r in records(outbox=adopter)] == [1]


class TestIntermediateSymlinkAncestors:
    """A symlink anywhere above the root — not only at the root — must be
    refused: every path component is opened relative to the previous
    component's pinned descriptor with O_NOFOLLOW, so neither adoption of
    an already-existing root nor creation of a missing one can be
    redirected into an external tree, and a later ancestor swap cannot
    leak writes out of the adopted root."""

    def test_existing_root_behind_intermediate_symlink_is_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        # The symlink sits two levels above the configured root, and the
        # root already exists through it, world-readable: the old absolute
        # walk adopted and chmod'ed it. The component walk must refuse
        # before anything outside the configured tree is touched.
        base = tmp_path / "tree"
        outside = base / "outside" / "existing" / "facts"
        outside.mkdir(parents=True)
        os.chmod(outside, 0o755)
        (outside / "unrelated").write_text("keep")
        runtime = base / "runtime"
        runtime.mkdir()
        os.symlink(base / "outside", runtime / "alias")
        root = runtime / "alias" / "existing" / "facts"
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "permission")
        assert not sf.observe_run(repo="osac", run=make_run(), window="recent_completed")
        assert drops(wfe, "permission") == before + 1
        # The external tree is untouched: no chmod, no new entries, no
        # segments, and no false acknowledgement or seen key.
        assert stat_mode(outside) == 0o755
        assert sorted(p.name for p in outside.iterdir()) == ["unrelated"]
        assert sf._seen_runs == set()
        # A genuine safe root still writes afterwards.
        safe = wfe.SourceFactsOutbox(root=str(tmp_path / "safe"))
        assert safe.observe_coverage(repo="osac", payload={"w": 1})
        assert [r["payload"]["w"] for r in records(outbox=safe)] == [1]

    def test_missing_root_behind_intermediate_symlink_creates_nothing_outside(
        self, wfe: ModuleType, tmp_path: Path
    ) -> None:
        # A missing root must not be created through a symlinked
        # ancestor: the refusal fires at the link, before any mkdir.
        base = tmp_path / "tree"
        (base / "outside").mkdir(parents=True)
        runtime = base / "runtime"
        runtime.mkdir()
        os.symlink(base / "outside", runtime / "alias")
        root = runtime / "alias" / "fresh" / "facts"
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        # Nothing was created through the link: outside stays empty and
        # no ancestor below the link exists either.
        assert list((base / "outside").iterdir()) == []
        assert not (runtime / "alias" / "fresh").exists()
        assert sf._seen_runs == set()

    def test_ancestor_swap_after_adoption_is_refused(self, wfe: ModuleType, tmp_path: Path) -> None:
        # After a clean adoption, the root's parent is renamed away and a
        # symlink to an external tree takes its place: every later root
        # open re-walks the components, so the next write is refused
        # instead of leaking through the link, and the adopted records
        # survive under the moved parent.
        base = tmp_path / "tree"
        parent = base / "parent"
        root = parent / "sf"
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        outside = base / "outside"
        outside.mkdir()
        moved = base / "parent-moved"
        os.rename(parent, moved)
        os.symlink(outside, parent)
        before = drops(wfe, "permission")
        sf._segment_size = sf.max_segment_bytes  # force a new segment
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "permission") == before + 1
        assert list(outside.iterdir()) == []
        # The adopted records survive in the moved (still safe) parent.
        assert [r["payload"]["w"] for r in records(outbox=wfe.SourceFactsOutbox(root=str(moved / "sf")))] == [1]


class TestSharedReaders:
    def test_complete_invalid_last_line_fails_the_reader(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        segment = next((tmp_path / "sf").glob("sf-*.jsonl"))
        with segment.open("ab") as handle:
            handle.write(b'{"broken":\n')  # complete line, invalid JSON
        with pytest.raises(ValueError):
            records(outbox=sf)

    def test_incomplete_trailing_line_is_tolerated(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        segment = next((tmp_path / "sf").glob("sf-*.jsonl"))
        with segment.open("ab") as handle:
            handle.write(b'{"broken":')  # a torn write: no trailing newline
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]


class TestWriterFilesystem:
    def test_private_directory_and_segment_modes(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"window": "active_runs", "complete": True})
        root = tmp_path / "sf"
        assert stat_mode(root) == 0o700
        (segment,) = root.glob("sf-*.jsonl")
        assert stat_mode(segment) == 0o600

    def test_exclusive_append_two_writers_never_share_a_segment(self, wfe: ModuleType, tmp_path: Path) -> None:
        root = str(tmp_path / "sf")
        first = wfe.SourceFactsOutbox(root=root)
        first.observe_coverage(repo="osac", payload={"w": 1})
        first.observe_coverage(repo="osac", payload={"w": 2})
        segment_before = next(Path(root).glob("sf-*.jsonl"))
        size_before = segment_before.stat().st_size
        # A second producer on the same root (restart, or a stray process)
        # continues the sequence and never appends to the closed segment.
        second = wfe.SourceFactsOutbox(root=root)
        second.observe_coverage(repo="osac", payload={"w": 3})
        names = [p.name for p in sorted(Path(root).glob("sf-*.jsonl"))]
        assert names[0] == segment_before.name
        assert segment_before.stat().st_size == size_before
        assert len(names) == 2
        assert names[1] > names[0]

    def test_rotation_at_segment_bytes(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), max_segment_bytes=120)
        for i in range(5):
            assert sf.observe_coverage(repo="osac", payload={"window": "w", "i": i})
        segments = sorted((tmp_path / "sf").glob("sf-*.jsonl"))
        # Every record is larger than the cap, so every write rotates.
        assert len(segments) == 5
        assert [p.name for p in segments] == [f"sf-{i:010d}.jsonl" for i in range(1, 6)]
        assert all(len(p.read_text().splitlines()) == 1 for p in segments)

    def test_record_cap_drops_and_does_not_retry(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), max_record_bytes=200)
        before = drops(wfe, "record_too_large")
        assert not sf.observe_run(repo="osac", run=make_run(name="x" * 500), window="recent_completed")
        # Deterministic drop: not retried on the next sighting.
        assert not sf.observe_run(repo="osac", run=make_run(name="x" * 500), window="recent_completed")
        assert drops(wfe, "record_too_large") == before + 1
        # The consumer learns this drop from the outbox itself, durably.
        (loss,) = records(outbox=sf)
        assert loss["payload"] == {
            "window": "outbox_health",
            "event": "record_dropped",
            "dropped_kind": "run",
            "reason": "record_too_large",
        }
        assert loss["run_id"] == 100

    def test_segment_cap_refuses_without_deleting(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), max_segment_bytes=1, max_segments=2)
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        before = drops(wfe, "segment_cap")
        assert not sf.observe_coverage(repo="osac", payload={"w": 3})
        assert drops(wfe, "segment_cap") == before + 1
        # Unacknowledged data is never deleted: both segments and records
        # stay exactly as they were.
        assert len(list((tmp_path / "sf").glob("sf-*.jsonl"))) == 2
        assert len(records(outbox=sf)) == 2

    def test_segment_listing_failure_refuses_to_write(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unreadable directory must not guess the segment cap into a
        # bypass: the optional write is refused, counted, and the existing
        # file is preserved untouched.
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), max_segments=1)
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        real_listdir = os.listdir

        def flaky(path: object) -> list[str]:
            # The listing goes through the pinned root descriptor (an int
            # fd), never a path a swapped symlink could redirect.
            if isinstance(path, int):
                raise OSError(errno.EIO, "injected")
            return real_listdir(path)

        monkeypatch.setattr(wfe.os, "listdir", flaky)
        before_write = drops(wfe, "write_error")
        before_cap = drops(wfe, "segment_cap")
        sf._segment_size = sf.max_segment_bytes  # force a rotation
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "write_error") == before_write + 1
        assert len(list((tmp_path / "sf").glob("sf-*.jsonl"))) == 1  # no second file
        monkeypatch.undo()
        # With the directory listable again, the real cap refuses.
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "segment_cap") == before_cap + 1
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]

    def test_unreadable_directory_refuses_from_the_start(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_listdir = os.listdir

        def flaky(path: object) -> list[str]:
            if isinstance(path, int):
                raise OSError(errno.EIO, "injected")
            return real_listdir(path)

        monkeypatch.setattr(wfe.os, "listdir", flaky)
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "write_error")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "write_error") == before + 1

    def test_restart_re_emits_at_least_once(self, wfe: ModuleType, tmp_path: Path) -> None:
        root = str(tmp_path / "sf")
        first = wfe.SourceFactsOutbox(root=root)
        assert first.observe_run(repo="osac", run=make_run(), window="recent_completed")
        # Dedupe is per-process: after a restart the same fact is written
        # again (at-least-once), into a new segment.
        second = wfe.SourceFactsOutbox(root=root)
        assert second.observe_run(repo="osac", run=make_run(), window="recent_completed")
        runs = of_kind(recs=records(outbox=second), kind="run")
        assert len(runs) == 2
        assert runs[0]["payload"]["run"]["id"] == runs[1]["payload"]["run"]["id"]

    def test_disk_full_is_contained_and_retryable(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        before = drops(wfe, "disk_full")
        real_write = os.write
        failed: list[bool] = []

        def enospc_once(fd: int, data: object) -> int:
            if not failed:
                failed.append(True)
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(wfe.os, "write", enospc_once)
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "disk_full") == before + 1
        monkeypatch.undo()
        # Retryable, not marked seen: the next sighting writes to a fresh
        # segment and the earlier record survives.
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 2]

    def test_torn_write_abandons_the_segment(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), max_segment_bytes=10**9)
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        seg1 = next((tmp_path / "sf").glob("sf-*.jsonl"))
        real_write = os.write
        state = {"torn": False}

        def torn(fd: int, data: object) -> int:
            if not state["torn"]:
                state["torn"] = True
                real_write(fd, data[: len(data) // 2])  # partial, then fail
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(wfe.os, "write", torn)
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 3})
        segments = sorted((tmp_path / "sf").glob("sf-*.jsonl"))
        assert len(segments) == 2
        # The abandoned segment keeps its complete line plus one torn
        # trailing line; the consumer ignores exactly that (records()).
        lines = seg1.read_text().splitlines()
        assert json.loads(lines[0])["payload"]["w"] == 1
        assert len(lines) == 2
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 3]

    def test_unusable_root_is_contained(self, wfe: ModuleType, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        sf = wfe.SourceFactsOutbox(root=str(blocker / "sf"))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        assert not (blocker / "sf").exists()

    def test_non_private_existing_dir_refuses_to_write(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sf"
        root.mkdir()
        os.chmod(root, 0o777)
        # A filesystem that silently ignores chmod: the writer must refuse
        # rather than record user-controlled GitHub strings world-readable.
        # Privacy is enforced through the pinned fd, so fchmod is the
        # call a refusing filesystem ignores.
        monkeypatch.setattr(wfe.os, "fchmod", lambda *args: None)
        sf = wfe.SourceFactsOutbox(root=str(root))
        before = drops(wfe, "permission")
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        assert list(root.iterdir()) == []

    def test_drop_reasons_map_oserror_errnos(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})  # a healthy segment exists
        real_write = os.write
        raised: list[int] = []

        def raise_errno(fd: int, data: object) -> int:
            code = raised.pop(0) if raised else None
            if code is not None:
                raise OSError(code, "injected")
            return real_write(fd, data)

        monkeypatch.setattr(wfe.os, "write", raise_errno)
        before_perm = drops(wfe, "permission")
        before_write = drops(wfe, "write_error")
        raised.append(errno.EACCES)
        assert not sf.observe_coverage(repo="osac", payload={"w": 2})
        assert drops(wfe, "permission") == before_perm + 1
        raised.append(errno.EIO)
        assert not sf.observe_coverage(repo="osac", payload={"w": 3})
        assert drops(wfe, "write_error") == before_write + 1
        monkeypatch.undo()
        # Each failure abandoned its segment; nothing value-bearing was
        # recorded anywhere.
        assert sf.observe_coverage(repo="osac", payload={"w": 4})
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1, 4]

    def test_seen_cap_clears_and_re_emits(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_run(repo="osac", run=make_run(id=1), window="recent_completed")
        # Past its cap the dedupe set clears on the next remember: facts
        # re-emit (at-least-once) instead of observation silently stopping.
        sf._seen_runs.update((i,) for i in range(sf.SEEN_CAP + 1))
        assert sf.observe_run(repo="osac", run=make_run(id=2), window="recent_completed")  # triggers the clear
        assert sf.observe_run(repo="osac", run=make_run(id=1), window="recent_completed")  # re-emitted
        assert len(of_kind(recs=records(outbox=sf), kind="run")) == 3

    def test_seq_collision_skips_to_a_free_segment(self, wfe: ModuleType, tmp_path: Path) -> None:
        root = tmp_path / "sf"
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})  # segment 1 open, seq scanned
        # A concurrent process squats on the next sequence number after
        # this writer already scanned the directory: the exclusive create
        # fails and the writer moves on to a free one.
        (root / "sf-0000000002.jsonl").write_text("{}\n")
        sf._segment_size = sf.max_segment_bytes  # force a rotation
        assert sf.observe_coverage(repo="osac", payload={"w": 2})
        assert sorted(p.name for p in root.glob("sf-*.jsonl")) == [
            "sf-0000000001.jsonl",
            "sf-0000000002.jsonl",
            "sf-0000000003.jsonl",
        ]
        assert json.loads((root / "sf-0000000003.jsonl").read_text())["payload"] == {"w": 2}

    def test_malformed_segment_names_do_not_break_the_sequence(self, wfe: ModuleType, tmp_path: Path) -> None:
        root = tmp_path / "sf"
        root.mkdir()
        (root / "sf-not-a-number.jsonl").write_text("")
        (root / "unrelated.txt").write_text("")
        (root / "sf-0000000007.jsonl").write_text("{}\n")
        sf = wfe.SourceFactsOutbox(root=str(root))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert (root / "sf-0000000008.jsonl").exists()

    def test_segment_open_failure_is_contained(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        real_open = os.open
        before = drops(wfe, "permission")

        def denied(path: object, flags: int, mode: int = 0o777, dir_fd: int | None = None) -> int:
            # Every open of the root component — the walk's probe, the
            # post-create pin, and the re-walks — is refused.
            if str(path) == "sf":
                raise OSError(errno.EACCES, "injected")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(wfe.os, "open", denied)
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        assert drops(wfe, "permission") == before + 1
        monkeypatch.undo()
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        assert len(records(outbox=sf)) == 1


class TestPendingRetry:
    def test_jobs_fact_retried_after_enospc_independent_of_upsert(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        route = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        run = make_run()
        real_write = os.write
        state = {"fail": True}

        def enospc(fd: int, data: object) -> int:
            if state["fail"]:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(wfe.os, "write", enospc)
        # First pass: the legacy row is stored and its metric counted once,
        # but every outbox write fails — the jobs fact becomes pending.
        assert exporter._process_completed_run("osac", run)
        state["fail"] = False  # storage recovered
        # The legacy upsert gate is closed now: reprocessing makes no API
        # call and cannot heal the missing jobs fact by itself.
        calls = len(github.calls)
        assert not exporter._process_completed_run("osac", run)
        assert len(github.calls) == calls
        assert [p.kind for p in sf.pending_facts()] == ["jobs"]
        # The pending obligation retries on the next cycle, re-fetching by
        # identity through the gated budget.
        with exporter._db() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        counter = wfe.completed_runs.labels(
            org=wfe.ORG, repo="osac", workflow=run["name"], conclusion="success"
        )._value.get()
        sf.new_cycle()
        exporter._retry_pending_source_facts()
        (jobs_rec,) = of_kind(recs=records(outbox=sf), kind="jobs")
        assert jobs_rec["run_id"] == 100 and jobs_rec["page_complete"] is True
        assert sf.pending_facts() == []
        # No duplicate legacy rows, counters, or extra API calls: exactly
        # one re-fetch beyond the original.
        assert github.count(route) == 2
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == rows
        assert (
            wfe.completed_runs.labels(org=wfe.ORG, repo="osac", workflow=run["name"], conclusion="success")._value.get()
            == counter
        )

    def drive_collect(self, exporter: workflow_exporter.WorkflowExporter, repos: list[str]) -> None:
        exporter._refresh_pr_map(repos)
        exporter.collect()

    def make_declined_then_recover_world(self, github: FakeGitHub, R: Callable[[str], str], status: str | None) -> str:
        """Route a two-page jobs listing for one in-progress (or None
        status) run, plus the collect scaffolding. The jobs page 2 is the
        tail the budget gate declines; the run then leaves the active
        window, so the only recovery fetch can come from the pending
        retry loop. Returns the page-1 route for call counting."""
        github.add(
            R("osac/actions/runs?status=queued&per_page=100"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        github.add(
            R("osac/actions/runs?status=in_progress&per_page=100"),
            FakeResponse(
                payload={
                    "workflow_runs": [make_run(id=100, status=status if status else "in_progress", conclusion=None)],
                    "total_count": 1,
                }
            ),
        )
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        base = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(
            base, FakeResponse(payload={"jobs": [make_gh_job("j1")]}, next_url=f"https://api.github.com{base}&page=2")
        )
        github.add(f"{base}&page=2", FakeResponse(payload={"jobs": [make_gh_job("j2")]}))
        return base

    def leave_active_window(self, github: FakeGitHub, R: Callable[[str], str]) -> None:
        """The run drops out of the active list: legacy no longer observes
        it, so any later jobs fetch belongs to the pending retry alone."""
        github.add(
            R("osac/actions/runs?status=in_progress&per_page=100"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )

    def assert_decline_then_settled_recovery(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        base: str,
        R: Callable[[str], str],
        wfe: ModuleType,
    ) -> None:
        # Decline cycle: the rate floor (which, unlike the budget, is
        # not reset by collect's new_cycle) declines the tail page; page 1
        # is fetched ungated, the listing is incomplete, and the
        # raw-status obligation is queued.
        exporter._api_rate_remaining = wfe.SOURCE_FACTS_RATE_RESERVE
        self.drive_collect(exporter, ["osac"])
        pending = sf.pending_facts()
        assert len(pending) == 1 and pending[0].kind == "jobs"
        # Normalized once at the add boundary, matching the settle key.
        assert pending[0].run_status == "active"
        assert github.count(base) == 1
        assert github.count(f"{base}&page=2") == 0
        assert [r for r in records(outbox=sf) if r["kind"] == "jobs"] == []

        # The run leaves the window and the rate floor lifts: recovery
        # comes only from the pending retry loop.
        self.leave_active_window(github, R)
        exporter._api_rate_remaining = None
        sf.new_cycle()
        self.drive_collect(exporter, ["osac"])
        assert sf.pending_facts() == []
        (jobs_rec,) = [r for r in records(outbox=sf) if r["kind"] == "jobs"]
        assert jobs_rec["page_complete"] is True
        assert [j["name"] for j in jobs_rec["payload"]["jobs"]] == ["j1", "j2"]
        # One recovery fetch of the full listing: pages 1 and 2, once.
        assert github.count(base) == 2
        assert github.count(f"{base}&page=2") == 1

        # Two later cycles: nothing re-fetched, nothing re-queued, no
        # false expiry for delivered data.
        self.drive_collect(exporter, ["osac"])
        self.drive_collect(exporter, ["osac"])
        assert github.count(base) == 2 and github.count(f"{base}&page=2") == 1
        assert sf.pending_facts() == []
        expired = [
            r for r in records(outbox=sf) if r["kind"] == "coverage" and r["payload"].get("event") == "pending_expired"
        ]
        assert expired == []

    def test_in_progress_declined_jobs_settles_through_collect(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 1)
        base = self.make_declined_then_recover_world(github, R, "in_progress")
        self.assert_decline_then_settled_recovery(sf, exporter, github, base, R, wfe)
        # Legacy untouched: an active run is never upserted or counted.
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    def test_none_status_declined_jobs_settles_through_collect(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 1)
        base = self.make_declined_then_recover_world(github, R, None)
        self.assert_decline_then_settled_recovery(sf, exporter, github, base, R, wfe)

    def test_later_decline_after_retained_active_fact_settles(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # The active fact is retained first; a later decline queues an
        # obligation whose already-seen early path must settle it instead
        # of consuming budget every cycle.
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        exporter._source_facts = sf
        assert sf.observe_jobs(
            repo="osac",
            run_id=100,
            run_attempt=1,
            jobs=[{"name": "e2e"}],
            complete=True,
            window="active_runs",
            run_status="in_progress",
        )
        sf.new_cycle()
        sf.add_pending(
            kind="jobs", repo="osac", run_id=100, run_attempt=1, window="active_runs", run_status="in_progress"
        )
        assert len(sf.pending_facts()) == 1
        # The early already-seen path settles the normalized obligation.
        assert not sf.observe_jobs(
            repo="osac",
            run_id=100,
            run_attempt=1,
            jobs=[{"name": "e2e"}],
            complete=True,
            window="active_runs",
            run_status="in_progress",
        )
        assert sf.pending_facts() == []

    def test_run_fact_enospc_with_jobs_ok_heals_on_next_attempt(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ENOSPC on only the run-fact write: the jobs fact succeeds, the
        # run fact is lost and unqueued. At attempt 2 the history skip
        # must not treat attempt 1 as retained — its run object is
        # refetched and recorded, so the authoritative conclusion is
        # never silently lost.
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        github.add(
            R("osac/actions/runs/100/attempts/2/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        github.add(
            R("osac/actions/runs/100/attempts/1"),
            FakeResponse(payload=make_run(id=100, run_attempt=1, conclusion="failure")),
        )
        real_write = os.write
        state = {"fail": True}

        def enospc_first(fd: int, data: object) -> int:
            if state["fail"]:
                state["fail"] = False
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(wfe.os, "write", enospc_first)
        assert exporter._process_completed_run("osac", make_run(run_attempt=1, conclusion="failure"))
        state["fail"] = False  # storage recovered
        assert of_kind(recs=records(outbox=sf), kind="run") == []
        assert len(of_kind(recs=records(outbox=sf), kind="jobs")) == 1
        assert sf.pending_facts() == []  # run facts never pend: the heal is the skip rule

        counter = wfe.completed_runs.labels(
            org=wfe.ORG, repo="osac", workflow="E2E VMaaS", conclusion="failure"
        )._value.get()
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run(run_attempt=2, conclusion="failure"))
        # Attempt 1's run object was fetched and recorded, not skipped.
        hist = of_kind(recs=records(outbox=sf), kind="run_attempt")
        assert [h["run_attempt"] for h in hist] == [1]
        assert hist[0]["payload"]["run"]["conclusion"] == "failure"
        # The history fetch touched no legacy rows or counters: the run's
        # single row was updated to attempt 2 by the upsert, nothing more.
        with exporter._db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c, MAX(run_attempt) AS a FROM jobs").fetchone()
            assert (row["c"], row["a"]) == (1, 2)
        assert (
            wfe.completed_runs.labels(org=wfe.ORG, repo="osac", workflow="E2E VMaaS", conclusion="failure")._value.get()
            == counter + 1
        )
        # A third attempt skips attempt 1 now that both facts are held.
        github.add(
            R("osac/actions/runs/100/attempts/3/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        sf.new_cycle()
        assert exporter._process_completed_run("osac", make_run(run_attempt=3, conclusion="failure"))
        assert len(of_kind(recs=records(outbox=sf), kind="run_attempt")) == 1

    def test_expired_pending_states_coverage(self, wfe: ModuleType, tmp_path: Path) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), pending_max_age=0.0)
        sf.add_pending(
            kind="jobs", repo="osac", run_id=100, run_attempt=1, window="recent_completed", run_status="completed"
        )
        sf.expire_due_pending()
        assert sf.pending_facts() == []
        (expired,) = records(outbox=sf)
        assert expired["kind"] == "coverage"
        assert expired["run_id"] == 100 and expired["run_attempt"] == 1
        assert expired["payload"] == {"window": "outbox_health", "event": "pending_expired", "pending_kind": "jobs"}

    def test_pending_overflow_expires_the_oldest(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wfe.SourceFactsOutbox, "PENDING_MAX", 2)
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), pending_max_age=3600.0)
        sf.add_pending(kind="jobs", repo="osac", run_id=1, run_attempt=1, window="w", run_status="completed")
        sf.add_pending(kind="jobs", repo="osac", run_id=2, run_attempt=1, window="w", run_status="completed")
        sf.add_pending(kind="jobs", repo="osac", run_id=3, run_attempt=1, window="w", run_status="completed")
        assert [p.run_id for p in sf.pending_facts()] == [2, 3]
        (expired,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert expired["run_id"] == 1
        assert expired["payload"]["pending_kind"] == "jobs"

    def test_collect_drives_pending_retries(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 0)
        # An obligation owed by an earlier cycle.
        sf.new_cycle()
        sf.add_pending(
            kind="jobs", repo="osac", run_id=100, run_attempt=1, window="recent_completed", run_status="completed"
        )
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        exporter.collect()
        assert sf.pending_facts() == []
        (jobs_rec,) = of_kind(recs=records(outbox=sf), kind="jobs")
        assert jobs_rec["run_id"] == 100 and jobs_rec["page_complete"] is True

    def test_write_failures_of_history_and_artifacts_become_pending(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), artifacts_enabled=True, pending_max_age=3600.0)
        real_write = os.write
        state = {"fail": True}

        def enospc(fd: int, data: object) -> int:
            if state["fail"]:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(wfe.os, "write", enospc)
        assert not sf.observe_run_attempt(
            repo="osac", run_id=100, attempt_run=make_run(id=100, run_attempt=1), jobs=[], complete=True
        )
        assert not sf.observe_artifacts(
            repo="osac", run_id=100, run_attempt=None, artifacts=[{"id": 1}], complete=True, window="recent_completed"
        )
        state["fail"] = False
        assert [(p.kind, p.run_attempt) for p in sf.pending_facts()] == [("run_attempt", 1), ("artifacts", None)]
        # Storage recovered: both obligations settle on their next write.
        assert sf.observe_run_attempt(
            repo="osac", run_id=100, attempt_run=make_run(id=100, run_attempt=1), jobs=[], complete=True
        )
        assert sf.observe_artifacts(
            repo="osac", run_id=100, run_attempt=None, artifacts=[{"id": 1}], complete=True, window="recent_completed"
        )
        assert sf.pending_facts() == []

    def test_retry_stops_cleanly_when_the_budget_is_spent(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        sf.new_cycle()
        sf._attempt_calls_left = 0
        sf.add_pending(
            kind="jobs", repo="osac", run_id=100, run_attempt=1, window="recent_completed", run_status="completed"
        )
        calls = len(github.calls)
        exporter._retry_pending_source_facts()
        # No request was made and the obligation survives for a later cycle.
        assert len(github.calls) == calls
        assert [p.kind for p in sf.pending_facts()] == ["jobs"]

    def test_retry_keeps_pending_when_the_refetch_fails(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        sf.new_cycle()
        sf.add_pending(
            kind="jobs", repo="osac", run_id=100, run_attempt=1, window="recent_completed", run_status="completed"
        )
        github.add(R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(500))
        exporter._retry_pending_source_facts()
        assert github.count(R("osac/actions/runs/100/attempts/1/jobs?per_page=30")) == 1
        assert [p.kind for p in sf.pending_facts()] == ["jobs"]
        assert of_kind(recs=records(outbox=sf), kind="jobs") == []

    def test_history_retry_stops_at_each_gate_and_keeps_pending(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        sf.new_cycle()
        sf.add_pending(
            kind="run_attempt", repo="osac", run_id=100, run_attempt=1, window="attempt_history", run_status="completed"
        )
        # Budget 0: the run-object gate stops the loop before any request.
        sf._attempt_calls_left = 0
        calls = len(github.calls)
        exporter._retry_pending_source_facts()
        assert len(github.calls) == calls
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]
        # Budget for the run object only: the jobs-page gate stops it.
        sf.new_cycle()
        sf._attempt_calls_left = 1
        run_route = R("osac/actions/runs/100/attempts/1")
        github.add(run_route, FakeResponse(payload=make_run(run_attempt=1, conclusion="failure")))
        exporter._retry_pending_source_facts()
        assert github.count(run_route) == 1
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]
        # A failed refetch of the run object keeps it pending too.
        sf.new_cycle()
        github.add(run_route, FakeResponse(500))
        exporter._retry_pending_source_facts()
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]
        # Run object refetched but the jobs refetch failing keeps it pending.
        sf.new_cycle()
        github.add(run_route, FakeResponse(payload=make_run(run_attempt=1, conclusion="failure")))
        github.add(R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(500))
        exporter._retry_pending_source_facts()
        assert [p.kind for p in sf.pending_facts()] == ["run_attempt"]

    def test_artifacts_retry_stops_when_the_artifact_budget_is_spent(
        self,
        wfe: ModuleType,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        tmp_path: Path,
    ) -> None:
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"), artifacts_enabled=True)
        exporter._source_facts = sf
        sf.new_cycle()
        sf._artifact_calls_left = 0
        sf.add_pending(kind="artifacts", repo="osac", run_id=100, run_attempt=None, window="recent_completed")
        calls = len(github.calls)
        exporter._retry_pending_source_facts()
        assert len(github.calls) == calls
        assert [p.kind for p in sf.pending_facts()] == ["artifacts"]
        # Budget restored but the refetch failing keeps it pending.
        sf.new_cycle()
        github.add(R("osac/actions/runs/100/artifacts?per_page=100"), FakeResponse(500))
        exporter._retry_pending_source_facts()
        assert [p.kind for p in sf.pending_facts()] == ["artifacts"]

    def test_write_serialize_guard_counts_and_never_raises(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defense in depth behind the strict validator: if json.dumps
        # itself fails, the record is refused, counted, and never raises.
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        before = drops(wfe, "serialize_error")

        def boom(*args: object, **kwargs: object) -> str:
            raise ValueError("injected")

        monkeypatch.setattr(wfe.json, "dumps", boom)
        assert not sf.observe_coverage(repo="osac", payload={"w": 1})
        monkeypatch.undo()
        # Both the record and its loss-state note were refused by the
        # injected dumps failure: counted, never raised, nothing written.
        assert drops(wfe, "serialize_error") == before + 2
        assert records(outbox=sf) == []


class TestIngestionFacts:
    def test_ignored_event_is_a_source_fact_but_never_a_row(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        run = make_run(event="dynamic")
        assert not exporter._process_completed_run("osac", run)
        recs = records(outbox=sf)
        assert kinds(recs=recs) == ["run"]
        assert recs[0]["payload"]["run"]["event"] == "dynamic"
        with exporter._db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        # No jobs fetch was spent on it either.
        assert github.calls == []

    def test_same_attempt_observed_once_per_process(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        route = R("osac/actions/runs/100/attempts/1/jobs?per_page=30")
        github.add(route, FakeResponse(payload={"jobs": [make_gh_job("e2e")]}))
        run = make_run()
        assert exporter._process_completed_run("osac", run)
        assert not exporter._process_completed_run("osac", run)
        assert len(of_kind(recs=records(outbox=sf), kind="run")) == 1
        assert len(of_kind(recs=records(outbox=sf), kind="jobs")) == 1

    def test_active_then_completed_are_separate_observations(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=queued&per_page=100"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        github.add(
            R("osac/actions/runs?status=in_progress&per_page=100"),
            FakeResponse(
                payload={"workflow_runs": [make_run(status="in_progress", conclusion=None)], "total_count": 1}
            ),
        )
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        runs, complete = exporter._fetch_active_runs("osac")
        assert complete and len(runs) == 1
        assert exporter._process_completed_run("osac", make_run())
        assert len(of_kind(recs=records(outbox=sf), kind="run")) == 2
        assert len(of_kind(recs=records(outbox=sf), kind="jobs")) == 2

    def test_collect_resets_budgets_and_records_the_catchup_window(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(wfe, "REPOS_FILTER", ["osac"])
        monkeypatch.setattr(exporter, "_refresh_pr_map", lambda repos: None)
        monkeypatch.setattr(exporter, "_run_count", lambda repo, status: 0)
        exporter.active_runs = [{"id": 20, "repo": "osac"}]
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        github.add(
            R("osac/actions/runs/20"), FakeResponse(payload=make_run(id=20, run_attempt=2, conclusion="failure"))
        )
        github.add(
            R("osac/actions/runs/20/attempts/2/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure", [make_step("Run E2E tests", "failure")])]}),
        )
        github.add(
            R("osac/actions/runs/20/attempts/1"),
            FakeResponse(payload=make_run(id=20, run_attempt=1, conclusion="failure")),
        )
        github.add(
            R("osac/actions/runs/20/attempts/1/jobs?per_page=30"),
            FakeResponse(payload={"jobs": [make_gh_job("e2e", "failure")]}),
        )
        exporter.collect()
        recs = records(outbox=sf)
        assert of_kind(recs=recs, kind="run")[0]["payload"]["window"] == "catchup"
        assert of_kind(recs=recs, kind="jobs")[0]["payload"]["window"] == "catchup"
        assert [h["run_attempt"] for h in of_kind(recs=recs, kind="run_attempt")] == [1]
        # collect() reset the cycle budget; history spent exactly two of it.
        assert sf._attempt_calls_left == wfe.SOURCE_FACTS_ATTEMPT_CALL_BUDGET - 2


class TestCoverage:
    def test_recent_completed_top50_truncation_is_stated(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [make_run() for _ in range(50)], "total_count": 137}),
        )
        assert len(exporter._recent_completed("osac")) == 50
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"] == {"window": "recent_completed", "complete": False, "runs_seen": 50, "runs_total": 137}

    def test_recent_completed_complete_when_the_page_holds_all(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [make_run()], "total_count": 1}),
        )
        exporter._recent_completed("osac")
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"]["complete"] is True

    def test_recent_completed_fetch_failure_is_stated(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(R("osac/actions/runs?status=completed&per_page=50"), FakeResponse(500))
        assert exporter._recent_completed("osac") == []
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"]["event"] == "fetch_failed"
        assert cov["payload"]["complete"] is False

    def test_missing_total_count_is_an_explicit_unknown(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        # A missing total must not be replaced by runs_seen and claimed
        # complete: the window is unknown.
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"), FakeResponse(payload={"workflow_runs": [make_run()]})
        )
        exporter._recent_completed("osac")
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"] == {
            "window": "recent_completed",
            "complete": None,
            "event": "total_unknown",
            "runs_seen": 1,
        }

    def test_malformed_total_count_is_an_explicit_unknown(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=completed&per_page=50"),
            FakeResponse(payload={"workflow_runs": [], "total_count": "many"}),
        )
        assert exporter._recent_completed("osac") == []
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"]["event"] == "total_unknown"
        assert cov["payload"]["complete"] is None

    def test_initial_load_window(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=completed&per_page=100"),
            FakeResponse(payload={"workflow_runs": [make_run()], "total_count": 900}),
        )
        runs = exporter._fetch_recent_history("osac")
        assert len(runs) == 1
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"]["window"] == "initial_load"
        assert cov["payload"]["complete"] is False

    def test_initial_load_missing_total_is_an_explicit_unknown(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=completed&per_page=100"), FakeResponse(payload={"workflow_runs": [make_run()]})
        )
        exporter._fetch_recent_history("osac")
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"] == {"window": "initial_load", "complete": None, "event": "total_unknown", "runs_seen": 1}

    def test_backfill_probe_failure_coverage(
        self, sf: workflow_exporter.SourceFactsOutbox, exporter: workflow_exporter.WorkflowExporter
    ) -> None:
        since = datetime(2026, 10, 1, tzinfo=UTC)
        until = since + timedelta(hours=1)
        assert exporter._backfill_range("osac", since, until) == (0, 0)
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"]["window"] == "backfill_range"
        assert cov["payload"]["complete"] is False
        assert cov["payload"]["event"] == "fetch_failed"

    def test_backfill_page_failure_gap(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add("/p1", FakeResponse(500, text="page broke"))
        assert exporter._backfill_page("osac", "/p1") == (0, 0)
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"] == {
            "window": "backfill_page",
            "complete": False,
            "event": "fetch_failed",
            "pages_fetched": 0,
        }

    def test_active_runs_window_and_ignored_retained(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs?status=queued&per_page=100"),
            FakeResponse(
                payload={
                    "workflow_runs": [make_run(id=1, event="dynamic", status="queued", conclusion=None)],
                    "total_count": 1,
                }
            ),
        )
        github.add(
            R("osac/actions/runs?status=in_progress&per_page=100"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        runs, complete = exporter._fetch_active_runs("osac")
        assert complete and runs == []
        recs = records(outbox=sf)
        # The ignored-event run is retained as a raw fact before the filter.
        assert of_kind(recs=recs, kind="run")[0]["payload"]["run"]["event"] == "dynamic"
        (cov,) = of_kind(recs=recs, kind="coverage")
        assert cov["payload"] == {"window": "active_runs", "complete": True, "runs_seen": 1}

    def test_active_runs_incomplete_window(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(R("osac/actions/runs?status=queued&per_page=100"), FakeResponse(502))
        github.add(
            R("osac/actions/runs?status=in_progress&per_page=100"),
            FakeResponse(payload={"workflow_runs": [], "total_count": 0}),
        )
        assert exporter._fetch_active_runs("osac") == ([], False)
        (cov,) = of_kind(recs=records(outbox=sf), kind="coverage")
        assert cov["payload"]["complete"] is False


class TestSecretSafety:
    def test_failures_never_leak_the_token_or_raw_errors(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
        wfe: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(wfe, "TOKEN", "ghp_SUPERSECRETTOKEN")
        github.add(R("osac/actions/runs?status=completed&per_page=50"), FakeResponse(500, text="internal boom"))
        with caplog.at_level(logging.WARNING):
            assert exporter._recent_completed("osac") == []
        assert "ghp_SUPERSECRETTOKEN" not in caplog.text
        for rec in records(outbox=sf):
            assert "ghp_SUPERSECRETTOKEN" not in json.dumps(rec)

    def test_outbox_records_carry_no_api_headers(
        self,
        sf: workflow_exporter.SourceFactsOutbox,
        exporter: workflow_exporter.WorkflowExporter,
        github: FakeGitHub,
        R: Callable[[str], str],
    ) -> None:
        github.add(
            R("osac/actions/runs/100/attempts/1/jobs?per_page=30"), FakeResponse(payload={"jobs": [make_gh_job("e2e")]})
        )
        assert exporter._process_completed_run("osac", make_run())
        for rec in records(outbox=sf):
            assert "token" not in json.dumps(rec).lower()


class _GitHubishHandler(BaseHTTPRequestHandler):
    """Serves GitHub-shaped JSON for the smoke test's exact URLs over a
    real local socket, including a real Link header that requests parses
    into resp.links."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send_json(self, payload: object, link_next: str | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if link_next:
            self.send_header("Link", f'<{link_next}>; rel="next"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if parsed.path.endswith("/actions/runs/100/attempts/2/jobs"):
            if "page=2" in parsed.query:
                self._send_json({"jobs": [self._job(3)]})
            else:
                self._send_json(
                    {"jobs": [self._job(1), self._job(2)]}, link_next=f"{base}{parsed.path}?per_page=30&page=2"
                )
        elif parsed.path.endswith("/actions/runs/100/attempts/1"):
            self._send_json(make_run(id=100, run_attempt=1, conclusion="failure"))
        elif parsed.path.endswith("/actions/runs/100/attempts/1/jobs"):
            self._send_json({"jobs": [self._job(9, "failure")]})
        else:
            self.send_response(404)
            self.end_headers()

    @staticmethod
    def _job(n: int, conclusion: str = "success") -> dict[str, object]:
        return {
            "name": f"job-{n}",
            "conclusion": conclusion,
            "status": "completed",
            "runner_name": "box-runner-01",
            "started_at": "2026-10-28T10:00:00Z",
            "completed_at": "2026-10-28T10:30:00Z",
            "steps": [
                {
                    "name": f"step-{n}",
                    "conclusion": conclusion,
                    "status": "completed",
                    "started_at": "2026-10-28T10:00:00Z",
                    "completed_at": "2026-10-28T10:30:00Z",
                }
            ],
        }


class TestRealLocalHTTPSmoke:
    """The real exporter flow over a real socket: real requests.get, real
    Link-header pagination, the real flat-script writer on real disk, and
    the real legacy SQLite row — the closest unit-tier approximation of
    the deployed container flow without credentials."""

    @pytest.fixture
    def server(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _GitHubishHandler)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
        httpd.server_close()

    def test_run_ingestion_through_real_http(
        self, wfe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: str
    ) -> None:
        monkeypatch.setattr(wfe, "API_URL", server)
        monkeypatch.setattr(wfe, "CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(wfe, "DB_FILE", str(tmp_path / "workflow-exporter.db"))
        monkeypatch.setattr(wfe, "LEGACY_CACHE_FILE", str(tmp_path / "cache.json"))
        monkeypatch.setattr(wfe, "TOKEN", None)
        monkeypatch.setattr(wfe, "REPOS_FILTER", [])
        exporter = wfe.WorkflowExporter()
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        exporter._source_facts = sf
        sf.new_cycle()

        run = make_run(id=100, run_attempt=2, conclusion="success")
        assert exporter._process_completed_run("osac", run)

        # Legacy behavior intact through the real HTTP path.
        with exporter._db() as conn:
            row = conn.execute("SELECT id, run_attempt, conclusion FROM jobs").fetchone()
        assert dict(row) == {"id": 100, "run_attempt": 2, "conclusion": "success"}

        # The outbox saw the run, both jobs pages (real Link parsing), and
        # attempt 1's terminal state.
        recs = records(outbox=sf)
        assert kinds(recs=recs) == ["run", "jobs", "run_attempt"]
        (jobs_rec,) = of_kind(recs=recs, kind="jobs")
        assert jobs_rec["page_complete"] is True
        assert [j["name"] for j in jobs_rec["payload"]["jobs"]] == ["job-1", "job-2", "job-3"]
        (hist,) = of_kind(recs=recs, kind="run_attempt")
        assert hist["run_attempt"] == 1
        assert hist["payload"]["run"]["conclusion"] == "failure"
        assert hist["payload"]["jobs"][0]["name"] == "job-9"
        # Every written line is strict, complete JSON.
        for segment in sorted((tmp_path / "sf").glob("sf-*.jsonl")):
            for line in segment.read_text().splitlines():
                strict_loads(line=line)


def _scan_keyword_only_gate(source: str) -> list[str]:
    """Shared gate scan: positional data parameters in functions added
    since the accepted BASE commit.

    Names are scope-qualified ("Class.method", "Class.method.nested"), so
    a short legacy name can never grandfather a new method or nested
    function that reuses it. Protocol callbacks are exempt by their
    explicit names only — there is no blanket dunder exemption, so a new
    positional `__init__` is still flagged."""
    import ast

    from source_outbox_helpers import LEGACY_FUNCTION_BASELINE, PROTOCOL_OVERRIDES

    tree = ast.parse(source)
    violations: list[str] = []

    def check(func: ast.FunctionDef, qualified_name: str, is_method: bool) -> None:
        if qualified_name in LEGACY_FUNCTION_BASELINE:
            return
        if func.name in PROTOCOL_OVERRIDES:
            return
        # A method's first parameter is the receiver; a nested function's
        # first parameter is data.
        skip = {"self", "cls"} if is_method else set()
        positional = [a.arg for a in func.args.args if a.arg not in skip]
        if positional:
            violations.append(f"{qualified_name}: positional {positional}")

    def collect(node: ast.AST, prefix: str, is_method: bool) -> None:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                collect(item, f"{prefix}.{node.name}" if prefix else node.name, is_method=True)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = f"{prefix}.{node.name}" if prefix else node.name
            check(node, qualified, is_method)
            for child in ast.iter_child_nodes(node):
                collect(child, qualified, is_method=False)
        else:
            for child in ast.iter_child_nodes(node):
                collect(child, prefix, is_method)

    for node in tree.body:
        collect(node, "", is_method=False)
    return violations


def test_all_new_outbox_and_helper_data_parameters_are_keyword_only(wfe: ModuleType) -> None:
    """AST gate: every function added since the accepted BASE commit must
    take its data arguments keyword-only.

    The legacy baseline is frozen in source_outbox_helpers.LEGACY_FUNCTION_BASELINE
    (derived once from the BASE commit, not from a live git object), so
    the gate works in shallow-clone CI. A method newly added to the
    exporter is automatically covered: it is not in the baseline, so its
    signature must be keyword-only."""
    source = Path(wfe.__file__).read_text(encoding="utf-8")
    violations = _scan_keyword_only_gate(source)
    assert not violations, f"positional data parameters in functions added since BASE: {violations}"


def test_ast_gate_flags_positional_mutants() -> None:
    """The gate must actually detect violations, not merely report zero on
    the current clean source. Three mutants through the same gate: the
    formerly missed `_fetch_run_jobs_paged`, a NEW class `__init__` with
    positional args (no blanket dunder exemption), and a nested function
    named after a legacy unqualified name (scope-qualified matching must
    not grandfather it)."""
    missed = "class _Probe:\n    def _fetch_run_jobs_paged(self, repo, run_id, run_attempt=None):\n        pass\n"
    violations = _scan_keyword_only_gate(missed)
    assert len(violations) == 1
    assert "_Probe._fetch_run_jobs_paged" in violations[0]
    assert "repo" in violations[0]

    # A NEW class constructor with positional args must be flagged.
    constructor = "class _New:\n    def __init__(self, root=None):\n        pass\n"
    violations = _scan_keyword_only_gate(constructor)
    assert len(violations) == 1
    assert "__init__" in violations[0]
    assert "root" in violations[0]

    # A nested function sharing a legacy module-level name ("main") must
    # not be grandfathered by the unqualified form in the baseline.
    nested = (
        "class _Scope:\n    def method(self):\n        def main(data_arg):\n            pass\n        return main\n"
    )
    violations = _scan_keyword_only_gate(nested)
    assert len(violations) == 1
    assert "main" in violations[0]
    assert "data_arg" in violations[0]


class TestStrictHelpers:
    """Regressions for the strict parser and record readers."""

    def test_strict_loads_rejects_duplicate_keys(self) -> None:
        with pytest.raises(ValueError, match="duplicate key"):
            strict_loads(line='{"kind": "run", "kind": "jobs"}')

    def test_strict_loads_rejects_nan(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            strict_loads(line='{"kind": "run", "value": NaN}')

    def test_strict_loads_rejects_infinity(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            strict_loads(line='{"kind": "run", "value": Infinity}')

    def test_of_kind_missing_kind_raises(self) -> None:
        with pytest.raises(KeyError):
            of_kind(recs=[{"payload": {}}], kind="run")

    def test_kinds_missing_kind_raises(self) -> None:
        with pytest.raises(KeyError):
            kinds(recs=[{"payload": {}}])

    def test_records_rejects_malformed_complete_line(self, wfe: ModuleType, tmp_path: Path) -> None:
        """A complete (newline-terminated) line with NaN must fail."""
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        segment = next((tmp_path / "sf").glob("sf-*.jsonl"))
        with segment.open("a") as handle:
            handle.write('{"value": NaN}\n')
        with pytest.raises(ValueError, match="non-finite"):
            records(outbox=sf)

    def test_records_tolerates_only_the_final_torn_line(self, wfe: ModuleType, tmp_path: Path) -> None:
        """A torn trailing write (no newline) is skipped; the complete
        record before it survives."""
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        segment = next((tmp_path / "sf").glob("sf-*.jsonl"))
        with segment.open("a") as handle:
            handle.write('{"torn":')  # no trailing newline
        assert [r["payload"]["w"] for r in records(outbox=sf)] == [1]

    def test_records_rejects_duplicate_key_complete_line(self, wfe: ModuleType, tmp_path: Path) -> None:
        """A complete line with duplicate keys must fail through records()."""
        sf = wfe.SourceFactsOutbox(root=str(tmp_path / "sf"))
        assert sf.observe_coverage(repo="osac", payload={"w": 1})
        segment = next((tmp_path / "sf").glob("sf-*.jsonl"))
        with segment.open("a") as handle:
            handle.write('{"a": 1, "a": 2}\n')
        with pytest.raises(ValueError, match="duplicate key"):
            records(outbox=sf)

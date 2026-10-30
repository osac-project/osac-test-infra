"""Shared readers for the W01c source-outbox test files.

One owned helper for its two real callers (the pagination and outbox test
files), not a framework: strict JSON parsing of one persisted line and the
segment-order record reader, plus the small kind filters. Every complete
persisted line must parse strictly — any JSON constant (NaN/Infinity) or
duplicate object key is a failure — and only an incomplete trailing line
without its newline, a torn write, is tolerated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import workflow_exporter


# Qualified function and method names present in workflow-exporter.py at
# the accepted BASE commit a5b99d1, derived once with a scope-aware AST
# walk and frozen here so the keyword-only gate needs no git objects or
# network access in CI. Names keep their full scope path ("Class.method",
# "Class.method.nested") so a short legacy name cannot grandfather a new
# method or nested function that reuses it.
LEGACY_FUNCTION_BASELINE: frozenset[str] = frozenset(
    (
        "ExporterHandler.do_GET",
        "ExporterHandler.log_message",
        "WorkflowExporter.__init__",
        "WorkflowExporter._apply_ai_status",
        "WorkflowExporter._backfill_missing_pr_data",
        "WorkflowExporter._backfill_page",
        "WorkflowExporter._backfill_pr_approval_data_if_needed",
        "WorkflowExporter._backfill_pr_data_from_legacy_cache",
        "WorkflowExporter._backfill_queue_data_if_needed",
        "WorkflowExporter._backfill_range",
        "WorkflowExporter._categorize_workflow",
        "WorkflowExporter._category_of_run",
        "WorkflowExporter._classify_failure_reason",
        "WorkflowExporter._clean_hosted_runner_noise_if_needed",
        "WorkflowExporter._count_e2e_retests",
        "WorkflowExporter._db",
        "WorkflowExporter._extract_failed_steps",
        "WorkflowExporter._extract_merge_queue_pr",
        "WorkflowExporter._extract_runner_names",
        "WorkflowExporter._extract_step_durations",
        "WorkflowExporter._extract_trigger",
        "WorkflowExporter._fetch_active_runs",
        "WorkflowExporter._fetch_first_approval",
        "WorkflowExporter._fetch_recent_history",
        "WorkflowExporter._fetch_run_jobs",
        "WorkflowExporter._fmt_duration",
        "WorkflowExporter._get",
        "WorkflowExporter._init_db",
        "WorkflowExporter._is_gate_job",
        "WorkflowExporter._is_gate_name",
        "WorkflowExporter._is_gate_only_failure",
        "WorkflowExporter._is_orchestration_job",
        "WorkflowExporter._is_plumbing",
        "WorkflowExporter._is_suite_job",
        "WorkflowExporter._is_zombie_run",
        "WorkflowExporter._jobs_per_pr_bucket",
        "WorkflowExporter._lookup_pr",
        "WorkflowExporter._lookup_queued_at",
        "WorkflowExporter._make_job_record",
        "WorkflowExporter._migrate_json_cache_if_needed",
        "WorkflowExporter._needs_upsert",
        "WorkflowExporter._normalize_iso",
        "WorkflowExporter._normalize_pipeline_rows_if_needed",
        "WorkflowExporter._parse_grafana_param",
        "WorkflowExporter._parse_limit",
        "WorkflowExporter._percentile",
        "WorkflowExporter._pipeline_workflow_name",
        "WorkflowExporter._process_completed_run",
        "WorkflowExporter._prune_jobs",
        "WorkflowExporter._prune_pr_merges",
        "WorkflowExporter._purge_ignored_events_if_needed",
        "WorkflowExporter._recategorize_jobs_if_needed",
        "WorkflowExporter._recent_completed",
        "WorkflowExporter._reclassify_ai_status_if_needed",
        "WorkflowExporter._reclassify_failure_reasons_if_needed",
        "WorkflowExporter._refine_pipeline_category",
        "WorkflowExporter._refresh_pr_map",
        "WorkflowExporter._row_to_job",
        "WorkflowExporter._run_count",
        "WorkflowExporter._seconds_between",
        "WorkflowExporter._suite_of",
        "WorkflowExporter._suite_records",
        "WorkflowExporter._suite_result",
        "WorkflowExporter._update_rate_limit",
        "WorkflowExporter._upsert_job",
        "WorkflowExporter._upsert_job_and_suites",
        "WorkflowExporter._upsert_pr_merge",
        "WorkflowExporter.backfill",
        "WorkflowExporter.collect",
        "WorkflowExporter.get_active_repos",
        "WorkflowExporter.get_avg_duration_by_type_json",
        "WorkflowExporter.get_avg_step_duration_json",
        "WorkflowExporter.get_cache_coverage",
        "WorkflowExporter.get_counts_by_workflow_json",
        "WorkflowExporter.get_counts_json",
        "WorkflowExporter.get_failed_steps_json",
        "WorkflowExporter.get_flake_rate_json",
        "WorkflowExporter.get_jobs_json",
        "WorkflowExporter.get_jobs_json.matches_active",
        "WorkflowExporter.get_jobs_per_pr_json",
        "WorkflowExporter.get_merge_queue_metrics_json",
        "WorkflowExporter.get_merge_queue_metrics_json.percentile_stats",
        "WorkflowExporter.get_mttr_json",
        "WorkflowExporter.get_pr_merge_time_json",
        "WorkflowExporter.get_pr_merge_time_json.avg",
        "WorkflowExporter.get_pr_merge_time_json.median",
        "WorkflowExporter.get_pr_merge_time_json.stats",
        "WorkflowExporter.get_presubmit_infra_failures_json",
        "WorkflowExporter.get_repos",
        "WorkflowExporter.get_workflows_json",
        "WorkflowExporter.initial_load",
        "main",
    )
)

# Framework callbacks whose callable shape is fixed by the framework, not
# by us. Everything else that is new since BASE must be keyword-only.
PROTOCOL_OVERRIDES: frozenset[str] = frozenset(("log_message", "do_GET"))


def strict_loads(*, line: str) -> dict[str, object]:
    """Parse one persisted line the way the documented consumer contract
    requires: any JSON constant (NaN/Infinity) or duplicate object key is
    a failure, never silently accepted."""

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite constant: {value}")

    def no_dup_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate key: {key}")
            seen.add(key)
        return dict(pairs)

    return json.loads(line, parse_constant=reject_constant, object_pairs_hook=no_dup_keys)


def records(*, outbox: workflow_exporter.SourceFactsOutbox) -> list[dict[str, object]]:
    """All envelopes written so far, in segment/write order, parsed
    strictly. A complete line ends with a newline and must parse; the only
    tolerated incomplete line is a torn trailing write without its
    newline. A complete invalid line fails the reader."""
    root = Path(outbox.root)
    if not root.exists():
        return []
    out: list[dict[str, object]] = []
    for segment in sorted(root.glob("sf-*.jsonl")):
        data = segment.read_bytes()
        lines = data.split(b"\n")
        if data and not data.endswith(b"\n"):
            lines = lines[:-1]  # torn trailing write: not a complete record
        elif lines and lines[-1] == b"":
            lines = lines[:-1]  # the structural empty after the final newline
        for raw in lines:
            out.append(strict_loads(line=raw.decode("utf-8")))
    return out


def of_kind(*, recs: list[dict[str, object]], kind: str) -> list[dict[str, object]]:
    return [r for r in recs if r["kind"] == kind]


def kinds(*, recs: list[dict[str, object]]) -> list[str]:
    return [r["kind"] for r in recs]

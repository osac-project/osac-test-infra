"""Tests for the pilot dashboard generator: human reading flow, minutes
display, stat-card targets, query shape, data fidelity, and security."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monitoring.ci_observability.pilot_dashboard import (
    DASHBOARD_UID,
    DATASOURCE_UID,
    INFINITY_TYPE,
    SnapshotError,
    build_dashboard,
    main,
)
from monitoring.ci_observability.pilot_report import build_report


def snapshot() -> dict[str, object]:
    return {
        "schema": "osac.ci-observability.pilot",
        "version": 1,
        "fetched_at": "2026-10-28T08:00:00Z",
        "repository": "osac-project/osac",
        "run": {
            "id": 40548825479,
            "attempt": 1,
            "name": "E2E VMaaS Full Install",
            "event": "schedule",
            "status": "completed",
            "conclusion": "failure",
            "head_sha": "abc123def456789012345678901234567890abcd",
            "created_at": "2026-10-27T02:07:00Z",
            "started_at": "2026-10-27T02:07:01Z",
            "updated_at": "2026-10-27T03:45:00Z",
            "url": "https://github.com/osac-project/osac/actions/runs/40548825479",
            "workflow_path": ".github/workflows/e2e-vmaas-full-install.yml",
        },
        "jobs": [
            {
                "id": 12345678,
                "name": "e2e-vmaas-full-install / e2e",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2026-10-27T02:08:00Z",
                "completed_at": "2026-10-27T03:44:00Z",
                "url": "https://github.com/osac-project/osac/actions/runs/40548825479/job/12345678",
                "steps": [
                    {
                        "number": 1,
                        "name": "Set up job",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-10-27T02:08:00Z",
                        "completed_at": "2026-10-27T02:08:05Z",
                    },
                    {
                        "number": 2,
                        "name": "Run E2E tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "started_at": "2026-10-27T02:10:00Z",
                        "completed_at": "2026-10-27T03:40:00Z",
                    },
                    {
                        "number": 3,
                        "name": "Post Run E2E tests",
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-10-27T03:40:00Z",
                        "completed_at": "2026-10-27T03:41:00Z",
                    },
                ],
            }
        ],
        "artifacts": [
            {
                "id": 9876543,
                "name": "osac-logs-full-install",
                "size_bytes": 12345678,
                "expired": False,
                "url": "https://github.com/osac-project/osac/actions/runs/40548825479/artifacts/9876543",
            }
        ],
        "coverage": {
            "jobs_complete": True,
            "artifacts_complete": False,
            "test_results": "not-collected",
            "tested_revision": "unverified",
            "notes": ["Snapshot taken from the GitHub Actions API only."],
        },
    }


def dashboard() -> dict[str, object]:
    return build_dashboard(snapshot=snapshot())


def report() -> dict[str, object]:
    return build_report(snapshot=snapshot())


def table_targets(*, dashboard_data: dict[str, object]) -> list[dict[str, object]]:
    return [target for panel in dashboard_data["panels"] if panel["type"] == "table" for target in panel["targets"]]


def stat_panels(*, dashboard_data: dict[str, object]) -> list[dict[str, object]]:
    return [panel for panel in dashboard_data["panels"] if panel["type"] == "stat"]


def get_panel(*, dashboard_data: dict[str, object], title: str) -> dict[str, object]:
    return next(panel for panel in dashboard_data["panels"] if panel["title"] == title)


def test_dashboard_identity_is_read_only_and_tagged() -> None:
    d = dashboard()
    assert d["uid"] == DASHBOARD_UID == "ci-run-pilot"
    assert d["editable"] is False
    assert d["tags"] == ["ci-pilot"]
    assert d["refresh"] is False


def test_time_window_comes_from_actual_source_times() -> None:
    d = dashboard()
    assert d["time"]["from"] == snapshot()["run"]["created_at"]
    assert d["time"]["to"] == snapshot()["run"]["updated_at"]


def test_every_table_target_uses_proven_inline_backend_shape() -> None:
    targets = table_targets(dashboard_data=dashboard())
    assert len(targets) >= 2
    for target in targets:
        assert target["type"] == "json"
        assert target["source"] == "inline"
        assert target["format"] == "table"
        assert target["parser"] == "backend"
        assert target["root_selector"] == ""
        assert target["url"] == ""
        assert target["datasource"]["type"] == INFINITY_TYPE
        assert target["datasource"]["uid"] == DATASOURCE_UID


def test_every_stat_panel_has_actual_inline_target_with_one_row() -> None:
    d = dashboard()
    stats = stat_panels(dashboard_data=d)
    assert len(stats) == 4
    for panel in stats:
        assert panel.get("datasource") is not None
        assert panel["datasource"]["uid"] == DATASOURCE_UID
        targets = panel.get("targets", [])
        assert len(targets) == 1
        rows = json.loads(targets[0]["data"])
        assert len(rows) == 1
        assert "value" in rows[0]


def test_stat_reduce_options_fields_matches_column_text() -> None:
    """The reduceOptions field selector must match the Infinity column's
    text, because Infinity names the frame column by column.text."""
    d = dashboard()
    for panel in stat_panels(dashboard_data=d):
        target = panel["targets"][0]
        columns = target["columns"]
        assert len(columns) == 1
        column_text = columns[0]["text"]
        fields = panel["options"]["reduceOptions"]["fields"]
        assert fields == column_text, f"panel {panel['title']}: fields={fields!r} != column text={column_text!r}"


def test_stat_card_values_match_derived_report_facts() -> None:
    rep = report()
    d = dashboard()
    stats = {p["title"]: p for p in stat_panels(dashboard_data=d)}

    result_rows = json.loads(stats["GitHub result"]["targets"][0]["data"])
    assert result_rows[0]["value"] == "Failed"

    span_rows = json.loads(stats["Job span"]["targets"][0]["data"])
    expected_span = rep["summary"]["job_span_minutes"]
    assert expected_span is not None
    assert span_rows[0]["value"] == f"{expected_span:.2f} min"

    fj = json.loads(stats["Failed jobs"]["targets"][0]["data"])
    assert fj[0]["value"] == f"{rep['summary']['failed_job_count']} / {rep['summary']['job_count']}"
    fs = json.loads(stats["Failed steps"]["targets"][0]["data"])
    assert fs[0]["value"] == f"{rep['summary']['failed_step_count']} / {rep['summary']['step_count']}"


def test_result_card_running_at_snapshot() -> None:
    snap = snapshot()
    snap["run"]["status"] = "in_progress"
    snap["run"]["conclusion"] = None
    d = build_dashboard(snapshot=snap)
    stats = stat_panels(dashboard_data=d)
    result = next(p for p in stats if p["title"] == "GitHub result")
    rows = json.loads(result["targets"][0]["data"])
    assert rows[0]["value"] == "Running at snapshot"


def test_job_span_null_with_incomplete_coverage_shows_unknown() -> None:
    snap = snapshot()
    snap["jobs"][0]["completed_at"] = None
    snap["coverage"]["jobs_complete"] = False
    d = build_dashboard(snapshot=snap)
    stats = stat_panels(dashboard_data=d)
    span = next(p for p in stats if p["title"] == "Job span")
    rows = json.loads(span["targets"][0]["data"])
    assert rows[0]["value"] == "Unknown"


def test_job_span_null_with_complete_coverage_shows_not_finished() -> None:
    snap = snapshot()
    snap["jobs"][0]["status"] = "in_progress"
    snap["jobs"][0]["completed_at"] = None
    snap["coverage"]["jobs_complete"] = True
    d = build_dashboard(snapshot=snap)
    stats = stat_panels(dashboard_data=d)
    span = next(p for p in stats if p["title"] == "Job span")
    rows = json.loads(span["targets"][0]["data"])
    assert rows[0]["value"] == "Not finished"


def test_measured_zero_duration_stays_zero() -> None:
    snap = snapshot()
    snap["jobs"][0]["steps"][0]["started_at"] = "2026-10-27T02:08:00Z"
    snap["jobs"][0]["steps"][0]["completed_at"] = "2026-10-27T02:08:00Z"
    d = build_dashboard(snapshot=snap)
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    rows = json.loads(step_panel["targets"][0]["data"])
    zero_rows = [r for r in rows if r["step"] == "Set up job"]
    assert len(zero_rows) == 1
    assert zero_rows[0]["duration_min"] == 0.0


def test_incomplete_job_coverage_counts_are_neutral() -> None:
    snap = snapshot()
    snap["coverage"]["jobs_complete"] = False
    snap["jobs"][0]["conclusion"] = "success"
    d = build_dashboard(snapshot=snap)
    fj = next(p for p in stat_panels(dashboard_data=d) if p["title"] == "Failed jobs")
    color = fj["fieldConfig"]["defaults"]["color"]["fixedColor"]
    assert color == "gray"


def test_manual_snapshot_in_freshness_notice() -> None:
    d = dashboard()
    notice = d["panels"][3]  # after result/span/failure cards
    assert "Manual snapshot" in notice["options"]["content"]
    assert "2026-10-28T08:00:00Z" in notice["options"]["content"]


def test_all_elapsed_fields_in_tables_use_minutes_not_seconds() -> None:
    d = dashboard()
    for target in table_targets(dashboard_data=d):
        for column in target["columns"]:
            text = column.get("text", "").lower()
            assert "duration (s)" not in text
            assert "duration (sec)" not in text


def test_minutes_use_fixed_custom_suffix_not_auto_hours() -> None:
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    overrides = step_panel["fieldConfig"]["overrides"]
    duration_override = next(o for o in overrides if o["matcher"]["options"] == "Duration (min)")
    unit = next(p for p in duration_override["properties"] if p["id"] == "unit")
    assert unit["value"] == "suffix: min"
    decimals = next(p for p in duration_override["properties"] if p["id"] == "decimals")
    assert decimals["value"] == 2


def test_duration_no_value_is_unknown_not_dash() -> None:
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    overrides = step_panel["fieldConfig"]["overrides"]
    duration_override = next(o for o in overrides if o["matcher"]["options"] == "Duration (min)")
    no_value = next(p for p in duration_override["properties"] if p["id"] == "noValue")
    assert no_value["value"] == "Unknown"


def test_barchart_uses_suffix_min_not_auto_hours() -> None:
    d = dashboard()
    chart = get_panel(dashboard_data=d, title="Where time went (all 3 nonzero measured steps)")
    unit = chart["fieldConfig"]["defaults"]["unit"]
    assert unit == "suffix: min"
    assert chart["fieldConfig"]["defaults"]["decimals"] == 2


def test_slowest_steps_bar_chart_exists_with_top_eight() -> None:
    d = dashboard()
    chart = get_panel(dashboard_data=d, title="Where time went (all 3 nonzero measured steps)")
    assert chart["type"] == "barchart"
    rows = json.loads(chart["targets"][0]["data"])
    assert len(rows) <= 8
    durations = [row["duration_min"] for row in rows]
    assert durations == sorted(durations, reverse=True)


def test_all_steps_table_retains_every_step_not_sampled() -> None:
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    rows = json.loads(step_panel["targets"][0]["data"])
    assert len(rows) == 3


def test_step_table_sorted_slowest_first() -> None:
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    rows = json.loads(step_panel["targets"][0]["data"])
    durations = [r["duration_min"] for r in rows if r["duration_min"] is not None]
    assert durations == sorted(durations, reverse=True)
    # Unmeasured rows go after measured rows
    has_none = any(r["duration_min"] is None for r in rows)
    if has_none:
        assert rows[-1]["duration_min"] is None


def test_step_table_has_github_link_column() -> None:
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    columns = step_panel["targets"][0]["columns"]
    assert any(c.get("text") == "GitHub" for c in columns)
    overrides = step_panel["fieldConfig"]["overrides"]
    github_override = next(o for o in overrides if o["matcher"]["options"] == "GitHub")
    assert any(p["id"] == "links" for p in github_override["properties"])


def test_step_table_uses_result_not_conclusion() -> None:
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    columns = step_panel["targets"][0]["columns"]
    assert any(c.get("text") == "Result" for c in columns)
    assert not any(c.get("text") == "Conclusion" for c in columns)


def test_failed_counts_show_denominator() -> None:
    d = dashboard()
    stats = stat_panels(dashboard_data=d)
    failed_jobs = next(p for p in stats if p["title"] == "Failed jobs")
    rows = json.loads(failed_jobs["targets"][0]["data"])
    assert rows[0]["value"] == "1 / 1"


def test_failure_location_prefers_steps_with_evidence_link() -> None:
    d = dashboard()
    text = get_panel(dashboard_data=d, title="Failure location")
    content = text["options"]["content"]
    assert "Run E2E tests" in content
    assert "does not prove earliest cause" in content
    assert 'href="https://github.com/' in content


def test_failure_location_cancellation_before_failure_prefers_failure() -> None:
    snap = snapshot()
    # Insert a cancelled step before the failing one
    snap["jobs"][0]["steps"].insert(
        1,
        {
            "number": 1,
            "name": "Early cancelled step",
            "status": "completed",
            "conclusion": "cancelled",
            "started_at": "2026-10-27T02:08:30Z",
            "completed_at": "2026-10-27T02:08:35Z",
        },
    )
    d = build_dashboard(snapshot=snap)
    text = get_panel(dashboard_data=d, title="Failure location")
    content = text["options"]["content"]
    assert "Run E2E tests" in content
    assert "Early cancelled step" not in content


def test_failure_location_falls_back_to_job_when_no_steps() -> None:
    snap = snapshot()
    snap["jobs"][0]["steps"] = []
    d = build_dashboard(snapshot=snap)
    text = get_panel(dashboard_data=d, title="Failure location")
    content = text["options"]["content"]
    assert "step details unavailable" in content
    assert 'href="https://github.com/' in content


def test_failure_location_no_failures_says_none_observed() -> None:
    snap = snapshot()
    snap["jobs"][0]["conclusion"] = "success"
    for step in snap["jobs"][0]["steps"]:
        step["conclusion"] = "success"
    d = build_dashboard(snapshot=snap)
    text = get_panel(dashboard_data=d, title="Failure location")
    assert "No failed step or job observed" in text["options"]["content"]


def test_failure_location_escapes_hostile_step_name() -> None:
    snap = snapshot()
    snap["jobs"][0]["steps"][1]["name"] = '<script>alert("step")</script>'
    d = build_dashboard(snapshot=snap)
    text = get_panel(dashboard_data=d, title="Failure location")
    content = text["options"]["content"]
    assert "<script>" not in content
    assert "&lt;script&gt;" in content


def test_failure_location_escapes_hostile_job_name() -> None:
    snap = snapshot()
    snap["jobs"][0]["steps"] = []
    snap["jobs"][0]["name"] = '<img src="x" onerror="alert(1)">'
    d = build_dashboard(snapshot=snap)
    text = get_panel(dashboard_data=d, title="Failure location")
    content = text["options"]["content"]
    assert "<img" not in content
    assert "&lt;img" in content


def test_coverage_notice_visible_near_top() -> None:
    d = dashboard()
    notice = d["panels"][3]
    assert notice["type"] == "text"
    assert "GitHub metadata only" in notice["options"]["content"]
    assert notice["gridPos"]["y"] <= 8


def test_technical_identity_at_bottom() -> None:
    d = dashboard()
    identity = get_panel(dashboard_data=d, title="GitHub identity")
    assert identity["gridPos"]["y"] > 20
    assert "UNVERIFIED" in identity["options"]["content"]


def test_identity_panel_shows_event_created_updated_workflow() -> None:
    d = dashboard()
    identity = get_panel(dashboard_data=d, title="GitHub identity")
    content = identity["options"]["content"]
    assert "schedule" in content
    assert "Run name" in content
    assert "E2E VMaaS Full Install" in content
    assert "2026-10-27T02:07:00Z" in content
    assert "2026-10-27T03:45:00Z" in content
    assert "e2e-vmaas-full-install.yml" in content


def test_run_link_points_to_selected_attempt() -> None:
    d = dashboard()
    assert "/attempts/1" in d["links"][0]["url"]
    snap = snapshot()
    snap["run"]["attempt"] = 3
    d3 = build_dashboard(snapshot=snap)
    assert "/attempts/3" in d3["links"][0]["url"]


def test_jobs_table_has_clickable_github_links() -> None:
    d = dashboard()
    job_panel = get_panel(dashboard_data=d, title="Jobs")
    overrides = job_panel["fieldConfig"]["overrides"]
    github_override = next(o for o in overrides if o["matcher"]["options"] == "GitHub")
    links = next(p for p in github_override["properties"] if p["id"] == "links")
    assert links["value"][0]["url"] == "${__value.raw}"
    cell = next(p for p in github_override["properties"] if p["id"] == "custom.cellOptions")
    assert cell["value"]["type"] == "data-links"


def test_artifact_panel_has_clickable_links() -> None:
    d = dashboard()
    artifact_panel = get_panel(dashboard_data=d, title="Artifacts and evidence")
    overrides = artifact_panel["fieldConfig"]["overrides"]
    link_override = next(o for o in overrides if o["matcher"]["options"] == "Link")
    links = next(p for p in link_override["properties"] if p["id"] == "links")
    assert links["value"][0]["url"] == "${__value.raw}"


def test_summary_and_top8_match_build_report_numbers() -> None:
    rep = report()
    d = dashboard()
    step_panel = get_panel(dashboard_data=d, title="All steps (slowest first)")
    step_rows = json.loads(step_panel["targets"][0]["data"])
    assert len(step_rows) == len(rep["steps"])
    chart = get_panel(dashboard_data=d, title="Where time went (all 3 nonzero measured steps)")
    chart_rows = json.loads(chart["targets"][0]["data"])
    assert len(chart_rows) == len(rep["slowest_steps"])
    for chart_row, report_step in zip(chart_rows, rep["slowest_steps"], strict=True):
        assert chart_row["duration_min"] == report_step["duration_minutes"]


def test_open_jobs_have_null_duration_not_zero() -> None:
    snap = snapshot()
    snap["jobs"][0]["completed_at"] = None
    d = build_dashboard(snapshot=snap)
    job_panel = get_panel(dashboard_data=d, title="Jobs")
    rows = json.loads(job_panel["targets"][0]["data"])
    assert rows[0]["duration_min"] is None


def test_null_timestamps_stay_null_not_empty_string() -> None:
    snap = snapshot()
    snap["jobs"][0]["started_at"] = None
    d = build_dashboard(snapshot=snap)
    job_panel = get_panel(dashboard_data=d, title="Jobs")
    rows = json.loads(job_panel["targets"][0]["data"])
    assert rows[0]["started"] is None


def test_no_panel_overlap() -> None:
    d = dashboard()
    positions = [(p["gridPos"]["x"], p["gridPos"]["y"], p["gridPos"]["h"]) for p in d["panels"]]
    for i, (x1, y1, h1) in enumerate(positions):
        for x2, y2, h2 in positions[i + 1 :]:
            if x1 == x2 and y1 < y2:
                assert y1 + h1 <= y2
            elif x1 == x2 and y2 < y1:
                assert y2 + h2 <= y1


def test_failure_observations_table_shows_overlap_disclaimer() -> None:
    d = dashboard()
    panel = get_panel(dashboard_data=d, title="Failure observations")
    assert "separate observations" in panel["description"]


def test_invalid_schema_is_rejected() -> None:
    snap = snapshot()
    snap["schema"] = "wrong"
    with pytest.raises(SnapshotError, match="schema is not recognized"):
        build_dashboard(snapshot=snap)


def test_missing_required_field_is_rejected() -> None:
    snap = snapshot()
    del snap["run"]["head_sha"]
    with pytest.raises(SnapshotError, match="missing the field"):
        build_dashboard(snapshot=snap)


def test_empty_jobs_is_valid() -> None:
    snap = snapshot()
    snap["jobs"] = []
    d = build_dashboard(snapshot=snap)
    job_panel = get_panel(dashboard_data=d, title="Jobs")
    rows = json.loads(job_panel["targets"][0]["data"])
    assert rows == []


def test_no_artifacts_produces_no_artifact_panel() -> None:
    snap = snapshot()
    snap["artifacts"] = []
    d = build_dashboard(snapshot=snap)
    assert not any(p["title"] == "Artifacts and evidence" for p in d["panels"])


def test_malformed_repository_is_rejected() -> None:
    snap = snapshot()
    snap["repository"] = "javascript:alert(1)"
    with pytest.raises(SnapshotError, match="owner/name"):
        build_dashboard(snapshot=snap)


def test_negative_job_id_is_rejected() -> None:
    snap = snapshot()
    snap["jobs"][0]["id"] = -1
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_missing_id_is_rejected() -> None:
    snap = snapshot()
    del snap["artifacts"][0]["id"]
    with pytest.raises(SnapshotError, match="missing the field"):
        build_dashboard(snapshot=snap)


def test_artifact_zero_id_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["id"] = 0
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_negative_id_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["id"] = -1
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_string_id_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["id"] = "evil"
    with pytest.raises(SnapshotError, match="positive integer"):
        build_dashboard(snapshot=snap)


def test_artifact_over_cap_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"] = [{"id": i + 1, "name": f"a{i}", "size_bytes": 100, "expired": False} for i in range(1001)]
    with pytest.raises(SnapshotError, match="bounded list"):
        build_dashboard(snapshot=snap)


def test_artifact_valid_zero_size_is_accepted() -> None:
    snap = snapshot()
    snap["artifacts"][0]["size_bytes"] = 0
    d = build_dashboard(snapshot=snap)
    artifact_panel = get_panel(dashboard_data=d, title="Artifacts and evidence")
    rows = json.loads(artifact_panel["targets"][0]["data"])
    assert rows[0]["size_bytes"] == 0


def test_artifact_negative_size_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["size_bytes"] = -5
    with pytest.raises(SnapshotError, match="non-negative integer"):
        build_dashboard(snapshot=snap)


def test_artifact_non_bool_expired_is_rejected() -> None:
    snap = snapshot()
    snap["artifacts"][0]["expired"] = "yes"
    with pytest.raises(SnapshotError, match="boolean"):
        build_dashboard(snapshot=snap)


def test_expired_artifact_is_clearly_marked() -> None:
    snap = snapshot()
    snap["artifacts"][0]["expired"] = True
    d = build_dashboard(snapshot=snap)
    artifact_panel = get_panel(dashboard_data=d, title="Artifacts and evidence")
    rows = json.loads(artifact_panel["targets"][0]["data"])
    assert rows[0]["expired"] == "yes"


def test_javascript_url_is_ignored_in_links() -> None:
    snap = snapshot()
    snap["run"]["url"] = "javascript:alert(1)"
    d = build_dashboard(snapshot=snap)
    assert d["links"][0]["url"] == "https://github.com/osac-project/osac/actions/runs/40548825479/attempts/1"


def test_multiple_attempt_is_noted_but_not_claimed_comparable() -> None:
    snap = snapshot()
    snap["run"]["attempt"] = 3
    d = build_dashboard(snapshot=snap)
    identity = get_panel(dashboard_data=d, title="GitHub identity")
    assert "attempt 3" in identity["options"]["content"]
    assert "Previous attempts have separate GitHub results" in identity["options"]["content"]
    assert "does not claim they are comparable" in identity["options"]["content"]


def test_cli_writes_private_file_and_refuses_overwrite(*, tmp_path: Path) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(snapshot()), encoding="utf-8")
    out_file = tmp_path / "dashboard.json"
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 0
    assert out_file.exists()
    assert (out_file.stat().st_mode & 0o777) == 0o600
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 1


def test_cli_rejects_symlink_output(*, tmp_path: Path) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text(json.dumps(snapshot()), encoding="utf-8")
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    result = main(argv=["--snapshot", str(snap_file), "--output", str(link)])
    assert result == 1


def test_cli_constant_error_does_not_echo_payload(*, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text('{"schema": "attacker-controlled-value-here"}', encoding="utf-8")
    out_file = tmp_path / "out.json"
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 1
    assert not out_file.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attacker-controlled-value-here" not in captured.err
    assert "Traceback" not in captured.err


def test_cli_constant_boundary_on_malformed_json(*, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snap_file = tmp_path / "snap.json"
    snap_file.write_text("{invalid json", encoding="utf-8")
    out_file = tmp_path / "out.json"
    result = main(argv=["--snapshot", str(snap_file), "--output", str(out_file)])
    assert result == 1
    assert not out_file.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "ERROR: snapshot is not valid JSON"
    assert "Traceback" not in captured.err


def test_invalid_attempt_cannot_build_a_link() -> None:
    input_snapshot = snapshot()
    input_snapshot["run"]["attempt"] = 0
    with pytest.raises(SnapshotError, match="run attempt must be a positive integer"):
        build_dashboard(snapshot=input_snapshot)


def test_invalid_timestamp_is_not_reported_as_unmeasured() -> None:
    input_snapshot = snapshot()
    input_snapshot["jobs"][0]["started_at"] = "invalid"
    with pytest.raises(SnapshotError, match="snapshot timestamp is invalid"):
        build_dashboard(snapshot=input_snapshot)


def test_artifact_table_builds_urls_from_numeric_ids() -> None:
    input_snapshot = snapshot()
    input_snapshot["artifacts"][0]["url"] = "javascript:alert(1)"
    rendered = build_dashboard(snapshot=input_snapshot)
    panel = get_panel(dashboard_data=rendered, title="Artifacts and evidence")
    rows = json.loads(panel["targets"][0]["data"])
    assert rows[0]["name"] == "osac-logs-full-install"
    assert rows[0]["size_bytes"] == 12345678
    assert rows[0]["link"] == "https://github.com/osac-project/osac/actions/runs/40548825479/artifacts/9876543"
    assert "javascript:" not in json.dumps(rendered)


def test_job_table_matches_input_count_and_values() -> None:
    rendered = dashboard()
    panel = get_panel(dashboard_data=rendered, title="Jobs")
    rows = json.loads(panel["targets"][0]["data"])
    assert rows == [
        {
            "job": "e2e-vmaas-full-install / e2e",
            "result": "failure",
            "duration_min": 96.0,
            "github": "https://github.com/osac-project/osac/actions/runs/40548825479/job/12345678",
            "started": "2026-10-27T02:08:00Z",
            "completed": "2026-10-27T03:44:00Z",
        }
    ]


def test_cancelled_observations_are_not_labeled_failed() -> None:
    input_snapshot = snapshot()
    input_snapshot["run"]["conclusion"] = "cancelled"
    input_snapshot["jobs"][0]["conclusion"] = "cancelled"
    for step in input_snapshot["jobs"][0]["steps"]:
        step["conclusion"] = "cancelled"
    rendered = build_dashboard(snapshot=input_snapshot)
    location = get_panel(dashboard_data=rendered, title="Failure location")
    assert "No failed step or job observed" in location["options"]["content"]
    observations = get_panel(dashboard_data=rendered, title="Failure observations")
    rows = json.loads(observations["targets"][0]["data"])
    assert len(rows) == 4
    assert all(row["result"] == "cancelled" for row in rows)


def test_completed_job_with_missing_time_bound_shows_unknown() -> None:
    input_snapshot = snapshot()
    input_snapshot["jobs"][0]["completed_at"] = None
    rendered = build_dashboard(snapshot=input_snapshot)
    panel = get_panel(dashboard_data=rendered, title="Job span")
    assert json.loads(panel["targets"][0]["data"]) == [{"value": "Unknown"}]


def test_missing_coverage_fields_display_unknown() -> None:
    input_snapshot = snapshot()
    input_snapshot["coverage"] = {}
    rendered = build_dashboard(snapshot=input_snapshot)
    panel = get_panel(dashboard_data=rendered, title="Source coverage")
    assert "Jobs complete: Unknown" in panel["options"]["content"]
    assert "Artifacts complete: Unknown" in panel["options"]["content"]
    assert "None" not in panel["options"]["content"]

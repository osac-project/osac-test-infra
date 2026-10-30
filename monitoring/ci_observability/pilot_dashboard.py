"""Generate a read-only Grafana run-investigation dashboard from one
GitHub snapshot, arranged for human reading.

Usage:
    python -m monitoring.ci_observability.pilot_dashboard \
        --snapshot /private/snapshot.json --output /private/dashboard.json
"""

from __future__ import annotations

import argparse
import json
import operator
import os
import sys
from html import escape as _escape_html
from pathlib import Path

from monitoring.ci_observability.pilot_report import JSONObject, JSONValue, SnapshotError, build_report, load_snapshot

DASHBOARD_UID = "ci-run-pilot"
DATASOURCE_UID = "ci-pilot-inline"
INFINITY_TYPE = "yesoreyeram-infinity-datasource"

_RESULT_LABELS = {
    ("completed", "success"): ("Passed", "green"),
    ("completed", "failure"): ("Failed", "red"),
    ("completed", "cancelled"): ("Cancelled", "orange"),
    ("completed", "timed_out"): ("Timed out", "red"),
    ("completed", "startup_failure"): ("Startup failure", "red"),
    ("completed", "action_required"): ("Action required", "orange"),
    ("in_progress", None): ("Running at snapshot", "blue"),
    ("queued", None): ("Queued at snapshot", "blue"),
    ("waiting", None): ("Waiting at snapshot", "blue"),
    ("requested", None): ("Requested", "blue"),
    ("pending", None): ("Pending", "blue"),
}


def _result_label(*, status: str, conclusion: str | None) -> tuple[str, str]:
    key = (status, conclusion) if conclusion else (status, None)
    if key in _RESULT_LABELS:
        return _RESULT_LABELS[key]
    if conclusion:
        return (conclusion.replace("_", " ").title(), "orange")
    if status:
        return (status.replace("_", " ").title(), "blue")
    return ("Unknown", "gray")


def _format_minutes(*, value: float | None) -> str:
    if value is None:
        return "Unknown"
    return f"{value:.2f} min"


def _safe_link(*, url: str, text: str) -> str:
    return f'<a href="{_escape_html(url)}" target="_blank" rel="noopener noreferrer">{_escape_html(text)}</a>'


def _inline_target(*, ref_id: str, data: list[JSONObject], columns: list[dict[str, str]]) -> JSONObject:
    return {
        "refId": ref_id,
        "datasource": {"type": INFINITY_TYPE, "uid": DATASOURCE_UID},
        "type": "json",
        "source": "inline",
        "format": "table",
        "parser": "backend",
        "data": json.dumps(data),
        "columns": columns,
        "root_selector": "",
        "url": "",
    }


def _text_panel(*, panel_id: int, title: str, description: str, content: str, grid_pos: dict[str, int]) -> JSONObject:
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "text",
        "gridPos": grid_pos,
        "options": {"mode": "html", "content": content},
    }


def _stat_panel(
    *, panel_id: int, title: str, description: str, value: str, color: str, grid_pos: dict[str, int], label: str
) -> JSONObject:
    row = [{"value": value}]
    columns = [{"selector": "value", "text": label, "type": "string"}]
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "stat",
        "gridPos": grid_pos,
        "datasource": {"type": INFINITY_TYPE, "uid": DATASOURCE_UID},
        "targets": [_inline_target(ref_id="A", data=row, columns=columns)],
        "fieldConfig": {
            "defaults": {
                "mappings": [{"type": "value", "options": {value: {"color": color, "index": 0}}}],
                "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]},
                "color": {"mode": "fixed", "fixedColor": color},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": label},
            "orientation": "auto",
            "textMode": "value",
            "wideLayout": True,
            "graphMode": "none",
        },
    }


def _barchart_panel(
    *, panel_id: int, title: str, description: str, target: JSONObject, grid_pos: dict[str, int]
) -> JSONObject:
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "barchart",
        "gridPos": grid_pos,
        "datasource": {"type": INFINITY_TYPE, "uid": DATASOURCE_UID},
        "targets": [target],
        "fieldConfig": {
            "defaults": {
                "unit": "suffix: min",
                "min": 0,
                "decimals": 2,
                "custom": {
                    "axisPlacement": "auto",
                    "axisLabel": "Minutes",
                    "drawStyle": "bars",
                    "fillOpacity": 80,
                    "lineWidth": 0,
                    "showValue": "always",
                },
            },
            "overrides": [],
        },
        "options": {
            "orientation": "horizontal",
            "showValue": "always",
            "stacking": "none",
            "groupWidth": 0.7,
            "barWidth": 0.9,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single", "sort": "desc"},
        },
    }


def _table_panel(
    *,
    panel_id: int,
    title: str,
    description: str,
    target: JSONObject,
    grid_pos: dict[str, int],
    index_by_name: dict[str, int],
    overrides: list[JSONObject],
) -> JSONObject:
    return {
        "id": panel_id,
        "title": title,
        "description": description,
        "type": "table",
        "gridPos": grid_pos,
        "targets": [target],
        "transformations": [
            {"id": "organize", "options": {"indexByName": index_by_name, "excludeByName": {}, "renameByName": {}}}
        ],
        "fieldConfig": {"defaults": {}, "overrides": overrides},
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
        },
    }


def _github_link_overrides(*, column_name: str = "GitHub") -> list[JSONObject]:
    return [
        {
            "matcher": {"id": "byName", "options": column_name},
            "properties": [
                {"id": "links", "value": [{"title": "Open on GitHub", "url": "${__value.raw}", "targetBlank": True}]},
                {"id": "custom.cellOptions", "value": {"type": "data-links"}},
            ],
        }
    ]


def _minutes_override(*, column_name: str = "Duration (min)") -> list[JSONObject]:
    return [
        {
            "matcher": {"id": "byName", "options": column_name},
            "properties": [
                {"id": "displayName", "value": column_name},
                {"id": "custom.width", "value": 130},
                {"id": "unit", "value": "suffix: min"},
                {"id": "decimals", "value": 2},
                {"id": "noValue", "value": "Unknown"},
            ],
        }
    ]


def _pick_failure_observation(
    *, failed_steps: list[JSONObject], failed_jobs: list[JSONObject]
) -> tuple[str, str, str | None]:
    """Keep cancellations in the attention table rather than labeling them failures."""

    exact_failed_steps = [s for s in failed_steps if s["conclusion"] == "failure"]
    if exact_failed_steps:
        first = exact_failed_steps[0]
        location = f"<b>{_escape_html(first['name'])}</b> in job {_escape_html(first['job_name'])}"
        detail = f"conclusion: {_escape_html(first['conclusion'])}"
        return location, detail, first["url"]
    exact_failed_jobs = [j for j in failed_jobs if j["conclusion"] == "failure"]
    if exact_failed_jobs:
        first = exact_failed_jobs[0]
        location = f"Job <b>{_escape_html(first['name'])}</b> (step details unavailable)"
        detail = f"conclusion: {_escape_html(first['conclusion'])}"
        return location, detail, first["url"]
    return "No failed step or job observed in available metadata", "", None


def _coverage_label(*, value: JSONValue) -> str:
    if value is None:
        return "Unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def build_dashboard(*, snapshot: JSONObject) -> JSONObject:
    report = build_report(snapshot=snapshot)
    source = report["source"]
    summary = report["summary"]
    jobs = report["jobs"]
    steps = report["steps"]
    failures = report["failures"]
    artifacts = report["artifacts"]
    slowest_steps = report["slowest_steps"]
    slowest_steps_total = report["slowest_steps_total"]
    coverage = report["coverage"]

    run = snapshot["run"]
    run_created = run["created_at"]
    run_updated = run["updated_at"]
    repository = source["repository"]
    run_id = source["run_id"]
    attempt = source["run_attempt"]

    run_label, run_color = _result_label(status=summary["status"], conclusion=summary["conclusion"])
    span_minutes = summary["job_span_minutes"]

    failure_location, failure_detail, failure_url = _pick_failure_observation(
        failed_steps=failures["steps"], failed_jobs=failures["jobs"]
    )

    coverage_short = (
        f"Manual snapshot taken {_escape_html(source['fetched_at'])}. "
        f"GitHub metadata only: no test results, no cluster state. "
        f"Tested revision: UNVERIFIED."
    )
    if not coverage.get("jobs_complete"):
        coverage_short += " Job data may be incomplete."

    step_table_rows = []
    for step in steps:
        step_table_rows.append(
            {
                "step": step["name"],
                "result": "Unknown" if step["conclusion"] is None else step["conclusion"],
                "duration_min": step["duration_minutes"],
                "job": step["job_name"],
                "github": step["url"],
                "started": step["started_at"],
                "completed": step["completed_at"],
                "number": step["number"],
            }
        )
    measured = [row for row in step_table_rows if row["duration_min"] is not None]
    unmeasured = [row for row in step_table_rows if row["duration_min"] is None]
    measured.sort(key=operator.itemgetter("duration_min"), reverse=True)
    step_table_rows = measured + unmeasured

    step_columns = [
        {"selector": "step", "text": "Step", "type": "string"},
        {"selector": "result", "text": "Result", "type": "string"},
        {"selector": "duration_min", "text": "Duration (min)", "type": "number"},
        {"selector": "job", "text": "Job", "type": "string"},
        {"selector": "github", "text": "GitHub", "type": "string"},
        {"selector": "started", "text": "Started (UTC)", "type": "string"},
        {"selector": "completed", "text": "Completed (UTC)", "type": "string"},
        {"selector": "number", "text": "#", "type": "number"},
    ]

    job_table_rows = []
    for job in jobs:
        job_table_rows.append(
            {
                "job": job["name"],
                "result": "Unknown" if job["conclusion"] is None else job["conclusion"],
                "duration_min": job["duration_minutes"],
                "github": job["url"],
                "started": job["started_at"],
                "completed": job["completed_at"],
            }
        )
    job_columns = [
        {"selector": "job", "text": "Job", "type": "string"},
        {"selector": "result", "text": "Result", "type": "string"},
        {"selector": "duration_min", "text": "Duration (min)", "type": "number"},
        {"selector": "github", "text": "GitHub", "type": "string"},
        {"selector": "started", "text": "Started (UTC)", "type": "string"},
        {"selector": "completed", "text": "Completed (UTC)", "type": "string"},
    ]

    slowest_chart_rows = [
        {"step": step["name"], "job": step["job_name"], "duration_min": step["duration_minutes"]}
        for step in slowest_steps
    ]
    slowest_chart_columns = [
        {"selector": "step", "text": "Step", "type": "string"},
        {"selector": "duration_min", "text": "Duration (min)", "type": "number"},
    ]

    artifact_rows = [
        {
            "name": artifact["name"],
            "size_bytes": artifact["size_bytes"],
            "expired": "yes" if artifact["expired"] else "no",
            "link": artifact["url"],
        }
        for artifact in artifacts
    ]
    artifact_columns = [
        {"selector": "name", "text": "Artifact", "type": "string"},
        {"selector": "size_bytes", "text": "Size (bytes)", "type": "number"},
        {"selector": "expired", "text": "Expired", "type": "string"},
        {"selector": "link", "text": "Link", "type": "string"},
    ]

    failure_table_rows = []
    for step in failures["steps"]:
        failure_table_rows.append(
            {
                "kind": "step",
                "name": step["name"],
                "job": step["job_name"],
                "result": step["conclusion"],
                "github": step["url"],
            }
        )
    for job in failures["jobs"]:
        failure_table_rows.append(
            {"kind": "job", "name": job["name"], "job": job["name"], "result": job["conclusion"], "github": job["url"]}
        )
    failure_columns = [
        {"selector": "kind", "text": "Kind", "type": "string"},
        {"selector": "name", "text": "Name", "type": "string"},
        {"selector": "job", "text": "Job", "type": "string"},
        {"selector": "result", "text": "Result", "type": "string"},
        {"selector": "github", "text": "GitHub", "type": "string"},
    ]

    identity_lines = [
        f"<b>Repository</b>: {_escape_html(repository)}",
        f"<b>Run name</b>: {_escape_html(run['name'])}",
        f"<b>Run</b>: #{run_id} (attempt {attempt})",
        f"<b>Event</b>: {_escape_html(run['event'])}",
        f"<b>GitHub head SHA</b>: <code>{_escape_html(run['head_sha'])}</code>",
        "<b>Tested revision</b>: UNVERIFIED",
        f"<b>Created</b>: {_escape_html(run['created_at'])}",
        f"<b>Updated</b>: {_escape_html(run['updated_at'])}",
    ]
    if run.get("workflow_path"):
        identity_lines.append(f"<b>Workflow</b>: <code>{_escape_html(run['workflow_path'])}</code>")
    if attempt > 1:
        identity_lines.append(
            "Previous attempts have separate GitHub results; this dashboard does not claim they are comparable."
        )

    coverage_lines = [
        f"<li>Jobs complete: {_escape_html(_coverage_label(value=coverage.get('jobs_complete')))}</li>",
        f"<li>Artifacts complete: {_escape_html(_coverage_label(value=coverage.get('artifacts_complete')))}</li>",
        f"<li>Test results: {_escape_html(_coverage_label(value=coverage.get('test_results')))}</li>",
        f"<li>Tested revision: {_escape_html(_coverage_label(value=coverage.get('tested_revision')))}</li>",
    ]
    notes = coverage.get("notes")
    if notes is not None:
        for note in notes[:10]:
            coverage_lines.append(f"<li>{_escape_html(str(note))}</li>")

    panels: list[JSONObject] = []
    panel_id = 1

    panels.append(
        _stat_panel(
            panel_id=panel_id,
            title="GitHub result",
            description=f"Raw GitHub status/conclusion for attempt {attempt}. Not a tested-revision claim.",
            value=run_label,
            color=run_color,
            grid_pos={"h": 6, "w": 8, "x": 0, "y": 0},
            label="Result",
        )
    )
    panel_id += 1

    span_display = _format_minutes(value=span_minutes)
    if (
        span_minutes is None
        and coverage.get("jobs_complete") is True
        and any(job["status"] != "completed" for job in jobs)
    ):
        span_display = "Not finished"
    panels.append(
        _stat_panel(
            panel_id=panel_id,
            title="Job span",
            description=(
                "Earliest job start to latest job finish. Excludes queue time before jobs; "
                "parallel jobs overlap, so this is not the sum of durations."
            ),
            value=span_display,
            color="blue",
            grid_pos={"h": 6, "w": 8, "x": 8, "y": 0},
            label="Span",
        )
    )
    panel_id += 1

    failure_link = _safe_link(url=failure_url, text="View evidence on GitHub") if failure_url else ""
    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="Failure location",
            description="First listed failed step or job in API listing order. Not a root-cause diagnosis.",
            content=(
                f"<p style='font-size:1.1em'>{failure_location}</p>"
                f"<p>{failure_detail}</p>"
                f"<p>{failure_link}</p>"
                "<p style='font-size:0.85em;color:#888'>API listing order does not prove earliest cause.</p>"
            ),
            grid_pos={"h": 6, "w": 8, "x": 16, "y": 0},
        )
    )
    panel_id += 1

    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="",
            description="",
            content=f"<p style='font-size:0.85em;color:#888;padding:4px 8px'>{coverage_short}</p>",
            grid_pos={"h": 2, "w": 24, "x": 0, "y": 6},
        )
    )
    panel_id += 1

    jobs_complete = coverage.get("jobs_complete") is True
    count_color = "gray"
    step_count_color = "gray"
    if jobs_complete:
        count_color = "green"
        step_count_color = "green"
    if summary["failed_job_count"] > 0:
        count_color = "red"
    if summary["failed_step_count"] > 0:
        step_count_color = "red"
    panels.append(
        _stat_panel(
            panel_id=panel_id,
            title="Failed jobs",
            description=f"{summary['failed_job_count']} of {summary['job_count']} job(s) concluded failure.",
            value=f"{summary['failed_job_count']} / {summary['job_count']}",
            color=count_color,
            grid_pos={"h": 4, "w": 6, "x": 0, "y": 8},
            label="Failed jobs",
        )
    )
    panel_id += 1

    panels.append(
        _stat_panel(
            panel_id=panel_id,
            title="Failed steps",
            description=f"{summary['failed_step_count']} of {summary['step_count']} step(s) concluded failure.",
            value=f"{summary['failed_step_count']} / {summary['step_count']}",
            color=step_count_color,
            grid_pos={"h": 4, "w": 6, "x": 0, "y": 12},
            label="Failed steps",
        )
    )
    panel_id += 1

    subset_label = (
        f"top {len(slowest_chart_rows)} of {slowest_steps_total}"
        if slowest_steps_total > len(slowest_chart_rows)
        else f"all {slowest_steps_total}"
    )
    panels.append(
        _barchart_panel(
            panel_id=panel_id,
            title=f"Where time went ({subset_label} nonzero measured steps)",
            description=(
                f"Top {len(slowest_chart_rows)} slowest nonzero measured steps ({subset_label}). "
                "Parallel steps overlap; this is not an exact critical path."
            ),
            target=_inline_target(ref_id="A", data=slowest_chart_rows, columns=slowest_chart_columns),
            grid_pos={"h": 8, "w": 18, "x": 6, "y": 8},
        )
    )
    panel_id += 1
    y = 16

    panels.append(
        _table_panel(
            panel_id=panel_id,
            title="Jobs",
            description=f"{len(jobs)} job(s). Durations in minutes; Unknown means a time bound is missing.",
            target=_inline_target(ref_id="A", data=job_table_rows, columns=job_columns),
            grid_pos={"h": 6, "w": 24, "x": 0, "y": y},
            index_by_name={
                "Job": 0,
                "Result": 1,
                "Duration (min)": 2,
                "GitHub": 3,
                "Started (UTC)": 4,
                "Completed (UTC)": 5,
            },
            overrides=[*_github_link_overrides(column_name="GitHub"), *_minutes_override(column_name="Duration (min)")],
        )
    )
    panel_id += 1
    y += 6

    panels.append(
        _table_panel(
            panel_id=panel_id,
            title="All steps (slowest first)",
            description=(
                f"{len(step_table_rows)} step(s) across {len(jobs)} job(s). "
                "Sorted by measured duration; Unknown means a time bound is missing. All steps retained."
            ),
            target=_inline_target(ref_id="A", data=step_table_rows, columns=step_columns),
            grid_pos={"h": 12, "w": 24, "x": 0, "y": y},
            index_by_name={
                "Step": 0,
                "Result": 1,
                "Duration (min)": 2,
                "Job": 3,
                "GitHub": 4,
                "Started (UTC)": 5,
                "Completed (UTC)": 6,
                "#": 7,
            },
            overrides=[
                *_minutes_override(column_name="Duration (min)"),
                *_github_link_overrides(column_name="GitHub"),
                {"matcher": {"id": "byName", "options": "Step"}, "properties": [{"id": "custom.width", "value": 360}]},
                {"matcher": {"id": "byName", "options": "Job"}, "properties": [{"id": "custom.width", "value": 240}]},
            ],
        )
    )
    panel_id += 1
    y += 12

    if failure_table_rows:
        panels.append(
            _table_panel(
                panel_id=panel_id,
                title="Failure observations",
                description=(
                    "Attention list: failed, cancelled, timed_out, startup_failure, action_required. "
                    "A failed job and its failed step are separate observations, not two incidents."
                ),
                target=_inline_target(ref_id="A", data=failure_table_rows, columns=failure_columns),
                grid_pos={"h": 6, "w": 24, "x": 0, "y": y},
                index_by_name={"Kind": 0, "Name": 1, "Job": 2, "Result": 3, "GitHub": 4},
                overrides=_github_link_overrides(column_name="GitHub"),
            )
        )
        panel_id += 1
        y += 6

    if artifact_rows:
        panels.append(
            _table_panel(
                panel_id=panel_id,
                title="Artifacts and evidence",
                description=f"{len(artifact_rows)} artifact(s). Expired artifacts cannot be downloaded.",
                target=_inline_target(ref_id="A", data=artifact_rows, columns=artifact_columns),
                grid_pos={"h": 6, "w": 24, "x": 0, "y": y},
                index_by_name={"Artifact": 0, "Expired": 1, "Size (bytes)": 2, "Link": 3},
                overrides=_github_link_overrides(column_name="Link"),
            )
        )
        panel_id += 1
        y += 6

    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="GitHub identity",
            description="Technical metadata. The head SHA is the GitHub run head, not a verified tested revision.",
            content="<br>".join(identity_lines),
            grid_pos={"h": 5, "w": 12, "x": 0, "y": y},
        )
    )
    panel_id += 1

    panels.append(
        _text_panel(
            panel_id=panel_id,
            title="Source coverage",
            description="What this snapshot does and does not cover.",
            content="<ul>" + "".join(coverage_lines) + "</ul>",
            grid_pos={"h": 5, "w": 12, "x": 12, "y": y},
        )
    )

    dashboard: JSONObject = {
        "uid": DASHBOARD_UID,
        "title": f"CI Run: {_escape_html(source['run_name'])} #{run_id} attempt {attempt}",
        "description": (
            f"Read-only investigation snapshot for run #{run_id} (attempt {attempt}) "
            f"of {_escape_html(repository)}. Data is embedded; refresh is manual. "
            "Tested revision is UNVERIFIED."
        ),
        "tags": ["ci-pilot"],
        "editable": False,
        "graphTooltip": 0,
        "schemaVersion": 39,
        "version": 1,
        "refresh": False,
        "timezone": "utc",
        "time": {"from": run_created, "to": run_updated},
        "timepicker": {"refresh_intervals": []},
        "templating": {"list": []},
        "annotations": {"list": []},
        "links": [{"title": "GitHub run", "type": "link", "url": source["github_url"], "targetBlank": True}],
        "panels": panels,
    }
    return dashboard


def _write_output(*, dashboard: JSONObject, path: Path) -> None:
    if path.is_symlink():
        raise SnapshotError("output must not be a symlink")
    if path.exists():
        raise SnapshotError("output already exists; refusing to overwrite")
    if not path.parent.is_dir():
        raise SnapshotError("output parent directory does not exist")
    payload = json.dumps(dashboard, indent=2, sort_keys=False) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        data = payload.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def main(*, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m monitoring.ci_observability.pilot_dashboard",
        description="Generate a read-only Grafana run-investigation dashboard from one GitHub snapshot.",
    )
    parser.add_argument("--snapshot", required=True, help="Path to the snapshot JSON file")
    parser.add_argument("--output", required=True, help="Path for the output dashboard JSON")
    args = parser.parse_args(argv)

    snapshot_path = Path(args.snapshot)
    output_path = Path(args.output)

    try:
        snapshot = load_snapshot(path=snapshot_path)
        dashboard = build_dashboard(snapshot=snapshot)
        _write_output(dashboard=dashboard, path=output_path)
    except SnapshotError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError) as error:
        print(f"ERROR: snapshot cannot be processed: {type(error).__name__}", file=sys.stderr)
        return 1

    print(f"Dashboard written: {output_path} (mode 600, {len(dashboard['panels'])} panels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

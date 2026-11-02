"""Structural tests for the CI Observe cluster dashboard.

monitoring/config/grafana/dashboards/ci-observe-cluster.json answers the
cluster question for one capture: at every minute of the run, how many pods
existed, what each pod used in CPU and in memory, what each node used, and
which pods existed when. It is provisioned by
monitoring/config/grafana/dashboards.yml into the OSAC CI folder next to the
other dashboards and reads the ci-observe Prometheus backend (uid
ci-observe-metrics, the otel-lgtm bundle on port 9092), which scrapes the
kubelet summary API every 60 seconds and keeps 15 days.

Live-verified against that backend on 2026-11-02 with capture
caas-main-41446977693-a1 (samples 13:31:38Z to 13:50:38Z, 243 pod CPU series,
247 pod working-set series, one node, no k8s_node_name label on any pod
series). What the panels rely on:

- k8s_pod_cpu_usage is CPU cores, not a percentage: the sum over pods is
  ~2.2 while k8s_node_cpu_usage is ~3.5 at the same step.
- `@ end()` in the topk ranking makes the top-10 membership fixed for the
  whole range, so the panel draws exactly ten lines instead of every pod that
  was ever in the top ten at some step (18 pods without it).
- A bare instant query of a raw metric returns nothing once the last sample is
  more than the 5-minute lookback old, which is why the table wraps every
  selector in a range function over $__range.
- timestamp() inside a min_over_time/max_over_time subquery returns epoch
  seconds; the table multiplies by 1000 so Grafana's dateTimeAsIso unit, which
  reads milliseconds, formats it as a time.
- The table's sample times are keyed on the working-set gauge, not on CPU:
  CPU usage is a rate, so a pod scraped only once has memory samples and no
  CPU sample (four such pods in that capture), while every scraped pod reports
  a working set (the CPU pod set was a strict subset of the working-set pod
  set). Keying on CPU would leave those pods without a first or last sample.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

GRAFANA_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana"
DASHBOARDS_DIR = GRAFANA_DIR / "dashboards"
DASHBOARD_FILE = DASHBOARDS_DIR / "ci-observe-cluster.json"
DATASOURCES_FILE = GRAFANA_DIR / "datasources.yml"

# The Prometheus datasource the ci-observe backend provisions on port 9092
# with a 60-second collection interval. The default `prometheus` datasource
# (port 9091) has different metrics and must not be used here.
PROMETHEUS_UID = "ci-observe-metrics"
PROMETHEUS_PORT = "9092"

CAPTURE_FILTER = 'osac_ci_capture_id="$capture_id"'

# The one metric each time-series panel must read.
TIMESERIES_METRICS = {
    "Pod count": "k8s_pod_cpu_usage",
    "CPU by pod": "k8s_pod_cpu_usage",
    "Memory by pod": "k8s_pod_memory_working_set_bytes",
    "Node CPU": "k8s_node_cpu_usage",
    "Node memory": "k8s_node_memory_usage_bytes",
}

# Pod-level metrics: every query over these must keep k8s_pod_name.
POD_METRICS = ("k8s_pod_cpu_usage", "k8s_pod_memory_working_set_bytes")

# Aggregators that drop labels unless a `by` clause names them. topk keeps the
# labels of its input, and the *_over_time functions are range functions, not
# aggregations across series, so neither appears here.
DROPPING_AGGREGATORS = ("sum", "avg", "max", "min", "count")

# The only ungrouped aggregator allowed anywhere: the pod-count panel counts
# distinct pods, which is the whole point of the panel.
ALLOWED_UNGROUPED = {"Pod count": ["count"]}

TABLE_COLUMNS = {"Pod", "Namespace", "First sample", "Last sample", "Peak CPU", "Peak memory"}

# Grafana built-in variables ($__interval, $__range, ...) are allowed in
# queries; every other $name must be a dashboard template variable.
GRAFANA_BUILTIN_VARIABLE = re.compile(r"^__")


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD_FILE.read_text())


@pytest.fixture(scope="module")
def datasources() -> dict:
    return yaml.safe_load(DATASOURCES_FILE.read_text())


def _iter_panels(panels: list[dict]) -> Iterator[dict]:
    for panel in panels:
        yield panel
        yield from _iter_panels(panel.get("panels", []))


def _content_panels(dash: dict) -> list[dict]:
    return [panel for panel in _iter_panels(dash["panels"]) if panel.get("type") != "row"]


def _panels_by_title(dash: dict) -> dict[str, dict]:
    return {panel["title"]: panel for panel in _iter_panels(dash["panels"])}


def _expressions(dash: dict) -> list[tuple[str, str]]:
    """(panel title, PromQL expression) for every target in the dashboard."""
    return [
        (panel["title"], target["expr"])
        for panel in _iter_panels(dash["panels"])
        for target in panel.get("targets", [])
        if target.get("expr")
    ]


def _override_for(panel: dict, field: str) -> dict | None:
    for override in panel["fieldConfig"].get("overrides", []):
        if override["matcher"].get("options") == field:
            return override
    return None


def _override_properties(panel: dict, field: str) -> dict[str, object]:
    override = _override_for(panel, field)
    if override is None:
        return {}
    return {prop["id"]: prop["value"] for prop in override["properties"]}


def _walk_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


# ---------------------------------------------------------------------------
# File-level structure
# ---------------------------------------------------------------------------


def test_json_is_valid_and_parses(dashboard: dict) -> None:
    assert DASHBOARD_FILE.exists()
    assert isinstance(dashboard, dict)


def test_required_uid_and_title(dashboard: dict) -> None:
    assert dashboard["uid"] == "ci-observe-cluster"
    assert dashboard["title"] == "CI Observe — Cluster"
    assert dashboard["schemaVersion"] == 39
    assert dashboard["version"] == 1
    assert dashboard["description"], "the dashboard needs a description"
    assert "60 seconds" in dashboard["description"]
    assert "15 days" in dashboard["description"]


def test_dashboard_is_prometheus_only(dashboard: dict) -> None:
    """Cluster evidence is the Prometheus backend; traces and logs are elsewhere."""
    raw = DASHBOARD_FILE.read_text()
    for foreign in ("ci-observe-traces", "ci-observe-logs", "workflow-api"):
        assert foreign not in raw, f"{foreign} does not belong on the cluster dashboard"


def test_every_panel_uses_the_provisioned_prometheus_uid(dashboard: dict, datasources: dict) -> None:
    """The uid is read from the datasource provisioning file, never invented."""
    entries = {ds["uid"]: ds for ds in datasources["datasources"]}
    assert PROMETHEUS_UID in entries, f"datasources.yml provisions no {PROMETHEUS_UID} datasource"
    provisioned = entries[PROMETHEUS_UID]
    assert provisioned["type"] == "prometheus"
    assert provisioned["url"].endswith(PROMETHEUS_PORT)
    assert provisioned["jsonData"]["timeInterval"] == "60s"

    panels = _content_panels(dashboard)
    assert panels, "dashboard has no content panels"
    for panel in panels:
        datasources_used = [panel.get("datasource")] + [t.get("datasource") for t in panel.get("targets", [])]
        for datasource in datasources_used:
            if datasource is None:
                continue
            assert isinstance(datasource, dict), f"panel {panel['title']}: datasource must be an object"
            assert datasource["uid"] == PROMETHEUS_UID, f"panel {panel['title']}: unexpected datasource"


def test_panels_are_in_reading_order(dashboard: dict) -> None:
    positions = [(panel["gridPos"]["y"], panel["gridPos"]["x"]) for panel in dashboard["panels"]]
    assert positions == sorted(positions)


def test_every_panel_has_title_and_description(dashboard: dict) -> None:
    for panel in _iter_panels(dashboard["panels"]):
        assert panel.get("title"), f"panel without a title: {panel.get('id')}"
        assert panel.get("description"), f"panel without a description: {panel['title']}"


# ---------------------------------------------------------------------------
# Variables and scoping
# ---------------------------------------------------------------------------


def test_capture_id_is_a_textbox_variable(dashboard: dict) -> None:
    variables = dashboard["templating"]["list"]
    assert [v["name"] for v in variables] == ["capture_id"]

    capture_var = variables[0]
    assert capture_var["type"] == "textbox"
    assert capture_var["hide"] == 0
    assert capture_var["label"] == "Run"
    assert "datasource" not in capture_var, "a textbox reads no datasource"


def test_every_query_filters_on_the_capture(dashboard: dict) -> None:
    expressions = _expressions(dashboard)
    assert len(expressions) >= 5, "the dashboard lost most of its queries"
    for title, expr in expressions:
        assert CAPTURE_FILTER in expr, f"{title}: query is not scoped to the capture: {expr}"


def test_no_unresolved_template_tokens(dashboard: dict) -> None:
    variables = {v["name"] for v in dashboard["templating"]["list"]}
    token = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
    for text in _walk_strings(dashboard):
        for match in token.findall(text):
            if match.startswith("$"):
                continue
            assert match in variables or GRAFANA_BUILTIN_VARIABLE.match(match), (
                f"unresolved template token {match!r} in {DASHBOARD_FILE.name}"
            )


# ---------------------------------------------------------------------------
# No query may hide the pod label
# ---------------------------------------------------------------------------


def test_no_aggregation_hides_the_pod_label(dashboard: dict) -> None:
    """A pod panel must aggregate *by pod*, never across pods.

    A bare sum()/max()/count() over a pod metric collapses every pod into one
    nameless line, which is the exact question this dashboard must answer.
    """
    pod_expressions = [(title, expr) for title, expr in _expressions(dashboard) if any(m in expr for m in POD_METRICS)]
    assert pod_expressions, "no panel reads a pod-level metric"

    for title, expr in pod_expressions:
        assert "by (k8s_pod_name" in expr, f"{title}: pod query does not aggregate by pod: {expr}"
        ungrouped = re.findall(r"\b(" + "|".join(DROPPING_AGGREGATORS) + r")\s*\(", expr)
        assert sorted(ungrouped) == sorted(ALLOWED_UNGROUPED.get(title, [])), (
            f"{title}: ungrouped aggregation hides the pod label: {expr}"
        )


# ---------------------------------------------------------------------------
# Time series panels
# ---------------------------------------------------------------------------


def test_timeseries_panels_use_the_right_metrics(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    titles = {panel["title"] for panel in _content_panels(dashboard) if panel["type"] == "timeseries"}
    assert titles == set(TIMESERIES_METRICS), f"unexpected time-series panels: {sorted(titles)}"

    for title, metric in TIMESERIES_METRICS.items():
        panel = panels[title]
        assert len(panel["targets"]) == 1, f"{title}: one query per panel"
        expr = panel["targets"][0]["expr"]
        assert metric in expr, f"{title}: expected {metric}: {expr}"
        assert panel["targets"][0]["range"] is True
        assert panel["targets"][0]["instant"] is False
        assert panel["fieldConfig"]["defaults"]["noValue"], f"{title}: needs a noValue text"


def test_pod_count_counts_distinct_pods(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Pod count"]
    expr = panel["targets"][0]["expr"]
    assert expr == 'count(count by (k8s_pod_name) (k8s_pod_cpu_usage{osac_ci_capture_id="$capture_id"}))'
    assert panel["targets"][0]["legendFormat"] == "pods"
    assert panel["fieldConfig"]["defaults"]["unit"] == "short"


def test_pod_panels_rank_the_top_ten_by_mean(dashboard: dict) -> None:
    """The ten busiest pods by mean over the range, with the real per-minute values.

    The left side plots the raw per-step values; the right side only decides
    which pods qualify, and `@ end()` pins that decision to the end of the
    range so the ranking cannot change from step to step.
    """
    panels = _panels_by_title(dashboard)
    for title in ("CPU by pod", "Memory by pod"):
        expr = panels[title]["targets"][0]["expr"]
        assert "sum by (k8s_pod_name) (" in expr
        assert "and on (k8s_pod_name) topk(10, sum by (k8s_pod_name) (avg_over_time(" in expr
        assert "[$__range] @ end()" in expr, f"{title}: the top-10 ranking must be fixed at the range end"
        assert panels[title]["targets"][0]["legendFormat"] == "{{k8s_pod_name}}"


def test_node_panels_have_one_line_per_node(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    for title in ("Node CPU", "Node memory"):
        target = panels[title]["targets"][0]
        assert target["expr"].startswith("sum by (k8s_node_name) (")
        assert target["legendFormat"] == "{{k8s_node_name}}"


def test_units_match_what_the_metrics_hold(dashboard: dict) -> None:
    panels = _panels_by_title(dashboard)
    assert panels["CPU by pod"]["fieldConfig"]["defaults"]["unit"] == "short"
    assert panels["Node CPU"]["fieldConfig"]["defaults"]["unit"] == "short"
    assert panels["Memory by pod"]["fieldConfig"]["defaults"]["unit"] == "bytes"
    assert panels["Node memory"]["fieldConfig"]["defaults"]["unit"] == "bytes"
    # k8s_pod_cpu_usage is cores, live-verified: summing pods gives ~2.2 while
    # k8s_node_cpu_usage reads ~3.5 at the same step.
    assert panels["CPU by pod"]["fieldConfig"]["defaults"]["custom"]["axisLabel"] == "cores"


# ---------------------------------------------------------------------------
# Pods seen table
# ---------------------------------------------------------------------------


def test_pods_seen_table_has_the_expected_columns(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Pods seen"]
    assert panel["type"] == "table"

    transformation_ids = [t["id"] for t in panel["transformations"]]
    assert transformation_ids == ["merge", "organize"], transformation_ids

    organize = panel["transformations"][1]["options"]
    assert set(organize["renameByName"].values()) == TABLE_COLUMNS
    for time_column in ("Time", "Time 1", "Time 2", "Time 3"):
        assert organize["excludeByName"][time_column] is True, f"{time_column} must not be a column"

    # Reading order: pod, namespace, then when, then the peaks.
    order = organize["indexByName"]
    assert order["k8s_pod_name"] < order["k8s_namespace_name"]
    assert order["Value #C"] < order["Value #D"] < order["Value #A"] < order["Value #B"]


def test_pods_seen_table_reads_every_value_from_a_range_function(dashboard: dict) -> None:
    panel = _panels_by_title(dashboard)["Pods seen"]
    targets = panel["targets"]
    assert len(targets) == 4
    for target in targets:
        assert target["instant"] is True
        assert target["format"] == "table"
        assert "[$__range" in target["expr"], "an instant query of a raw metric goes stale after five minutes"

    peak_cpu, peak_memory, first_sample, last_sample = (t["expr"] for t in targets)
    assert peak_cpu.startswith("max by (k8s_pod_name, k8s_namespace_name, k8s_node_name)")
    assert "max_over_time(k8s_pod_cpu_usage" in peak_cpu
    assert "max_over_time(k8s_pod_memory_working_set_bytes" in peak_memory
    assert "min_over_time(timestamp(k8s_pod_memory_working_set_bytes" in first_sample
    assert "max_over_time(timestamp(k8s_pod_memory_working_set_bytes" in last_sample
    # CPU usage is a rate, so a pod scraped only once has memory samples and no
    # CPU sample (four of them in the verified capture). Keying the times on the
    # working-set gauge keeps those pods in the table with real first/last times.
    assert "k8s_pod_cpu_usage" not in first_sample
    assert "k8s_pod_cpu_usage" not in last_sample


def test_pods_seen_table_sample_times_are_milliseconds(dashboard: dict) -> None:
    """timestamp() returns seconds; Grafana's dateTimeAsIso unit reads ms."""
    panel = _panels_by_title(dashboard)["Pods seen"]
    for ref_id, column in (("C", "First sample"), ("D", "Last sample")):
        expr = next(t["expr"] for t in panel["targets"] if t["refId"] == ref_id)
        assert expr.endswith("* 1000"), f"{column}: the subquery result must be converted to milliseconds"
        assert _override_properties(panel, column)["unit"] == "dateTimeAsIso"


def test_pods_seen_table_has_no_node_column(dashboard: dict) -> None:
    """Pod series carry no k8s_node_name label, so a Node column would render
    empty on every row. The table omits it and the description says why."""
    panel = _panels_by_title(dashboard)["Pods seen"]
    assert "Node" not in panel["transformations"][1]["options"]["renameByName"].values()
    assert "k8s_node_name" not in panel["transformations"][1]["options"]["indexByName"]
    assert "no node label" in panel["description"]
    intro = _panels_by_title(dashboard)["What this page shows"]["options"]["content"]
    assert "Node metrics are cluster-wide" in intro


# ---------------------------------------------------------------------------
# Reading guide and time range
# ---------------------------------------------------------------------------


def test_intro_panel_states_the_limits(dashboard: dict) -> None:
    first = dashboard["panels"][0]
    assert first["type"] == "text"
    content = first["options"]["content"]

    assert "60 seconds" in content, "the sample interval must be stated"
    assert "15 days" in content, "the retention must be stated"
    assert "time range must cover the capture" in content, "the range requirement must be stated"
    assert "renders empty" in content, "a capture outside the range must be described plainly"
    assert "five minutes" in content, "the instant-query staleness must be stated"


def test_default_time_range_covers_a_capture(dashboard: dict) -> None:
    assert dashboard["time"] == {"from": "now-24h", "to": "now"}

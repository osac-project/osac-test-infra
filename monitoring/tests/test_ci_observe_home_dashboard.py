"""Structural tests for the ci-observe entry page.

monitoring/config/grafana/dashboards/ci-observe-runs-home.json is the
router, not one of the three ci-observe dashboards (see ci-observe/model.md).
It accepts a run id or capture id in **Find**, a pull request from the
**Pull request lookup**, and a test id from the **Test id lookup**, and it
lists the run index behind an empty input.

The page reads only the workflow exporter's HTTP endpoints. Tempo is not a
data source here: the run index, the verdict cards and the capture lookup
come from /api/ci-observe/runs and /api/ci-observe/captures. The endpoint's
verdict is the only source of `capture`, `not collected` and `unknown`; the
page never derives one from a Tempo answer.

These tests pin five things a reader at the front door depends on:

- the exact lookups: `run=` for a run id and `capture_id=` for a capture id;
- the real counts and both truncation counts, with no page-side row cap;
- the run table's capture link carries `var-capture_id` and the run's own
  start and finish as `from` and `to`;
- each lookup column exists in the dashboard it links to, so the two cannot
  drift apart;
- every selector resolves against a fixture document with the endpoint's keys.

The data-path half lives at the bottom of this file: fixture documents use
the keys the endpoints return, and every column selector is resolved against
them so a filter change that silently empties a panel fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse


DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
HOME_FILE = DASHBOARDS_DIR / "ci-observe-runs-home.json"
HOME = json.loads(HOME_FILE.read_text())
PANELS = {panel["title"]: panel for panel in HOME["panels"]}
INFINITY = {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
TEMPO = {"type": "tempo", "uid": "ci-observe-traces"}
TEMPLATE_TOKEN = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
API = "http://127.0.0.1:9103/api/ci-observe"

PR_LOOKUP = "Pull request lookup, one pull request per row, last $days days (workflow exporter)"
TEST_LOOKUP = "Test id lookup, one test per row, last 7 days (workflow exporter)"
RUN_LOOKUP = "Lookup by run id, exact match (workflow exporter + capture registry)"
CAPTURE_LOOKUP = "Lookup by capture id, exact match (capture registry)"
E2E_RUNS = "Recent E2E runs and their capture, all jobs, last $days days (workflow exporter + capture registry)"
COMPLETENESS = "Run index completeness and capture registry coverage, last $days days"
CARD_TITLES = [
    "Runs in the window, all jobs, last $days days",
    "capture, all jobs, last $days days",
    "not collected, all jobs, last $days days",
    "unknown, all jobs, last $days days",
]

PRS_DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-pull-requests.json").read_text())
TRENDS_DASHBOARD = json.loads((DASHBOARDS_DIR / "ci-observe-trends.json").read_text())


def variables() -> dict[str, dict]:
    return {variable["name"]: variable for variable in HOME["templating"]["list"]}


def override(panel: dict, field: str) -> dict | None:
    for entry in panel["fieldConfig"]["overrides"]:
        if entry["matcher"]["options"] == field:
            return entry
    return None


def properties(panel: dict, field: str) -> dict[str, object]:
    entry = override(panel, field)
    assert entry is not None, f"{panel['title']}: no override for field {field!r}"
    return {prop["id"]: prop["value"] for prop in entry["properties"]}


def links(panel: dict, field: str) -> list[dict]:
    return properties(panel, field).get("links", [])


def selectors(panel: dict) -> set[str]:
    return {column["selector"] for column in panel["targets"][0]["columns"]}


def url_params(panel: dict, **values: str) -> dict[str, str]:
    """The panel's URL with its template variables filled in, as a mapping."""
    url = panel["targets"][0]["url"]
    for name, value in values.items():
        url = url.replace("${" + name + "}", value)
    return dict(parse_qsl(urlparse(url).query))


def resolved_url(panel: dict, field: str, raw: str) -> str:
    """The link's URL with Grafana's row-value tokens filled in."""
    url = links(panel, field)[0]["url"]
    return (
        url.replace("${__value.raw:percentencode}", quote(raw, safe=""))
        .replace("${__value.raw}", raw)
        .replace("${__data.fields.repo}", "osac")
    )


def resolve_rows(document: dict, root_selector: str) -> list[dict]:
    if root_selector == "$":
        return [document]
    node = document
    for part in root_selector.lstrip("$.").split("."):
        node = node[part]
    return node if isinstance(node, list) else [node]


def resolve_field(row: dict, selector: str):
    node = row
    for part in selector.split("."):
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# The page as a router
# ---------------------------------------------------------------------------
def test_the_find_box_is_kept() -> None:
    find = variables()["find"]
    assert find["type"] == "textbox"
    assert find["current"]["value"] == ""
    assert "run id" in find["label"].lower()
    assert "capture id" in find["label"].lower()


def test_the_page_accepts_every_unit() -> None:
    """The four inputs model.md promises: run id, capture id, PR, test id."""
    content = PANELS["Start here"]["options"]["content"]
    assert "run id" in content.lower()
    assert "capture id" in content.lower()
    assert "pull request" in content.lower()
    assert "test id" in content.lower()
    assert "router" in content.lower()


def test_the_intro_defines_every_capture_verdict_and_its_reason() -> None:
    """A reader must know what each verdict means before reading a row."""
    content = PANELS["Start here"]["options"]["content"]
    start = content.index("**Runs with no capture.**")
    paragraph = content[start:].split("\n\n", 1)[0]

    assert "**Capture** column is the registry endpoint's verdict" in paragraph
    assert "**capture** means at least one capture id names the run" in paragraph
    assert "**not collected** means the run finished, the registry covered its window, and no telemetry arrived" in paragraph
    assert "**unknown** means the registry could not tell" in paragraph
    assert "**Why** column carries the reason" in paragraph
    assert "**unknown** is never a claim that a capture is missing" in paragraph
    assert "statement about the recording, not about the result" in paragraph
    assert "never infers a verdict from Tempo" in paragraph


def test_the_intro_glosses_suite_and_osac_in_one_line() -> None:
    content = PANELS["Start here"]["options"]["content"]
    paragraph = content.split("**Suite and OSAC.**", 1)[1].split("\n\n", 1)[0]
    assert "**suite** is the test target" in paragraph
    assert "**OSAC** is the product repository" in paragraph
    assert "source revision" in paragraph


def test_the_intro_labels_each_window() -> None:
    content = PANELS["Start here"]["options"]["content"]
    windows = content.split("**Windows.**", 1)[1].split("\n\n", 1)[0]
    assert "Pull request lookup reads $days days" in windows
    assert "Test id lookup always reads 7 days" in windows
    assert "run table and its cards read $days days" in windows
    assert "dashboard time range does not affect" in windows


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {
        json.loads(path.read_text())["uid"]
        for path in DASHBOARDS_DIR.glob("ci-observe-*.json")
    }
    body = json.dumps(HOME)
    targets = set(re.findall(r"/d/([a-z0-9-]+)/", body))
    assert targets, "the page links to no dashboard"
    assert targets <= uids, f"links to unknown dashboards: {sorted(targets - uids)}"


def test_the_page_has_no_tempo_target() -> None:
    """The registry endpoints answer every question on this page. No panel may
    fall back to a wildcard TraceQL search for capture ids or a span cap."""
    for panel in HOME["panels"]:
        assert panel.get("datasource") != TEMPO, panel["title"]
        for target in panel.get("targets", []):
            assert target.get("queryType") != "traceql", panel["title"]
            assert "query" not in target, panel["title"]
            assert "spss" not in target, panel["title"]
            assert "osac.ci.capture_id" not in json.dumps(target), panel["title"]


# ---------------------------------------------------------------------------
# Pull request lookup
# ---------------------------------------------------------------------------
def test_pull_request_lookup_reads_the_prs_endpoint() -> None:
    panel = PANELS[PR_LOOKUP]
    assert panel["type"] == "table"
    assert panel["datasource"] == INFINITY

    target = panel["targets"][0]
    assert target["url"] == f"{API}/prs?days=${{days}}"
    assert target["root_selector"] == "$.prs"
    assert target["url_options"] == {"method": "GET", "data": ""}


def test_pull_request_lookup_uses_its_own_window() -> None:
    days = variables()["days"]
    assert days["type"] == "custom"
    assert [option["value"] for option in days["options"]] == ["7", "30", "60"]
    assert days["current"]["value"] == "30"
    label = days["label"].lower()
    assert "lookup" in label and "run" in label
    description = days["description"].lower()
    assert "pull request lookup" in description
    assert "runs table" in description


def test_pull_request_lookup_carries_the_columns_the_endpoint_returns() -> None:
    assert selectors(PANELS[PR_LOOKUP]) == {"number", "repo", "title", "final_stage", "url"}


def test_pull_request_lookup_columns_exist_in_the_pull_requests_dashboard() -> None:
    """The lookup reads the same response the Pull requests dashboard reads,
    so every column it shows must be one that dashboard already requires."""
    remote = {
        column["selector"]
        for panel in PRS_DASHBOARD["panels"]
        for target in panel.get("targets", [])
        for column in target.get("columns", [])
    }
    assert selectors(PANELS[PR_LOOKUP]) <= remote


def test_pull_request_lookup_opens_the_pull_requests_dashboard() -> None:
    """`pr_number` and `pr_repo` are the variables that dashboard declares."""
    remote = {variable["name"] for variable in PRS_DASHBOARD["templating"]["list"]}
    url = resolved_url(PANELS[PR_LOOKUP], "number", "101")

    assert url.startswith("/d/ci-observe-pull-requests/ci-observe-pull-requests?")
    assert "var-pr_number=101" in url
    assert "var-pr_repo=osac" in url
    assert {"pr_number", "pr_repo"} <= remote


def test_pull_request_lookup_links_the_github_pull_request() -> None:
    url = resolved_url(PANELS[PR_LOOKUP], "url", "https://github.com/osac-project/osac/pull/101")
    assert url == "https://github.com/osac-project/osac/pull/101"


def test_pull_request_lookup_says_where_its_rows_come_from() -> None:
    description = PANELS[PR_LOOKUP]["description"]
    assert "last $days days" in PANELS[PR_LOOKUP]["title"]
    assert "merged or closed" in description
    assert "one pull request" in description


# ---------------------------------------------------------------------------
# Test id lookup
# ---------------------------------------------------------------------------
def test_test_id_lookup_reads_the_rollup() -> None:
    panel = PANELS[TEST_LOOKUP]
    assert panel["type"] == "table"
    assert panel["datasource"] == INFINITY

    target = panel["targets"][0]
    assert target["url"] == f"{API}/trends?days=7&summary=test"
    assert target["root_selector"] == "tests"
    assert target["url_options"] == {"method": "GET", "data": ""}
    assert selectors(panel) == {"test_id", "suite", "repo", "runs", "failures"}


def test_test_id_lookup_takes_the_sums_from_the_endpoint() -> None:
    """The rollup endpoint returns one row per day and source SHA, capped at
    5000 rows. Summing those rows on the page would silently total only the
    newest days of a busy week, so the page asks the endpoint for the sums
    and renders them unchanged. No transformation may re-aggregate them."""
    panel = PANELS[TEST_LOOKUP]
    params = url_params(panel)
    assert params["summary"] == "test", "the lookup needs the per-test summary, not raw rollup rows"
    assert panel.get("transformations") in (None, [])

    assert properties(panel, "runs")["displayName"] == "Runs"
    assert properties(panel, "failures")["displayName"] == "Failures"
    assert override(panel, "runs (sum)") is None, "a page-side sum would cover only part of the window"


def test_test_id_lookup_columns_exist_in_the_trends_rollup_table() -> None:
    rollup = next(panel for panel in TRENDS_DASHBOARD["panels"] if panel["title"].startswith("Rollup rows"))
    remote = {column["selector"] for column in rollup["targets"][0]["columns"]}
    assert selectors(PANELS[TEST_LOOKUP]) <= remote


def test_test_id_lookup_opens_trends_with_the_test_selected() -> None:
    """`test` is the variable the Trends dashboard queries the rollup with."""
    remote = {variable["name"] for variable in TRENDS_DASHBOARD["templating"]["list"]}
    url = resolved_url(PANELS[TEST_LOOKUP], "test_id", "tests/e2e/vmaas/test_vm::test_restart")

    assert url.startswith("/d/ci-observe-trends/ci-observe-trends?")
    assert f"var-test={quote('tests/e2e/vmaas/test_vm::test_restart', safe='')}" in url
    assert "test" in remote


def test_test_id_lookup_percent_encodes_special_characters() -> None:
    """A parametrized test id can contain &, #, + or spaces; raw interpolation
    would cut the Trends filter at the first one."""
    raw = "tests/e2e/vmaas/test_vm.py::test_x[a&b]"
    url = resolved_url(PANELS[TEST_LOOKUP], "test_id", raw)
    assert f"var-test={quote(raw, safe='')}" in url
    assert "&b]" not in url


def test_test_id_lookup_states_its_fixed_window() -> None:
    description = PANELS[TEST_LOOKUP]["description"]
    assert "last 7 days" in PANELS[TEST_LOOKUP]["title"]
    assert "artifact after 7 days" in description


# ---------------------------------------------------------------------------
# Find: the two exact lookups
# ---------------------------------------------------------------------------
def test_find_by_run_id_reads_the_exact_run_endpoint() -> None:
    panel = PANELS[RUN_LOOKUP]
    assert panel["type"] == "table"
    assert panel["datasource"] == INFINITY

    target = panel["targets"][0]
    assert target["url"] == f"{API}/runs?run=${{find}}"
    assert target["root_selector"] == "$.runs"
    assert target["url_options"] == {"method": "GET", "data": ""}


def test_find_by_capture_id_reads_the_registry_endpoint() -> None:
    panel = PANELS[CAPTURE_LOOKUP]
    assert panel["type"] == "table"
    assert panel["datasource"] == INFINITY

    target = panel["targets"][0]
    assert target["url"] == f"{API}/captures?capture_id=${{find}}"
    assert target["root_selector"] == "$.captures"
    assert target["url_options"] == {"method": "GET", "data": ""}


def test_capture_lookup_carries_the_registry_fields() -> None:
    assert selectors(PANELS[CAPTURE_LOOKUP]) >= {
        "capture_id", "run_id", "run_attempt", "suite", "pr", "workflow",
        "test_ref", "test_sha", "test_infra_ref", "test_infra_sha",
        "test_infra_workflow_sha", "test_suite", "test_filter", "test_marker",
        "identity_source", "capture_started_at", "first_seen_at", "last_seen_at",
    }
    assert "No capture has this id in the registry" in PANELS[CAPTURE_LOOKUP]["fieldConfig"]["defaults"]["noValue"]


# ---------------------------------------------------------------------------
# The run index: cards, completeness and table
# ---------------------------------------------------------------------------
def test_the_run_table_reads_the_runs_endpoint_without_a_page_limit() -> None:
    panel = PANELS[E2E_RUNS]
    assert panel["datasource"] == INFINITY

    params = url_params(panel, days="7")
    assert params == {"days": "7"}, "the endpoint owns the page size; a limit here would hide rows"
    assert panel["targets"][0]["url"].startswith(f"{API}/runs?")
    assert panel["targets"][0]["root_selector"] == "$.runs"


def test_the_page_never_hardcodes_two_hundred_rows() -> None:
    body = json.dumps(HOME)
    assert "limit=200" not in body
    assert "limit=2000" not in body


def test_the_four_cards_read_the_run_index() -> None:
    for title in CARD_TITLES:
        panel = PANELS[title]
        assert panel["type"] == "stat", title
        assert panel["datasource"] == INFINITY, title
        assert panel["targets"][0]["url"] == f"{API}/runs?days=${{days}}", title

    total = PANELS[CARD_TITLES[0]]["targets"][0]
    assert total["root_selector"] == "$", "the window total is the endpoint's total, not the rows shown"
    assert selectors(PANELS[CARD_TITLES[0]]) == {"total"}
    assert PANELS[CARD_TITLES[0]]["options"]["reduceOptions"] == {
        "calcs": ["lastNotNull"],
        "fields": "total",
        "values": False,
    }

    expected = {
        CARD_TITLES[1]: "counts.capture",
        CARD_TITLES[2]: "counts.not collected",
        CARD_TITLES[3]: "counts.unknown",
    }
    for title, field in expected.items():
        panel = PANELS[title]
        # The cards count the whole window from the endpoint's counts, not
        # the 200 rows the table renders.
        assert panel["targets"][0]["root_selector"] == "$", title
        assert selectors(panel) == {field}, title
        assert panel["options"]["reduceOptions"] == {
            "calcs": ["lastNotNull"],
            "fields": field,
            "values": False,
        }, title


def test_the_verdict_cards_sum_to_the_window_total() -> None:
    """A card may never be a page size: the three verdicts plus nothing else
    cover every run in the window."""
    counts = RUNS_DOCUMENT["counts"]
    assert counts["capture"] + counts["not collected"] + counts["unknown"] == counts["matched"]


def test_the_three_verdict_cards_never_filter_the_page() -> None:
    """filterByValue over $.runs would cap a card at the page size."""
    for title in CARD_TITLES[1:]:
        assert PANELS[title].get("transformations") == [], title


def test_the_completeness_panel_states_both_counts_and_truncation() -> None:
    """A truncated page must say how many rows it shows and how many exist."""
    panel = PANELS[COMPLETENESS]
    assert panel["datasource"] == INFINITY
    assert panel["targets"][0]["root_selector"] == "$"
    assert selectors(panel) >= {"truncated", "shown", "total"}
    assert panel["targets"][0]["url"] == f"{API}/runs?days=${{days}}"

    description = panel["description"]
    assert "Shown is the number of run rows returned" in description
    assert "Total is the number of runs" in description
    assert "Truncated says whether more runs matched" in description
    assert "Both counts come from the endpoint" in description

    default = panel["fieldConfig"]["defaults"]["noValue"]
    assert "returned no completeness row" in default


def test_the_run_table_carries_the_identity_the_verdict_and_the_reason() -> None:
    assert selectors(PANELS[E2E_RUNS]) >= {
        "id", "workflow", "trigger", "branch", "conclusion",
        "capture_state", "capture_reason", "identity_source",
        "capture_id", "created_at", "url",
    }


def test_the_run_table_shows_each_verdict_distinctly() -> None:
    """The three states come from the endpoint. Each must be visible and
    distinct: a reader who cannot tell `unknown` from `not collected` will
    read a registry gap as a CI problem."""
    mapping = properties(PANELS[E2E_RUNS], "capture_state")["mappings"][0]["options"]

    assert mapping["capture"]["text"] == "capture"
    assert mapping["capture"]["color"] == "green"
    assert mapping["not collected"]["text"] == "not collected"
    assert mapping["not collected"]["color"] == "orange"
    assert mapping["unknown"]["text"] == "unknown"
    assert mapping["unknown"]["color"] == "text"


def test_the_reason_column_is_visible_next_to_the_verdict() -> None:
    reason = properties(PANELS[E2E_RUNS], "capture_reason")
    assert reason["displayName"] == "Why"
    state = properties(PANELS[E2E_RUNS], "capture_state")
    assert state["displayName"] == "Capture"


def test_the_run_id_links_to_capture_with_the_runs_own_window() -> None:
    """The run row opens Capture with `var-capture_id` and the run's own start
    and finish as `from` and `to`, so a reader does not land on "now"."""
    link = links(PANELS[E2E_RUNS], "id")[0]
    url = link["url"]
    assert url.startswith("/d/ci-observe-runs/ci-observe-e28094-run-investigation?")
    assert "var-capture_id=${__data.fields.capture_id}" in url
    # Epoch ms: Grafana's URL parser ignores a 20-character ISO string.
    assert "from=${__data.fields.started_at_ms}" in url
    assert "to=${__data.fields.done_at_ms}" in url


def test_the_run_id_also_offers_github_when_there_is_no_capture() -> None:
    """A not-collected row has an empty capture id, so the Capture link would
    open with an empty variable. The same cell offers the GitHub run."""
    urls = [link["url"] for link in links(PANELS[E2E_RUNS], "id")]
    assert "${__data.fields.url}" in urls
    assert any("no capture" in link["title"] for link in links(PANELS[E2E_RUNS], "id"))


def test_the_capture_lookup_link_carries_its_own_window() -> None:
    url = links(PANELS[CAPTURE_LOOKUP], "capture_id")[0]["url"]
    assert url.startswith("/d/ci-observe-runs/ci-observe-e28094-run-investigation?")
    assert "var-capture_id=${__value.raw}" in url
    assert "from=${__data.fields.capture_window_from_ms}" in url
    assert "to=${__data.fields.capture_window_to_ms}" in url


def test_a_not_collected_row_links_to_github() -> None:
    """A run with no capture keeps its logs in the GitHub artifact for 7
    days, so the verdict cell leads there."""
    assert properties(PANELS[E2E_RUNS], "capture_state")["displayName"] == "Capture"
    link = links(PANELS[E2E_RUNS], "capture_state")[0]
    assert link["url"] == "${__data.fields.url}"
    assert "GitHub" in link["title"]


def test_the_table_description_keeps_unknown_honest() -> None:
    description = PANELS[E2E_RUNS]["description"]
    assert "not collected" in description
    assert "unknown" in description
    assert "never infers a verdict from Tempo" in description
    assert "7 days" in description
    assert "last $days days" in PANELS[E2E_RUNS]["title"]


def test_only_the_run_table_claims_a_capture_verdict() -> None:
    """A capture verdict needs the registry join, which only /runs does. The
    lookups report registry rows, not what failed to arrive, so a verdict
    value there would be invented."""
    for panel in HOME["panels"]:
        if panel["title"] in (E2E_RUNS, RUN_LOOKUP):
            continue
        for entry in panel["fieldConfig"].get("overrides", []):
            assert "not collected" not in json.dumps(entry), panel["title"]


def test_every_exporter_target_declares_columns() -> None:
    """An Infinity target with no columns renders an empty panel, which looks
    like "no data" instead of a wiring mistake."""
    for panel in HOME["panels"]:
        if panel.get("datasource") != INFINITY:
            continue
        for target in panel["targets"]:
            assert target["root_selector"] is not None, panel["title"]
            assert target["columns"], panel["title"]
            for column in target["columns"]:
                assert column["type"] in ("string", "number", "timestamp"), panel["title"]


def _walk_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def test_no_template_token_is_left_unresolved() -> None:
    names = set(variables()) | {"__value", "__data", "__from", "__to"}
    for text in _walk_strings(HOME):
        for token in TEMPLATE_TOKEN.findall(text):
            assert token in names, f"unresolved template token {token!r} in {text[:80]}"


# ---------------------------------------------------------------------------
# Data path: the columns above against the endpoint contract
# ---------------------------------------------------------------------------
# One response document, pinned here so this file passes in a tree without the
# backend. The real endpoint resolves the same selectors in
# test_ci_observe_runs.py and test_ci_observe_capture.py.
RUNS_DOCUMENT = {
    "days": 7,
    "window_from": "2026-10-28T10:00:00Z",
    "window_to": "2026-11-04T10:00:00Z",
    "source": {
        "state": "ok",
        "last_ok_at": "2026-11-04T09:59:00Z",
        "coverage_from": "2026-10-20T10:00:00Z",
        "captures": 217,
        "rejected": {"count": 2, "ids": ["direct-4418", "router-direct"]},
    },
    "truncated": True,
    "shown": 200,
    "total": 1737,
    "counts": {
        "returned": 200,
        "matched": 1737,
        "capture": 1500,
        "not collected": 200,
        "unknown": 37,
    },
    "runs": [{
        "id": "41310072671",
        "workflow": "E2E VMaaS Full Install",
        "trigger": "PR #123",
        "branch": "fix/restart",
        "conclusion": "failure",
        "created_at": "2026-11-03T10:00:00Z",
        "started_at": "2026-11-03T10:01:00Z",
        "started_at_ms": 1762164060000,
        "done_at_ms": 1762166520000,
        "finished_at": "2026-11-03T10:42:00Z",
        "url": "https://github.com/osac-project/osac/actions/runs/41310072671",
        "capture_state": "capture",
        "capture_ids": ["vmaas-pr123-41310072671-a1"],
        "capture_id": "vmaas-pr123-41310072671-a1",
        "capture_reason": "",
        "identity_source": "info",
    }, {
        "id": "41310072672",
        "workflow": "E2E VMaaS Full Install",
        "trigger": "main",
        "branch": "main",
        "conclusion": "failure",
        "created_at": "2026-11-03T09:00:00Z",
        "started_at": "2026-11-03T09:01:00Z",
        "started_at_ms": 1762160460000,
        "done_at_ms": 1762163400000,
        "finished_at": "2026-11-03T09:50:00Z",
        "url": "https://github.com/osac-project/osac/actions/runs/41310072672",
        "capture_state": "not collected",
        "capture_ids": [],
        "capture_id": "",
        "capture_reason": "",
        "identity_source": "none",
    }, {
        "id": "41310072673",
        "workflow": "E2E VMaaS Full Install",
        "trigger": "main",
        "branch": "main",
        "conclusion": "",
        "created_at": "2026-11-04T09:55:00Z",
        "started_at": "2026-11-04T09:55:00Z",
        "started_at_ms": 1762247700000,
        "done_at_ms": 1762250100000,
        "finished_at": "",
        "url": "https://github.com/osac-project/osac/actions/runs/41310072673",
        "capture_state": "unknown",
        "capture_ids": [],
        "capture_id": "",
        "capture_reason": "the run has not finished",
        "identity_source": "none",
    }],
    "notes": [],
}

CAPTURES_DOCUMENT = {
    "days": 30,
    "window_from": "2026-10-05T10:00:00Z",
    "window_to": "2026-11-04T10:00:00Z",
    "captures": [{
        "capture_id": "vmaas-pr123-41310072671-a1",
        "run_id": "41310072671",
        "run_attempt": "1",
        "suite": "vmaas",
        "pr": "123",
        "workflow": "E2E VMaaS Full Install",
        "test_ref": "refs/pull/123/head",
        "test_sha": "a" * 40,
        "test_infra_ref": "refs/heads/main",
        "test_infra_sha": "b" * 40,
        "test_infra_workflow_sha": "c" * 40,
        "test_suite": "vmaas",
        "test_filter": "none",
        "test_marker": "none",
        "identity_source": "info",
        "capture_started_at": "2026-11-03T10:01:00Z",
        "first_seen_at": "2026-11-03T10:02:00Z",
        "last_seen_at": "2026-11-04T09:59:00Z",
        "capture_window_from": "2026-11-03T10:01:00Z",
        "capture_window_to": "2026-11-04T09:59:00Z",
        "capture_window_from_ms": 1762164060000,
        "capture_window_to_ms": 1762261140000,
    }],
    "notes": [],
}


def test_every_run_column_resolves_against_the_runs_contract() -> None:
    panel = PANELS[E2E_RUNS]
    rows = resolve_rows(RUNS_DOCUMENT, panel["targets"][0]["root_selector"])
    assert rows, "the contract document has no rows"
    for column in panel["targets"][0]["columns"]:
        resolve_field(rows[0], column["selector"])


def test_every_card_column_resolves_against_the_runs_contract() -> None:
    for title in CARD_TITLES:
        panel = PANELS[title]
        rows = resolve_rows(RUNS_DOCUMENT, panel["targets"][0]["root_selector"])
        for column in panel["targets"][0]["columns"]:
            resolve_field(rows[0], column["selector"])


def test_every_completeness_column_resolves_against_the_runs_contract() -> None:
    panel = PANELS[COMPLETENESS]
    (row,) = resolve_rows(RUNS_DOCUMENT, panel["targets"][0]["root_selector"])
    for column in panel["targets"][0]["columns"]:
        resolve_field(row, column["selector"])


def test_every_capture_column_resolves_against_the_capture_contract() -> None:
    panel = PANELS[CAPTURE_LOOKUP]
    rows = resolve_rows(CAPTURES_DOCUMENT, panel["targets"][0]["root_selector"])
    assert rows, "the capture contract has no rows"
    for column in panel["targets"][0]["columns"]:
        resolve_field(rows[0], column["selector"])


def test_the_fixture_documents_show_every_verdict_and_both_counts() -> None:
    """The fixture is the endpoint contract: three verdicts, one empty reason
    for a capture, a real reason for unknown, and both truncation counts."""
    states = {run["capture_state"] for run in RUNS_DOCUMENT["runs"]}
    assert states == {"capture", "not collected", "unknown"}
    assert RUNS_DOCUMENT["truncated"] is True
    assert RUNS_DOCUMENT["shown"] < RUNS_DOCUMENT["total"]
    unknown = next(run for run in RUNS_DOCUMENT["runs"] if run["capture_state"] == "unknown")
    assert unknown["capture_reason"] == "the run has not finished"

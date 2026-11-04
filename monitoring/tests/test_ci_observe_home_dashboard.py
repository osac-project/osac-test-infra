"""Structural tests for the ci-observe entry page.

monitoring/config/grafana/dashboards/ci-observe-runs-home.json is the
router, not one of the three ci-observe dashboards (see ci-observe/model.md).
It accepts a run id or capture id in **Find**, a pull request from the
**Pull request lookup** and a test id from the **Test id lookup**, and it
lists the run index behind an empty input.

These tests pin four things a reader at the front door depends on:

- the Find box is still there and still narrows every Tempo query;
- each lookup reads the endpoint of the dashboard it opens, and its link
  carries the exact Grafana variable that dashboard expects
  (`pr_number`/`pr_repo` for Pull requests, `test` for Trends);
- every lookup column exists in the dashboard it links to, so the two
  cannot drift apart;
- the all-runs table states the honest limit of the data: the workflow
  exporter stores no capture id and cannot read Tempo, so the page cannot
  print a `not collected` verdict on a row, and it must not pretend to.

The test id lookup takes its per-test sums from the endpoint's
`summary=test` mode rather than re-aggregating the capped rollup rows on
the page; that mode and its arithmetic are pinned in
test_ci_observe_trends.py. The data-path half of the all-runs table lives at
the bottom of this file: the real /api/jobs response is resolved against
this dashboard's URL and selectors, so a filter change that silently empties
the table fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote
from urllib.parse import parse_qsl, urlparse


DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
HOME_FILE = DASHBOARDS_DIR / "ci-observe-runs-home.json"
HOME = json.loads(HOME_FILE.read_text())
PANELS = {panel["title"]: panel for panel in HOME["panels"]}
INFINITY = {"type": "yesoreyeram-infinity-datasource", "uid": "workflow-api"}
TEMPLATE_TOKEN = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

PR_LOOKUP = "Pull request lookup, one pull request per row, last $days days (workflow exporter)"
TEST_LOOKUP = "Test id lookup, one test per row, last 7 days (workflow exporter)"
E2E_RUNS = "Recent E2E runs and their capture, all jobs, last $days days (workflow exporter + Tempo)"

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


def test_the_intro_defines_every_capture_verdict() -> None:
    """A reader must know what each verdict means before reading a row."""
    content = PANELS["Start here"]["options"]["content"]
    start = content.index("**Runs with no capture.**")
    paragraph = content[start:].split("\n\n", 1)[0]

    assert "**Capture** column is the verdict" in paragraph
    assert "**capture** means at least one capture id names the run" in paragraph
    assert "**not collected** means GitHub has the run and no telemetry arrived" in paragraph
    assert "**unknown** means the page could not tell" in paragraph
    assert "**unknown** is never a claim that a capture is missing" in paragraph
    assert "statement about the recording, not about the result" in paragraph


def test_every_link_opens_a_dashboard_that_exists() -> None:
    uids = {
        json.loads(path.read_text())["uid"]
        for path in DASHBOARDS_DIR.glob("ci-observe-*.json")
    }
    body = json.dumps(HOME)
    targets = set(re.findall(r"/d/([a-z0-9-]+)/", body))
    assert targets, "the page links to no dashboard"
    assert targets <= uids, f"links to unknown dashboards: {sorted(targets - uids)}"


# ---------------------------------------------------------------------------
# Pull request lookup
# ---------------------------------------------------------------------------
def test_pull_request_lookup_reads_the_prs_endpoint() -> None:
    panel = PANELS[PR_LOOKUP]
    assert panel["type"] == "table"
    assert panel["datasource"] == INFINITY

    target = panel["targets"][0]
    assert target["url"] == "http://127.0.0.1:9103/api/ci-observe/prs?days=${days}"
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
    assert target["url"] == "http://127.0.0.1:9103/api/ci-observe/trends?days=7&summary=test"
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
# The all-runs table and its capture verdict
# ---------------------------------------------------------------------------
def test_all_runs_table_reads_the_runs_endpoint() -> None:
    panel = PANELS[E2E_RUNS]
    assert panel["datasource"] == INFINITY

    params = url_params(panel, days="7")
    assert params["days"] == "7"
    assert params["limit"] == "200"
    assert panel["targets"][0]["url"].startswith("http://127.0.0.1:9103/api/ci-observe/runs?")
    assert panel["targets"][0]["root_selector"] == "$.runs"


def test_all_runs_table_carries_the_identity_and_the_verdict() -> None:
    assert selectors(PANELS[E2E_RUNS]) == {
        "id", "workflow", "trigger", "branch", "conclusion",
        "capture_state", "created_at", "url",
    }


def test_all_runs_table_shows_each_verdict_distinctly() -> None:
    """The three states come from the endpoint. Each must be visible and
    distinct: a reader who cannot tell `unknown` from `not collected` will
    read a Tempo outage as a CI problem."""
    mapping = properties(PANELS[E2E_RUNS], "capture_state")["mappings"][0]["options"]

    assert mapping["capture"]["text"] == "capture"
    assert mapping["capture"]["color"] == "green"
    assert mapping["not collected"]["text"] == "not collected"
    assert mapping["not collected"]["color"] == "orange"
    assert mapping["unknown"]["text"] == "unknown"
    assert mapping["unknown"]["color"] == "text"


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
    assert "Tempo was unreachable" in description
    assert "30-day retention" in description
    assert "last $days days" in PANELS[E2E_RUNS]["title"]


def test_all_runs_table_links_the_run_and_github() -> None:
    panel = PANELS[E2E_RUNS]
    run_url = resolved_url(panel, "id", "41310072671")

    assert run_url.startswith("/d/ci-observe-runs-home/ci-observe-runs-home?")
    assert "var-find=41310072671" in run_url
    assert resolved_url(panel, "url", "https://github.com/osac-project/osac/actions/runs/1") == (
        "https://github.com/osac-project/osac/actions/runs/1"
    )


def test_a_run_id_link_finds_its_capture() -> None:
    """The entry page's own contract: `find` is a regex over the capture id,
    and the capture id embeds the GitHub run id, so a run id clicks through
    to its capture. When no capture exists the same click lands on the
    narrowed index, which is the point of listing the run at all."""
    url = resolved_url(PANELS[E2E_RUNS], "id", "41310072671")
    find = re.search(r"var-find=([^&]+)", url).group(1)

    assert re.search(find, "vmaas-main-41310072671-a1")
    assert re.search(find, "caas-pr123-41310072671-a2")


def test_only_the_runs_table_claims_a_capture_verdict() -> None:
    """A capture verdict per row needs the Tempo join, which only
    /api/ci-observe/runs does. The capture tables above report what arrived,
    not what failed to arrive, so a verdict value there would be invented."""
    for panel in HOME["panels"]:
        if panel["title"] == E2E_RUNS:
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
# One response row, pinned here so this file passes in a tree without the
# backend. The real endpoint resolves the same selectors in
# test_ci_observe_runs.py::test_the_dashboard_selectors_resolve_against_the_endpoint.
RUNS_DOCUMENT = {
    "days": 7,
    "window_from": "2026-10-28T10:00:00Z",
    "window_to": "2026-11-04T10:00:00Z",
    "tempo": {"url": "http://127.0.0.1:3210", "tag": "resource.osac.ci.capture_id",
              "chunks": 1, "cached": False, "captures": 1, "state": "ok", "note": ""},
    "runs": [{
        "id": "41310072671",
        "workflow": "E2E VMaaS Full Install",
        "trigger": "PR #123",
        "branch": "fix/restart",
        "conclusion": "failure",
        "created_at": "2026-11-03T10:00:00Z",
        "url": "https://github.com/osac-project/osac/actions/runs/41310072671",
        "capture_state": "capture",
        "capture_ids": ["vmaas-pr123-41310072671-a1"],
        "capture_reason": "",
    }],
    "notes": [],
}


def test_every_column_resolves_against_the_runs_contract() -> None:
    panel = PANELS[E2E_RUNS]
    selector = panel["targets"][0]["root_selector"]
    node = RUNS_DOCUMENT
    for part in selector.lstrip("$.").split("."):
        node = node[part]
    rows = node if isinstance(node, list) else [node]

    assert rows, "the contract document has no rows"
    for column in panel["targets"][0]["columns"]:
        assert column["selector"] in rows[0], f"column {column['selector']} is not in the contract"

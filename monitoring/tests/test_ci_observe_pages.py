"""Structural tests for the four across-runs pages: Overview, Where the time
goes, Why runs fail and Test history.

The rules every panel follows, so a reader can trust any number on sight:

* It names its population, its time axis and its unit in its description
  (the ⓘ), as "Population: … Time: … Unit: …".
* A windowed panel follows the Window: its query and its description say
  `$days`.
* A duration reads as `d hh:mm:ss`, a share as a percent, hours with an `h`.
* A series wears the categorical palette in its fixed order; a good or bad
  value wears a status color. No other color appears.
* Missing data reads as missing: a stat says "not measured", never 0.

The queries themselves are checked against the served API by
test_ci_observe_dashboard_contract.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DASHBOARDS = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
API = "http://127.0.0.1:9103/api/ci-observe/"
PAGES = {
    "ci-observe-home.json": ("ci-observe-home", "CI Observe — Overview"),
    "ci-observe-time.json": ("ci-observe-time", "CI Observe — Where the time goes"),
    "ci-observe-failures.json": ("ci-observe-failures", "CI Observe — Why runs fail"),
    "ci-observe-test.json": ("ci-observe-test", "CI Observe — Test history"),
}
PALETTE = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
STATUS = ("#0ca30c", "#fab219", "#ec835a", "#d03b3b", "#898781")
PHASES = ("queue", "prepare", "cluster", "build", "install", "test", "collect", "other")
AREAS = ("area_tests", "area_build", "area_install", "area_infra", "area_blocked")


def page(name):
    return json.loads((DASHBOARDS / name).read_text())


def walk(panels):
    for panel in panels:
        yield panel
        yield from walk(panel.get("panels") or [])


def data_panels(document):
    return [panel for panel in walk(document["panels"]) if panel["type"] not in ("row", "text")]


def find(document, title):
    return next(panel for panel in walk(document["panels"]) if panel.get("title") == title)


def overrides(panel, field):
    found = {}
    for entry in panel["fieldConfig"]["overrides"]:
        if entry["matcher"]["options"] == field:
            found.update({prop["id"]: prop["value"] for prop in entry["properties"]})
    return found


def colors(value):
    return set(re.findall(r'"fixedColor": "(#[0-9a-f]{6})"|"color": "(#[0-9a-f]{6})"', json.dumps(value)))


@pytest.mark.parametrize("name", sorted(PAGES))
def test_identity(name):
    document = page(name)
    uid, title = PAGES[name]
    assert (document["uid"], document["title"]) == (uid, title)
    assert "ci-observe" in document["tags"]
    assert document["timepicker"]["hidden"] is True, "the Window variable is the time range"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_the_filters_are_window_suite_trigger_and_repository(name):
    document = page(name)
    variables = {variable["name"]: variable for variable in document["templating"]["list"]}
    expected = {"days", "suite", "trigger", "repo"} | ({"test"} if name == "ci-observe-test.json" else set())
    assert set(variables) == expected
    assert [option["value"] for option in variables["days"]["options"]] == ["7", "14", "30", "90"]
    assert variables["days"]["current"]["value"] == ("30" if name == "ci-observe-test.json" else "7")
    assert [option["value"] for option in variables["suite"]["options"]] == ["all", "vmaas", "caas", "bmaas"]
    assert [option["value"] for option in variables["trigger"]["options"]] == [
        "all", "pr", "merge_queue", "scheduled", "manual",
    ]
    assert [option["value"] for option in variables["repo"]["options"]] == ["all", "osac", "osac-test-infra"]
    for variable in variables.values():
        if variable["type"] == "custom":
            assert variable["current"]["selected"] is True
            texts = [f"{option['text']} : {option['value']}" for option in variable["options"]]
            assert variable["query"] == ",".join(texts), variable["name"]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_query_carries_the_filters(name):
    for panel in data_panels(page(name)):
        for target in panel["targets"]:
            assert target["url"].startswith(API), panel["title"]
            if "/api/ci-observe/prs" in target["url"]:
                assert target["url"].endswith("?days=${days}&repo=${repo}"), panel["title"]
                continue
            for variable in ("${days}", "${suite}", "${repo}", "${trigger}"):
                assert variable in target["url"], (panel["title"], variable)
            if name == "ci-observe-test.json" and "/api/ci-observe/test?" in target["url"]:
                assert "&test=${test:percentencode}" in target["url"], panel["title"]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_panel_names_its_population_time_and_unit(name):
    for panel in data_panels(page(name)):
        description = panel.get("description", "")
        for word in ("Population:", "Time:", "Unit:"):
            assert word in description, (panel["title"], word)
        if panel["title"] != "About these numbers":
            assert "$days" in description, panel["title"]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_page_ends_with_what_it_counts(name):
    document = page(name)
    last = max(document["panels"], key=lambda panel: panel["gridPos"]["y"])
    assert last["title"] == "About these numbers"
    assert last["targets"][0]["root_selector"] == "about"
    assert [column["selector"] for column in last["targets"][0]["columns"]] == [
        "population", "window", "missing", "generated_at",
    ]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_durations_shares_and_hours_read_one_way(name):
    for panel in data_panels(page(name)):
        units = [panel["fieldConfig"]["defaults"].get("unit")]
        for entry in panel["fieldConfig"]["overrides"]:
            units += [prop["value"] for prop in entry["properties"] if prop["id"] == "unit"]
        for unit in units:
            assert unit in (None, "dtdhms", "percentunit", "suffix:h", "none", "dateTimeAsLocal"), (
                panel["title"], unit,
            )
        fields = [column["selector"] for target in panel["targets"] for column in target["columns"]]
        for field in fields:
            if field.endswith("_s") and panel["type"] == "table" and not overrides(panel, field).get("custom.hidden"):
                assert overrides(panel, field).get("unit") == "dtdhms", (panel["title"], field)


@pytest.mark.parametrize("name", sorted(PAGES))
def test_a_stat_without_data_says_not_measured(name):
    for panel in data_panels(page(name)):
        if panel["type"] == "stat":
            assert panel["fieldConfig"]["defaults"]["noValue"] == "not measured", panel["title"]


@pytest.mark.parametrize("name", sorted(PAGES))
def test_only_palette_and_status_colors_appear(name):
    used = {color for pair in colors(page(name)["panels"]) for color in pair if color}
    assert used <= set(PALETTE) | set(STATUS), used - set(PALETTE) - set(STATUS)


def series_colors(panel, fields):
    return [overrides(panel, field)["color"]["fixedColor"] for field in fields]


def test_phases_wear_the_palette_in_run_order():
    for name, title in (("ci-observe-home.json", "Where a passing run spends its time"),
                        ("ci-observe-time.json", "A passing run, phase by phase"),
                        ("ci-observe-time.json", "Phase time per day")):
        panel = find(page(name), title)
        fields = [column["selector"] for column in panel["targets"][0]["columns"][1:]]
        assert fields == list(PHASES), title
        assert series_colors(panel, fields) == list(PALETTE), title


def test_failure_areas_wear_the_first_five_colors():
    for name, title in (("ci-observe-home.json", "Why runs failed"),
                        ("ci-observe-failures.json", "Failed runs per day, by area"),
                        ("ci-observe-failures.json", "Failures by suite")):
        panel = find(page(name), title)
        fields = [column["selector"] for column in panel["targets"][0]["columns"][1:]]
        assert fields == list(AREAS), title
        assert series_colors(panel, fields) == list(PALETTE[:5]), title


def test_pass_and_fail_wear_status_colors():
    test_page = page("ci-observe-test.json")
    runs = find(test_page, "Duration of every run")
    assert overrides(runs, "passed_s")["color"]["fixedColor"] == "#0ca30c"
    assert overrides(runs, "failed_s")["color"]["fixedColor"] == "#d03b3b"
    assert overrides(runs, "passed_s")["custom.drawStyle"] == "points", "one point per run"


def test_every_link_keeps_the_filters_between_the_four_pages():
    for name in PAGES:
        links = {link["title"]: link for link in page(name)["links"]}
        for title in ("Overview", "Where the time goes", "Why runs fail", "Test history"):
            if PAGES[name][1].endswith(title):
                assert title not in links, "a page does not link to itself"
                continue
            assert links[title]["includeVars"] is True, (name, title)
        assert links["Pull requests"]["includeVars"] is False, "Pull requests has its own window"
        assert links["All runs"]["includeVars"] is False, "the runs list has its own suite list"


def test_the_overview_answers_every_question_on_one_page():
    """Is CI healthy, where does the time go, why do runs fail, which tests
    hurt, and how long pull requests wait: one panel each, linked to the page
    that goes deeper."""
    overview = page("ci-observe-home.json")
    titles = [panel["title"] for panel in data_panels(overview)]
    for title in (
        "Pass rate", "Typical time to result", "Typical wait for a runner", "Failures caused by CI",
        "Suite health", "Where a passing run spends its time", "Why runs failed", "Pass rate per day",
        "Where the tests' time goes", "What the tests wait for in the product", "Tests that fail most",
        "Tests that take the most time", "Latest failed runs", "Wait for first review", "In the merge queue",
        "Rebased",
    ):
        assert title in titles, title
    suite_health = find(overview, "Suite health")
    assert overrides(suite_health, "time_url")["links"][0]["url"] == "${__value.raw}"
    assert overrides(suite_health, "failures_url")["links"][0]["url"] == "${__value.raw}"


def test_a_test_name_always_opens_its_history():
    for name in PAGES:
        for panel in data_panels(page(name)):
            fields = [column["selector"] for column in panel["targets"][0]["columns"]]
            if "test_url" in fields and "test" in fields:
                assert overrides(panel, "test")["links"][0]["url"] == "${__data.fields.test_url}", panel["title"]
                assert overrides(panel, "test_url")["custom.hidden"] is True


def test_a_run_always_opens_its_run_page_or_github():
    for name in PAGES:
        for panel in data_panels(page(name)):
            fields = [column["selector"] for column in panel["targets"][0]["columns"]]
            if "run" in fields:
                assert "details_url" in fields, panel["title"]
                assert overrides(panel, "run")["links"][0]["url"] == "${__data.fields.details_url}"


def test_the_test_picker_lists_tests_by_the_time_they_take():
    variable = next(item for item in page("ci-observe-test.json")["templating"]["list"] if item["name"] == "test")
    query = variable["query"]["infinityQuery"]
    assert query["url"] == API + "tests?days=${days}&suite=${suite}&repo=${repo}&trigger=${trigger}&sort=time"
    assert query["root_selector"] == "$.options"
    assert [column["text"] for column in query["columns"]] == ["__text", "__value"]
    assert variable["refresh"] == 1, "the list follows the window"
    assert variable["allowCustomValue"] is True, "a pasted node id works"


def test_the_part_to_whole_bars_add_up():
    """Where the tests' time goes: four shares of one whole, labeled on the
    bar. Where this test's time goes: four durations that add up to the
    test's duration."""
    shares = find(page("ci-observe-time.json"), "Where the tests' time goes")
    assert [column["selector"] for column in shares["targets"][0]["columns"][1:]] == [
        "setup_share", "waits_share", "actions_share", "teardown_share",
    ]
    assert shares["fieldConfig"]["defaults"]["unit"] == "percentunit"
    assert shares["fieldConfig"]["defaults"]["max"] == 1
    assert shares["options"]["showValue"] == "auto"
    split = find(page("ci-observe-test.json"), "Where this test's time goes")
    assert [column["selector"] for column in split["targets"][0]["columns"][1:]] == [
        "setup", "waits", "actions", "teardown",
    ]
    assert split["options"]["stacking"] == "normal"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_no_template_token_is_left_unresolved(name):
    document = page(name)
    known = {variable["name"] for variable in document["templating"]["list"]}
    for token in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", json.dumps(document)):
        assert token in known or token.startswith("__"), token


@pytest.mark.parametrize("name", sorted(PAGES))
def test_panels_do_not_overlap(name):
    taken = set()
    for panel in page(name)["panels"]:
        position = panel["gridPos"]
        cells = {(x, y) for x in range(position["x"], position["x"] + position["w"])
                 for y in range(position["y"], position["y"] + position["h"])}
        assert not cells & taken, panel.get("title")
        assert position["x"] + position["w"] <= 24, panel.get("title")
        taken |= cells

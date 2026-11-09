"""Every ci-observe panel reads fields the API serves.

The pages are JSON and the API is Python, so a renamed field breaks a panel
without failing anything else. This test serves the real workflow exporter
over the seeded dataset (ci_observe_seed: job timelines, JUnit rows, span
rollup, pull requests) and runs every Infinity query of every ci-observe
page against it, with the page's own default filters: the root selector
must resolve and every column must be a field of the rows it returns.

It also pins the links: every page a panel, a link or the API points at
exists, under the slug Grafana derives from its title.
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest

import ci_observe_seed
from helpers import FakeGitHub

DASHBOARDS = Path(__file__).resolve().parents[1] / "config" / "grafana" / "dashboards"
API = "http://127.0.0.1:9103"
NOW = datetime.now(UTC).replace(microsecond=0)

# Panels that read the trace through Tempo (the capture view): no Tempo
# here, so their fields are pinned by test_ci_observe_dashboard.py and by
# test_the_capture_rows_carry_every_run_page_column below.
TEMPO_PATHS = {"/api/ci-observe/capture"}
# Roots the seed leaves empty on purpose: a run page's JUnit evidence comes
# from a GitHub artifact the seed does not serve.
MAY_BE_EMPTY = {("/api/ci-observe/run", "native.failed_tests")}


def pages():
    return {path.name: json.loads(path.read_text()) for path in sorted(DASHBOARDS.glob("ci-observe-*.json"))}


def walk(panels):
    for panel in panels:
        yield panel
        yield from walk(panel.get("panels") or [])


def infinity_targets(document):
    """(panel title, target) for every Infinity query of the page, panels and variables."""
    for panel in walk(document["panels"]):
        for target in panel.get("targets") or []:
            if target.get("url", "").startswith(API):
                yield panel.get("title") or panel["type"], target
    for variable in document["templating"]["list"]:
        query = variable.get("query")
        if isinstance(query, dict) and "infinityQuery" in query:
            yield f"variable {variable['name']}", query["infinityQuery"]


def grafana_slug(title):
    """Grafana's slug of a title: ASCII letters and digits lowercased, spaces
    and dashes to one dash, any other character as the hex of its UTF-8."""
    parts = []
    for char in title.strip():
        if char.isascii() and char.isalnum():
            parts.append(char.lower())
        elif char in " -_":
            parts.append("-")
        elif not char.isascii():
            parts.append(char.encode().hex())
    return re.sub(r"-+", "-", "".join(parts)).strip("-")


def resolve(document, root):
    node = document
    path = root.removeprefix("$").lstrip(".")
    for part in path.split(".") if path else []:
        assert isinstance(node, dict) and part in node, f"root {root!r}: no {part!r}"
        node = node[part]
    return node if isinstance(node, list) else [node]


class Served:
    def __init__(self, base_url, seeded):
        self.base_url = base_url
        self.seeded = seeded
        self.cache = {}

    def get(self, url):
        if url not in self.cache:
            parsed = urlparse(url)
            target = urlparse(self.base_url)
            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=30)
            try:
                connection.request("GET", f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path)
                response = connection.getresponse()
                body = response.read()
                assert response.status == 200, f"{url}: HTTP {response.status}"
                self.cache[url] = json.loads(body)
            finally:
                connection.close()
        return self.cache[url]


@pytest.fixture(scope="module")
def served(wfe, tmp_path_factory):
    directory = tmp_path_factory.mktemp("seed")
    seeded = ci_observe_seed.seed(directory, now=NOW)
    patch = pytest.MonkeyPatch()
    patch.setattr(wfe, "CACHE_DIR", str(directory))
    patch.setattr(wfe, "DB_FILE", seeded["jobs_db"])
    patch.setattr(wfe, "LEGACY_CACHE_FILE", str(directory / "workflow-exporter-cache.json"))
    patch.setattr(wfe, "TOKEN", None)
    patch.setattr(wfe, "REPOS_FILTER", [])
    # Nothing leaves the test: every GitHub call is a 404.
    github = FakeGitHub(wfe.API_URL)
    patch.setattr(wfe.requests, "get", github.get)
    patch.setattr(wfe.requests, "post", github.post)
    exporter = wfe.WorkflowExporter()
    patch.setattr(wfe.ExporterHandler, "exporter", exporter)
    server = ThreadingHTTPServer(("127.0.0.1", 0), wfe.ExporterHandler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield Served(f"http://127.0.0.1:{server.server_address[1]}", seeded)
    finally:
        server.shutdown()
        server.server_close()
        patch.undo()


def variable_values(document, served):
    """The page's defaults: a custom variable's current value, a query
    variable's first option, and a recorded capture for the run page."""
    values = {}
    for variable in document["templating"]["list"]:
        if variable["type"] == "custom":
            values[variable["name"]] = variable["current"]["value"]
    for variable in document["templating"]["list"]:
        query = variable.get("query")
        if variable["type"] != "query":
            continue
        if isinstance(query, dict) and "infinityQuery" in query:
            infinity = query["infinityQuery"]
            rows = resolve(served.get(interpolate(infinity["url"], values)), infinity["root_selector"])
            value_column = next(column["selector"] for column in infinity["columns"] if column["text"] == "__value")
            assert rows, f"variable {variable['name']} has no options on the seed"
            if variable.get("includeAll") and (variable.get("current") or {}).get("value") == "$__all":
                # A page that opens on All sends the variable's All value.
                values[variable["name"]] = variable["allValue"]
                continue
            values[variable["name"]] = str(rows[0][value_column])
            if variable["name"] == "pr_number":
                # The newest pull request may have run no E2E suite, which the
                # page shows as such; check the panels against one that did.
                def ran_e2e(number):
                    url = f"{API}/api/ci-observe/prs?number={number}&repo=${{pr_repo}}&days=${{days}}"
                    return bool(served.get(interpolate(url, values)).get("runs"))

                numbers = [str(row[value_column]) for row in rows]
                values["pr_number"] = next((number for number in numbers if ran_e2e(number)), numbers[0])
        elif variable["name"] == "capture_id":
            conn = sqlite3.connect(served.seeded["jobs_db"])
            values["capture_id"] = conn.execute("SELECT capture_id FROM captures ORDER BY capture_id").fetchone()[0]
            conn.close()
    return values


def interpolate(text, values):
    def encoded(match):
        return quote(str(values[match.group(1)]), safe="")

    text = re.sub(r"\$\{(\w+):percentencode\}", encoded, text)
    text = re.sub(r"\$\{(\w+)\}", lambda match: str(values[match.group(1)]), text)
    return re.sub(r"\$(\w+)", lambda match: str(values.get(match.group(1), match.group(0))), text)


@pytest.mark.parametrize("name", sorted(pages()))
def test_every_query_resolves_against_the_served_api(served, name):
    document = pages()[name]
    values = variable_values(document, served)
    checked = 0
    for title, target in infinity_targets(document):
        url = interpolate(target["url"], values)
        path = urlparse(url).path
        if path in TEMPO_PATHS:
            continue
        rows = resolve(served.get(url), target["root_selector"])
        if not rows:
            assert (path, target["root_selector"]) in MAY_BE_EMPTY, f"{name} / {title}: no rows at {url}"
            continue
        fields = set().union(*(row.keys() for row in rows if isinstance(row, dict)))
        for column in target["columns"]:
            assert column["selector"] in fields, f"{name} / {title}: {column['selector']!r} is not served by {url}"
            if column["type"] == "timestamp":
                value = next(row[column["selector"]] for row in rows if row.get(column["selector"]))
                assert datetime.fromisoformat(str(value).replace("Z", "+00:00")), (name, title, value)
        checked += 1
    assert checked, f"{name} has no query this test could check"


def test_the_capture_rows_carry_every_run_page_column(wfe):
    """The run page's wait and fixture tables read rows the capture view
    builds from spans; those builders are pure, so their keys are checked
    without Tempo."""
    import ci_observe_capture_view as view

    entry = {
        "name": "wait for ComputeInstance ci-1 to become Ready", "worker": "gw0",
        "started_at": "2026-11-01T10:00:00Z", "duration_s": 12.0, "status": "ok",
        "attributes": {"poll.attempts": 4, "poll.condition_met": True, "osac.wait.resource_kind": "ComputeInstance",
                       "pytest.fixture_scope": "session", "code.function": "cluster"},
    }
    rows = {"waits": view.CIObserveCaptureView._wait_row(entry),
            "fixtures": view.CIObserveCaptureView._fixture_row({**entry, "name": "cluster setup"})}
    document = pages()["ci-observe-runs.json"]
    checked = 0
    for title, target in infinity_targets(document):
        if urlparse(target["url"]).path != "/api/ci-observe/capture" or target["root_selector"] not in rows:
            continue
        for column in target["columns"]:
            assert column["selector"] in rows[target["root_selector"]], (title, column["selector"])
        checked += 1
    assert checked == 2


def page_paths():
    """{"/d/<uid>/<slug>": title} for every ci-observe page."""
    return {
        f"/d/{document['uid']}/{grafana_slug(document['title'])}": document["title"]
        for document in pages().values()
    }


def test_the_page_paths_match_the_provisioned_pages(wfe):
    import ci_observe_phases as phases

    paths = page_paths()
    for constant in ("OVERVIEW_PAGE", "TIME_PAGE", "FAILURES_PAGE", "TEST_PAGE", "PR_PAGE", "RUNS_PAGE", "RUN_PAGE"):
        assert getattr(phases, constant) in paths, constant


def test_the_slug_rule_matches_grafana():
    assert grafana_slug("CI Observe — Start here") == "ci-observe-e28094-start-here"
    assert grafana_slug("CI Observe — E2E run") == "ci-observe-e28094-e2e-run"


def dashboard_uids():
    return {document["uid"] for document in pages().values()}


def links_in(value):
    """Every /d/<uid> link in a page or a document."""
    return re.findall(r"/d/([A-Za-z0-9_-]+)", json.dumps(value))


@pytest.mark.parametrize("name", sorted(pages()))
def test_every_link_on_a_page_opens_a_page_that_exists(name):
    uids = dashboard_uids()
    for uid in links_in(pages()[name]):
        assert uid in uids, f"{name} links to {uid}"


def test_every_link_the_api_serves_opens_a_page_that_exists(served):
    uids = dashboard_uids()
    tests = served.get(f"{API}/api/ci-observe/tests?days=30")["tests"]
    documents = [
        served.get(f"{API}/api/ci-observe/{kind}?days=30")
        for kind in ("health", "time", "failures", "tests", "waits")
    ] + [
        served.get(f"{API}/api/ci-observe/test?days=30&test={quote(tests[0]['test_key'], safe='')}"),
        served.get(f"{API}/api/ci-observe/prs?days=30&repo=osac"),
        served.get(f"{API}/api/ci-observe/runs?days=30"),
    ]
    seen = 0
    for document in documents:
        for uid in links_in(document):
            assert uid in uids, f"a served document links to {uid}"
            seen += 1
    assert seen > 50, "the documents carry page links"


def test_the_overview_is_the_entry_page():
    overview = pages()["ci-observe-home.json"]
    assert overview["title"] == "CI Observe — Overview"
    for name, document in pages().items():
        if name == "ci-observe-home.json":
            continue
        assert any(link["url"].startswith("/d/ci-observe-home/") for link in document["links"]), name

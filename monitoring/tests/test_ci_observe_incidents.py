"""The incidents layer (ci_observe_incidents.py): root-cause records a
person wrote, served where a window asks for them.

Hand-built files pin each rule: what a record must hold (the diagnosis
confidence, the terminal-status and follow-up gates, the vague-verb and
human-error lints), which windows a record's window overlaps, the one line
the health and failure answers carry for an overlap -- and only for the
suites and repositories the query counts -- and the degradation when the
file cannot be read (a note, never an error, never a silent empty answer).
The repo's own incidents file is pinned too: it is the seed data the layer
serves in production, and its migration to the record structure is part of
what the layer promises to read.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
import pytest

from test_ci_observe_insights import NOW, Stores, get

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REPO_INCIDENTS = Path(__file__).resolve().parents[1] / "config" / "ci-observe" / "incidents.yml"


@pytest.fixture
def stores(tmp_path):
    """The same hand-built stores the insights tests use, with the incidents
    file next to them."""
    return Stores(tmp_path)


def _load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


incidents = _load("ci_observe_incidents", "ci_observe_incidents.py")
insights = _load("ci_observe_insights", "ci_observe_insights.py")


def write(path, records):
    Path(path).write_text(yaml.safe_dump({"incidents": records}), encoding="utf-8")


def record(**overrides):
    """One usable incident record; the overrides replace whole fields."""
    base = {
        "id": "INC-1",
        "title": "Installs fail on a broken snapshot",
        "status": "identified",
        "diagnosis_confidence": "confirmed",
        "window": {"from": "2026-11-05T00:00:00Z"},
        "areas": ["install"],
        "suites": ["vmaas", "caas", "bmaas"],
        "repos": ["osac", "osac-test-infra"],
        "summary": "Install failures doubled from Nov 5. The snapshots were broken.",
        "contributing_factors": ["The freshness check failed silently, so broken snapshots rolled out."],
        "evidence": [{"label": "the post-mortem", "url": "https://example.com/pm"}],
        "follow_ups": [{"text": "rebuild the snapshot", "owner": "", "due": "", "ticket": "",
                        "state": "open"}],
    }
    base.update(overrides)
    return base


def moment(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# -- the file and its records ---------------------------------------------------------


def test_a_record_loads_with_its_optionals_defaulted(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [{"id": "INC-1", "title": "t", "status": "identified",
                  "diagnosis_confidence": "suspected",
                  "window": {"from": "2026-11-05"},
                  "summary": "s",
                  "evidence": [{"label": "the post-mortem", "url": "https://example.com/pm"}]}])
    records, notes, unavailable = incidents.load_incidents(str(path))
    assert unavailable == "" and notes == []
    [only] = records
    assert only["_to"] is None, "no window.to means ongoing"
    assert (only["areas"], only["suites"], only["repos"]) == ([], [], [])
    assert only["contributing_factors"] == []
    assert only["follow_ups"] == []


def test_an_old_records_single_cause_and_plain_follow_ups_still_load(tmp_path):
    """A record written before the structure is not re-diagnosed to be read:
    the single `cause` line is one contributing factor, a plain follow-up
    line is an open one with the rest empty."""
    path = tmp_path / "incidents.yml"
    write(path, [{"id": "INC-1", "title": "t", "status": "identified",
                  "diagnosis_confidence": "suspected",
                  "window": {"from": "2026-11-05"},
                  "summary": "s",
                  "cause": "The freshness check failed silently.",
                  "evidence": [{"label": "the post-mortem", "url": "https://example.com/pm"}],
                  "follow_ups": ["rebuild the snapshot"]}])
    records, notes, unavailable = incidents.load_incidents(str(path))
    assert unavailable == "" and notes == []
    [only] = records
    assert only["contributing_factors"] == ["The freshness check failed silently."]
    assert "cause" not in only, "the old spelling is folded into the list it became"
    assert only["follow_ups"] == [{"text": "rebuild the snapshot", "owner": "", "due": "",
                                   "ticket": "", "state": "open", "declined_reason": ""}]


def test_records_are_served_oldest_window_first(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-new", window={"from": "2026-11-10T00:00:00Z"}),
                 record(id="INC-old", window={"from": "2026-11-01T00:00:00Z"})])
    records, _notes, unavailable = incidents.load_incidents(str(path))
    assert unavailable == ""
    assert [one["id"] for one in records] == ["INC-old", "INC-new"]


def test_a_missing_file_is_a_reason_not_an_error(tmp_path):
    records, notes, unavailable = incidents.load_incidents(str(tmp_path / "absent.yml"))
    assert records == [] and notes == []
    assert "absent.yml" in unavailable and "cannot be read" in unavailable


def test_a_file_that_is_not_yaml_or_not_the_incidents_mapping_is_a_reason(tmp_path):
    broken = tmp_path / "broken.yml"
    broken.write_text("title: [unclosed", encoding="utf-8")
    assert "cannot be read" in incidents.load_incidents(str(broken))[2]
    listed = tmp_path / "listed.yml"
    listed.write_text("- id: INC-1\n", encoding="utf-8")
    records, notes, unavailable = incidents.load_incidents(str(listed))
    assert records == [] and notes == []
    assert 'holds no "incidents" list of records' in unavailable
    empty = tmp_path / "empty.yml"
    empty.write_text("", encoding="utf-8")
    assert incidents.load_incidents(str(empty)) == ([], [], "")
    none_left = tmp_path / "none-listed.yml"
    none_left.write_text("incidents: []\n", encoding="utf-8")
    assert incidents.load_incidents(str(none_left)) == ([], [], "")


def test_a_record_that_does_not_hold_its_shape_is_named_and_left_out(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [
        "just a string",
        record(id="INC-bad-status", status="resolved"),
        record(id="INC-no-window", window={}),
        record(id="INC-bad-to", window={"from": "2026-11-05T00:00:00Z", "to": "Nov 5"}),
        record(id="INC-bad-evidence", evidence=[{"label": "no url"}]),
        record(id="INC-good"),
    ])
    records, notes, unavailable = incidents.load_incidents(str(path))
    assert unavailable == ""
    assert [one["id"] for one in records] == ["INC-good"]
    assert len(notes) == 5
    assert "incident record 1 is not usable" in notes[0]
    for note in notes[1:]:
        assert note.startswith("incident INC-"), "the note names the record"
    assert "status 'resolved' is none of" in notes[1]
    assert "window.from is missing" in notes[2]
    assert "window.to is not a UTC date or time" in notes[3]
    assert "evidence is not a list of label and url" in notes[4]


def test_a_record_recorded_twice_keeps_the_first(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(), record(title="Another take on the same incident")])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert len(records) == 1
    assert records[0]["title"] == "Installs fail on a broken snapshot"
    assert any("recorded twice" in note for note in notes)


# -- the validator gates ----------------------------------------------------------------


def test_a_record_needs_its_diagnosis_confidence(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-none", diagnosis_confidence=None),
                 record(id="INC-likely", diagnosis_confidence="likely"),
                 record(id="INC-suspected", diagnosis_confidence="suspected")])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-suspected"]
    assert "diagnosis_confidence None is none of confirmed, suspected, superseded" in notes[0]
    assert "diagnosis_confidence 'likely' is none of confirmed, suspected, superseded" in notes[1]


def test_a_terminal_record_must_say_when_it_ended(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-open-end", status="fixed"),
                 record(id="INC-closed-end", status="closed",
                        window={"from": "2026-11-05T00:00:00Z", "to": "2026-11-10T00:00:00Z"},
                        follow_ups=[{"text": "rebuild the snapshot", "state": "done"}])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-closed-end"]
    assert ("incident INC-open-end is not usable and was left out: status 'fixed' is terminal, so"
            " window.to must say when the incident ended (UTC)") in notes[0]


def test_a_terminal_record_may_not_leave_an_open_follow_up(tmp_path):
    path = tmp_path / "incidents.yml"
    ended = {"from": "2026-11-05T00:00:00Z", "to": "2026-11-10T00:00:00Z"}
    write(path, [record(id="INC-left-open", status="fixed", window=ended),
                 record(id="INC-wrapped-up", status="fixed", window=ended,
                        follow_ups=[{"text": "rebuild the snapshot", "state": "done"},
                                    {"text": "alert on stale snapshots", "state": "declined",
                                     "declined_reason": "the flavor-tag move covers it"}])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-wrapped-up"]
    assert ("incident INC-left-open is not usable and was left out: status 'fixed' is terminal, but"
            " follow-up 1 is still open: mark it done, decline it with a declined_reason, or the"
            " incident is not over") in notes[0]
    assert records[0]["follow_ups"][1]["declined_reason"] == "the flavor-tag move covers it"


def test_a_declined_follow_up_needs_its_reason(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-why-not", follow_ups=[{"text": "rebuild the snapshot",
                                                       "state": "declined"}]),
                 record(id="INC-said-why", follow_ups=[{"text": "rebuild the snapshot",
                                                        "state": "declined",
                                                        "declined_reason": "the flavor is gone"}])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-said-why"]
    assert ("incident INC-why-not is not usable and was left out: follow-up 1 is declined without"
            " a declined_reason: say why the work is not wanted") in notes[0]


def test_a_follow_up_that_is_only_a_verb_is_left_out(tmp_path):
    """The vague-verb lint: "improve" alone names no work, while the verb
    with an object does ("Investigate why 4.22.14 clones place br-ex on the
    secondary network")."""
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-improve", follow_ups=["improve"]),
                 record(id="INC-investigate", follow_ups=["Investigate"]),
                 record(id="INC-review", follow_ups=["review."]),
                 record(id="INC-object",
                        follow_ups=["Investigate why 4.22.14 clones place br-ex on the secondary"
                                    " network"])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-object"]
    assert len(notes) == 3
    assert all("is a verb with no object: say what to improve, investigate or review" in note
               for note in notes)


def test_human_error_is_not_a_contributing_factor(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-blame", contributing_factors=["Human error in the release checklist"]),
                 record(id="INC-check", contributing_factors=["The release checklist had no step"
                                                              " for the credential rotation"])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-check"]
    assert ("incident INC-blame is not usable and was left out: contributing factor 1 says"
            " 'human error': name the missing check or the wrong assumption that let the mistake"
            " through, not the person") in notes[0]


def test_a_record_needs_a_link_for_its_evidence(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(id="INC-no-evidence", evidence=[]),
                 record(id="INC-empty-url", evidence=[{"label": "no url", "url": ""}]),
                 record(id="INC-linked", evidence=[{"label": "the post-mortem",
                                                    "url": "https://example.com/pm"}])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-linked"]
    assert len(notes) == 2
    assert all("evidence holds no link: a record needs at least one url" in note for note in notes)


def test_an_evidence_window_must_be_a_utc_span(tmp_path):
    path = tmp_path / "incidents.yml"
    the_link = {"label": "the post-mortem", "url": "https://example.com/pm"}
    write(path, [record(id="INC-bad-from", evidence=[{**the_link, "window": {"from": "Nov 5"}}]),
                 record(id="INC-bad-to", evidence=[{**the_link, "window": {"from": "2026-11-05",
                                                                          "to": "Nov 10"}}]),
                 record(id="INC-spanned", evidence=[{**the_link, "window": {"from": "2026-11-05",
                                                                           "to": "2026-11-10"}}])])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert [one["id"] for one in records] == ["INC-spanned"]
    assert "evidence 1's window is not a UTC from and to span" in notes[0]
    assert "evidence 1's window.to is not a UTC date or time" in notes[1]
    assert records[0]["evidence"][0]["window"] == {"from": "2026-11-05T00:00:00Z",
                                                   "to": "2026-11-10T00:00:00Z"}


def test_a_record_may_supersede_another_and_a_dangling_pointer_is_a_note(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(), record(id="INC-2", title="The real cause", supersedes="INC-1")])
    records, notes, unavailable = incidents.load_incidents(str(path))
    assert unavailable == "" and notes == []
    assert incidents.row(records[1])["supersedes"] == "INC-1"

    write(path, [record(), record(id="INC-2", title="The real cause", supersedes="INC-absent")])
    records, notes, _unavailable = incidents.load_incidents(str(path))
    assert len(records) == 2, "the record is usable; the pointer is the problem"
    assert notes == ["incident INC-2 supersedes INC-absent, which the file does not hold"]


def test_the_row_serves_the_record_without_its_parsed_moments(tmp_path):
    path = tmp_path / "incidents.yml"
    write(path, [record(window={"from": "2026-11-05T00:00:00Z", "to": "2026-11-10T00:00:00Z"},
                        supersedes="INC-0")])
    [only] = incidents.load_incidents(str(path))[0]
    row = incidents.row(only)
    assert row == {
        "id": "INC-1",
        "title": "Installs fail on a broken snapshot",
        "status": "identified",
        "diagnosis_confidence": "confirmed",
        "supersedes": "INC-0",
        "window_from": "2026-11-05T00:00:00Z",
        "window_to": "2026-11-10T00:00:00Z",
        "areas": ["install"],
        "suites": ["vmaas", "caas", "bmaas"],
        "repos": ["osac", "osac-test-infra"],
        "summary": "Install failures doubled from Nov 5. The snapshots were broken.",
        "contributing_factors": ["The freshness check failed silently, so broken snapshots rolled out."],
        "evidence": [{"label": "the post-mortem", "url": "https://example.com/pm"}],
        "follow_ups": [{"text": "rebuild the snapshot", "owner": "", "due": "", "ticket": "",
                        "state": "open", "declined_reason": ""}],
    }
    assert "_from" not in row and "_to" not in row


# -- which windows a record overlaps ---------------------------------------------------


def test_an_ongoing_incident_reaches_every_window_after_it_started():
    ongoing = {"_from": moment("2026-11-05T00:00:00Z"), "_to": None}
    assert incidents.overlaps(ongoing, NOW - timedelta(days=7), NOW)
    assert not incidents.overlaps(ongoing, NOW - timedelta(days=40), NOW - timedelta(days=30))


def test_the_window_boundaries_do_not_overlap():
    """An incident that ended exactly when the window starts, or begins
    exactly when the window ends, shares no moment with it."""
    ended_at_start = {"_from": moment("2026-10-20T00:00:00Z"), "_to": moment("2026-10-31T12:00:00Z")}
    starts_at_end = {"_from": moment("2026-11-07T12:00:00Z"), "_to": None}
    window = (NOW - timedelta(days=7), NOW)  # 2026-10-31 12:00 to 2026-11-07 12:00
    assert not incidents.overlaps(ended_at_start, *window)
    assert not incidents.overlaps(starts_at_end, *window)


def test_an_incident_inside_the_window_and_around_it_overlap():
    inside = {"_from": moment("2026-11-01T00:00:00Z"), "_to": moment("2026-11-02T00:00:00Z")}
    around = {"_from": moment("2026-10-01T00:00:00Z"), "_to": moment("2026-12-01T00:00:00Z")}
    window = (NOW - timedelta(days=7), NOW)
    assert incidents.overlaps(inside, *window)
    assert incidents.overlaps(around, *window)


# -- the incidents document ------------------------------------------------------------


def api_on(store, records):
    store.write_incidents(records)
    return store.api()


def test_the_document_serves_the_records_whose_window_overlaps(stores):
    api = api_on(stores, [record(), record(id="INC-old",
                                           window={"from": "2026-10-01T00:00:00Z", "to": "2026-10-05T00:00:00Z"})])

    document = get(api, "incidents", days=7)

    assert [row["id"] for row in document["incidents"]] == ["INC-1"]
    assert document["incidents"][0]["window_to"] == "", "ongoing"
    # No stored runs at all: the reverse gate cannot cross-check the ongoing
    # record and says so -- missing data, never a healthy zero.
    assert document["notes"] == [
        "incident INC-1 claims ongoing, but the archive cannot say whether its suites still degrade:"
        " no pass rate for vmaas, caas, bmaas to compare (the last 7 days against the 7 before them)"
    ]
    about = document["about"][0]
    assert "recorded CI incidents whose window overlaps the last 7 days" in about["population"]
    assert about["window"] == "2026-10-31 12:00 to 2026-11-07 12:00 UTC"
    assert about["missing"] == document["notes"][0]
    assert str(stores.incidents_file) in about["sources"]


def test_a_window_that_no_incident_overlaps_answers_no_rows(stores):
    api = api_on(stores, [record()])

    document = get(api, "incidents", days=7, end="2026-10-20")

    assert document["incidents"] == []
    assert document["window"][0]["window_to"] == "2026-10-20T00:00:00Z"


def test_a_file_that_cannot_be_read_says_so_in_the_document(stores):
    api = stores.api()
    Path(stores.incidents_file).unlink()

    document = get(api, "incidents", days=7)

    assert document["incidents"] == []
    assert "cannot be read" in document["unavailable"]
    assert any("cannot be read" in note for note in document["notes"])
    assert "Nothing is missing" not in document["about"][0]["missing"]


def test_a_record_left_out_is_a_note_in_the_document(stores):
    api = api_on(stores, [record(), "not a record"])

    document = get(api, "incidents", days=7)

    assert [row["id"] for row in document["incidents"]] == ["INC-1"]
    assert any("incident record 2 is not usable" in note for note in document["notes"])


# -- the one-line note on the health and failure answers -------------------------------


def test_health_and_failures_carry_one_line_per_overlapping_incident(stores):
    stores.run(NOW - timedelta(hours=5))
    api = api_on(stores, [record(), record(id="INC-2", title="Another one",
                                           window={"from": "2026-11-06T00:00:00Z"})])

    for kind in ("health", "failures"):
        document = get(api, kind, days=7)
        lines = [note for note in document["notes"] if note.startswith("this window overlaps incident")]
        assert lines == [
            "this window overlaps incident INC-1: Installs fail on a broken snapshot"
            " (identified, confirmed) — Install failures doubled from Nov 5. (see the incidents tool)",
            "this window overlaps incident INC-2: Another one"
            " (identified, confirmed) — Install failures doubled from Nov 5. (see the incidents tool)",
        ], kind
        # The About panel's Missing column is built from the notes, so the
        # line reaches the page and the tool answer both.
        assert "this window overlaps incident INC-1" in document["about"][0]["missing"]


def test_the_line_fires_only_for_incidents_covering_the_suites_and_repos_asked_about(stores):
    """Scoped enrichment: the note explains the numbers the query shows, so
    an incident that never touched the suite or repository asked about is
    not their story. A query for all of them matches every record."""
    stores.run(NOW - timedelta(hours=5))
    api = api_on(stores, [record(),
                          record(id="INC-caas", title="A CaaS-only incident", suites=["caas"]),
                          record(id="INC-infra", title="A test-infra-only incident",
                                 suites=["vmaas"], repos=["osac-test-infra"])])

    def lines(kind, **params):
        document = get(api, kind, days=7, **params)
        return [note.split(":")[0] for note in document["notes"]
                if note.startswith("this window overlaps incident")]

    for kind in ("health", "failures"):
        # A VMaaS question: the CaaS-only incident is not its story.
        assert lines(kind, suite="vmaas") == [
            "this window overlaps incident INC-1",
            "this window overlaps incident INC-infra",
        ], kind
        # A VMaaS question about the osac repository: the test-infra-only
        # incident is not either.
        assert lines(kind, suite="vmaas", repo="osac") == [
            "this window overlaps incident INC-1",
        ], kind
        # A CaaS question: both records that cover CaaS answer it.
        assert lines(kind, suite="caas") == [
            "this window overlaps incident INC-1",
            "this window overlaps incident INC-caas",
        ], kind
        # No suite or repository named: every record matches.
        assert len(lines(kind)) == 3, kind


def test_the_other_documents_carry_no_incident_line(stores):
    stores.run(NOW - timedelta(hours=5))
    api = api_on(stores, [record()])

    for kind in ("time", "tests", "waits"):
        document = get(api, kind, days=7)
        assert not any("overlaps incident" in note for note in document["notes"]), kind


def test_a_window_no_incident_overlaps_carries_no_line(stores):
    stores.run(NOW - timedelta(days=3))
    api = api_on(stores, [record()])

    document = get(api, "failures", days=7, end="2026-10-20")

    assert not any("overlaps incident" in note for note in document["notes"])


def test_an_unreadable_file_degrades_to_a_note_on_health_and_failures(stores):
    stores.run(NOW - timedelta(hours=5))
    api = stores.api()
    Path(stores.incidents_file).unlink()

    for kind in ("health", "failures"):
        document = get(api, kind, days=7)
        note = next((note for note in document["notes"] if "no incident context is noted" in note), "")
        assert "cannot be read" in note, kind
        assert document["tiles"], kind  # the answer itself stands


# -- the repo's own incidents file (the seed data) --------------------------------------


def test_the_repos_incidents_file_holds_the_seeded_incident():
    records, notes, unavailable = incidents.load_incidents(str(REPO_INCIDENTS))
    assert unavailable == "" and notes == []
    [only] = records
    assert only["id"] == "INC-2026-11-install"
    assert only["title"] == ("Install failures from an AAP readiness race after"
                             " the fleet-wide snapshot refresh")
    assert only["window"]["from"] == "2026-11-05T00:00:00Z"
    assert only["window"].get("to") is None, "ongoing as of 2026-11-23"
    assert only["status"] == "identified"
    assert only["diagnosis_confidence"] == "confirmed"
    assert only["areas"] == ["install"]
    assert only["suites"] == ["vmaas", "caas", "bmaas"]
    assert only["repos"] == ["osac", "osac-test-infra"]
    assert only["summary"].startswith("Install failures in the osac mono-repo CI rose sharply from Nov 5")
    assert "no healthy upstream" in only["summary"]
    # The record is migrated in full: the cause chain is a list of factors,
    # not the single `cause` line it was before.
    assert "cause" not in only
    factors = only["contributing_factors"]
    assert len(factors) == 9
    assert all(factor.strip() for factor in factors)
    assert "sha256:e58fe43c367aa8f32c310cbd2a4a2e7b040897abbb7dcea9397e2ad43014ab71" in factors[0]
    assert any("scm_refspec" in factor and "#2590" in factor for factor in factors), (
        "the AAP git-tree mechanism and its fix")
    assert any("aap-operator" in factor and "startingCSV" in factor for factor in factors), (
        "the confirmed promotion co-candidate")
    assert any("first-boot race divergence" in factor for factor in factors), "the OVN finding"
    assert [item["url"] for item in only["evidence"]] == [
        "https://redhat-internal.slack.com/archives/C0B8KR3HK16/p1794819597154149",
        "https://github.com/osac-project/osac-test-infra/pull/797",
        "https://github.com/osac-project/cluster-tool/pull/45",
        "https://github.com/osac-project/osac/actions/runs/44967627752",
        "https://github.com/osac-project/osac/actions/runs/43865187352",
        "https://github.com/osac-project/osac/pull/2407",
        "https://github.com/osac-project/osac-test-infra/pull/828",
        "https://github.com/osac-project/osac-test-infra/pull/825",
        "https://github.com/osac-project/osac/pull/2590",
        "https://osac-ci.redhat.com:3000/d/ci-observe-failures/ci-observe-e28094-why-runs-fail",
    ]
    # A label holds text a plain YAML scalar cannot (# and : start comments
    # and mappings): quoted, it loads whole.
    assert only["evidence"][0]["label"] == "Post-mortem in #wg-infra (iskornya, 2026-11-16 08:59 UTC)"
    assert only["evidence"][9]["label"].startswith("ci-observe: failure signatures")
    # The signatures link explains the Nov 6-23 windows its label names.
    assert only["evidence"][9]["window"] == {"from": "2026-11-06T00:00:00Z", "to": "2026-11-23T00:00:00Z"}
    # Every follow-up is structured: the open one that lands osac PR #2590
    # carries its owner, its ticket and no due date, and the finished one
    # (the ci-observe selection bias) is done.
    follow_ups = only["follow_ups"]
    assert len(follow_ups) == 8
    assert all(set(follow_up) == {"text", "owner", "due", "ticket", "state", "declined_reason"}
               for follow_up in follow_ups)
    [land] = [follow_up for follow_up in follow_ups if "#2590" in follow_up["text"]]
    assert (land["owner"], land["due"], land["state"]) == ("omer-vishlitzky", "", "open")
    assert land["ticket"] == "https://github.com/osac-project/osac/pull/2590"
    [done] = [follow_up for follow_up in follow_ups if follow_up["state"] == "done"]
    assert "signature selection bias" in done["text"]
    states = [follow_up["state"] for follow_up in follow_ups]
    assert states.count("done") == 1 and set(states) <= {"open", "done"}


def test_the_seeded_incident_serves_the_windows_it_hurt():
    records, _notes, _unavailable = incidents.load_incidents(str(REPO_INCIDENTS))
    [only] = records
    during = (datetime(2026, 11, 23, tzinfo=UTC) - timedelta(days=7), datetime(2026, 11, 23, tzinfo=UTC))
    before = (datetime(2026, 10, 1, tzinfo=UTC) - timedelta(days=7), datetime(2026, 10, 1, tzinfo=UTC))
    assert incidents.overlaps(only, *during)
    assert not incidents.overlaps(only, *before)
    line = incidents.note_line(only)
    assert line.startswith("this window overlaps incident INC-2026-11-install:")
    assert line.endswith("(see the incidents tool)")
    assert "(identified, confirmed)" in line
    assert "Install failures in the osac mono-repo CI rose sharply from Nov 5" in line


def test_the_default_path_points_at_the_repo_file():
    assert Path(incidents.incidents_file()) == REPO_INCIDENTS


def test_the_env_var_overrides_the_default_path(monkeypatch):
    monkeypatch.setenv("CI_OBSERVE_INCIDENTS_FILE", "/app/incidents.yml")
    assert incidents.incidents_file() == "/app/incidents.yml"

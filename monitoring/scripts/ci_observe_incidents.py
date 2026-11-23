"""The incidents layer of ci-observe: root-cause records a person wrote,
served as they are.

An incident is what a post-mortem concluded: a window, the suites and
repositories it hurt, the cause chain, the evidence. None of that is
telemetry -- the stores hold what happened, the incidents file holds why --
so the records live in git (monitoring/config/ci-observe/incidents.yml) and
this module reads them where they are asked for:

- the incidents document (ci_observe_insights, /api/ci-observe/incidents,
  the MCP `incidents` tool): the records whose window overlaps the asked
  window;
- the health and failure documents: one line per overlapping incident, so a
  bad-looking pass rate says what it is (see note_line).

The file is read on every use and is never written here. A file that cannot
be read, or a record that does not hold its shape, is a note naming the
problem -- never an error, and never a silent empty answer: an incidents
file that is missing is a deployment gap, not "nothing ever broke".
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

# What a record's status may be. `identified` is the state after the
# post-mortem but before the fix; `ongoing` is still being diagnosed; the
# distinction matters to a reader deciding whether to look for a fix landing.
STATUSES = ("identified", "fixed", "ongoing")

# The two spellings a window moment may take, the same ones the insight
# windows read: a UTC day or a UTC second-precision timestamp.
MOMENT_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ")


def incidents_file() -> str:
    """The incidents file to read. CI_OBSERVE_INCIDENTS_FILE overrides (the
    exporter container mounts the file at /app/incidents.yml, flat like its
    scripts); the default resolves next to the deployed scripts directory,
    where monitoring-setup.sh copies it: monitoring/config/ci-observe/ in
    the repo, ~/.monitoring-server/config/ci-observe/ on the central host."""
    override = os.getenv("CI_OBSERVE_INCIDENTS_FILE")
    if override:
        return override
    return str(Path(__file__).resolve().parents[1] / "config" / "ci-observe" / "incidents.yml")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_moment(value: object) -> datetime | None:
    """A UTC date or second-precision UTC timestamp, or None when the value
    is neither: a window a reader cannot place is a record's problem, not a
    crash of the page that found it. pyyaml reads a bare timestamp as a
    datetime (and a bare date as a date), so those are taken as they come;
    a naive datetime is the file's UTC, which its header asks for."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, str):
        return None
    for pattern in MOMENT_FORMATS:
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _record_problems(record: object, index: int) -> tuple[str, list[str]]:
    """(name, problems) of one record: the name notes cite, and why the
    record is not usable. Empty problems means it is."""
    name = f"incident record {index + 1}"
    if not isinstance(record, dict):
        return name, ["it is not a mapping"]
    for field in ("id", "title", "summary", "status"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return name, [f"{field} is missing or not text"]
    name = f"incident {record['id']}"
    if record["status"] not in STATUSES:
        return name, [f"status {record['status']!r} is none of {', '.join(STATUSES)}"]
    window = record.get("window")
    if not isinstance(window, dict) or parse_moment(window.get("from")) is None:
        return name, ["window.from is missing or not a UTC date (2026-11-05) or time"
                      " (2026-11-05T00:00:00Z)"]
    if window.get("to") is not None and parse_moment(window.get("to")) is None:
        return name, ["window.to is not a UTC date or time; use null while the incident"
                      " is ongoing"]
    for field in ("areas", "suites", "repos"):
        if field in record and (not isinstance(record[field], list)
                                or not all(isinstance(item, str) for item in record[field])):
            return name, [f"{field} is not a list of names"]
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list) or not all(
            isinstance(item, dict) and isinstance(item.get("label"), str)
            and isinstance(item.get("url"), str) for item in evidence):
        return name, ["evidence is not a list of label and url"]
    follow_ups = record.get("follow_ups", [])
    if not isinstance(follow_ups, list) or not all(isinstance(item, str) for item in follow_ups):
        return name, ["follow_ups is not a list of lines"]
    return name, []


def load_incidents(path: str) -> tuple[list[dict], list[str], str]:
    """(records, notes, unavailable): the incident records the file holds,
    one note per record that is not usable, and why the whole file could not
    be read ("" when it could). A record is normalized: window moments are
    parsed (window.to absent or null means the incident is ongoing), the
    optional lists default to empty, and the row it serves is `row`'s."""
    try:
        with open(path, encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        return [], [], f"the incidents file {path} cannot be read ({error})"
    if document is None:
        # An empty file is no recorded incident, not a broken one.
        document = {"incidents": []}
    if not isinstance(document, dict) or not isinstance(document.get("incidents"), list):
        return [], [], (f'the incidents file {path} holds no "incidents" list of records')
    records: list[dict] = []
    notes: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(document["incidents"]):
        name, problems = _record_problems(entry, index)
        if problems:
            notes.append(f"{name} is not usable and was left out: {'; '.join(problems)}")
            continue
        record = dict(entry)
        if record["id"] in seen:
            notes.append(f"incident {record['id']} is recorded twice; the first record is kept")
            continue
        seen.add(record["id"])
        record["_from"] = parse_moment(record["window"]["from"])
        record["_to"] = parse_moment(record["window"].get("to"))
        # The window serves as text (a bare timestamp loads as a datetime,
        # and the documents carry ISO strings like every other time); `to`
        # stays null while the incident is ongoing.
        record["window"] = {
            "from": _iso(record["_from"]),
            "to": _iso(record["_to"]) if record["_to"] is not None else None,
        }
        record.setdefault("areas", [])
        record.setdefault("suites", [])
        record.setdefault("repos", [])
        record.setdefault("cause", "")
        record.setdefault("evidence", [])
        record.setdefault("follow_ups", [])
        records.append(record)
    records.sort(key=lambda record: (record["_from"], record["id"]))
    return records, notes, ""


def overlaps(record: dict, window_from: datetime, window_to: datetime) -> bool:
    """Whether the incident's window and the asked window share a moment. An
    incident still ongoing (no window.to) reaches every window that starts
    after it began; one that ended exactly when the window starts, or begins
    exactly when the window ends, does not overlap it."""
    if record["_from"] >= window_to:
        return False
    return record["_to"] is None or record["_to"] > window_from


def first_sentence(text: str) -> str:
    """The text up to and including its first period, or the whole text when
    it has none: the one-line note carries a sentence, not a fragment."""
    head, found, rest = text.partition(".")
    return head + found if found else text.strip()


def note_line(record: dict) -> str:
    """The one line the health and failure answers carry for an incident
    their window overlaps: enough to know what the numbers are, with the
    incidents tool holding the rest."""
    return (f"this window overlaps incident {record['id']}: {record['title']} —"
            f" {first_sentence(record['summary'])} (see the incidents tool)")


def row(record: dict) -> dict:
    """The record as the incidents document serves it: the internal parsed
    moments stay internal."""
    return {
        "id": record["id"],
        "title": record["title"],
        "status": record["status"],
        "window_from": record["window"]["from"],
        "window_to": record["window"].get("to") or "",
        "areas": list(record["areas"]),
        "suites": list(record["suites"]),
        "repos": list(record["repos"]),
        "summary": record["summary"],
        "cause": record["cause"],
        "evidence": [dict(item) for item in record["evidence"]],
        "follow_ups": list(record["follow_ups"]),
    }

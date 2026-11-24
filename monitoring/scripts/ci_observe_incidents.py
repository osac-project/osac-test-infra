"""The incidents layer of ci-observe: root-cause records a person wrote,
served as they are.

An incident is what a post-mortem concluded: a window, the suites and
repositories it hurt, the contributing factors, the evidence. None of that
is telemetry -- the stores hold what happened, the incidents file holds why
-- so the records live in git (monitoring/config/ci-observe/incidents.yml)
and this module reads them where they are asked for:

- the incidents document (ci_observe_insights, /api/ci-observe/incidents,
  the MCP `incidents` tool): the records whose window overlaps the asked
  window, each ongoing one cross-checked against the run archive (a record
  whose suites have shown no degradation for a week is told to close or
  re-verify);
- the health and failure documents: one line per overlapping incident that
  covers the suites and repositories the query counts, so a bad-looking
  pass rate says what it is (see note_line).

The file is read on every use and is never written here. A file that cannot
be read, or a record that does not hold its shape, is a note naming the
problem -- never an error, and never a silent empty answer: an incidents
file that is missing is a deployment gap, not "nothing ever broke".

A record holds what a post-mortem is asked to hold: the factors that
together produced the incident (never a single blame line), the confidence
the diagnosis is trusted with, follow-ups with an owner and a state, and
evidence links scoped to the part of the window they explain.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

# What a record's status may be. `identified` is the state after the
# post-mortem but before the fix; `ongoing` is still being diagnosed; the
# distinction matters to a reader deciding whether to look for a fix landing.
# `fixed` and `closed` are terminal: the incident is over, so the record's
# window must say when it ended and no follow-up may be left open.
STATUSES = ("identified", "fixed", "ongoing", "closed")
ONGOING_STATUSES = ("identified", "ongoing")
TERMINAL_STATUSES = ("fixed", "closed")

# How far the record's diagnosis is trusted. `confirmed` is what the
# post-mortem concluded; `suspected` explains the evidence but was not
# proven; `superseded` marks a diagnosis a later record replaced (see
# `supersedes`) -- it stays served so its window is not diagnosed twice,
# and every reader sees the warning.
CONFIDENCES = ("confirmed", "suspected", "superseded")

# What a follow-up's state may be. A declined follow-up carries its reason:
# "we decided not to" is an answer, "we forgot" is not.
FOLLOW_UP_STATES = ("open", "done", "declined")

# A follow-up that is only a verb names no work -- the record's reader
# cannot tell what "investigate" would even look at. The verb with an
# object passes ("Investigate why X places br-ex on the secondary network").
VAGUE_FOLLOW_UP = re.compile(r"^\s*(?:improve|investigate|review)\s*[.!]?\s*$", re.IGNORECASE)

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


def _contributing_factors(record: dict) -> list | None:
    """The record's contributing factors: a list of lines. A record written
    before the list (a single `cause` string) still loads, as one factor."""
    factors = record.get("contributing_factors")
    if factors is None and isinstance(record.get("cause"), str):
        factors = [record["cause"]]
    return factors


def _follow_up(entry: object, number: int) -> tuple[dict | None, str]:
    """(follow-up, problem): one follow-up in the shape the records serve
    ({text, owner, due, ticket, state, declined_reason}), and why it is not
    usable ("" when it is). A plain line from a record written before the
    structure loads as {text, state: open} with the rest empty."""
    if isinstance(entry, str):
        entry = {"text": entry}
    if not isinstance(entry, dict):
        return None, f"follow-up {number} is neither a line nor a mapping"
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, f"follow-up {number} is missing its text"
    state = entry.get("state", "open")
    if state not in FOLLOW_UP_STATES:
        return None, f"follow-up {number}'s state {state!r} is none of {', '.join(FOLLOW_UP_STATES)}"
    reason = entry.get("declined_reason") or ""
    if state == "declined" and not str(reason).strip():
        return None, (f"follow-up {number} is declined without a declined_reason:"
                      " say why the work is not wanted")
    for field in ("owner", "ticket"):
        if entry.get(field) is not None and not isinstance(entry.get(field), str):
            return None, f"follow-up {number}'s {field} is not text"
    due = entry.get("due") or ""
    moment = parse_moment(due) if due != "" else None
    if due != "" and moment is None:
        return None, (f"follow-up {number}'s due {due!r} is not a UTC date (2026-12-01) or time"
                      " (2026-12-01T00:00:00Z)")
    if VAGUE_FOLLOW_UP.match(text):
        return None, (f"follow-up {number} ({text.strip()!r}) is a verb with no object:"
                      " say what to improve, investigate or review")
    return {
        "text": text,
        "owner": entry.get("owner") or "",
        "due": _iso(moment) if moment is not None else "",
        "ticket": entry.get("ticket") or "",
        "state": state,
        "declined_reason": str(reason),
    }, ""


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
    confidence = record.get("diagnosis_confidence")
    if confidence not in CONFIDENCES:
        return name, [f"diagnosis_confidence {confidence!r} is none of {', '.join(CONFIDENCES)}"]
    supersedes = record.get("supersedes")
    if supersedes is not None and (not isinstance(supersedes, str) or not supersedes.strip()
                                   or supersedes == record["id"]):
        return name, ["supersedes is not the id of another incident record"]
    window = record.get("window")
    if not isinstance(window, dict) or parse_moment(window.get("from")) is None:
        return name, ["window.from is missing or not a UTC date (2026-11-05) or time"
                      " (2026-11-05T00:00:00Z)"]
    if window.get("to") is not None and parse_moment(window.get("to")) is None:
        return name, ["window.to is not a UTC date or time; use null while the incident"
                      " is ongoing"]
    if window.get("to") is None and record["status"] in TERMINAL_STATUSES:
        return name, [f"status {record['status']!r} is terminal, so window.to must say when"
                      " the incident ended (UTC)"]
    for field in ("areas", "suites", "repos"):
        if field in record and (not isinstance(record[field], list)
                                or not all(isinstance(item, str) for item in record[field])):
            return name, [f"{field} is not a list of names"]
    factors = _contributing_factors(record)
    if factors is None:
        # No factors recorded is thin but not broken; a wrong shape is.
        factors = []
    if not isinstance(factors, list) or not all(isinstance(item, str) and item.strip()
                                                for item in factors):
        return name, ["contributing_factors is not a list of lines (a record's old single"
                      " `cause` line still loads as one)"]
    for number, factor in enumerate(factors, start=1):
        if "human error" in factor.lower():
            return name, [f"contributing factor {number} says 'human error': name the missing check"
                          " or the wrong assumption that let the mistake through, not the person"]
    evidence = record.get("evidence", [])
    if not isinstance(evidence, list) or not all(
            isinstance(item, dict) and isinstance(item.get("label"), str)
            and isinstance(item.get("url"), str) for item in evidence):
        return name, ["evidence is not a list of label and url"]
    if not any(item["url"].strip() for item in evidence):
        return name, ["evidence holds no link: a record needs at least one url (a post-mortem,"
                      " a pull request, an example run)"]
    for number, item in enumerate(evidence, start=1):
        span = item.get("window")
        if span is None:
            continue
        if not isinstance(span, dict) or parse_moment(span.get("from")) is None:
            return name, [f"evidence {number}'s window is not a UTC from and to span"]
        if span.get("to") is not None and parse_moment(span.get("to")) is None:
            return name, [f"evidence {number}'s window.to is not a UTC date or time"]
    follow_ups = record.get("follow_ups", [])
    if not isinstance(follow_ups, list):
        return name, ["follow_ups is not a list"]
    for number, entry in enumerate(follow_ups, start=1):
        _parsed, problem = _follow_up(entry, number)
        if problem:
            return name, [problem]
    if record["status"] in TERMINAL_STATUSES:
        for number, entry in enumerate(follow_ups, start=1):
            state = entry.get("state", "open") if isinstance(entry, dict) else "open"
            if state == "open":
                return name, [f"status {record['status']!r} is terminal, but follow-up {number} is"
                              " still open: mark it done, decline it with a declined_reason, or"
                              " the incident is not over"]
    return name, []


def load_incidents(path: str) -> tuple[list[dict], list[str], str]:
    """(records, notes, unavailable): the incident records the file holds,
    one note per record that is not usable, and why the whole file could not
    be read ("" when it could). A record is normalized: window moments are
    parsed (window.to absent or null means the incident is ongoing), the
    optional lists default to empty, an old single `cause` line loads as one
    contributing factor, a plain-line follow-up loads as an open one, and
    the row it serves is `row`'s."""
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
        record["contributing_factors"] = list(_contributing_factors(record) or [])
        # The old single-line spelling is folded into the list it became.
        record.pop("cause", None)
        evidence = []
        for item in record.get("evidence", []):
            served = dict(item)
            span = served.get("window")
            if span is not None:
                served["window"] = {
                    "from": _iso(parse_moment(span["from"])),
                    "to": _iso(parse_moment(span["to"])) if span.get("to") is not None else None,
                }
            evidence.append(served)
        record["evidence"] = evidence
        follow_ups = []
        for number, written in enumerate(record.get("follow_ups", []), start=1):
            parsed, _problem = _follow_up(written, number)
            follow_ups.append(parsed)
        record["follow_ups"] = follow_ups
        records.append(record)
    records.sort(key=lambda record: (record["_from"], record["id"]))
    for record in records:
        supersedes = record.get("supersedes")
        if supersedes and supersedes not in seen:
            notes.append(f"incident {record['id']} supersedes {supersedes}, which the file does not hold")
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
    head, found, _rest = text.partition(".")
    return head + found if found else text.strip()


def note_line(record: dict) -> str:
    """The one line the health and failure answers carry for an incident
    their window overlaps: enough to know what the numbers are -- and how
    far the diagnosis is trusted -- with the incidents tool holding the
    rest."""
    return (f"this window overlaps incident {record['id']}: {record['title']}"
            f" ({record['status']}, {record['diagnosis_confidence']}) —"
            f" {first_sentence(record['summary'])} (see the incidents tool)")


def scope_matches(record: dict, suite: str, repo: str) -> bool:
    """Whether the record is about the suite and repository a query counts.
    The one-line note explains the numbers a health or failure answer shows,
    and an incident that never touched the suite or repository asked about
    is not their story; a query for all of them matches every record."""
    if suite != "all" and suite not in record["suites"]:
        return False
    if repo and repo not in record["repos"]:
        return False
    return True


def row(record: dict) -> dict:
    """The record as the incidents document serves it: the internal parsed
    moments stay internal."""
    return {
        "id": record["id"],
        "title": record["title"],
        "status": record["status"],
        "diagnosis_confidence": record["diagnosis_confidence"],
        "supersedes": record.get("supersedes") or "",
        "window_from": record["window"]["from"],
        "window_to": record["window"]["to"] or "",
        "areas": list(record["areas"]),
        "suites": list(record["suites"]),
        "repos": list(record["repos"]),
        "summary": record["summary"],
        "contributing_factors": list(record["contributing_factors"]),
        "evidence": [dict(item) for item in record["evidence"]],
        "follow_ups": [dict(item) for item in record["follow_ups"]],
    }

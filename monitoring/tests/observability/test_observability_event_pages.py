"""Real-PostgreSQL and real-HTTP tests for bounded event-history pages (W01d).

The events route serves byte-capped keyset pages (cursor mode) and streams
the legacy offset window in bounded chunks, so a request over hundreds of
MiB of events never materializes them. These tests walk real near-1 MiB
events and more than ten thousand rows through real HTTP, verify exact
coverage against direct SQL, and measure an isolated server process's
peak memory while it streams a ~500 MiB response.

Run from the repository root:

    CI_OBSERVABILITY_TEST_DSN=postgresql://... \\
        python -m pytest monitoring/tests/observability/test_observability_event_pages.py --no-cov
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

try:
    import psycopg
except ModuleNotFoundError:
    if os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip():
        # A configured database with a missing driver is a broken setup.
        raise
    pytest.skip("psycopg is not installed and CI_OBSERVABILITY_TEST_DSN is not set", allow_module_level=True)

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from monitoring.ci_observability import api, ledger
from monitoring.ci_observability.ledger import JournalEvent

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_TIME = datetime(2026, 10, 28, 14, 0, 0, tzinfo=UTC)
EXECUTION_ID = "gh-1-r900-a1-j1"
BULK_EXECUTION_ID = "gh-1-r901-a1-j1"
# Near the 1 MiB payload cap: canonical size 1,039,017 bytes.
BIG_EVENT_PAD_BYTES = 1_039_000
SMALL_EVENT_COUNT = 20_400
BULK_EVENT_COUNT = 480
UNREACHABLE_DSN = "postgresql://ci_observability@127.0.0.1:1/ci_observability?connect_timeout=2"


def _test_dsn() -> str:
    value = os.environ.get("CI_OBSERVABILITY_TEST_DSN", "").strip()
    if not value:
        pytest.skip("CI_OBSERVABILITY_TEST_DSN is not set: these tests require a real PostgreSQL server")
    return value


@pytest.fixture(scope="session")
def dsn() -> str:
    value = _test_dsn()
    try:
        with psycopg.connect(value, autocommit=True, connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"CI_OBSERVABILITY_TEST_DSN is set but the database is unreachable: {type(exc).__name__}", pytrace=False
        )
    return value


@pytest.fixture(scope="session")
def test_schema(dsn: str) -> Iterator[str]:
    name = f"ci_obs_w01d_pages_{os.getpid()}"
    try:
        assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
        yield name
    finally:
        # Armed from the first owned mutation: a failed migration also drops.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def _register_execution(*, dsn: str, schema: str, execution_id: str, run_id: int) -> None:
    client = ledger.Ledger(dsn=dsn, schema=schema)
    try:
        client.register_execution(
            record=ledger.ExecutionRecord(
                execution_id=execution_id,
                schema_version=1,
                repository_id=1,
                run_id=run_id,
                run_attempt=1,
                logical_job_key="k.yml#j",
                executed=True,
            )
        )
    finally:
        client.close()


def _big_event(*, execution_id: str, journal_id: str, sequence: int, offset_seconds: float) -> JournalEvent:
    return JournalEvent(
        schema_version=1,
        execution_id=execution_id,
        journal_id=journal_id,
        sequence=sequence,
        recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(seconds=offset_seconds)),
        event="bulk_payload",
        data={"blob": "x" * BIG_EVENT_PAD_BYTES},
    )


def _append_batches(*, dsn: str, schema: str, events: list[JournalEvent], per_batch: int) -> None:
    client = ledger.Ledger(dsn=dsn, schema=schema)
    try:
        for start in range(0, len(events), per_batch):
            client.append_events(events=events[start : start + per_batch])
    finally:
        client.close()


@pytest.fixture(scope="session")
def seeded(dsn: str, test_schema: str) -> Iterator[str]:
    """One execution with near-1 MiB events, twenty thousand small events, and ties."""
    _register_execution(dsn=dsn, schema=test_schema, execution_id=EXECUTION_ID, run_id=900)
    big = [
        _big_event(execution_id=EXECUTION_ID, journal_id="biga", sequence=n + 1, offset_seconds=n) for n in range(40)
    ]
    _append_batches(dsn=dsn, schema=test_schema, events=big, per_batch=15)
    small = [
        JournalEvent(
            schema_version=1,
            execution_id=EXECUTION_ID,
            journal_id="small",
            sequence=n + 1,
            recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(hours=1, seconds=n)),
            event="small",
            data={"i": n},
        )
        for n in range(SMALL_EVENT_COUNT)
    ]
    _append_batches(dsn=dsn, schema=test_schema, events=small, per_batch=10_000)
    # Identical timestamps across two journals exercise the (journal_id,
    # sequence) tie-break; one record keeps nine fractional digits.
    ties: list[JournalEvent] = []
    # Two journals share each instant exactly, so the (journal_id, sequence)
    # tie-break decides the order; a third journal keeps nine fractional
    # digits on its own instants.
    for n in range(3):
        for journal_id in ("ta", "tb"):
            ties.append(
                JournalEvent(
                    schema_version=1,
                    execution_id=EXECUTION_ID,
                    journal_id=journal_id,
                    sequence=n + 1,
                    recorded_at=f"2026-10-28T22:00:0{n}Z",
                    event="tie",
                    data={"n": n, "journal": journal_id},
                )
            )
    for n in range(3):
        ties.append(
            JournalEvent(
                schema_version=1,
                execution_id=EXECUTION_ID,
                journal_id="nano",
                sequence=n + 1,
                recorded_at=f"2026-10-28T22:10:0{n}.123456789Z",
                event="nano",
                data={"n": n},
            )
        )
    _append_batches(dsn=dsn, schema=test_schema, events=ties, per_batch=10)
    yield test_schema


@pytest.fixture(scope="session")
def bulk_execution(dsn: str, test_schema: str) -> Iterator[str]:
    """A second execution holding ~500 MiB of near-1 MiB events."""
    _register_execution(dsn=dsn, schema=test_schema, execution_id=BULK_EXECUTION_ID, run_id=901)
    per_batch = 15
    client = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        for start in range(0, BULK_EVENT_COUNT, per_batch):
            batch = [
                _big_event(
                    execution_id=BULK_EXECUTION_ID,
                    journal_id="bulk",
                    sequence=start + n + 1,
                    offset_seconds=10_000 + start + n,
                )
                for n in range(min(per_batch, BULK_EVENT_COUNT - start))
            ]
            client.append_events(events=batch)
    finally:
        client.close()
    yield test_schema


@pytest.fixture(scope="session")
def server(dsn: str, seeded: str) -> Iterator[api.ReadAPIServer]:
    server = api.ReadAPIServer(dsn=dsn, schema=seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def base_url(server: api.ReadAPIServer) -> str:
    return f"http://127.0.0.1:{server.port}"


def events_url(*, base_url: str, execution_id: str) -> str:
    return f"{base_url}/api/v1/executions/{execution_id}/events"


def http_get_raw(*, url: str) -> tuple[int, bytes, dict[str, object]]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return response.status, raw, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, raw, json.loads(raw)


def walk_pages(
    *, base_url: str, execution_id: str, query: str, limit: int, start_cursor: str | None = None
) -> tuple[list[dict[str, object]], list[int]]:
    """Walk every page of one timeline through real HTTP.

    Asserts the page invariants while walking: every body is within the
    server byte cap, count is the exact number of returned rows, every
    non-final page advances, and the walk terminates."""

    root = events_url(base_url=base_url, execution_id=execution_id)
    events: list[dict[str, object]] = []
    page_sizes: list[int] = []
    cursor: str | None = start_cursor
    pages = 0
    while True:
        url = f"{root}?limit={limit}{query}"
        if cursor is not None:
            url += f"&cursor={urllib.parse.quote(cursor, safe='')}"
        status, raw, body = http_get_raw(url=url)
        assert status == 200, (status, body)
        page_events = body["events"]
        assert body["count"] == len(page_events)
        assert body["limit"] == limit
        assert body["byte_cap"] == api.MAX_EVENT_PAGE_BYTES
        assert len(raw) <= body["byte_cap"], f"body {len(raw)} exceeds the cap {body['byte_cap']}"
        assert isinstance(body["next_cursor"], (str, type(None)))
        if not page_events:
            assert body["next_cursor"] is None, "an empty page must end the walk"
            break
        events.extend(page_events)
        page_sizes.append(len(raw))
        pages += 1
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]
    assert pages >= 1
    return events, page_sizes


def direct_keys(*, dsn: str, schema: str, execution_id: str, journal_id: str | None = None) -> list[tuple[str, int]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        if journal_id is None:
            rows = conn.execute(
                sql.SQL(
                    "SELECT journal_id, sequence FROM {}.ledger_event WHERE execution_id = %s"
                    " ORDER BY recorded_at, journal_id, sequence"
                ).format(sql.Identifier(schema)),
                (execution_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                sql.SQL(
                    "SELECT journal_id, sequence FROM {}.ledger_event WHERE execution_id = %s AND journal_id = %s"
                    " ORDER BY recorded_at, journal_id, sequence"
                ).format(sql.Identifier(schema)),
                (execution_id, journal_id),
            ).fetchall()
    return [(row[0], row[1]) for row in rows]


def event_keys(*, events: list[dict[str, object]]) -> list[tuple[str, int]]:
    return [(event["journal_id"], event["sequence"]) for event in events]


def test_cursor_walk_is_exact_complete_and_byte_bounded(dsn: str, seeded: str, base_url: str) -> None:
    events, page_sizes = walk_pages(base_url=base_url, execution_id=EXECUTION_ID, query="", limit=500)
    expected = direct_keys(dsn=dsn, schema=seeded, execution_id=EXECUTION_ID)
    assert event_keys(events=events) == expected
    assert len(expected) == 40 + SMALL_EVENT_COUNT + 9
    # Near-1 MiB events really flowed: several pages hit the byte cap.
    assert max(page_sizes) > 6 * 1024 * 1024
    # Forward progress with no duplicates is implied by the exact order match.


def test_walk_traverses_beyond_the_offset_cap(dsn: str, seeded: str, base_url: str) -> None:
    events, _ = walk_pages(base_url=base_url, execution_id=EXECUTION_ID, query="&journal_id=small", limit=500)
    assert len(events) == SMALL_EVENT_COUNT > 10_000
    expected = direct_keys(dsn=dsn, schema=seeded, execution_id=EXECUTION_ID, journal_id="small")
    assert event_keys(events=events) == expected
    # The legacy offset cap still holds for compat, and the cursor passes it.
    status, _, body = http_get_raw(
        url=events_url(base_url=base_url, execution_id=EXECUTION_ID) + "?limit=10&offset=10000"
    )
    assert status == 200 and body["count"] == 10
    status, _, body = http_get_raw(
        url=events_url(base_url=base_url, execution_id=EXECUTION_ID) + "?limit=10&offset=10001"
    )
    assert status == 400 and body == {"error": "offset must be an integer between 0 and 10000"}


def test_filters_apply_across_cursor_pages(dsn: str, seeded: str, base_url: str) -> None:
    expected = direct_keys(dsn=dsn, schema=seeded, execution_id=EXECUTION_ID, journal_id="biga")
    events, _ = walk_pages(base_url=base_url, execution_id=EXECUTION_ID, query="&journal_id=biga", limit=7)
    assert event_keys(events=events) == expected

    after = ledger.format_utc(value=BASE_TIME + timedelta(hours=1, seconds=100))
    before = ledger.format_utc(value=BASE_TIME + timedelta(hours=1, seconds=199))
    events, _ = walk_pages(
        base_url=base_url,
        execution_id=EXECUTION_ID,
        query=f"&journal_id=small&recorded_after={after}&recorded_before={before}",
        limit=13,
    )
    assert [event["sequence"] for event in events] == list(range(101, 201))


def test_cursor_tokens_are_bound_to_their_query(base_url: str) -> None:
    root = events_url(base_url=base_url, execution_id=EXECUTION_ID)
    _, _, first = http_get_raw(url=f"{root}?limit=3&journal_id=biga")
    token = first["next_cursor"]
    assert isinstance(token, str)
    quoted = urllib.parse.quote(token, safe="")
    # A different filter, a different execution, and a different window all
    # refuse the cursor with a fixed message and no echo of the token.
    for query in (f"?limit=3&cursor={quoted}", f"?limit=3&journal_id=small&cursor={quoted}"):
        status, _, body = http_get_raw(url=root + query)
        assert status == 400
        assert body == {"error": "cursor does not match this query"}
    other = events_url(base_url=base_url, execution_id="gh-1-r900-a2-j1")
    status, _, body = http_get_raw(url=other + f"?limit=3&cursor={quoted}")
    assert status == 404, body  # that execution does not exist in this schema


def test_malformed_cursors_get_fixed_rejections(base_url: str) -> None:
    root = events_url(base_url=base_url, execution_id=EXECUTION_ID)
    good = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "r": "2026-10-28T14:00:00Z", "j": "biga", "s": 1, "x": "0" * 64}).encode()
    ).decode("ascii")
    bad_tokens = {
        "not-base64!!": "cursor is malformed",
        base64.urlsafe_b64encode(b"not json").decode(): "cursor is malformed",
        base64.urlsafe_b64encode(json.dumps({"v": 2}).encode()).decode(): "cursor is malformed",
        base64.urlsafe_b64encode(
            json.dumps({"v": 1, "r": "2026-10-28T14:00:00Z", "j": "biga", "s": 1, "x": "0" * 64, "extra": 1}).encode()
        ).decode(): "cursor is malformed",
        base64.urlsafe_b64encode(
            json.dumps({"v": 1, "r": "2026-10-28T14:00:00Z", "j": "has space", "s": 1, "x": "0" * 64}).encode()
        ).decode(): "cursor is malformed",
        good: "cursor does not match this query",
        "A" * 600: "cursor is malformed",
    }
    for token, message in bad_tokens.items():
        status, raw, body = http_get_raw(url=f"{root}?limit=3&cursor={urllib.parse.quote(token, safe='')}")
        assert status == 400, (token[:20], status, body)
        assert body == {"error": message}
        # No request value, including the token, is echoed back.
        assert token[:20] not in raw.decode("utf-8", errors="replace")
    status, _, body = http_get_raw(url=f"{root}?limit=3&cursor={urllib.parse.quote(good, safe='')}&offset=4")
    assert status == 400
    assert body == {"error": "cursor and offset cannot be combined"}


def test_timestamp_text_and_tie_breaks_survive_paging(dsn: str, seeded: str, base_url: str) -> None:
    root = events_url(base_url=base_url, execution_id=EXECUTION_ID)
    after = ledger.format_utc(value=BASE_TIME + timedelta(hours=7))
    events, _ = walk_pages(base_url=base_url, execution_id=EXECUTION_ID, query=f"&recorded_after={after}", limit=2)
    keys = event_keys(events=events)
    assert keys == [
        ("ta", 1),
        ("tb", 1),
        ("ta", 2),
        ("tb", 2),
        ("ta", 3),
        ("tb", 3),
        ("nano", 1),
        ("nano", 2),
        ("nano", 3),
    ]
    # The nine fractional digits are served exactly as delivered.
    assert [event["recorded_at"] for event in events if event["journal_id"] == "nano"] == [
        "2026-10-28T22:10:00.123456789Z",
        "2026-10-28T22:10:01.123456789Z",
        "2026-10-28T22:10:02.123456789Z",
    ]
    # The cursor itself carries the original text and resumes exactly.
    _, _, page = http_get_raw(url=f"{root}?limit=4&recorded_after={after}")
    token = page["next_cursor"]
    payload = json.loads(base64.urlsafe_b64decode(token))
    assert payload["v"] == api._CURSOR_VERSION
    assert payload["r"] == "2026-10-28T22:00:01Z"
    _, _, resumed = http_get_raw(
        url=f"{root}?limit=4&recorded_after={after}&cursor={urllib.parse.quote(token, safe='')}"
    )
    assert event_keys(events=resumed["events"]) == [("ta", 3), ("tb", 3), ("nano", 1), ("nano", 2)]


def test_reader_role_pages_without_write_privilege(dsn: str, seeded: str) -> None:
    role = f"ci_obs_w01d_reader_{os.getpid()}"
    try:
        # Creation, grants, and DSN parsing all sit inside the cleanup
        # scope: a failed grant or parse cannot leak the role.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(seeded), sql.Identifier(role)))
            conn.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    sql.Identifier(seeded), sql.Identifier(role)
                )
            )
        info = conninfo_to_dict(dsn)
        reader_dsn = f"postgresql://{role}@{info['host']}:{info['port']}/{info['dbname']}"
        server = api.ReadAPIServer(dsn=reader_dsn, schema=seeded, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            events, _ = walk_pages(
                base_url=f"http://127.0.0.1:{server.port}", execution_id=EXECUTION_ID, query="&journal_id=biga", limit=3
            )
            assert len(events) == 40
        finally:
            server.shutdown()
            server.server_close()
        with psycopg.connect(reader_dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.ledger_event (execution_id, journal_id, sequence, schema_version,"
                    " recorded_at, recorded_at_text, event, data, data_sha256)"
                    " VALUES ('x', 'y', 1, 1, now(), '2026-10-28T14:00:00Z', 'e', %s::json, %s)"
                ).format(sql.Identifier(seeded)),
                ("{}", "0" * 64),
            )
    finally:
        # The role is dropped even when an assertion or grant fails, so a
        # failed run cannot leak it into the shared server.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                    sql.Identifier(seeded), sql.Identifier(role)
                )
            )
            conn.execute(
                sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(sql.Identifier(seeded), sql.Identifier(role))
            )
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_keyset_queries_use_the_timeline_index(dsn: str, seeded: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        # A deployed database has autovacuum statistics; refresh them so the
        # plan test sees what the planner really does with this data.
        conn.execute(sql.SQL("ANALYZE {}.ledger_event").format(sql.Identifier(seeded)))
        metadata_plan = conn.execute(
            sql.SQL(
                "EXPLAIN (FORMAT JSON) SELECT journal_id, sequence, recorded_at_text, octet_length(data::text)"
                " FROM {}.ledger_event WHERE execution_id = %s AND journal_id = %s"
                " AND (recorded_at, journal_id, sequence) > (%s, %s, %s)"
                " ORDER BY recorded_at, journal_id, sequence LIMIT 500"
            ).format(sql.Identifier(seeded)),
            (EXECUTION_ID, "small", BASE_TIME + timedelta(hours=1), "small", 100),
        ).fetchone()[0]
        # The payload fetch selects exactly the planned (journal_id,
        # sequence) pairs; the shape the ledger issues for every page.
        key_placeholders = ", ".join(["(%s, %s)"] * 3)
        fetch_plan = conn.execute(
            sql.SQL(
                "EXPLAIN (FORMAT JSON) SELECT sequence FROM {}.ledger_event WHERE execution_id = %s"
                " AND (journal_id, sequence) IN (" + key_placeholders + ")"
                " ORDER BY recorded_at, journal_id, sequence"
            ).format(sql.Identifier(seeded)),
            (EXECUTION_ID, "small", 101, "small", 102, "small", 103),
        ).fetchone()[0]

    def node_types(plan: object) -> list[str]:
        def collect(node: dict[str, object]) -> list[str]:
            found = [str(node["Node Type"])]
            for child in node.get("Plans", []):  # type: ignore[union-attr]
                found.extend(collect(child))  # type: ignore[arg-type]
            return found

        return collect(plan[0]["Plan"])  # type: ignore[index]

    metadata_types = node_types(metadata_plan)
    fetch_types = node_types(fetch_plan)
    assert any("Index" in node for node in metadata_types), metadata_types
    assert "Seq Scan" not in metadata_types, metadata_types
    assert any("Index" in node for node in fetch_types), fetch_types
    assert "Seq Scan" not in fetch_types, fetch_types


def test_writes_between_reads_follow_the_documented_refresh_semantics(dsn: str, seeded: str, base_url: str) -> None:
    root = events_url(base_url=base_url, execution_id=EXECUTION_ID)
    _, _, first = http_get_raw(url=f"{root}?limit=5&journal_id=biga")
    token = first["next_cursor"]
    assert isinstance(token, str)

    client = ledger.Ledger(dsn=dsn, schema=seeded)
    try:
        # Rows appended after the cursor position are visible on later pages.
        client.append_events(
            events=[
                JournalEvent(
                    schema_version=1,
                    execution_id=EXECUTION_ID,
                    journal_id="late",
                    sequence=n + 1,
                    recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(days=1, seconds=n)),
                    event="late",
                    data={"n": n},
                )
                for n in range(3)
            ]
        )
        # Rows appended with earlier timestamps sort before the cursor and
        # are never returned by the forward walk.
        client.append_events(
            events=[
                JournalEvent(
                    schema_version=1,
                    execution_id=EXECUTION_ID,
                    journal_id="small",
                    sequence=SMALL_EVENT_COUNT + n + 1,
                    recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(minutes=n)),
                    event="backdated",
                    data={"n": n},
                )
                for n in range(2)
            ]
        )
    finally:
        client.close()

    events, _ = walk_pages(
        base_url=base_url, execution_id=EXECUTION_ID, query="&journal_id=biga", limit=500, start_cursor=token
    )
    # The walk continues the biga journal exactly where the cursor left it.
    assert [event["sequence"] for event in events] == list(range(6, 41))
    late_events, _ = walk_pages(base_url=base_url, execution_id=EXECUTION_ID, query="&journal_id=late", limit=500)
    assert [event["sequence"] for event in late_events] == [1, 2, 3]
    backdated, _ = walk_pages(
        base_url=base_url,
        execution_id=EXECUTION_ID,
        query="&journal_id=small&recorded_after=" + ledger.format_utc(value=BASE_TIME + timedelta(hours=2)),
        limit=500,
    )
    assert all(event["event"] != "backdated" for event in backdated)


def test_reset_client_mid_stream_and_database_failure_are_safe(dsn: str, seeded: str, base_url: str) -> None:
    root = events_url(base_url=base_url, execution_id=EXECUTION_ID)
    # A client that vanishes mid-stream must not take the server down.
    stream_path = urllib.parse.urlparse(root).path + "?limit=500"
    rude = socket.create_connection(("127.0.0.1", server_port(base_url=base_url)), timeout=5)
    rude.sendall(f"GET {stream_path} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
    rude.recv(65536)
    rude.close()
    time.sleep(0.3)
    status, _, body = http_get_raw(url=f"{root}?limit=3&journal_id=biga")
    assert status == 200 and body["count"] == 3

    broken = api.ReadAPIServer(dsn=UNREACHABLE_DSN, schema=seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=broken.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = http_get_raw(
            url=f"http://127.0.0.1:{broken.port}/api/v1/executions/{EXECUTION_ID}/events?limit=3"
        )
        assert status == 500
        assert body == {"error": "internal error"}
    finally:
        broken.shutdown()
        broken.server_close()


def server_port(*, base_url: str) -> int:
    return int(base_url.rsplit(":", 1)[1])


def test_health_and_metrics_stay_distinct_from_database_availability(seeded: str) -> None:
    broken = api.ReadAPIServer(dsn=UNREACHABLE_DSN, schema=seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=broken.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = http_get_raw(url=f"http://127.0.0.1:{broken.port}/healthz")
        assert status == 503 and body == {"status": "unavailable"}
        with urllib.request.urlopen(f"http://127.0.0.1:{broken.port}/metrics", timeout=10) as response:
            metrics = response.read().decode("ascii")
        assert response.status == 200
        assert "ci_observability_ledger_healthcheck_success 0" in metrics
    finally:
        broken.shutdown()
        broken.server_close()


def _server_vms_hwm(*, pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    raise AssertionError("VmHWM not found in server status")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_server_memory_is_bounded_while_streaming_500_mib(dsn: str, bulk_execution: str) -> None:
    """The isolated server streams a ~500 MiB window without materializing it.

    Peak process memory is measured from /proc, not inferred from the
    already-truncated response: a fetchall-and-dump implementation would
    need well over a gigabyte for this request."""

    port = _free_port()
    env = dict(os.environ)
    env["CI_OBSERVABILITY_DSN"] = dsn
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "monitoring.ci_observability.api",
            "serve",
            "--schema",
            bulk_execution,
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    root = f"http://127.0.0.1:{port}/api/v1/executions/{BULK_EXECUTION_ID}/events"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                pass
            assert time.monotonic() < deadline, "server never became healthy"
            time.sleep(0.1)
        baseline = _server_vms_hwm(pid=process.pid)

        # The legacy window: 480 near-1 MiB events, one ~500 MiB response,
        # read in chunks and never retained by the client either.
        total_bytes = 0
        journal_markers = 0
        with urllib.request.urlopen(f"{root}?limit=500&offset=0", timeout=300) as response:
            assert response.status == 200
            while chunk := response.read(1 << 20):
                total_bytes += len(chunk)
                journal_markers += chunk.count(b'"journal_id"')
        assert journal_markers == BULK_EVENT_COUNT
        assert total_bytes > 470 * 1024 * 1024

        # The cursor walk over the same data: every page within the cap.
        events, page_sizes = walk_pages(
            base_url=f"http://127.0.0.1:{port}", execution_id=BULK_EXECUTION_ID, query="", limit=500
        )
        assert len(events) == BULK_EVENT_COUNT
        assert max(page_sizes) <= api.MAX_EVENT_PAGE_BYTES
        assert min(len(event["data"]["blob"]) for event in events) == BIG_EVENT_PAD_BYTES

        peak = _server_vms_hwm(pid=process.pid)
    finally:
        process.terminate()
        process.wait(timeout=15)
    # A materializing implementation holds the rows, the parsed objects,
    # and the serialized body at once and would exceed a gigabyte here.
    assert peak < 350 * 1024 * 1024, f"server peak {peak} with baseline {baseline}"
    assert baseline < 200 * 1024 * 1024


def test_planned_payload_fetch_is_bounded_under_concurrent_inserts(dsn: str, seeded: str) -> None:
    """The payload fetch returns exactly the planned keys, never a range.

    Reproduces the reviewer's deterministic metadata/fetch interleave: rows
    appended inside the planned span between the two queries must not join
    the payload fetch, so the materialized bytes stay bounded by the plan
    and the walk cannot end while matching rows remain."""

    execution_id = "gh-1-r902-a1-j1"
    _register_execution(dsn=dsn, schema=seeded, execution_id=execution_id, run_id=902)
    pad = "y" * 1_039_000
    planned = [
        JournalEvent(
            schema_version=1,
            execution_id=execution_id,
            journal_id="plan",
            sequence=n + 1,
            recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(seconds=n)),
            event="e",
            data={"blob": pad},
        )
        for n in range(3)
    ]
    _append_batches(dsn=dsn, schema=seeded, events=planned, per_batch=3)

    reader = ledger.Ledger(dsn=dsn, schema=seeded)
    writer = ledger.Ledger(dsn=dsn, schema=seeded)
    try:
        clauses, params = reader._event_filter_clauses(
            execution_id=execution_id, journal_id=None, recorded_after=None, recorded_before=None
        )
        # The metadata plan: three near-1 MiB keys, as one 8 MiB page start.
        metadata = reader._event_key_metadata(clauses=clauses, params=params, after=None, limit=3)
        assert len(metadata) == 3
        planned_keys = [(row[0], row[1]) for row in metadata]
        planned_bytes = sum(row[3] for row in metadata)

        # Between the plan and the payload fetch, a concurrent writer
        # appends twenty near-1 MiB rows whose sort keys fall inside the
        # planned span [first, last].
        first_text, last_text = metadata[0][2], metadata[-1][2]
        first_at, last_at = ledger.parse_utc_text(value=first_text), ledger.parse_utc_text(value=last_text)
        spread = (last_at - first_at) / 20
        race_events = [
            JournalEvent(
                schema_version=1,
                execution_id=execution_id,
                journal_id="race",
                sequence=n + 1,
                recorded_at=ledger.format_utc(value=first_at + spread * (n + 1)),
                event="race",
                data={"blob": pad},
            )
            for n in range(20)
        ]
        # Two batches: twenty near-1 MiB events exceed the 16 MiB batch cap.
        writer.append_events(events=race_events[:10])
        writer.append_events(events=race_events[10:])

        events = reader._fetch_selected_events(clauses=clauses, params=params, keys=metadata)
        assert [(event.journal_id, event.sequence) for event in events] == planned_keys
        fetched_bytes = sum(len(ledger.canonical_payload(data=event.data).encode("utf-8")) for event in events)
        assert fetched_bytes == planned_bytes
        # The page itself, served through the public API, is the planned
        # prefix: three events, a cursor, and no silent end.
        page = reader.list_event_page(execution_id=execution_id, limit=3)
        assert len(page.events) == 3
        assert page.next_cursor is not None
    finally:
        reader.close()
        writer.close()


def test_concurrent_appends_cannot_end_a_walk_silently(dsn: str, seeded: str, base_url: str) -> None:
    """A walk under concurrent appends never claims exhaustion by trimming."""
    execution_id = "gh-1-r905-a1-j1"
    _register_execution(dsn=dsn, schema=seeded, execution_id=execution_id, run_id=905)
    initial = [
        JournalEvent(
            schema_version=1,
            execution_id=execution_id,
            journal_id="walk",
            sequence=n + 1,
            recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(seconds=n)),
            event="e",
            data={"i": n},
        )
        for n in range(600)
    ]
    _append_batches(dsn=dsn, schema=seeded, events=initial, per_batch=10_000)

    stop = threading.Event()
    appended: list[int] = []
    writer_client = ledger.Ledger(dsn=dsn, schema=seeded)
    next_sequence = {"hammer0": 1, "hammer1": 1}

    def append_batches() -> None:
        batch_number = 0
        try:
            while not stop.is_set():
                batch_number += 1
                journal_id = f"hammer{batch_number % 2}"
                first_sequence = next_sequence[journal_id]
                next_sequence[journal_id] += 100
                writer_client.append_events(
                    events=[
                        JournalEvent(
                            schema_version=1,
                            execution_id=execution_id,
                            journal_id=journal_id,
                            sequence=first_sequence + n,
                            recorded_at=ledger.format_utc(
                                value=BASE_TIME + timedelta(milliseconds=(first_sequence + n) * 7)
                            ),
                            event="hammer",
                            data={"batch": batch_number, "i": n},
                        )
                        for n in range(100)
                    ]
                )
                appended.append(100)
                time.sleep(0.02)
        except Exception:
            # A writer failure fails the test through the main thread's
            # final coverage assertion rather than a dangling thread.
            stop.set()
            raise

    writer = threading.Thread(target=append_batches)
    writer.start()
    full_pages = 0
    try:
        root = events_url(base_url=base_url, execution_id=execution_id)
        cursor: str | None = None
        seen: list[tuple[str, int]] = []
        while True:
            url = f"{root}?limit=50"
            if cursor is not None:
                url += f"&cursor={urllib.parse.quote(cursor, safe='')}"
            status, raw, body = http_get_raw(url=url)
            assert status == 200, body
            assert len(raw) <= body["byte_cap"]
            if body["count"] == 50:
                full_pages += 1
                # A full page always plans more rows: the cursor must be
                # present, so the walk cannot silently end here.
                assert isinstance(body["next_cursor"], str), body
            seen.extend((event["journal_id"], event["sequence"]) for event in body["events"])
            if body["next_cursor"] is None:
                break
            cursor = body["next_cursor"]
        assert full_pages >= 10
    finally:
        stop.set()
        writer.join(timeout=60)
        writer_client.close()

    # Once the writers stop, a fresh walk covers every row exactly.
    events, _ = walk_pages(base_url=base_url, execution_id=execution_id, query="", limit=500)
    expected = direct_keys(dsn=dsn, schema=seeded, execution_id=execution_id)
    assert event_keys(events=events) == expected
    assert len(expected) == 600 + sum(appended)


def test_page_cap_holds_at_legal_field_maxima(dsn: str, seeded: str, base_url: str) -> None:
    """Adversarial legal maxima and a tuned payload keep the body within the cap.

    Every envelope field sits at its legal maximum, so one event's wire
    fragment exceeds the ledger's 512-byte planning allowance and the API's
    honest byte cut is what bounds the page. The reserved suffix must then
    cover the real serialized suffix, or the body would exceed the
    advertised cap exactly as the review reproduced."""

    # 200-character execution id: the pattern maximum.
    execution_id = "e" * 200
    _register_execution(dsn=dsn, schema=seeded, execution_id=execution_id, run_id=903)
    journal_id = "j" * 128
    event_name = "v" * 128
    invocation_id = "i" * 128
    clock_id = "c" * 128
    recorded_at = "2026-10-28T23:00:00.123456789Z"
    source_at = "2026-10-28T22:59:59.123456789Z"

    # The envelope of one event, measured from a sample with every field at
    # its maximum; a blob of N bytes adds exactly N ('x' needs no escaping).
    sample = ledger.StoredEvent(
        schema_version=1,
        execution_id=execution_id,
        journal_id=journal_id,
        sequence=497,
        recorded_at=recorded_at,
        event=event_name,
        data={"blob": ""},
        # Microseconds: the worst-case serialized ingested_at length.
        ingested_at=BASE_TIME + timedelta(microseconds=123456),
        source_at=source_at,
        invocation_id=invocation_id,
        clock_id=clock_id,
        clock_uncertainty_ns=9_223_372_036_854_775_807,
        mono_ns=9_223_372_036_854_775_807,
    )
    envelope = len(api._event_fragment(event=sample))
    assert envelope > ledger._EVENT_ENVELOPE_ALLOWANCE_BYTES, "the maxima must push past the ledger allowance"
    prefix_len = len(('{"execution_id":' + json.dumps(execution_id) + ',"events":[').encode())
    # Tune so the full 500-row page overshoots the cap by a few kilobytes:
    # the byte cut then binds just below the row cap.
    total_events = 500
    pad = (
        (api.MAX_EVENT_PAGE_BYTES - prefix_len - api._CURSOR_SUFFIX_SLACK_BYTES - (total_events - 1)) // total_events
        - envelope
        + 16
    )
    assert 0 < pad < ledger.MAX_EVENT_DATA_BYTES

    events = [
        JournalEvent(
            schema_version=1,
            execution_id=execution_id,
            journal_id=journal_id,
            sequence=n + 1,
            recorded_at=recorded_at,
            event=event_name,
            data={"blob": "x" * pad},
            source_at=source_at,
            invocation_id=invocation_id,
            clock_id=clock_id,
            clock_uncertainty_ns=9_223_372_036_854_775_807,
            mono_ns=9_223_372_036_854_775_807,
        )
        for n in range(total_events)
    ]
    _append_batches(dsn=dsn, schema=seeded, events=events, per_batch=100)

    root = events_url(base_url=base_url, execution_id=execution_id)
    status, raw, first = http_get_raw(url=f"{root}?limit=500")
    assert status == 200, first
    assert len(raw) <= first["byte_cap"], f"body {len(raw)} exceeds the advertised cap {first['byte_cap']}"
    # The byte cut, not the row cap, bounded this page.
    assert first["count"] < 500
    fragment_bytes = envelope + pad
    assert len(raw) > api.MAX_EVENT_PAGE_BYTES - 2 * fragment_bytes, "the cut did not exercise the cap boundary"
    # The reserved suffix covers the actual serialized suffix of this page,
    # measured from the exact served fragments rather than an estimate.
    served = first["events"]
    fragments_total = sum(len(json.dumps(event, separators=(",", ":")).encode()) for event in served)
    actual_suffix = len(raw) - prefix_len - fragments_total - (len(served) - 1)
    assert 0 < actual_suffix <= api._CURSOR_SUFFIX_SLACK_BYTES, actual_suffix
    # The walk covers every adversarial event exactly once, forward-only.
    walked, page_sizes = walk_pages(base_url=base_url, execution_id=execution_id, query="", limit=500)
    assert event_keys(events=walked) == [(journal_id, n) for n in range(1, total_events + 1)]
    assert all(size <= api.MAX_EVENT_PAGE_BYTES for size in page_sizes)


def test_offset_windows_past_the_end_are_empty(dsn: str, seeded: str, base_url: str) -> None:
    """An offset beyond the matching rows is an empty window, not a wrap-around."""
    root = events_url(base_url=base_url, execution_id=EXECUTION_ID)
    empty_execution = "gh-1-r904-a1-j1"
    _register_execution(dsn=dsn, schema=seeded, execution_id=empty_execution, run_id=904)

    cases = [
        ("?limit=5", 5),  # offset 0 on a 40-event journal-filtered window
        ("?limit=5&journal_id=biga", 5),
        ("?limit=2&journal_id=biga&offset=40", 0),  # exactly at the end
        ("?limit=2&journal_id=biga&offset=50", 0),  # past the end
        ("?limit=5&journal_id=biga&offset=39", 1),  # the last row
    ]
    for query, expected_count in cases:
        status, raw, body = http_get_raw(url=root + query)
        assert status == 200, (query, body)
        assert body["count"] == expected_count, (query, body["count"])
        if expected_count == 0:
            assert body["events"] == []
            assert body["next_cursor"] is None
        # The stream always terminates cleanly (complete JSON body).
        assert raw.endswith(b"}")

    for query in ("?limit=5", "?limit=5&offset=3"):
        status, _, body = http_get_raw(url=events_url(base_url=base_url, execution_id=empty_execution) + query)
        assert status == 200, body
        assert body["count"] == 0
        assert body["events"] == []
        assert body["next_cursor"] is None


def test_database_failure_midstream_ends_the_stream(dsn: str) -> None:
    """A database fault after the stream started ends it, cleanly and once.

    The client sees an incomplete chunked body, never a healthy truncated
    200 and never a second status line or error body written into the
    stream."""

    schema = f"ci_obs_w01d_fault_{os.getpid()}"
    server = None
    stream = None
    try:
        # Owned from the first mutation: a failed setup also drops the
        # schema instead of leaking it into the shared server.
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == [1, 2, 3]
        execution_id = "gh-1-r906-a1-j1"
        _register_execution(dsn=dsn, schema=schema, execution_id=execution_id, run_id=906)
        pad = "z" * 1_039_000
        events = [
            JournalEvent(
                schema_version=1,
                execution_id=execution_id,
                journal_id="fault",
                sequence=n + 1,
                recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(seconds=n)),
                event="e",
                data={"blob": pad},
            )
            for n in range(24)
        ]
        _append_batches(dsn=dsn, schema=schema, events=events, per_batch=15)

        server = api.ReadAPIServer(dsn=dsn, schema=schema, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.port}/api/v1/executions/{execution_id}/events?limit=500&offset=0"
        stream = urllib.request.urlopen(url, timeout=60)
        assert stream.status == 200
        # Pull the first chunk so at least one page fetch already ran.
        stream.read(1 << 20)
        # The database fault: the schema the stream is reading vanishes.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        received = stream.read()
        # A healthy truncated 200 would return a complete body here.
        raise AssertionError(f"stream completed with {len(received)} trailing bytes")
    except http.client.IncompleteRead as exc:
        partial = exc.partial
        assert len(partial) > (1 << 20), "the client should have received the streamed prefix"
        assert b'"error"' not in partial, "no error body may be written into the stream"
        assert b"HTTP/1.1" not in partial, "no second status line may be written into the stream"
    finally:
        if stream is not None:
            stream.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(scope="session")
def protocol_schema(dsn: str, test_schema: str) -> str:
    """One execution of near-1 MiB events for the raw-socket protocol tests."""
    execution_id = "gh-1-r907-a1-j1"
    _register_execution(dsn=dsn, schema=test_schema, execution_id=execution_id, run_id=907)
    events = [
        _big_event(execution_id=execution_id, journal_id="proto", sequence=n + 1, offset_seconds=n) for n in range(12)
    ]
    _append_batches(dsn=dsn, schema=test_schema, events=events, per_batch=12)
    return execution_id


def _open_raw_connection(*, port: int) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port), timeout=30)
    connection.settimeout(30)
    return connection


def _send_get(*, connection: socket.socket, path: str) -> None:
    connection.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))


class _RawReader:
    """Sequential reads from one raw socket, buffered across recv boundaries."""

    def __init__(self, *, connection: socket.socket, initial: bytes = b"") -> None:
        self._connection = connection
        self._buffer = bytearray(initial)

    def _fill(self) -> None:
        chunk = self._connection.recv(65536)
        if not chunk:
            raise http.client.IncompleteRead(bytes(self._buffer), None)
        self._buffer += chunk

    def read_line(self) -> bytes:
        while b"\r\n" not in self._buffer:
            self._fill()
        line, _, remainder = bytes(self._buffer).partition(b"\r\n")
        self._buffer = bytearray(remainder)
        return line + b"\r\n"

    def read_exact(self, *, size: int) -> bytes:
        while len(self._buffer) < size:
            self._fill()
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


def _read_response_head(*, connection: socket.socket) -> tuple[int, dict[str, str], bytes]:
    """Read one response's status line and headers; returns any body bytes too."""
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = connection.recv(65536)
        if not chunk:
            raise AssertionError(f"connection closed before headers: {buffer[:64]!r}")
        buffer += chunk
    raw_head, _, remainder = buffer.partition(b"\r\n\r\n")
    lines = raw_head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers, remainder


def _read_full_body(*, connection: socket.socket, headers: dict[str, str], received: bytes = b"") -> bytes:
    """Read a Content-Length body to completion, starting from bytes already buffered."""
    reader = _RawReader(connection=connection, initial=received)
    return reader.read_exact(size=int(headers["content-length"]))


def _drain_chunked_body(*, connection: socket.socket, received: bytes = b"") -> bytes:
    """Read a chunked body to its terminating zero chunk, from buffered bytes."""
    reader = _RawReader(connection=connection, initial=received)
    body = b""
    while True:
        size = int(reader.read_line().strip().split(b";")[0], 16)
        if size == 0:
            reader.read_exact(size=2)  # trailing CRLF after the last chunk
            return body
        body += reader.read_exact(size=size)
        reader.read_exact(size=2)  # chunk-terminating CRLF


def test_cursor_response_stall_aborts_without_a_second_status(dsn: str, test_schema: str, protocol_schema: str) -> None:
    """A client that stalls mid-body gets one 200 and a clean cutoff, never a 500.

    The reviewer's reproduction: the server starts a 200 cursor page, the
    client reads a prefix and stalls past the socket write timeout, and the
    outer handler must not write a second HTTP status or error body into
    the same stream. The follow-up request proves the request slot was
    released even with max_concurrent_requests=1."""

    server = api.ReadAPIServer(
        dsn=dsn, schema=test_schema, host="127.0.0.1", port=0, max_concurrent_requests=1, socket_timeout_seconds=2.0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = None
    try:
        path = f"/api/v1/executions/{protocol_schema}/events?limit=500"
        connection = _open_raw_connection(port=server.port)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        _send_get(connection=connection, path=path)
        status, headers, remainder = _read_response_head(connection=connection)
        assert status == 200
        assert int(headers["content-length"]) > (7 << 20)

        # Read a small prefix, then stall past the server's two-second
        # write timeout while it blocks on the full socket.
        prefix = bytearray(remainder)
        while len(prefix) < 8192:
            chunk = connection.recv(65536)
            if not chunk:
                break
            prefix += chunk
        assert len(prefix) >= 8192
        time.sleep(3.0)

        received = prefix
        while True:
            try:
                chunk = connection.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            received += chunk
        # The response ends short of the advertised Content-Length.
        assert len(received) < int(headers["content-length"])
        # One status line only, and no error body injected into the stream.
        assert received.count(b"HTTP/1.1") == 0, "body must not contain a second status line"
        assert b'"error"' not in received, "no error body may be written into the stream"
        connection.close()
        connection = None

        # The request slot was released: a fresh request on a new
        # connection answers normally.
        follow_up = _open_raw_connection(port=server.port)
        try:
            _send_get(connection=follow_up, path=f"/api/v1/executions/{protocol_schema}/events?limit=2")
            status, headers, remainder = _read_response_head(connection=follow_up)
            assert status == 200
            _read_full_body(connection=follow_up, headers=headers, received=remainder)
        finally:
            follow_up.close()
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()
        server.server_close()


def test_offset_stream_stall_aborts_the_chunked_body_once(dsn: str, test_schema: str, protocol_schema: str) -> None:
    """The offset stream under the same stall ends once, without a second status.

    The chunked header write itself precedes the stream's inner try, but a
    pure-network header-flush timeout cannot be triggered deterministically
    on a fresh request: the socket send buffer is empty when the headers
    are written, so the first blocking write is a body chunk. The unified
    boundary covers the header path structurally: send_response marks the
    response started before any wire write, so any later failure, including
    one between the header write and the inner try, only closes the
    connection and logs the exception type."""

    server = api.ReadAPIServer(
        dsn=dsn, schema=test_schema, host="127.0.0.1", port=0, max_concurrent_requests=1, socket_timeout_seconds=2.0
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = None
    try:
        path = f"/api/v1/executions/{protocol_schema}/events?limit=500&offset=0"
        connection = _open_raw_connection(port=server.port)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        _send_get(connection=connection, path=path)
        status, headers, remainder = _read_response_head(connection=connection)
        assert status == 200
        assert headers["transfer-encoding"] == "chunked"

        # The prefix chunk is tiny and arrives before the first page fetch;
        # keep reading until a substantial body prefix is buffered.
        prefix = bytearray(remainder)
        while len(prefix) < 8192:
            chunk = connection.recv(65536)
            if not chunk:
                break
            prefix += chunk
        assert len(prefix) >= 8192
        time.sleep(3.0)

        received = prefix
        while True:
            try:
                chunk = connection.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            received += chunk
        # The stream terminated early and cleanly: no second status line
        # and no error JSON inside the chunked body.
        assert received.count(b"HTTP/1.1") == 0, "body must not contain a second status line"
        assert b'"error"' not in received, "no error body may be written into the stream"
        connection.close()
        connection = None

        follow_up = _open_raw_connection(port=server.port)
        try:
            _send_get(connection=follow_up, path=path)
            status, headers, remainder = _read_response_head(connection=follow_up)
            assert status == 200
            body = _drain_chunked_body(connection=follow_up, received=remainder)
            assert body.endswith(b"}")
        finally:
            follow_up.close()
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()
        server.server_close()


def test_keepalive_resets_response_state_between_requests(dsn: str) -> None:
    """A successful 200 must not poison later requests on the same connection.

    The handler instance is reused across keepalive requests, so the
    response-started state must reset per request: after a complete cursor
    200, a malformed cursor still gets its own 400, and a pre-response
    database failure still gets its own 500, neither a stale silent close.
    Uses a dedicated schema because request 3 drops it."""

    schema = f"ci_obs_w01d_keepalive_{os.getpid()}"
    execution_id = "gh-1-r908-a1-j1"
    server = None
    connection = None
    try:
        assert ledger.apply_migrations(dsn=dsn, schema=schema) == [1, 2, 3]
        _register_execution(dsn=dsn, schema=schema, execution_id=execution_id, run_id=908)
        _append_batches(
            dsn=dsn,
            schema=schema,
            events=[
                JournalEvent(
                    schema_version=1,
                    execution_id=execution_id,
                    journal_id="ka",
                    sequence=n + 1,
                    recorded_at=ledger.format_utc(value=BASE_TIME + timedelta(seconds=n)),
                    event="e",
                    data={"n": n},
                )
                for n in range(2)
            ],
            per_batch=2,
        )

        server = api.ReadAPIServer(dsn=dsn, schema=schema, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"/api/v1/executions/{execution_id}/events"

        # Request 1: a complete, successful cursor page.
        connection = _open_raw_connection(port=server.port)
        _send_get(connection=connection, path=f"{root}?limit=2")
        status, headers, remainder = _read_response_head(connection=connection)
        assert status == 200
        body = _read_full_body(connection=connection, headers=headers, received=remainder)
        assert json.loads(body)["count"] == 2

        # Request 2 on the same connection: a malformed cursor is a normal
        # pre-response 400 with its error body, not a stale close.
        _send_get(connection=connection, path=f"{root}?limit=2&cursor=not-base64!!")
        status, headers, remainder = _read_response_head(connection=connection)
        assert status == 400
        body = _read_full_body(connection=connection, headers=headers, received=remainder)
        assert json.loads(body) == {"error": "cursor is malformed"}

        # Request 3 on the same connection: the database vanishes, so the
        # page request fails before any response write and still gets its
        # own honest 500.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        _send_get(connection=connection, path=f"{root}?limit=2")
        status, headers, remainder = _read_response_head(connection=connection)
        assert status == 500
        body = _read_full_body(connection=connection, headers=headers, received=remainder)
        assert json.loads(body) == {"error": "internal error"}
    finally:
        if connection is not None:
            connection.close()
        if server is not None:
            server.shutdown()
            server.server_close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

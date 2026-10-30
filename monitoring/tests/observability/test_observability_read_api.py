"""Real-HTTP tests for the CI observability read-only API (W01a).

The server runs for real, on a loopback port, against the real
PostgreSQL database selected by CI_OBSERVABILITY_TEST_DSN, and every
request goes through an actual HTTP client. The tests create their own
schema and a throwaway reader role (distinct from the
deployment-owned ci_observability_reader) and drop both afterwards.

Run from the repository root:

    CI_OBSERVABILITY_TEST_DSN=postgresql://... \\
        python -m pytest monitoring/tests/observability/test_observability_read_api.py --no-cov
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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

from collections.abc import Iterator, Sequence

from prometheus_client.parser import text_string_to_metric_families
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from monitoring.ci_observability import api, ledger
from monitoring.ci_observability.ledger import ExecutionRecord, JournalEvent, JSONValue

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
MAIN_EXECUTION = "gh-1-r900-a1-j1"
UNREACHABLE_DSN = "postgresql://ci_observability@127.0.0.1:1/ci_observability?connect_timeout=2"


def make_execution(*, execution_id: str, **overrides: object) -> ExecutionRecord:
    values: dict[str, object] = {
        "execution_id": execution_id,
        "schema_version": 1,
        "repository_id": 1,
        "run_id": 900,
        "run_attempt": 1,
        "logical_job_key": "tests.yml#e2e",
        "executed": True,
        "github_job_id": None,
    }
    values.update(overrides)
    return ExecutionRecord(**values)  # type: ignore[arg-type]


def ts(*, offset_seconds: float) -> str:
    return ledger.format_utc(value=BASE_TIME + timedelta(seconds=offset_seconds))


def make_event(*, execution_id: str, journal_id: str, sequence: int, **overrides: object) -> JournalEvent:
    values: dict[str, object] = {
        "schema_version": 1,
        "execution_id": execution_id,
        "journal_id": journal_id,
        "sequence": sequence,
        "recorded_at": ts(offset_seconds=sequence),
        "event": "test_phase_started",
        "data": {"test_id": f"tests/e2e/caas/test_cluster.py::test_case_{sequence}"},
        "source_at": None,
        "invocation_id": None,
        "clock_id": None,
        "clock_uncertainty_ns": None,
        "mono_ns": None,
    }
    values.update(overrides)
    return JournalEvent(**values)  # type: ignore[arg-type]


def http_get(*, url: str, method: str = "GET") -> tuple[int, dict[str, JSONValue]]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_is_free(*, port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


def wait_for_health(*, base_url: str, deadline_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _ = http_get(url=f"{base_url}/healthz")
            if status == 200:
                return
        except urllib.error.URLError as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"server at {base_url} never became healthy: {last_error}")


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
    name = f"ci_obs_w01a_readapi_{os.getpid()}"
    assert ledger.apply_migrations(dsn=dsn, schema=name) == [1, 2, 3]
    yield name
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def seeded(dsn: str, test_schema: str) -> str:
    """Five executions and twelve events, stable for the whole session."""
    client = ledger.Ledger(dsn=dsn, schema=test_schema)
    try:
        client.register_execution(record=make_execution(execution_id=MAIN_EXECUTION, github_job_id=901))
        client.register_execution(
            record=make_execution(execution_id="gh-1-r900-a2-j1", run_attempt=2, github_job_id=902)
        )
        client.register_execution(
            record=make_execution(execution_id="gh-1-r900-a2-j1-inh", run_attempt=2, executed=False, github_job_id=901)
        )
        client.register_execution(record=make_execution(execution_id="gh-2-r901-a1-j1", repository_id=2, run_id=901))
        client.register_execution(
            record=make_execution(execution_id="gh-1-r900-a1-j1-queued", github_job_id=903, executed=None)
        )
        client.record_authoritative_conclusion(
            execution_id=MAIN_EXECUTION, github_run_conclusion="failure", github_job_conclusion="failure"
        )
        client.record_authoritative_conclusion(
            execution_id="gh-1-r900-a2-j1", github_run_conclusion="success", github_job_conclusion="success"
        )
        client.append_events(
            events=[
                make_event(execution_id=MAIN_EXECUTION, journal_id="main", sequence=1),
                make_event(execution_id=MAIN_EXECUTION, journal_id="main", sequence=2),
                make_event(
                    execution_id=MAIN_EXECUTION,
                    journal_id="main",
                    sequence=3,
                    event="execution_finished",
                    data={"outcome": "failed", "exit_code": 1, "big": 1e23, "neg": -0.0},
                    recorded_at="2026-10-28T12:00:03.123456789Z",
                    source_at="2026-10-28T12:00:02.500000001Z",
                    invocation_id="inv-7",
                    clock_id="mono-a",
                    clock_uncertainty_ns=250,
                ),
            ]
        )
        client.append_events(
            events=[
                make_event(
                    execution_id=MAIN_EXECUTION,
                    journal_id="gw0",
                    sequence=seq,
                    recorded_at=ts(offset_seconds=9 + seq),
                    mono_ns=seq - 1,
                )
                for seq in range(1, 10)
            ]
        )
    finally:
        client.close()
    return test_schema


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


@pytest.fixture(scope="session")
def reader_dsn(dsn: str, seeded: str) -> Iterator[str]:
    """A throwaway role with SELECT-only access to the test schema."""
    role = f"ci_obs_w01a_reader_{os.getpid()}"
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
    yield f"postgresql://{role}@{info['host']}:{info['port']}/{info['dbname']}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                sql.Identifier(seeded), sql.Identifier(role)
            )
        )
        conn.execute(sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(sql.Identifier(seeded), sql.Identifier(role)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def test_healthz_reports_ok(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/healthz")
    assert status == 200
    assert body == {"status": "ok"}


def test_metrics_are_valid_prometheus_text(base_url: str) -> None:
    with urllib.request.urlopen(f"{base_url}/metrics", timeout=10) as response:
        assert response.status == 200
        assert response.headers["Content-Type"].startswith("text/plain; version=0.0.4")
        metrics = list(text_string_to_metric_families(response.read().decode()))
    assert len(metrics) == 1
    assert metrics[0].type == "gauge"
    assert metrics[0].name == "ci_observability_ledger_healthcheck_success"
    assert metrics[0].samples[0].value == 1
    assert metrics[0].samples[0].labels == {}


def test_metrics_distinguish_a_running_api_from_a_failed_database_check(seeded: str) -> None:
    server = api.ReadAPIServer(dsn=UNREACHABLE_DSN, schema=seeded, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=10) as response:
            assert response.status == 200
            payload = response.read().decode()
        metrics = list(text_string_to_metric_families(payload))
        assert metrics[0].samples[0].value == 0
        assert "postgresql" not in payload
        assert "Traceback" not in payload
    finally:
        server.shutdown()
        server.server_close()


def test_healthz_reports_unavailable_when_database_is_down(dsn: str, seeded: str) -> None:
    server = api.ReadAPIServer(dsn=UNREACHABLE_DSN, schema=seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = http_get(url=f"http://127.0.0.1:{server.port}/healthz")
        assert status == 503
        assert body == {"status": "unavailable"}
    finally:
        server.shutdown()
        server.server_close()


def test_runs_separate_executions_from_registrations(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/runs")
    assert status == 200
    runs = {(run["repository_id"], run["run_id"]): run for run in body["runs"]}
    assert set(runs) == {(1, 900), (2, 901)}
    assert runs[(1, 900)]["run_attempts"] == [1, 2]
    assert runs[(1, 900)]["execution_count"] == 2
    assert runs[(1, 900)]["registration_count"] == 4
    assert runs[(2, 901)]["run_attempts"] == [1]
    assert runs[(2, 901)]["execution_count"] == 1
    assert runs[(2, 901)]["registration_count"] == 1


def test_runs_filters_and_pagination(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/runs?repository_id=2")
    assert status == 200
    assert [run["run_id"] for run in body["runs"]] == [901]
    status, body = http_get(url=f"{base_url}/api/v1/runs?limit=1&offset=1")
    assert status == 200
    assert len(body["runs"]) == 1
    assert body["limit"] == 1
    assert body["offset"] == 1


def test_executions_list_filters(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/executions?run_id=900")
    assert status == 200
    assert [row["execution_id"] for row in body["executions"]] == [
        "gh-1-r900-a1-j1",
        "gh-1-r900-a1-j1-queued",
        "gh-1-r900-a2-j1",
        "gh-1-r900-a2-j1-inh",
    ]
    status, body = http_get(url=f"{base_url}/api/v1/executions?run_id=900&executed=false")
    assert status == 200
    assert [row["execution_id"] for row in body["executions"]] == ["gh-1-r900-a2-j1-inh"]
    status, body = http_get(url=f"{base_url}/api/v1/executions?github_job_id=902")
    assert status == 200
    assert [row["execution_id"] for row in body["executions"]] == ["gh-1-r900-a2-j1"]
    status, body = http_get(url=f"{base_url}/api/v1/executions?repository_id=2")
    assert status == 200
    assert [row["execution_id"] for row in body["executions"]] == ["gh-2-r901-a1-j1"]
    status, body = http_get(url=f"{base_url}/api/v1/executions?run_id=900&run_attempt=1")
    assert status == 200
    assert [row["execution_id"] for row in body["executions"]] == [MAIN_EXECUTION, "gh-1-r900-a1-j1-queued"]
    status, body = http_get(url=f"{base_url}/api/v1/executions?logical_job_key=tests.yml%23e2e&run_id=901")
    assert status == 200
    assert [row["execution_id"] for row in body["executions"]] == ["gh-2-r901-a1-j1"]


def test_executions_list_time_window(base_url: str) -> None:
    after = ledger.format_utc(value=BASE_TIME - timedelta(days=1))
    before = ledger.format_utc(value=BASE_TIME + timedelta(days=1))
    status, body = http_get(url=f"{base_url}/api/v1/executions?registered_after={after}")
    assert status == 200
    assert len(body["executions"]) == 5
    status, body = http_get(url=f"{base_url}/api/v1/executions?registered_after={before}")
    assert status == 200
    assert body["executions"] == []


def test_executions_list_rejects_invalid_input_without_echoing_it(base_url: str) -> None:
    attacks = {
        "limit=0": "limit must be an integer between 1 and 500",
        "limit=501": "limit must be an integer between 1 and 500",
        "limit=-1": "limit must be an integer between 1 and 500",
        "limit=abc": "limit must be an integer between 1 and 500",
        "offset=-1": "offset must be an integer between 0 and 10000",
        "offset=10001": "offset must be an integer between 0 and 10000",
        "offset=abc": "offset must be an integer between 0 and 10000",
        "executed=maybe": "executed must be true or false",
        "repository_id=0": "repository_id must be an integer >= 1",
        "run_id=abc": "run_id must be an integer >= 1",
        "run_attempt=0": "run_attempt must be an integer >= 1",
        "github_job_id=-3": "github_job_id must be an integer >= 1",
        "registered_after=not-a-time": "registered_after must be a UTC ISO-8601 timestamp ending in Z",
        "registered_after=2026-10-28T12:00:00": "registered_after must be a UTC ISO-8601 timestamp ending in Z",
        "registered_after=2026-10-28T14:00:00%2B02:00": "registered_after must be a UTC ISO-8601 timestamp ending in Z",
        "logical_job_key=has%20space": "logical_job_key must be a bounded identifier",
        "bogus=1": "unknown query parameter",
        "limit=1&limit=2": "a query parameter must appear exactly once",
        "novalue": "malformed query string",
    }
    for query, message in attacks.items():
        status, body = http_get(url=f"{base_url}/api/v1/executions?{query}")
        assert status == 400, (query, status, body)
        assert body == {"error": message}, (query, body)
        # The refusal is fixed text: no request value is echoed back.
        for raw_fragment in ("abc", "not-a-time", "maybe", "has space", "novalue", "501"):
            assert raw_fragment not in str(body)


def test_executions_list_bounds_request_shape(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/executions?offset=10000&run_id=900")
    assert status == 200
    assert body["executions"] == []
    long_path = "/api/v1/executions?pad=" + "a" * 3000
    status, body = http_get(url=f"{base_url}{long_path}")
    assert status == 400
    assert body == {"error": "request path too long"}
    many_fields = "&".join(f"f{index}=1" for index in range(17))
    status, body = http_get(url=f"{base_url}/api/v1/executions?{many_fields}")
    assert status == 400
    assert body == {"error": "too many query parameters"}


def test_execution_lookup_with_counts(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/executions/{MAIN_EXECUTION}")
    assert status == 200
    assert body["execution_id"] == MAIN_EXECUTION
    assert body["github_run_conclusion"] == "failure"
    assert body["github_job_conclusion"] == "failure"
    assert body["github_job_id"] == 901
    assert body["executed"] is True
    assert body["repository_id"] == 1
    assert body["event_count"] == 12
    assert body["journal_count"] == 2
    assert body["registered_at"].endswith("Z")

    status, body = http_get(url=f"{base_url}/api/v1/executions/gh-1-r900-a2-j1-inh")
    assert status == 200
    assert body["executed"] is False
    assert body["github_job_id"] == 901
    assert body["github_job_conclusion"] is None  # not executed: no conclusion of its own

    status, body = http_get(url=f"{base_url}/api/v1/executions/gh-1-r900-a1-j1-queued")
    assert status == 200
    assert body["executed"] is None  # unknown execution state, not a fabricated one


def test_execution_lookup_not_found_and_invalid_ids(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/executions/gh-1-r900-a9-j404")
    assert status == 404
    assert body == {"error": "execution not found"}
    for bad_id in ("has space", "../../etc/passwd", "semi;colon", "x" * 201):
        escaped = urllib.request.quote(bad_id, safe="")
        status, body = http_get(url=f"{base_url}/api/v1/executions/{escaped}")
        assert status == 400, (bad_id, status, body)
        assert body == {"error": "execution id must be a bounded filename-safe identifier"}
        assert bad_id not in str(body)
    status, body = http_get(url=f"{base_url}/api/v1/executions/")
    assert status == 404


def test_events_serve_original_text_and_exact_payload_values(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/executions/{MAIN_EXECUTION}/events")
    assert status == 200
    events = body["events"]
    assert len(events) == 12
    assert [(event["journal_id"], event["sequence"]) for event in events] == [
        ("main", 1),
        ("main", 2),
        ("main", 3),
        *[("gw0", seq) for seq in range(1, 10)],
    ]
    finished = events[2]
    assert finished["event"] == "execution_finished"
    # The payload keeps the delivered types: 1e23 stays a float and -0.0
    # keeps its sign, where jsonb would have normalized both.
    assert finished["data"] == {"outcome": "failed", "exit_code": 1, "big": 1e23, "neg": -0.0}
    assert isinstance(finished["data"]["big"], float)
    assert str(finished["data"]["neg"]) == "-0.0"
    # Timestamps are the original delivered text, including the ninth digit.
    assert finished["recorded_at"] == "2026-10-28T12:00:03.123456789Z"
    assert finished["source_at"] == "2026-10-28T12:00:02.500000001Z"
    assert finished["invocation_id"] == "inv-7"
    assert finished["clock_id"] == "mono-a"
    assert finished["clock_uncertainty_ns"] == 250
    assert finished["mono_ns"] is None
    assert finished["ingested_at"].endswith("Z")
    # mono_ns zero is a measured value and must survive as 0, not null.
    assert events[3]["mono_ns"] == 0
    assert events[3]["source_at"] is None

    status, body = http_get(url=f"{base_url}/api/v1/executions/{MAIN_EXECUTION}/events?journal_id=gw0")
    assert status == 200
    assert [event["sequence"] for event in body["events"]] == list(range(1, 10))


def test_events_time_window_and_pagination(base_url: str) -> None:
    after = ledger.format_utc(value=BASE_TIME + timedelta(seconds=11))
    before = ledger.format_utc(value=BASE_TIME + timedelta(seconds=13))
    status, body = http_get(
        url=f"{base_url}/api/v1/executions/{MAIN_EXECUTION}/events?recorded_after={after}&recorded_before={before}"
    )
    assert status == 200
    assert [event["sequence"] for event in body["events"]] == [2, 3, 4]

    pages = []
    for offset in (0, 5, 10):
        status, body = http_get(url=f"{base_url}/api/v1/executions/{MAIN_EXECUTION}/events?limit=5&offset={offset}")
        assert status == 200
        pages.extend(body["events"])
    assert len(pages) == 12
    keys = [(event["journal_id"], event["sequence"]) for event in pages]
    assert len(set(keys)) == 12

    status, body = http_get(url=f"{base_url}/api/v1/executions/{MAIN_EXECUTION}/events?journal_id=bad%20id")
    assert status == 400
    assert body == {"error": "journal_id must be a bounded identifier"}
    status, body = http_get(url=f"{base_url}/api/v1/executions/gh-1-r900-a9-j404/events")
    assert status == 404


def test_write_methods_are_rejected(base_url: str) -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "BEARER_SECRET_TOKEN"):
        status, body = http_get(url=f"{base_url}/api/v1/executions", method=method)
        assert status == 405, (method, status, body)
        assert body == {"error": "method not allowed", "allow": "GET"}
    status, body = http_get(url=f"{base_url}/healthz", method="POST")
    assert status == 405


def test_head_is_rejected_without_a_response_body(base_url: str) -> None:
    request = urllib.request.Request(f"{base_url}/healthz", method="HEAD")
    with pytest.raises(urllib.error.HTTPError) as rejected:
        urllib.request.urlopen(request, timeout=5)
    assert rejected.value.code == 405
    assert rejected.value.headers["Allow"] == "GET"
    assert rejected.value.read() == b""


def test_injection_attempts_are_rejected(base_url: str, dsn: str, seeded: str) -> None:
    before = ledger.Ledger(dsn=dsn, schema=seeded)
    try:
        event_count, journal_count = before.execution_counts(execution_id=MAIN_EXECUTION)
        execution_ids = [row.execution_id for row in before.list_executions(run_id=900)]
    finally:
        before.close()

    attacks = [
        "/api/v1/executions?logical_job_key='; DROP TABLE ledger_event; --",
        "/api/v1/executions?run_id=1;DROP TABLE ledger_event;--",
        "/api/v1/executions?repository_id=1 OR 1=1",
        "/api/v1/executions/x';--",
        f"/api/v1/executions/{MAIN_EXECUTION}/events?journal_id=x';--",
        f"/api/v1/executions/{MAIN_EXECUTION}/events?recorded_after=1970-01-01' UNION SELECT 1--",
        "/api/v1/runs?repository_id=(SELECT 1)",
    ]
    for attack in attacks:
        url = f"{base_url}{urllib.request.quote(attack, safe='/?&=%')}"
        status, body = http_get(url=url)
        assert status == 400, (attack, status, body)
        assert "error" in body
        # No request value is echoed into the response.
        assert "DROP TABLE" not in str(body)
        assert "UNION SELECT" not in str(body)

    after = ledger.Ledger(dsn=dsn, schema=seeded)
    try:
        assert after.execution_counts(execution_id=MAIN_EXECUTION) == (event_count, journal_count)
        assert [row.execution_id for row in after.list_executions(run_id=900)] == execution_ids
    finally:
        after.close()


def test_pattern_legal_sql_lookalikes_match_nothing(base_url: str) -> None:
    status, body = http_get(url=f"{base_url}/api/v1/executions?logical_job_key=x=1--")
    assert status == 200
    assert body["executions"] == []
    assert body["count"] == 0


def test_internal_errors_are_opaque(dsn: str, seeded: str) -> None:
    server = api.ReadAPIServer(dsn=dsn, schema="ci_obs_w01a_not_migrated", host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = http_get(url=f"http://127.0.0.1:{server.port}/api/v1/executions")
        assert status == 500
        assert body == {"error": "internal error"}
        raw = json.dumps(body)
        assert "Traceback" not in raw
        assert "SELECT" not in raw
    finally:
        server.shutdown()
        server.server_close()


def test_server_logs_carry_no_values(dsn: str, seeded: str) -> None:
    """Failure paths log the exception type only, never SQL, DSNs, or request values."""
    captured = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = captured
    broken_schema = api.ReadAPIServer(dsn=dsn, schema="ci_obs_w01a_not_migrated", host="127.0.0.1", port=0)
    unreachable = api.ReadAPIServer(dsn=UNREACHABLE_DSN, schema=seeded, host="127.0.0.1", port=0)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (broken_schema, unreachable)]
    for thread in threads:
        thread.start()
    try:
        status, _ = http_get(url=f"http://127.0.0.1:{broken_schema.port}/api/v1/executions?logical_job_key=x%27--")
        assert status in (400, 500)
        status, _ = http_get(url=f"http://127.0.0.1:{broken_schema.port}/api/v1/executions?logical_job_key=ok")
        assert status == 500
        status, _ = http_get(url=f"http://127.0.0.1:{unreachable.port}/healthz")
        assert status == 503
        # A client that disconnects before reading the response must not
        # leak a traceback from the write error.
        rude = socket.create_connection(("127.0.0.1", unreachable.port), timeout=5)
        rude.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        rude.close()
        time.sleep(0.5)
    finally:
        sys.stderr = original_stderr
        for server in (broken_schema, unreachable):
            server.shutdown()
            server.server_close()
    logged = captured.getvalue()
    assert "ci-observability-api: request failed with" in logged
    assert "ci-observability-api: health check failed with" in logged
    assert "postgresql://" not in logged
    assert "SELECT" not in logged
    assert "schema_migrations" not in logged
    assert "x'--" not in logged
    assert "Traceback" not in logged


def test_connection_cap_bounds_handler_threads(dsn: str, seeded: str) -> None:
    """Idle, partial-header, and excess sockets cannot grow handler threads."""
    server = api.ReadAPIServer(
        dsn=dsn, schema=seeded, host="127.0.0.1", port=0, max_connections=4, socket_timeout_seconds=0.5
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opened: list[socket.socket] = []
    try:
        baseline = threading.active_count()
        for _ in range(10):
            opened.append(socket.create_connection(("127.0.0.1", server.port), timeout=5))
        time.sleep(0.3)
        assert threading.active_count() <= baseline + 4

        for _ in range(10):
            partial = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            with contextlib.suppress(OSError):
                partial.sendall(b"GET /api/v1/executions HTTP/1.1\r\nHost: x\r\n")
            opened.append(partial)
        time.sleep(0.3)
        assert threading.active_count() <= baseline + 4

        # After the socket timeout expires, the threads exit and the slots
        # recover.
        for sock in opened:
            sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and threading.active_count() > baseline:
            time.sleep(0.1)
        assert threading.active_count() <= baseline

        status, body = http_get(url=f"http://127.0.0.1:{server.port}/healthz")
        assert status == 200
        assert body == {"status": "ok"}
    finally:
        for sock in opened:
            sock.close()
        server.shutdown()
        server.server_close()


def test_overloaded_connections_are_refused_safely(dsn: str, seeded: str) -> None:
    server = api.ReadAPIServer(
        dsn=dsn, schema=seeded, host="127.0.0.1", port=0, max_connections=2, socket_timeout_seconds=5
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opened: list[socket.socket] = []
    try:
        for _ in range(2):
            opened.append(socket.create_connection(("127.0.0.1", server.port), timeout=5))
        time.sleep(0.2)
        # With every slot occupied by an idle connection, a real request is
        # closed instead of answered, and the server does not grow.
        with pytest.raises(OSError):
            http_get(url=f"http://127.0.0.1:{server.port}/healthz")
        # After the occupying sockets close, the server recovers.
        for sock in opened:
            sock.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status, body = http_get(url=f"http://127.0.0.1:{server.port}/healthz")
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        assert status == 200
        assert body == {"status": "ok"}
    finally:
        for sock in opened:
            sock.close()
        server.shutdown()
        server.server_close()


def test_concurrency_cap_refuses_excess_requests(dsn: str, seeded: str) -> None:
    server = api.ReadAPIServer(
        dsn=dsn, schema=seeded, host="127.0.0.1", port=0, max_concurrent_requests=1, request_wait_seconds=0.5
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.request_slots.acquire(blocking=False)
        status, body = http_get(url=f"http://127.0.0.1:{server.port}/api/v1/runs")
        assert status == 503
        assert body == {"error": "server busy"}
        server.request_slots.release()
        status, body = http_get(url=f"http://127.0.0.1:{server.port}/api/v1/runs")
        assert status == 200
        assert len(body["runs"]) == 2
    finally:
        server.shutdown()
        server.server_close()


def test_server_refuses_non_loopback_binding() -> None:
    for host in ("0.0.0.0", "192.168.1.10", "example.invalid"):
        with pytest.raises(ValueError, match="loopback"):
            api.ReadAPIServer(dsn="postgresql://unused", schema="ci_observability", host=host, port=0)
    allowed = api.ReadAPIServer(dsn="postgresql://unused", schema="ci_observability", host="localhost", port=0)
    allowed.server_close()


def test_server_defaults_to_loopback_9185() -> None:
    assert api.DEFAULT_HOST == "127.0.0.1"
    assert api.DEFAULT_PORT == 9185
    if not port_is_free(port=9185):
        pytest.skip("port 9185 is already occupied in this environment")
    server = api.ReadAPIServer(dsn="postgresql://unused", schema="ci_observability")
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] == 9185
    finally:
        server.server_close()


def test_reader_role_can_read_but_not_write(reader_dsn: str, seeded: str) -> None:
    server = api.ReadAPIServer(dsn=reader_dsn, schema=seeded, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = http_get(url=f"http://127.0.0.1:{server.port}/api/v1/executions?run_id=900")
        assert status == 200
        assert len(body["executions"]) == 4
        status, body = http_get(url=f"http://127.0.0.1:{server.port}/api/v1/executions/{MAIN_EXECUTION}/events?limit=3")
        assert status == 200
        assert len(body["events"]) == 3
    finally:
        server.shutdown()
        server.server_close()

    # The same DSN the API uses cannot write, even though the API never tries.
    with psycopg.connect(reader_dsn) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            sql.SQL(
                "INSERT INTO {}.ledger_event"
                " (execution_id, journal_id, sequence, schema_version, recorded_at, recorded_at_text,"
                " event, data, data_sha256)"
                " VALUES ('x', 'y', 1, 1, now(), '2026-10-28T12:00:00Z', 'e', %s::json, %s)"
            ).format(sql.Identifier(seeded)),
            ("{}", "0" * 64),
        )


def _run_cli(*, dsn: str | None, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("CI_OBSERVABILITY_DSN", None)
    if dsn is not None:
        env["CI_OBSERVABILITY_DSN"] = dsn
    return subprocess.run(
        [sys.executable, "-m", "monitoring.ci_observability.api", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_cli_reads_dsn_from_environment_not_argv(dsn: str) -> None:
    schema = f"ci_obs_w01a_cli_{os.getpid()}"
    try:
        result = _run_cli(dsn=dsn, args=["migrate", "--schema", schema])
        assert result.returncode == 0, result.stderr
        assert "applied migrations: 1" in result.stdout
        again = _run_cli(dsn=dsn, args=["migrate", "--schema", schema])
        assert again.returncode == 0, again.stderr
        assert "up to date" in again.stdout
        assert "postgresql://" not in result.stderr + again.stderr
        # The --dsn argument does not exist anymore: argv cannot carry a secret,
        # and the parser never echoes argument values back.
        rejected = _run_cli(dsn=dsn, args=["migrate", "--dsn", dsn, "--schema", schema])
        assert rejected.returncode == 2
        assert "invalid arguments" in rejected.stderr
        assert dsn not in rejected.stderr
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_cli_requires_dsn_environment() -> None:
    result = _run_cli(dsn=None, args=["migrate"])
    assert result.returncode == 2
    assert "CI_OBSERVABILITY_DSN is not set" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_failures_are_type_only_without_secrets(dsn: str) -> None:
    secret = "s3cret-password"
    info = conninfo_to_dict(dsn)
    malformed = f"postgresql://ci_observability:{secret}@{info['host']}:{info['port']}/nope?connect_timeout=2"
    result = _run_cli(dsn=malformed, args=["migrate"])
    assert result.returncode == 1
    assert secret not in result.stderr
    assert "postgresql://" not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_serve_process_cmdline_and_logs_have_no_dsn(dsn: str, seeded: str) -> None:
    secret = "s3cret-password"
    info = conninfo_to_dict(dsn)
    env_dsn = f"postgresql://ci_observability:{secret}@{info['host']}:{info['port']}/{info['dbname']}"
    port = free_port()
    env = dict(os.environ)
    env["CI_OBSERVABILITY_DSN"] = env_dsn
    process = subprocess.Popen(
        [sys.executable, "-m", "monitoring.ci_observability.api", "serve", "--schema", seeded, "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_for_health(base_url=base_url)
        # argv is world-readable through /proc: the secret must not be there.
        cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().decode("utf-8", errors="replace")
        assert secret not in cmdline
        assert "postgresql" not in cmdline
        status, body = http_get(url=f"{base_url}/api/v1/executions?run_id=900")
        assert status == 200
        assert len(body["executions"]) == 4
        status, body = http_get(url=f"{base_url}/api/v1/executions", method="POST")
        assert status == 405
    finally:
        process.terminate()
        process.wait(timeout=15)
        stderr = process.stderr.read() if process.stderr else ""
        assert secret not in stderr
        assert "postgresql://" not in stderr


def test_cli_serves_on_default_port_9185(dsn: str, seeded: str) -> None:
    if not port_is_free(port=9185):
        pytest.skip("port 9185 is already occupied in this environment")
    env = dict(os.environ)
    env["CI_OBSERVABILITY_DSN"] = dsn
    process = subprocess.Popen(
        [sys.executable, "-m", "monitoring.ci_observability.api", "serve", "--schema", seeded],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = "http://127.0.0.1:9185"
        wait_for_health(base_url=base_url)
        status, body = http_get(url=f"{base_url}/api/v1/runs")
        assert status == 200
        assert len(body["runs"]) == 2
    finally:
        process.terminate()
        process.wait(timeout=15)

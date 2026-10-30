"""Read-only HTTP API for the CI observability ledger (W01a).

Serves bounded ledger queries on a loopback port (default 9185) for
Grafana and local diagnostics: GET only, no write endpoint, no outbound
request, parameterized SQL. Invalid input is a 400 with a fixed message
that echoes no request value, server-side failures are an opaque 500, and
diagnostics log exception types only because exception text can carry SQL
and connection strings. Accepted connections and concurrent requests are
both capped, and every socket and database connection carries a finite
timeout.

Run from the repository root with the DSN in CI_OBSERVABILITY_DSN, never
in argv (argv is readable through /proc and can carry a password):

    CI_OBSERVABILITY_DSN=postgresql://ci_observability_reader@host/db \\
        python -m monitoring.ci_observability.api serve

Migrations use the ingest role; the API uses the SELECT-only reader role.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Sequence
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import ledger
from .ledger import (
    DEFAULT_EVENT_PAGE_BYTES,
    MAX_LIST_ROWS,
    MAX_OFFSET,
    EventCursor,
    ExecutionRow,
    JSONValue,
    Ledger,
    LedgerError,
    RunSummary,
    StoredEvent,
    ValidationError,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9185
MAX_REQUEST_PATH_BYTES = 2048
MAX_QUERY_FIELDS = 16
# The server-chosen byte cap of one cursor page: the page is cut on the
# real serialized bytes, data and metadata both, and always contains at
# least one event so a walk advances.
MAX_EVENT_PAGE_BYTES = DEFAULT_EVENT_PAGE_BYTES
# Fetch planning size for the offset (legacy) streaming path: memory is
# bounded by this chunk, not by the requested window.
LEGACY_STREAM_CHUNK_BYTES = 4 * 1_048_576
_CURSOR_VERSION = 1
_MAX_CURSOR_CHARS = 512


def _worst_case_page_suffix_bytes() -> int:
    """The largest suffix an events page response can carry, derived from
    the legal maxima of every field in it: a cursor token over a
    128-character journal id, a 30-character timestamp, a 19-digit
    sequence (the bigint maximum), and the 64-character context digest,
    plus three-digit count and limit. The actual suffix never exceeds
    this, so reserving it keeps the advertised byte cap honest."""

    worst_payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "r": "2026-10-28T12:00:00.123456789Z",
            "j": "j" * 128,
            "s": 9_223_372_036_854_775_807,
            "x": "x" * 64,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    worst_token = len(json.dumps(base64.urlsafe_b64encode(worst_payload).decode("ascii")).encode("utf-8"))
    return (
        len('],"count":')
        + 3
        + len(',"next_cursor":')
        + worst_token
        + len(',"limit":')
        + 3
        + len(',"offset":0,"byte_cap":')
        + len(str(MAX_EVENT_PAGE_BYTES))
        + 1
    )


# Reserved bytes for the JSON fields that follow the events array when a
# cursor page is cut on its real serialized size.
_CURSOR_SUFFIX_SLACK_BYTES = _worst_case_page_suffix_bytes()


class _BadRequest(Exception):
    """A request the API refuses before running any query. The message is fixed text."""


def _require_loopback(*, host: str) -> None:
    if host == "localhost":
        return
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("read API host must be a loopback address") from None
    if not parsed.is_loopback:
        raise ValueError("read API host must be a loopback address")


class ReadAPIServer(ThreadingHTTPServer):
    """Read-only HTTP server on a loopback port with bounded connections.

    max_connections caps accepted connections before a handler thread is
    spawned; socket_timeout_seconds is the finite header, body, and write
    timeout that expires idle connections; max_concurrent_requests also
    bounds the database connections, one per request.
    """

    def __init__(
        self,
        *,
        dsn: str,
        schema: str = ledger.DEFAULT_SCHEMA,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        max_concurrent_requests: int = 8,
        request_wait_seconds: float = 10.0,
        max_connections: int = 16,
        socket_timeout_seconds: float = 15.0,
    ) -> None:
        _require_loopback(host=host)
        if max_concurrent_requests < 1 or request_wait_seconds <= 0:
            raise ValueError("max_concurrent_requests must be >= 1 and request_wait_seconds positive")
        if max_connections < 1 or socket_timeout_seconds <= 0:
            raise ValueError("max_connections must be >= 1 and socket_timeout_seconds positive")
        self.dsn = dsn
        self.schema = schema
        self.request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self.request_wait_seconds = request_wait_seconds
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        self.socket_timeout_seconds = socket_timeout_seconds
        super().__init__((host, port), _ReadAPIHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        if not self.connection_slots.acquire(blocking=False):
            # Overloaded: refuse before any thread or database work exists.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def handle_error(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        # Type only: the socketserver default prints a full traceback with request data.
        exc = sys.exc_info()[1]
        print(f"ci-observability-api: connection failed with {type(exc).__name__}", file=sys.stderr)


class _ReadAPIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        # One finite timeout for header reads, body reads, and writes; an
        # idle keepalive connection expires and releases its slot.
        self.connection.settimeout(self.server.socket_timeout_seconds)
        self._response_started = False

    def handle_one_request(self) -> None:
        # Keepalive reuses this handler: every request starts a fresh
        # response, so a pre-response failure on a later request still gets
        # its own honest status line.
        self._response_started = False
        super().handle_one_request()

    def send_response(self, code: int, message: str | None = None) -> None:
        # The single chokepoint for the first wire write of any response:
        # once a status line is out, no failure may start another one.
        self._response_started = True
        super().send_response(code, message)

    def do_GET(self) -> None:
        if len(self.path.encode("utf-8")) > MAX_REQUEST_PATH_BYTES:
            self._send_json(status=400, body={"error": "request path too long"})
            return
        if not self.server.request_slots.acquire(timeout=self.server.request_wait_seconds):
            self._send_json(status=503, body={"error": "server busy"})
            return
        try:
            try:
                if self.path == "/metrics":
                    self._metrics()
                    return
                result = self._route()
                if result is None:
                    # The route streamed its own response (event pages).
                    return
                status, body = result
            except _BadRequest as exc:
                status, body = 400, {"error": str(exc)}
            except Exception as exc:
                # Exception types only: message text can carry SQL and
                # connection strings, and the client gets an opaque body.
                print(f"ci-observability-api: request failed with {type(exc).__name__}", file=sys.stderr)
                if self._response_started:
                    # A status line is already on the wire: a second one
                    # would corrupt that response. Close so the client sees
                    # a short response instead.
                    self.close_connection = True
                    return
                status, body = 500, {"error": "internal error"}
        finally:
            self.server.request_slots.release()
        self._send_json(status=status, body=body)

    def do_POST(self) -> None:
        self._reject_method()

    def do_PUT(self) -> None:
        self._reject_method()

    def do_PATCH(self) -> None:
        self._reject_method()

    def do_DELETE(self) -> None:
        self._reject_method()

    def do_HEAD(self) -> None:
        self._reject_method()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        if code == 501:
            self._reject_method()
            return
        self._send_json(status=code, body={"error": "invalid HTTP request"})

    def log_message(self, format: str, *args: object) -> None:
        # Suppress the default request log: request paths carry untrusted
        # input and must not be written to logs unescaped.
        del format, args

    def _reject_method(self) -> None:
        # Rejected requests close: no keepalive for refused methods.
        self.close_connection = True
        self._send_json(status=405, body={"error": "method not allowed", "allow": "GET"})

    def _send_json(self, *, status: int, body: dict[str, JSONValue]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self._send_content(status=status, content_type="application/json", payload=payload)

    def _send_content(self, *, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if status == 405:
            self.send_header("Allow", "GET")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _metrics(self) -> None:
        client = self._ledger()
        success = 1
        try:
            client.health_check()
        except Exception as error:
            print(f"ci-observability-api: metrics check failed with {type(error).__name__}", file=sys.stderr)
            success = 0
        finally:
            client.close()
        payload = (
            "# HELP ci_observability_ledger_healthcheck_success Whether the ledger health check succeeded.\n"
            "# TYPE ci_observability_ledger_healthcheck_success gauge\n"
            f"ci_observability_ledger_healthcheck_success {success}\n"
        ).encode("ascii")
        self._send_content(status=200, content_type="text/plain; version=0.0.4; charset=utf-8", payload=payload)

    def _ledger(self) -> Ledger:
        return Ledger(
            dsn=self.server.dsn,
            schema=self.server.schema,
            application_name="ci-observability-read-api",
            connect_timeout_seconds=5.0,
            statement_timeout_ms=5000,
            lock_timeout_ms=2000,
        )

    def _route(self) -> tuple[int, dict[str, JSONValue]] | None:
        parsed = urlparse(self.path)
        params = self._parse_query(raw=parsed.query)
        if len(params) > MAX_QUERY_FIELDS:
            raise _BadRequest("too many query parameters")
        if parsed.path == "/healthz":
            return self._health(params=params)
        if parsed.path == "/api/v1/runs":
            return self._runs(params=params)
        if parsed.path == "/api/v1/executions":
            return self._executions(params=params)
        if parsed.path.startswith("/api/v1/executions/"):
            remainder = parsed.path[len("/api/v1/executions/") :]
            if remainder.endswith("/events"):
                execution_id = unquote(remainder[: -len("/events")])
                return self._serve_events(execution_id=execution_id, params=params)
            if remainder.endswith("/environment"):
                execution_id = unquote(remainder[: -len("/environment")])
                return self._environments(execution_id=execution_id, params=params)
            if remainder and "/" not in remainder:
                return self._execution(execution_id=unquote(remainder), params=params)
        return 404, {"error": "not found"}

    def _parse_query(self, *, raw: str) -> dict[str, str]:
        if not raw:
            return {}
        try:
            parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise _BadRequest("malformed query string") from exc
        params: dict[str, str] = {}
        for key, values in parsed.items():
            if len(values) != 1:
                raise _BadRequest("a query parameter must appear exactly once")
            params[key] = values[0]
        return params

    def _health(self, *, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        _reject_leftovers(params=params)
        client = self._ledger()
        try:
            client.health_check()
        except Exception as exc:
            print(f"ci-observability-api: health check failed with {type(exc).__name__}", file=sys.stderr)
            return 503, {"status": "unavailable"}
        finally:
            client.close()
        return 200, {"status": "ok"}

    def _runs(self, *, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        repository_id = _query_int(params=params, name="repository_id")
        limit = _query_limit(params=params)
        offset = _query_offset(params=params)
        _reject_leftovers(params=params)
        client = self._ledger()
        try:
            runs = client.list_runs(repository_id=repository_id, limit=limit, offset=offset)
        finally:
            client.close()
        return 200, {"runs": [_run_json(run=run) for run in runs], "count": len(runs), "limit": limit, "offset": offset}

    def _executions(self, *, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        repository_id = _query_int(params=params, name="repository_id")
        run_id = _query_int(params=params, name="run_id")
        run_attempt = _query_int(params=params, name="run_attempt")
        github_job_id = _query_int(params=params, name="github_job_id")
        logical_job_key = _query_pattern(params=params, name="logical_job_key", pattern=ledger.LOGICAL_JOB_KEY_PATTERN)
        executed = _query_bool(params=params, name="executed")
        registered_after = _query_timestamp(params=params, name="registered_after")
        registered_before = _query_timestamp(params=params, name="registered_before")
        limit = _query_limit(params=params)
        offset = _query_offset(params=params)
        _reject_leftovers(params=params)
        client = self._ledger()
        try:
            rows = client.list_executions(
                repository_id=repository_id,
                run_id=run_id,
                run_attempt=run_attempt,
                logical_job_key=logical_job_key,
                github_job_id=github_job_id,
                executed=executed,
                registered_after=registered_after,
                registered_before=registered_before,
                limit=limit,
                offset=offset,
            )
        finally:
            client.close()
        return 200, {
            "executions": [_execution_json(row=row) for row in rows],
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }

    def _execution(self, *, execution_id: str, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        _reject_leftovers(params=params)
        _validate_execution_id(execution_id=execution_id)
        client = self._ledger()
        try:
            row = client.get_execution(execution_id=execution_id)
            if row is None:
                return 404, {"error": "execution not found"}
            event_count, journal_count = client.execution_counts(execution_id=execution_id)
        finally:
            client.close()
        body = _execution_json(row=row)
        body["event_count"] = event_count
        body["journal_count"] = journal_count
        return 200, body

    def _environments(self, *, execution_id: str, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]]:
        limit = _query_limit(params=params)
        offset = _query_offset(params=params)
        _reject_leftovers(params=params)
        _validate_execution_id(execution_id=execution_id)
        client = self._ledger()
        try:
            if client.get_execution(execution_id=execution_id) is None:
                return 404, {"error": "execution not found"}
            rows = client.list_environments(execution_id=execution_id, limit=limit, offset=offset)
        finally:
            client.close()
        return 200, {
            "execution_id": execution_id,
            "environments": rows,
            "count": len(rows),
            "limit": limit,
            "offset": offset,
        }

    def _serve_events(self, *, execution_id: str, params: dict[str, str]) -> tuple[int, dict[str, JSONValue]] | None:
        """Serve the events route: cursor pages or the offset window, both streamed.

        Returns None when the response was written directly (the events
        route streams); a tuple goes through the standard JSON sender."""

        journal_id = _query_pattern(params=params, name="journal_id", pattern=ledger.JOURNAL_ID_PATTERN)
        recorded_after = _query_timestamp(params=params, name="recorded_after")
        recorded_before = _query_timestamp(params=params, name="recorded_before")
        limit = _query_limit(params=params)
        cursor_token = params.pop("cursor", None)
        has_offset = "offset" in params
        if cursor_token is not None and has_offset:
            raise _BadRequest("cursor and offset cannot be combined")
        offset = _query_offset(params=params) if has_offset else 0
        _reject_leftovers(params=params)
        _validate_execution_id(execution_id=execution_id)
        client = self._ledger()
        try:
            if client.get_execution(execution_id=execution_id) is None:
                return 404, {"error": "execution not found"}
            if has_offset:
                # The legacy window: same rows as always, streamed in
                # bounded chunks, with a cursor to continue past the cap.
                self._serve_events_offset(
                    client=client,
                    execution_id=execution_id,
                    journal_id=journal_id,
                    recorded_after=recorded_after,
                    recorded_before=recorded_before,
                    limit=limit,
                    offset=offset,
                )
                return None
            after = None
            if cursor_token is not None:
                after = _decode_event_cursor(
                    token=cursor_token,
                    context=_cursor_context(
                        execution_id=execution_id,
                        journal_id=journal_id,
                        recorded_after=recorded_after,
                        recorded_before=recorded_before,
                    ),
                )
            self._serve_events_cursor(
                client=client,
                execution_id=execution_id,
                after=after,
                journal_id=journal_id,
                recorded_after=recorded_after,
                recorded_before=recorded_before,
                limit=limit,
            )
            return None
        finally:
            client.close()

    def _serve_events_cursor(
        self,
        *,
        client: Ledger,
        execution_id: str,
        after: EventCursor | None,
        journal_id: str | None,
        recorded_after: datetime | None,
        recorded_before: datetime | None,
        limit: int,
    ) -> None:
        """One byte-capped keyset page, cut on the real serialized bytes.

        The ledger plans the fetch from stored payload sizes; this layer
        counts the actual wire bytes and moves the cursor back to the last
        event that fits, so the cap is honest and no event is silently
        dropped: everything after the cut returns on the next page."""

        page = client.list_event_page(
            execution_id=execution_id,
            after=after,
            journal_id=journal_id,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
            limit=limit,
            byte_budget=MAX_EVENT_PAGE_BYTES,
        )
        prefix = ('{"execution_id":' + json.dumps(execution_id) + ',"events":[').encode("utf-8")
        fragments = [_event_fragment(event=event) for event in page.events]
        used = len(prefix) + _CURSOR_SUFFIX_SLACK_BYTES
        kept = 0
        for fragment in fragments:
            if kept and used + 1 + len(fragment) > MAX_EVENT_PAGE_BYTES:
                break
            used += (1 if kept else 0) + len(fragment)
            kept += 1
        more = kept < len(fragments) or page.next_cursor is not None
        next_cursor: EventCursor | None = None
        if more and kept:
            last = page.events[kept - 1]
            next_cursor = EventCursor(journal_id=last.journal_id, sequence=last.sequence, recorded_at=last.recorded_at)
        token = (
            "null"
            if next_cursor is None
            else json.dumps(
                _encode_event_cursor(
                    cursor=next_cursor,
                    context=_cursor_context(
                        execution_id=execution_id,
                        journal_id=journal_id,
                        recorded_after=recorded_after,
                        recorded_before=recorded_before,
                    ),
                )
            )
        )
        suffix = (
            f'],"count":{kept},"next_cursor":{token},"limit":{limit},"offset":0,"byte_cap":{MAX_EVENT_PAGE_BYTES}}}'
        ).encode()
        payload = prefix + b",".join(fragments[:kept]) + suffix
        self._send_content(status=200, content_type="application/json", payload=payload)

    def _serve_events_offset(
        self,
        *,
        client: Ledger,
        execution_id: str,
        journal_id: str | None,
        recorded_after: datetime | None,
        recorded_before: datetime | None,
        limit: int,
        offset: int,
    ) -> None:
        """The legacy offset window, streamed in fetch-planning chunks.

        Same rows and response fields as before; LEGACY_STREAM_CHUNK_BYTES
        plans how many rows each fetch round reads, so server memory is
        bounded by that budget even when the window is hundreds of MiB.
        A failure after the stream started closes the response; the
        response-started boundary guarantees no second status line."""

        start = client.event_cursor_after_offset(
            execution_id=execution_id,
            journal_id=journal_id,
            recorded_after=recorded_after,
            recorded_before=recorded_before,
            offset=offset,
        )
        self._start_chunked(content_type="application/json")
        try:
            context = _cursor_context(
                execution_id=execution_id,
                journal_id=journal_id,
                recorded_after=recorded_after,
                recorded_before=recorded_before,
            )
            prefix = ('{"execution_id":' + json.dumps(execution_id) + ',"events":[').encode()
            self._write_chunk(data=prefix)
            sent = 0
            last_event: StoredEvent | None = None
            window_has_more = False
            cursor = start.cursor
            # An exhausted start (fewer than offset rows match) skips the
            # loop entirely: the window is empty, not wrapped to the start.
            while not start.exhausted and sent < limit:
                page = client.list_event_page(
                    execution_id=execution_id,
                    after=cursor,
                    journal_id=journal_id,
                    recorded_after=recorded_after,
                    recorded_before=recorded_before,
                    limit=limit - sent,
                    byte_budget=LEGACY_STREAM_CHUNK_BYTES,
                )
                if not page.events:
                    break
                fragments = [_event_fragment(event=event) for event in page.events]
                chunk = b",".join(fragments)
                if sent:
                    chunk = b"," + chunk
                self._write_chunk(data=chunk)
                sent += len(page.events)
                last_event = page.events[-1]
                window_has_more = page.next_cursor is not None
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
            next_token = "null"
            if sent and window_has_more and last_event is not None:
                next_cursor = EventCursor(
                    journal_id=last_event.journal_id, sequence=last_event.sequence, recorded_at=last_event.recorded_at
                )
                next_token = json.dumps(_encode_event_cursor(cursor=next_cursor, context=context))
            suffix = f'],"count":{sent},"limit":{limit},"offset":{offset},"next_cursor":{next_token}}}'.encode()
            self._write_chunk(data=suffix)
            self._end_chunked()
        except Exception as exc:
            # The headers are already on the wire: no second response can
            # be written, so the stream ends and the client sees a short
            # body instead of a corrupted one.
            print(f"ci-observability-api: event stream failed with {type(exc).__name__}", file=sys.stderr)
            self.close_connection = True

    def _start_chunked(self, *, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Transfer-Encoding", "chunked")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def _write_chunk(self, *, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")

    def _end_chunked(self) -> None:
        self.wfile.write(b"0\r\n\r\n")


def _validate_execution_id(*, execution_id: str) -> None:
    if not ledger.EXECUTION_ID_PATTERN.fullmatch(execution_id):
        raise _BadRequest("execution id must be a bounded filename-safe identifier")


def _reject_leftovers(*, params: dict[str, str]) -> None:
    if params:
        raise _BadRequest("unknown query parameter")


def _query_int(*, params: dict[str, str], name: str) -> int | None:
    if name not in params:
        return None
    raw = params.pop(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"{name} must be an integer >= 1") from exc
    if value < 1:
        raise _BadRequest(f"{name} must be an integer >= 1")
    return value


def _query_limit(*, params: dict[str, str]) -> int:
    if "limit" not in params:
        return ledger.DEFAULT_LIST_ROWS
    raw = params.pop("limit")
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"limit must be an integer between 1 and {MAX_LIST_ROWS}") from exc
    if not 1 <= value <= MAX_LIST_ROWS:
        raise _BadRequest(f"limit must be an integer between 1 and {MAX_LIST_ROWS}")
    return value


def _query_offset(*, params: dict[str, str]) -> int:
    if "offset" not in params:
        return 0
    raw = params.pop("offset")
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(f"offset must be an integer between 0 and {MAX_OFFSET}") from exc
    if not 0 <= value <= MAX_OFFSET:
        raise _BadRequest(f"offset must be an integer between 0 and {MAX_OFFSET}")
    return value


def _query_bool(*, params: dict[str, str], name: str) -> bool | None:
    if name not in params:
        return None
    raw = params.pop(name)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise _BadRequest(f"{name} must be true or false")


def _query_pattern(*, params: dict[str, str], name: str, pattern: re.Pattern[str]) -> str | None:
    if name not in params:
        return None
    raw = params.pop(name)
    if not pattern.fullmatch(raw):
        raise _BadRequest(f"{name} must be a bounded identifier")
    return raw


def _query_timestamp(*, params: dict[str, str], name: str) -> datetime | None:
    if name not in params:
        return None
    raw = params.pop(name)
    try:
        return ledger.parse_utc_text(value=raw)
    except ValidationError as exc:
        raise _BadRequest(f"{name} must be a UTC ISO-8601 timestamp ending in Z") from exc


def _run_json(*, run: RunSummary) -> dict[str, JSONValue]:
    return {
        "repository_id": run.repository_id,
        "run_id": run.run_id,
        "run_attempts": list(run.run_attempts),
        "execution_count": run.execution_count,
        "registration_count": run.registration_count,
    }


def _execution_json(*, row: ExecutionRow) -> dict[str, JSONValue]:
    return {
        "execution_id": row.execution_id,
        "schema_version": row.schema_version,
        "repository_id": row.repository_id,
        "run_id": row.run_id,
        "run_attempt": row.run_attempt,
        "logical_job_key": row.logical_job_key,
        "github_job_id": row.github_job_id,
        "executed": row.executed,
        "github_run_conclusion": row.github_run_conclusion,
        "github_job_conclusion": row.github_job_conclusion,
        "registered_at": ledger.format_utc(value=row.registered_at),
    }


def _event_fragment(*, event: StoredEvent) -> bytes:
    """One event's exact response bytes, the honest unit of the page cap."""

    return json.dumps(_event_json(event=event), separators=(",", ":")).encode("utf-8")


def _cursor_context(
    *, execution_id: str, journal_id: str | None, recorded_after: datetime | None, recorded_before: datetime | None
) -> str:
    """The query shape a cursor belongs to: execution and filters, not limit."""

    parts = [
        execution_id,
        journal_id or "",
        ledger.format_utc(value=recorded_after) if recorded_after is not None else "",
        ledger.format_utc(value=recorded_before) if recorded_before is not None else "",
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _encode_event_cursor(*, cursor: EventCursor, context: str) -> str:
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "r": cursor.recorded_at, "j": cursor.journal_id, "s": cursor.sequence, "x": context},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_event_cursor(*, token: str, context: str) -> EventCursor:
    """Decode and validate one page cursor; refusals are fixed text without the token."""

    if not isinstance(token, str) or not 1 <= len(token) <= _MAX_CURSOR_CHARS:
        raise _BadRequest("cursor is malformed")
    try:
        document = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        raise _BadRequest("cursor is malformed") from None
    if not isinstance(document, dict) or set(document) != {"v", "r", "j", "s", "x"}:
        raise _BadRequest("cursor is malformed") from None
    if document["v"] != _CURSOR_VERSION:
        raise _BadRequest("cursor is malformed") from None
    try:
        cursor = EventCursor(journal_id=document["j"], sequence=document["s"], recorded_at=document["r"])
    except ValidationError:
        raise _BadRequest("cursor is malformed") from None
    if document["x"] != context:
        raise _BadRequest("cursor does not match this query") from None
    return cursor


def _event_json(*, event: StoredEvent) -> dict[str, JSONValue]:
    return {
        "execution_id": event.execution_id,
        "journal_id": event.journal_id,
        "sequence": event.sequence,
        "schema_version": event.schema_version,
        # The original delivered timestamp text, not a re-rendered form.
        "recorded_at": event.recorded_at,
        "source_at": event.source_at,
        "mono_ns": event.mono_ns,
        "invocation_id": event.invocation_id,
        "clock_id": event.clock_id,
        "clock_uncertainty_ns": event.clock_uncertainty_ns,
        "event": event.event,
        "data": event.data,
        "ingested_at": ledger.format_utc(value=event.ingested_at),
    }


def serve(*, dsn: str, schema: str = ledger.DEFAULT_SCHEMA, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ReadAPIServer(dsn=dsn, schema=schema, host=host, port=port)
    print(f"ci-observability read API listening on http://{host}:{server.port} (schema {schema})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _CLIArgumentParser(argparse.ArgumentParser):
    """Never echoes argv back: the default error output repeats the offending
    argument, which can be a DSN passed by habit."""

    def error(self, message: str) -> None:
        print("ci-observability-api: invalid arguments", file=sys.stderr)
        raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _CLIArgumentParser(
        prog="ci-observability-api",
        description="Migrate the CI observability ledger schema or serve its read-only HTTP API.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_command = commands.add_parser("migrate", help="apply pending ledger migrations")
    migrate_command.add_argument("--schema", default=ledger.DEFAULT_SCHEMA, help="target schema (default: %(default)s)")
    serve_command = commands.add_parser("serve", help="serve the read-only HTTP API on the loopback interface")
    serve_command.add_argument("--schema", default=ledger.DEFAULT_SCHEMA, help="schema to read (default: %(default)s)")
    serve_command.add_argument("--port", type=int, default=DEFAULT_PORT, help="loopback port (default: %(default)s)")
    args = parser.parse_args(argv)

    # The DSN can carry a password, and argv is readable through /proc, so
    # it is read from the environment and never accepted as an argument.
    dsn = os.environ.get("CI_OBSERVABILITY_DSN", "").strip()
    if not dsn:
        print("CI_OBSERVABILITY_DSN is not set: the DSN is required and is read from the environment", file=sys.stderr)
        return 2
    try:
        if args.command == "migrate":
            applied = ledger.apply_migrations(dsn=dsn, schema=args.schema)
            if applied:
                print(f"applied migrations: {', '.join(str(version) for version in applied)}")
            else:
                print("schema is up to date")
        else:
            serve(dsn=dsn, schema=args.schema, port=args.port)
    except LedgerError as exc:
        # Ledger errors are curated, value-free text.
        print(f"ci-observability-api: {args.command} failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Everything else is reported by type only: exception text can
        # echo the DSN or SQL.
        print(f"ci-observability-api: {args.command} failed with {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

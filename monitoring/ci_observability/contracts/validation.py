"""Strict validators for the CI observability contract, version 1.

Ingestion and transport call these validators, and the tests under
monitoring/tests/observability call the same code, so the contract is
enforced once instead of being re-implemented by every consumer.

Diagnostics are constant-safe: fixed codes plus file and record ordinals
or schema-owned paths, never producer-controlled strings. File access
never follows symlinks, never leaves the bundle, and never parses a file
before every file-level check has passed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import stat
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import jsonschema
from referencing import Registry, Resource

CONTRACTS_V1_DIR = Path(__file__).resolve().parent / "v1"
MANIFEST_NAME = "bundle-manifest.json"

MANIFEST_SCHEMA_ID = "https://ci-observability.osac-project.org/contracts/v1/bundle-manifest.schema.json"
JOURNAL_SCHEMA_ID = "https://ci-observability.osac-project.org/contracts/v1/journal-record.schema.json"

# Boundary limits. Exceeding one is a contract violation, never a default
# that silently truncates input. The per-file limit must stay well above
# the largest real native report (a Fulfillment integration Ginkgo report
# measures about 15.8 MiB).
MAX_FILES = 128
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_JOURNAL_RECORDS = 200_000
MAX_TOTAL_RECORDS = 2_000_000
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_DIRECTORY_ENTRIES = 4096
MAX_DIRECTORY_DEPTH = 16
_READ_CHUNK_BYTES = 1 << 20

_FIXTURE_PHASES = ("setup", "teardown")
_INTERVAL_STARTS = {"test_phase_started", "fixture_started", "operation_started"}
_INTERVAL_FINISHES = {"test_phase_finished", "fixture_finished", "operation_finished"}
_FAILURE_OUTCOMES = ("failed", "error", "timeout")


class ContractError(ValueError):
    """A contract violation.

    Messages carry fixed codes, file and record ordinals, counts, and
    schema-owned paths only. They never echo producer-controlled strings,
    so a rejected secret cannot leak through an error message.
    """


def _is_real_datetime(instance: object) -> bool:
    """Calendar validation for date-time strings, beyond their shape."""
    if not isinstance(instance, str):
        return True
    try:
        datetime.fromisoformat(instance.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


_DATETIME_CHECKS = jsonschema.FormatChecker()
_DATETIME_CHECKS.checks("date-time", raises=ValueError)(_is_real_datetime)


def _build_validators() -> dict[str, jsonschema.Draft202012Validator]:
    contents: dict[str, object] = {}
    resources: list[tuple[str, Resource]] = []
    for path in sorted(CONTRACTS_V1_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schema_id = cast(str, schema["$id"])
        contents[schema_id] = schema
        resources.append((schema_id, Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    return {
        schema_id: jsonschema.Draft202012Validator(
            contents[schema_id], registry=registry, format_checker=_DATETIME_CHECKS
        )
        for schema_id in (MANIFEST_SCHEMA_ID, JOURNAL_SCHEMA_ID)
    }


_VALIDATORS = _build_validators()
MANIFEST_VALIDATOR = _VALIDATORS[MANIFEST_SCHEMA_ID]
JOURNAL_VALIDATOR = _VALIDATORS[JOURNAL_SCHEMA_ID]


def validate_manifest(*, manifest: object) -> None:
    """Validate one bundle manifest against the v1 contract.

    Raises ContractError listing every violation. The schema is checked
    first; the source-role and campaign rules run only on a schema-valid
    manifest.
    """
    document = _as_object(manifest, "manifest")
    errors = list(MANIFEST_VALIDATOR.iter_errors(document))
    if errors:
        raise ContractError("\n".join(f"manifest: schema: {message}" for message in _safe_messages(errors)))
    _check_source_roles(document)
    _check_campaign(document)


def validate_record(*, record: object) -> None:
    """Validate one journal record against the v1 schema."""
    document = _as_object(record, "record")
    errors = list(JOURNAL_VALIDATOR.iter_errors(document))
    if errors:
        raise ContractError("\n".join(f"record: schema: {message}" for message in _safe_messages(errors)))


def validate_bundle(*, bundle_dir: Path) -> None:
    """Validate one bundle directory against the v1 contract.

    Order matters: the manifest schema, then the declared bounds, then
    every file-level check (open without following symlinks, regular file,
    streaming checksum, size, record count), then the bounded directory
    walk. Only when all of those pass is any journal record parsed. Raises
    ContractError listing the violations found in the phase that failed.
    """
    manifest = _load_manifest(bundle_dir)
    validate_manifest(manifest=manifest)

    problems = _check_declared_bounds(manifest)
    if problems:
        raise ContractError("\n".join(problems))
    problems += _check_listed_files(bundle_dir, manifest)
    problems += _check_directory(bundle_dir, manifest)
    if problems:
        raise ContractError("\n".join(problems))

    state = _BundleState()
    problems = []
    total_records = [0]
    for ordinal, entry in enumerate(cast(list[dict[str, object]], manifest["files"]), start=1):
        if entry["kind"] == "journal":
            _check_journal(bundle_dir, entry, ordinal, manifest, state, problems, total_records)
    _check_references(state, problems)
    problems += _check_identity_echo(manifest, state)
    problems += _check_complete_bundle(manifest, state)
    if problems:
        raise ContractError("\n".join(problems))


def _open_bundle_file(bundle_dir: Path, relative: str, ordinal: int) -> int:
    """Open one bundle file through its path components, following no links.

    Each component is opened with O_NOFOLLOW against the parent's directory
    descriptor, so a symlink anywhere in the path, a symlinked bundle root,
    a FIFO, or a device is rejected before the file is read.
    """
    try:
        directory = os.open(bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ContractError("bundle-root-not-openable") from None
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise ContractError(f"file {ordinal}: {_open_error_code(error)}") from None
    finally:
        os.close(directory)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ContractError(f"file {ordinal}: file-not-regular")
    if metadata.st_nlink != 1:
        os.close(descriptor)
        raise ContractError(f"file {ordinal}: file-hardlink")
    if metadata.st_size > (MAX_MANIFEST_BYTES if relative == MANIFEST_NAME else MAX_FILE_BYTES):
        os.close(descriptor)
        raise ContractError(f"file {ordinal}: file-oversize")
    return descriptor


def _open_error_code(error: OSError) -> str:
    if error.errno == errno.ENOENT:
        return "file-missing"
    if error.errno == errno.ELOOP:
        return "file-symlink"
    if error.errno == errno.ENOTDIR:
        return "path-not-directory"
    return "file-not-openable"


def _scan_file(descriptor: int, ordinal: int, *, journal: bool) -> tuple[str, int, int]:
    """Stream one open file: digest, exact byte size, non-blank line count.

    Reads in chunks, caps total bytes and journal line length while
    reading, and verifies against fstat afterwards so a file that grew
    during the scan is rejected rather than silently mis-hashed.
    """
    digest = hashlib.sha256()
    total = 0
    non_blank = 0
    buffer = b""
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise ContractError(f"file {ordinal}: file-oversize")
        digest.update(chunk)
        if not journal:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if len(line) > MAX_JSONL_LINE_BYTES:
                raise ContractError(f"file {ordinal}: line-oversize")
            if line.strip():
                non_blank += 1
        if len(buffer) > MAX_JSONL_LINE_BYTES:
            raise ContractError(f"file {ordinal}: line-oversize")
    if buffer.strip():
        non_blank += 1
    if os.fstat(descriptor).st_size != total:
        raise ContractError(f"file {ordinal}: file-changed-during-read")
    return digest.hexdigest(), total, non_blank


def _load_journal_records(
    *, descriptor: int, ordinal: int, state_total: list[int], entry: Mapping[str, object]
) -> list[object]:
    """Stream one open journal into parsed records, enforcing every cap.

    Blank lines are violations, not silently dropped lines. Duplicate JSON
    keys and non-finite numbers are rejected rather than normalized.
    """
    records: list[object] = []
    buffer = b""
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise ContractError(f"file {ordinal}: file-oversize")
        digest.update(chunk)
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            records = _accept_line(line, ordinal, records, state_total)
        if len(buffer) > MAX_JSONL_LINE_BYTES:
            raise ContractError(f"file {ordinal}: line-oversize")
    if buffer.strip():
        records = _accept_line(buffer, ordinal, records, state_total)
    elif buffer:
        raise ContractError(f"file {ordinal}: blank-line")
    if os.fstat(descriptor).st_size != size or size != entry["size_bytes"]:
        raise ContractError(f"file {ordinal}: size-mismatch")
    if digest.hexdigest() != entry["sha256"]:
        raise ContractError(f"file {ordinal}: checksum-mismatch")
    if len(records) != entry["record_count"]:
        raise ContractError(f"file {ordinal}: record-count-mismatch")
    return records


def _accept_line(line: bytes, ordinal: int, records: list[object], state_total: list[int]) -> list[object]:
    if not line.strip():
        raise ContractError(f"file {ordinal}: blank-line")
    if len(line) > MAX_JSONL_LINE_BYTES:
        raise ContractError(f"file {ordinal}: line-oversize")
    if len(records) >= MAX_JOURNAL_RECORDS:
        raise ContractError(f"file {ordinal}: record-limit")
    state_total[0] += 1
    if state_total[0] > MAX_TOTAL_RECORDS:
        raise ContractError(f"file {ordinal}: total-record-limit")
    records.append(_loads_strict(line, f"file {ordinal} record {len(records) + 1}"))
    return records


def _read_all_capped(descriptor: int, limit: int, code: str) -> bytes:
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ContractError(code)
        parts.append(chunk)
    return b"".join(parts)


def _reject_constant(name: str) -> object:
    raise ValueError(name)


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate-key")
        result[key] = value
    return result


def _loads_strict(raw: bytes, scope: str) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError(f"{scope}: not-utf8") from None
    try:
        return json.loads(text, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise ContractError(f"{scope}: malformed-json") from None


def _load_manifest(bundle_dir: Path) -> Mapping[str, object]:
    descriptor = _open_bundle_file(bundle_dir, MANIFEST_NAME, 0)
    try:
        raw = _read_all_capped(descriptor, MAX_MANIFEST_BYTES, "manifest-oversize")
    except OSError:
        raise ContractError("manifest-not-readable") from None
    finally:
        os.close(descriptor)
    return _as_object(_loads_strict(raw, "manifest"), "manifest")


def _walk_bundle(directory_fd: int, prefix: str, depth: int, counter: list[int], problems: list[str]) -> list[str]:
    """Collect the real files under one directory descriptor, bounded."""
    files: list[str] = []
    if depth >= MAX_DIRECTORY_DEPTH:
        problems.append("directory-depth-exceeded")
        return files
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                counter[0] += 1
                if counter[0] > MAX_DIRECTORY_ENTRIES:
                    problems.append("directory-entries-oversize")
                    return files
                if entry.is_symlink():
                    problems.append("symlink-in-bundle")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    subdirectory = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                    )
                    try:
                        files += _walk_bundle(subdirectory, prefix + entry.name + "/", depth + 1, counter, problems)
                    finally:
                        os.close(subdirectory)
                elif entry.is_file(follow_symlinks=False):
                    files.append(prefix + entry.name)
                else:
                    problems.append("special-file-in-bundle")
    except OSError:
        problems.append("directory-not-openable")
    return files


def _journal_entries(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    entries = cast(list[dict[str, object]], manifest["files"])
    return [entry for entry in entries if entry["kind"] == "journal"]


def _check_declared_bounds(manifest: Mapping[str, object]) -> list[str]:
    problems: list[str] = []
    entries = cast(list[dict[str, object]], manifest["files"])
    if len(entries) > MAX_FILES:
        problems.append(f"declared-file-limit: {len(entries)}")
    total = 0
    for entry in entries:
        declared = cast(int, entry["size_bytes"])
        if declared > MAX_FILE_BYTES:
            problems.append("declared-file-oversize")
        total += declared
        if entry["kind"] == "journal" and cast(int, entry["record_count"]) > MAX_JOURNAL_RECORDS:
            problems.append("declared-record-limit")
    if total > MAX_TOTAL_BYTES:
        problems.append("declared-total-bytes-oversize")
    return problems


def _check_listed_files(bundle_dir: Path, manifest: Mapping[str, object]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for ordinal, entry in enumerate(cast(list[dict[str, object]], manifest["files"]), start=1):
        relative = cast(str, entry["path"])
        canonical = _canonical(relative)
        if canonical is None:
            problems.append(f"file {ordinal}: path-not-canonical")
            continue
        if canonical in seen:
            problems.append(f"file {ordinal}: path-duplicate")
        seen.add(canonical)
        if not _stays_inside(bundle_dir, bundle_dir / relative):
            problems.append(f"file {ordinal}: path-escapes-bundle")
            continue
        try:
            descriptor = _open_bundle_file(bundle_dir, relative, ordinal)
        except ContractError as error:
            problems.append(str(error))
            continue
        except OSError:
            problems.append(f"file {ordinal}: file-not-readable")
            continue
        try:
            digest, size, non_blank = _scan_file(descriptor, ordinal, journal=entry["kind"] == "journal")
        except ContractError as error:
            problems.append(str(error))
            continue
        except OSError:
            problems.append(f"file {ordinal}: file-not-readable")
            continue
        finally:
            os.close(descriptor)
        if size != entry["size_bytes"]:
            problems.append(f"file {ordinal}: size-mismatch")
        if digest != entry["sha256"]:
            problems.append(f"file {ordinal}: checksum-mismatch")
        if entry["kind"] == "journal" and non_blank != entry["record_count"]:
            problems.append(f"file {ordinal}: record-count-mismatch")
    return problems


def _check_directory(bundle_dir: Path, manifest: Mapping[str, object]) -> list[str]:
    problems: list[str] = []
    try:
        root = os.open(bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise ContractError("bundle-root-not-openable") from None
    try:
        found = _walk_bundle(root, "", 0, [0], problems)
    finally:
        os.close(root)
    listed = {_canonical(cast(str, entry["path"])) for entry in cast(list[dict[str, object]], manifest["files"])}
    listed.discard(None)
    unlisted = [path for path in found if path != MANIFEST_NAME and path not in listed]
    if unlisted:
        problems.append(f"unlisted-file: {len(unlisted)}")
    return problems


def _canonical(relative: str) -> str | None:
    """The canonical form of a bundle path, or None when it has none."""
    if relative != posixpath.normpath(relative):
        return None
    if relative.startswith("/") or "\\" in relative:
        return None
    return relative


def _stays_inside(bundle_dir: Path, path: Path) -> bool:
    """Never open a path that resolves outside the bundle, even to checksum it."""
    try:
        return path.resolve().is_relative_to(bundle_dir.resolve())
    except (OSError, RuntimeError):
        return False


class _BundleState:
    """Facts gathered from every journal, checked across journals."""

    def __init__(self) -> None:
        self.execution_starts: list[Mapping[str, object]] = []
        self.event_names: list[str] = []
        # invocation_id -> invocation_started data
        self.invocations_started: dict[str, Mapping[str, object]] = {}
        # invocation_id -> set of invocation_finished seen
        self.invocations_finished: set[str] = set()
        # every invocation_id used on any record
        self.invocations_used: set[str] = set()
        # (invocation, test_id, attempt_index) -> worker_id of the first phase start
        self.attempts: dict[tuple[str, str, int | None], str | None] = {}
        self.phase_starts: set[tuple[str, str, int, str]] = set()
        self.phase_finishes: set[tuple[str, str, int, str]] = set()
        # (invocation, test_id, attempt_index) with a native attempt result
        self.native_attempts: set[tuple[str, str, int | None]] = set()
        # (invocation, test_id) -> declared aggregate attempt_count
        self.aggregate_counts: dict[tuple[str, str], int | None] = {}
        # fixture_instance_id -> identity fields stated so far
        self.fixture_identities: dict[tuple[str, str], dict[str, object]] = {}
        # (invocation, fixture_instance_id, phase) -> sequence of the start
        self.fixture_starts: dict[tuple[str, str, str], int] = {}
        # fixture_instance_id -> phases with a finish record
        self.fixture_finishes: dict[tuple[str, str], set[str]] = {}
        # fixture_instance_id -> use edges (where, invocation, test ref, worker)
        self.use_edges: dict[tuple[str, str], list[tuple[str, str, tuple[str, int], str]]] = {}
        # (invocation, operation_id) -> (sequence, resource, parent)
        self.operation_starts: dict[tuple[str, str], tuple[int, object, object]] = {}
        # (where, invocation, operation_id) triples for progress resolution
        self.progress_references: list[tuple[str, str, str]] = []
        # (where, invocation, operation_id, resource) for the finish echo check
        self.operation_finishes: list[tuple[str, str, str, object]] = []
        # (where, invocation, test ref) pairs whose test link must resolve
        self.operation_test_links: list[tuple[str, str, tuple[str, str, int]]] = []
        # invocation -> (outcome, collected_count, deselected_count) from collection_finished
        self.listed_counts: dict[str, tuple[str, int | None, int | None]] = {}
        # invocation -> total test_collected records
        self.collected_total: dict[str, int] = {}
        # invocation -> deselected test_collected records
        self.deselected_total: dict[str, int] = {}
        # invocation -> selected test ids
        self.selected: dict[str, set[str]] = {}
        # source -> terminal (stopped) collection_state seen
        self.source_states: dict[tuple[str, str], bool] = {}
        self.workers_started: set[tuple[str, str]] = set()
        self.workers_stopped: set[tuple[str, str]] = set()
        self.dangling: list[str] = []


def _check_journal(
    bundle_dir: Path,
    entry: Mapping[str, object],
    ordinal: int,
    manifest: Mapping[str, object],
    state: _BundleState,
    problems: list[str],
    total_records: list[int],
) -> None:
    relative = cast(str, entry["path"])
    descriptor = _open_bundle_file(bundle_dir, relative, ordinal)
    try:
        records = _load_journal_records(descriptor=descriptor, ordinal=ordinal, state_total=total_records, entry=entry)
    except OSError:
        raise ContractError(f"file {ordinal}: file-not-readable") from None
    finally:
        os.close(descriptor)
    expected_journal_id = Path(relative).stem
    execution = cast(Mapping[str, object], manifest["execution"])
    execution_id = cast(object, execution["execution_id"])
    last_mono: int | None = None
    clock_id: str | None = None
    open_intervals: dict[tuple[str, object], tuple[int, str]] = {}

    for position, record in enumerate(records, start=1):
        where = f"journal {ordinal} record {position}"
        schema_errors = list(JOURNAL_VALIDATOR.iter_errors(record))
        if schema_errors:
            raise ContractError("\n".join(f"{where}: schema: {message}" for message in _safe_messages(schema_errors)))
        document = cast(Mapping[str, object], record)
        sequence = document.get("sequence")
        if sequence != position:
            problems.append(f"{where}: sequence-not-contiguous")
        if document.get("execution_id") != execution_id:
            problems.append(f"{where}: cross-execution-reference")
        if document.get("journal_id") != expected_journal_id:
            problems.append(f"{where}: journal-id-mismatch")

        mono = document.get("mono_ns")
        if isinstance(mono, int):
            if last_mono is not None and mono < last_mono:
                problems.append(f"{where}: monotonic-backwards")
            last_mono = mono
        record_clock = document.get("clock_id")
        if isinstance(record_clock, str):
            if clock_id is not None and record_clock != clock_id:
                problems.append(f"{where}: clock-id-changed")
            clock_id = record_clock

        event = cast(str, document["event"])
        invocation = cast(str, document.get("invocation_id", ""))
        if invocation:
            state.invocations_used.add(invocation)
        data = cast(Mapping[str, object], document.get("data", {}))
        state.event_names.append(event)
        _gather_semantics(event, invocation, data, sequence, where, state, open_intervals, problems)

    for _key, (start_sequence, label) in open_intervals.items():
        state.dangling.append(f"journal {ordinal} record {start_sequence}: dangling-{label}")


def _gather_semantics(
    event: str,
    invocation: str,
    data: Mapping[str, object],
    sequence: object,
    where: str,
    state: _BundleState,
    open_intervals: dict[tuple[str, object], tuple[int, str]],
    problems: list[str],
) -> None:
    seq = cast(int, sequence)

    if event == "execution_started":
        state.execution_starts.append(data)
    if event == "invocation_started":
        invocation_id = cast(str, data["invocation_id"])
        if invocation and invocation != invocation_id:
            problems.append(f"{where}: invocation-id-echo-mismatch")
        if invocation_id in state.invocations_started:
            problems.append(f"{where}: duplicate-invocation-start")
        state.invocations_started[invocation_id] = data
        if data.get("framework") == "mixed":
            problems.append(f"{where}: invocation-framework-mixed")
    if event == "invocation_finished":
        invocation_id = cast(str, data["invocation_id"])
        if invocation and invocation != invocation_id:
            problems.append(f"{where}: invocation-id-echo-mismatch")
        if invocation_id in state.invocations_finished:
            problems.append(f"{where}: duplicate-invocation-finish")
        state.invocations_finished.add(invocation_id)
    if event == "worker_started":
        state.workers_started.add((invocation, cast(str, data["worker_id"])))
    if event == "worker_stopped":
        state.workers_stopped.add((invocation, cast(str, data["worker_id"])))

    if event == "test_phase_started":
        key = (invocation, cast(str, data["test_id"]), cast(int, data["attempt_index"]))
        worker = cast(str, data["worker_id"])
        known = state.attempts.get(key)
        if known is not None and known != worker:
            problems.append(f"{where}: attempt-worker-mismatch")
        else:
            state.attempts[key] = worker

    if event in ("test_phase_started", "test_phase_finished"):
        phase_key = (invocation, cast(str, data["test_id"]), cast(int, data["attempt_index"]), cast(str, data["phase"]))
        seen = state.phase_starts if event == "test_phase_started" else state.phase_finishes
        if phase_key in seen:
            problems.append(f"{where}: duplicate-test-phase")
        seen.add(phase_key)
        if (invocation, phase_key[1], phase_key[2]) in state.native_attempts:
            problems.append(f"{where}: duplicate-result-representation")
        if event == "test_phase_finished" and state.attempts.get(phase_key[:3]) != data["worker_id"]:
            problems.append(f"{where}: attempt-worker-mismatch")

    if event == "test_result":
        test_id = cast(str, data["test_id"])
        if data["result_scope"] == "attempt":
            attempt_index = data["attempt_index"]
            key = (invocation, test_id, cast(int | None, attempt_index))
            if key in state.attempts:
                problems.append(f"{where}: duplicate-result-representation")
            state.native_attempts.add(key)
            state.attempts.setdefault(key, cast(str | None, data.get("worker_id")))
        else:
            key = (invocation, test_id)
            if key in state.aggregate_counts:
                problems.append(f"{where}: duplicate-aggregate-result")
            state.aggregate_counts[key] = cast(object, data.get("attempt_count"))

    if event == "test_collected":
        state.collected_total[invocation] = state.collected_total.get(invocation, 0) + 1
        if cast(bool, data["deselected"]):
            state.deselected_total[invocation] = state.deselected_total.get(invocation, 0) + 1
        else:
            state.selected.setdefault(invocation, set()).add(cast(str, data["test_id"]))
    if event == "collection_finished":
        counts = (
            cast(str, data["outcome"]),
            cast(object, data.get("collected_count")),
            cast(object, data.get("deselected_count")),
        )
        if invocation in state.listed_counts:
            problems.append(f"{where}: duplicate-collection-finished")
        state.listed_counts[invocation] = counts

    if event == "collection_state":
        source = cast(str, data["source"])
        state.source_states[(invocation, source)] = data["status"] in ("stopped", "unavailable")

    if event in _INTERVAL_STARTS or event in _INTERVAL_FINISHES:
        label, key = _interval_key(event, data)
        scoped = (invocation, key)
        if event in _INTERVAL_STARTS:
            if scoped in open_intervals:
                problems.append(f"{where}: duplicate-start")
            else:
                open_intervals[scoped] = (seq, label)
            if event == "fixture_started":
                _check_fixture_identity(event, invocation, data, where, state, problems)
                state.fixture_starts[(invocation, cast(str, data["fixture_instance_id"]), cast(str, data["phase"]))] = (
                    seq
                )
            if event == "operation_started":
                operation_key = (invocation, cast(str, data["operation_id"]))
                if operation_key in state.operation_starts:
                    problems.append(f"{where}: duplicate-operation-start")
                resource = data.get("resource")
                parent = data.get("parent_operation_id")
                state.operation_starts[operation_key] = (seq, resource, parent)
        if event in _INTERVAL_FINISHES:
            if scoped not in open_intervals:
                problems.append(f"{where}: finish-without-start")
            elif open_intervals[scoped][0] >= seq:
                problems.append(f"{where}: finish-before-start")
            else:
                del open_intervals[scoped]
            if event == "fixture_finished":
                _check_fixture_identity(event, invocation, data, where, state, problems)
                instance = cast(str, data["fixture_instance_id"])
                state.fixture_finishes.setdefault((invocation, instance), set()).add(cast(str, data["phase"]))
            if event == "operation_finished":
                operation_id = cast(str, data["operation_id"])
                state.operation_finishes.append((where, invocation, operation_id, data.get("resource")))

    if event == "fixture_used":
        _check_fixture_identity(event, invocation, data, where, state, problems)
        instance = cast(str, data["fixture_instance_id"])
        test_ref = (cast(str, data["test_id"]), cast(int, data["attempt_index"]))
        worker = cast(str, data["worker_id"])
        state.use_edges.setdefault((invocation, instance), []).append((where, invocation, test_ref, worker))

    if event == "operation_progress":
        state.progress_references.append((where, invocation, cast(str, data["operation_id"])))

    if event in ("operation_started", "operation_finished", "operation_progress") and isinstance(
        data.get("test"), Mapping
    ):
        test = cast(Mapping[str, object], data["test"])
        target_invocation = cast(str, test.get("invocation_id", invocation))
        link = (target_invocation, cast(str, test["test_id"]), cast(int, test["attempt_index"]))
        state.operation_test_links.append((where, invocation, link))


def _check_fixture_identity(
    event: str, invocation: str, data: Mapping[str, object], where: str, state: _BundleState, problems: list[str]
) -> None:
    """Records of one fixture instance must not contradict its identity.

    Only fields a record actually restates are compared: a use edge carries
    the worker, a finish record may omit the parameter fingerprint, and
    neither omission is a contradiction.
    """
    del event
    instance = cast(str, data["fixture_instance_id"])
    known = state.fixture_identities.setdefault((invocation, instance), {})
    for field in ("fixture_id", "scope", "worker_id", "params_fingerprint"):
        if field not in data:
            continue
        value = data[field]
        if field in known and known[field] != value:
            problems.append(f"{where}: fixture-identity-echo-mismatch")
            return
        known[field] = value


def _interval_key(event: str, data: Mapping[str, object]) -> tuple[str, object]:
    if event.startswith("test_phase"):
        return "test-phase", (data["test_id"], data["attempt_index"], data["phase"])
    if event.startswith("fixture"):
        return "fixture-phase", (data["fixture_instance_id"], data["phase"])
    return "operation", data["operation_id"]


def _check_references(state: _BundleState, problems: list[str]) -> None:
    """Resolve cross-record links after every journal has been read."""
    for invocation in sorted(state.invocations_used):
        if invocation not in state.invocations_started:
            problems.append("invocation-without-start")
    for invocation in state.invocations_finished:
        if invocation not in state.invocations_started:
            problems.append("invocation-finish-without-start")
    for (fixture_invocation, instance), edges in state.use_edges.items():
        for where, invocation, (test_id, attempt_index), worker in edges:
            attempt = (invocation, test_id, attempt_index)
            if attempt not in state.attempts:
                problems.append(f"{where}: use-edge-unknown-attempt")
            elif state.attempts[attempt] is not None and state.attempts[attempt] != worker:
                problems.append(f"{where}: use-edge-worker-mismatch")
        if (fixture_invocation, instance, "setup") not in state.fixture_starts:
            problems.append("use-edge-without-fixture-setup")

    for where, invocation, operation_id in state.progress_references:
        if (invocation, operation_id) not in state.operation_starts:
            problems.append(f"{where}: operation-progress-without-start")
    for where, invocation, operation_id, resource in state.operation_finishes:
        started = state.operation_starts.get((invocation, operation_id))
        if started is not None and resource is not None and started[1] != resource:
            problems.append(f"{where}: operation-resource-echo-mismatch")
    for where, _invocation, link in state.operation_test_links:
        if link not in state.attempts:
            problems.append(f"{where}: operation-test-link-unknown-attempt")

    _check_operation_parents(state, problems)

    attempts_by_test: dict[tuple[str, str], list[int | None]] = {}
    for invocation, test_id, index in state.attempts:
        attempts_by_test.setdefault((invocation, test_id), []).append(index)
    for test_key, attempt_count in state.aggregate_counts.items():
        if attempt_count is None or test_key not in attempts_by_test:
            continue
        indexes = attempts_by_test[test_key]
        if len(indexes) > attempt_count:
            problems.append("aggregate-attempt-count-exceeded")
        if any(index is not None and index > attempt_count for index in indexes):
            problems.append("aggregate-attempt-index-exceeded")


def _check_operation_parents(state: _BundleState, problems: list[str]) -> None:
    """Parent links must resolve within the invocation and never cycle."""
    checked: set[tuple[str, str]] = set()
    for operation_key in state.operation_starts:
        current = operation_key
        path: set[tuple[str, str]] = set()
        while current not in checked:
            if current in path:
                problems.append("operation-parent-cycle")
                break
            path.add(current)
            next_parent = state.operation_starts[current][2]
            if next_parent is None:
                break
            parent_key = (current[0], cast(str, next_parent))
            if parent_key not in state.operation_starts:
                problems.append("operation-parent-unknown")
                break
            current = parent_key
        checked.update(path)


def _check_identity_echo(manifest: Mapping[str, object], state: _BundleState) -> list[str]:
    problems: list[str] = []
    if state.event_names.count("execution_finished") > 1:
        problems.append("duplicate-execution-finish")
    if len(state.execution_starts) != 1:
        problems.append(f"execution-start-count: {len(state.execution_starts)}")
        return problems
    execution = cast(Mapping[str, object], manifest["execution"])
    started = state.execution_starts[0]
    if started.get("job") != execution.get("job"):
        problems.append("identity-echo-job-mismatch")
    if started.get("suite") != manifest.get("suite"):
        problems.append("identity-echo-suite-mismatch")
    suite = cast(Mapping[str, object], manifest["suite"])
    if suite.get("framework") == "mixed" and not state.invocations_started:
        problems.append("mixed-suite-without-invocations")
    return problems


def _check_complete_bundle(manifest: Mapping[str, object], state: _BundleState) -> list[str]:
    completeness = cast(Mapping[str, object], manifest["completeness"])
    if completeness.get("complete") is not True:
        return []
    problems: list[str] = []
    if "execution_finished" not in state.event_names:
        problems.append("complete-missing-execution-finished")
    if completeness.get("dropped_records") != 0:
        problems.append("complete-with-dropped-records")
    if completeness.get("collection_errors") != []:
        problems.append("complete-with-collection-errors")
    for entry in _journal_entries(manifest):
        if entry.get("complete") is not True:
            problems.append("complete-journal-entry-not-complete")

    for invocation in sorted(state.invocations_started):
        if invocation not in state.invocations_finished:
            problems.append("complete-invocation-without-finish")
    for _worker in sorted(state.workers_started - state.workers_stopped):
        problems.append("complete-worker-without-stop")
    for item in state.dangling:
        problems.append(f"complete-{item}")
    for _source, terminal in state.source_states.items():
        if not terminal:
            problems.append("complete-source-without-terminal-state")
            break

    instances = {(invocation, instance) for (invocation, instance, _phase) in state.fixture_starts}
    instances |= set(state.fixture_finishes)
    instances |= set(state.use_edges)
    for instance in sorted(instances):
        missing = [phase for phase in _FIXTURE_PHASES if phase not in state.fixture_finishes.get(instance, set())]
        if missing:
            problems.append("complete-fixture-missing-phase-finish")

    problems += _check_collection_consistency(state)
    return problems


def _check_collection_consistency(state: _BundleState) -> list[str]:
    """Count and membership checks, only where collection is actually known."""
    problems: list[str] = []
    for invocation, (outcome, listed_collected, listed_deselected) in state.listed_counts.items():
        if outcome != "completed":
            continue
        if listed_collected != state.collected_total.get(invocation, 0):
            problems.append("collected-count-mismatch")
        if listed_deselected != state.deselected_total.get(invocation, 0):
            problems.append("deselected-count-mismatch")
        if listed_collected and state.collected_total.get(invocation, 0) == 0:
            problems.append("completed-collection-without-records")
    for invocation, test_id, _attempt in state.attempts:
        collection = state.listed_counts.get(invocation)
        if collection is None or collection[0] != "completed":
            continue
        if test_id not in state.selected.get(invocation, set()):
            problems.append("executed-not-collected")
            break
    return problems


def _check_source_roles(manifest: Mapping[str, object]) -> None:
    environment = cast(Mapping[str, object], manifest["environment"])
    sources = cast(list[dict[str, object]], environment["sources"])
    roles = [cast(str, source["role"]) for source in sources]
    if len(set(roles)) != len(roles):
        raise ContractError("source-role-duplicate")
    for required in ("tested", "workflow"):
        if required not in roles:
            raise ContractError(f"source-role-missing-{required}")


def _check_campaign(manifest: Mapping[str, object]) -> None:
    suite = cast(Mapping[str, object], manifest["suite"])
    campaign = suite.get("campaign")
    if not isinstance(campaign, Mapping):
        return
    if cast(int, campaign["repetition"]) > cast(int, campaign["repetitions_total"]):
        raise ContractError("campaign-repetition-exceeds-total")


def _safe_messages(errors: Iterable[jsonschema.ValidationError]) -> list[str]:
    """Schema-side error text only; instance values are never echoed."""
    messages: list[str] = []
    for error in errors:
        location = "schema/" + "/".join(str(part) for part in error.absolute_schema_path)
        if error.validator == "enum":
            allowed = ", ".join(repr(value) for value in error.validator_value)
            messages.append(f"{location}: must be one of {allowed}")
        elif error.validator == "required":
            messages.append(f"{location}: {error.message}")
        else:
            messages.append(f"{location}: fails '{error.validator}' validation")
    return messages


def _as_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label}-not-an-object")
    return cast(Mapping[str, object], value)

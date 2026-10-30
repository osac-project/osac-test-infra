"""Freeze one v1 bundle into an immutable in-memory snapshot for ingest.

``validate_bundle`` re-opens files by design ("validation does not freeze
files"), so calling it and then re-reading for storage would let a producer
that can still write its directory pass validation and deliver different
bytes to the ledger: a validation-then-reopen TOCTOU. This module reads
every byte of the input exactly once, verifies the manifest against those
exact bytes, validates the records parsed from them, and returns the same
frozen bytes for storage. The ledger write path never re-opens the
producer's files, and ingest revalidates any FrozenBundle it is handed
(revalidate_frozen) by deriving everything from the pinned bytes.

Order matters: the manifest is parsed and fully validated (schema,
declared bounds, canonical paths) before any listed file is opened, so a
hostile manifest cannot make the reader open or probe files outside the
bundle. Archives are data: they are preflighted (entry count and central
directory size) before any ZipInfo is allocated, and nothing in them is
executed or fetched.
"""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import stat
import struct
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

# WHY private imports: validate_bundle() is directory-based and cannot
# freeze bytes, so the ingest drives the same frozen-contract semantics
# over its in-memory snapshot instead of re-implementing them. Version 1
# is frozen, so these entry points are stable; a future contract version
# that renames them fails loudly here instead of diverging silently.
from .contracts.validation import (
    MANIFEST_NAME,
    MAX_DIRECTORY_DEPTH,
    MAX_DIRECTORY_ENTRIES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_JOURNAL_RECORDS,
    MAX_JSONL_LINE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_BYTES,
    MAX_TOTAL_RECORDS,
    ContractError,
    _BundleState,
    _check_complete_bundle,
    _check_declared_bounds,
    _check_identity_echo,
    _check_references,
    _gather_semantics,
    _loads_strict,
    _open_bundle_file,
    validate_manifest,
    validate_record,
)

__all__ = [
    "BundleArchive",
    "BundleDirectory",
    "BundleError",
    "Delivery",
    "FrozenBundle",
    "FrozenFile",
    "ensure_private_directory",
    "freeze",
    "materialize_quarantine",
    "parse_journal_records",
    "read_bundle_archive",
    "read_bundle_directory",
    "revalidate_frozen",
]

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None

_READ_CHUNK_BYTES = 1 << 20
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# The archive is transport for at most MAX_TOTAL_BYTES of bundle content
# plus per-member zip overhead, so this bound admits every legal bundle
# and rejects anything larger before a single member is decompressed.
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + (MAX_FILES + 2) * 1024
MAX_ARCHIVE_MEMBERS = MAX_FILES + 1
# A central-directory entry is at least 46 bytes plus its name; one
# kilobyte per allowed member bounds every legitimate directory while
# keeping a forged multi-gigabyte one out of ZipFile entirely.
MAX_CENTRAL_DIRECTORY_BYTES = (MAX_ARCHIVE_MEMBERS + 1) * 1024
_EOCD_SIGNATURE = 0x06054B50
_ZIP64_EOCD_SIGNATURE = 0x06064B50
_ZIP64_LOCATOR_SIGNATURE = 0x07064B50
_EOCD_SEARCH_TAIL = 65_574  # EOCD record plus the maximum comment length


class BundleError(ValueError):
    """A delivery cannot be frozen or fails the v1 contract.

    The code is a fixed identifier. The detail, when present, is curated
    text: the contract's own diagnostics (fixed codes and ordinals only)
    or a fixed sentence from this module. Producer bytes, paths, and names
    never reach a message.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class BundleDirectory:
    """An explicit private bundle directory delivered by a trusted caller."""

    path: Path


@dataclass(frozen=True, kw_only=True)
class BundleArchive:
    """An archive received by a trusted caller.

    received_digest is the sha256 of the exact bytes the caller received,
    verified by the caller against the artifacts API where one exists; the
    reader re-verifies the archive file against it, so that digest anchors
    every byte that follows.
    """

    path: Path
    received_digest: str


Delivery: TypeAlias = BundleDirectory | BundleArchive


@dataclass(frozen=True, kw_only=True)
class FrozenFile:
    """One bundle file, as read exactly once.

    ordinal is the file's position in the manifest (the manifest itself is
    0) and is the only identifier used in diagnostics, so producer-chosen
    paths never reach an error message.
    """

    path: str
    kind: str
    sha256: str
    size_bytes: int
    data: bytes
    ordinal: int
    record_count: int | None = None
    complete: bool | None = None


@dataclass(frozen=True, kw_only=True)
class FrozenBundle:
    """A validated snapshot of one delivered bundle.

    manifest_text is the pinned manifest bytes; everything derived
    (claims, digests, counts) comes from re-parsing those bytes, and
    ingest revalidates any FrozenBundle it is handed rather than trusting
    its parsed fields (revalidate_frozen).
    """

    manifest: dict[str, JSONValue]
    manifest_text: str
    manifest_sha256: str
    files: tuple[FrozenFile, ...]
    execution_id: str
    total_records: int
    source_states: dict[str, str]

    @property
    def journal_files(self) -> tuple[FrozenFile, ...]:
        return tuple(file for file in self.files if file.kind == "journal")


def freeze(*, delivery: Delivery) -> FrozenBundle:
    """Freeze one delivery, whatever transport it arrived on."""

    if isinstance(delivery, BundleDirectory):
        return read_bundle_directory(bundle_dir=delivery.path)
    return read_bundle_archive(archive_path=delivery.path, received_digest=delivery.received_digest)


def read_bundle_directory(*, bundle_dir: Path) -> FrozenBundle:
    """Freeze a bundle directory: every file is read exactly once.

    The manifest is opened, parsed, and fully validated (schema, declared
    bounds, canonical paths) before any listed file is opened, so a
    hostile manifest cannot turn the reader into a probe for files
    outside the bundle. Each listed file is then opened through its path
    components with O_NOFOLLOW and read once into the snapshot. Nothing is
    opened twice, so a producer mutating files while the freeze runs can
    only produce a rejected bundle or the frozen content, never a mix.
    """

    try:
        manifest_descriptor = _open_bundle_file(bundle_dir, MANIFEST_NAME, 0)
    except ContractError as exc:
        raise BundleError("contract-violation", str(exc)) from None
    try:
        manifest_bytes = _read_opened(manifest_descriptor, MAX_MANIFEST_BYTES, "manifest-oversize")
    finally:
        os.close(manifest_descriptor)
    manifest = _validated_manifest(manifest_bytes)
    file_bytes = _read_listed_files(bundle_dir, manifest)
    present = _walk_bundle_directory(bundle_dir)
    return _finish_freeze(
        manifest_bytes=manifest_bytes, manifest=manifest, file_bytes=file_bytes, present_paths=present
    )


def read_bundle_archive(*, archive_path: Path, received_digest: str) -> FrozenBundle:
    """Freeze an archive after verifying it against the received digest.

    The archive file is opened without following a final-component symlink
    or blocking on a non-regular file, read once in full (bounded), hashed
    against the digest the trusted caller verified on receipt, and
    preflighted (entry count, central-directory size) before any ZipInfo
    is allocated. Members are extracted from those in-memory bytes, so a
    file swapped under the archive path after the read cannot change what
    is ingested.
    """

    if not isinstance(received_digest, str) or not _SHA256_DIGEST_RE.fullmatch(received_digest):
        raise BundleError("malformed-received-digest")
    archive_bytes = _read_archive_bytes(archive_path)
    digest = "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    if digest != received_digest:
        raise BundleError("archive-digest-mismatch")
    member_bytes = _extract_archive_members(archive_bytes)
    manifest_bytes = member_bytes.pop(MANIFEST_NAME, None)
    if manifest_bytes is None:
        raise BundleError("archive-missing-manifest")
    manifest = _validated_manifest(manifest_bytes)
    present = frozenset(member_bytes) | {MANIFEST_NAME}
    return _finish_freeze(
        manifest_bytes=manifest_bytes, manifest=manifest, file_bytes=member_bytes, present_paths=present
    )


def revalidate_frozen(*, bundle: FrozenBundle) -> FrozenBundle:
    """Re-derive a frozen bundle from its pinned bytes.

    A FrozenBundle is data crossing a trust boundary: its parsed manifest
    dict is mutable and its file tuples could be forged or swapped. This
    re-parses the pinned manifest text, re-runs the full manifest
    validation, matches each manifest entry's declared digest and size to
    a payload actually carried by the bundle, and re-runs every file and
    journal check on the matched bytes. The returned bundle is derived
    from the bytes alone; a mutated parsed manifest or a swapped file
    tuple cannot bypass validation or change what is stored.
    """

    manifest_bytes = bundle.manifest_text.encode("utf-8")
    manifest = _validated_manifest(manifest_bytes)
    file_bytes: dict[str, bytes] = {}
    for ordinal, entry in enumerate(_manifest_files(manifest), start=1):
        relative = cast(str, entry["path"])
        declared_digest = cast(str, entry["sha256"])
        declared_size = cast(int, entry["size_bytes"])
        payload = next(
            (
                file.data
                for file in bundle.files
                if len(file.data) == declared_size and hashlib.sha256(file.data).hexdigest() == declared_digest
            ),
            None,
        )
        if payload is None:
            raise BundleError("contract-violation", f"file {ordinal}: file-missing")
        file_bytes[relative] = payload
    present = frozenset(file_bytes) | {MANIFEST_NAME}
    return _finish_freeze(
        manifest_bytes=manifest_bytes, manifest=manifest, file_bytes=file_bytes, present_paths=present
    )


def parse_journal_records(*, file: FrozenFile) -> list[dict[str, JSONValue]]:
    """Re-parse one journal's frozen bytes for storage.

    The digest, size, and record count are re-verified on the exact bytes
    parsed, which is the contract's rule for any consumer that reads bytes
    again; here it guards the wiring between the frozen snapshot and the
    ledger writes. Diagnostics use the manifest ordinal.
    """

    if file.kind != "journal" or file.record_count is None:
        raise BundleError("not-a-journal")
    if len(file.data) != file.size_bytes or hashlib.sha256(file.data).hexdigest() != file.sha256:
        raise BundleError("journal-frozen-bytes-mismatch")
    records: list[dict[str, JSONValue]] = []
    for line in _iter_journal_lines(file.data):
        records.append(_parse_journal_line(line, file.ordinal, len(records)))
    if len(records) != file.record_count:
        raise BundleError("journal-record-count-mismatch")
    return records


def ensure_private_directory(*, directory: Path) -> None:
    """Create the quarantine root, or validate the existing one.

    A missing root (and any missing ancestors) is created top-down with
    mode 0700, and the parent entry that holds each directory this call
    creates is fsynced before the directory is used, so a crash cannot
    lose the creation. An existing root — or the deepest existing anchor
    a partial creation left behind — is adopted only after its parent
    entry is fsynced too, because the earlier or concurrent creator may
    have failed before flushing it. An existing root must be a directory
    (never a symlink), owned by this user, with no group or other
    permissions; existing components are never chmodded, and ancestors on
    the caller's path are only fsynced, never made private.
    """

    if directory.exists():
        descriptor = _open_private_directory(directory)
        try:
            _require_private_directory(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        _fsync_parent_entry(directory)
        return
    missing: list[str] = []
    probe = directory
    while not probe.exists():
        missing.append(probe.name)
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    if not missing:
        # A concurrent creator made the root between the exists() check
        # and the walk: adopt it exactly like the existing-root path.
        descriptor = _open_private_directory(directory)
        try:
            _require_private_directory(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        _fsync_parent_entry(directory)
        return
    # The deepest existing ancestor is opened by path (the caller's path
    # semantics, as before); every component created below it is opened
    # relatively without following links.
    try:
        descriptor = os.open(probe, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise BundleError("quarantine-not-writable") from None
    try:
        # The anchor is adopted: repair the entry that holds it before
        # anything is created inside it. Inside the cleanup scope, so a
        # failed repair closes the anchor descriptor instead of leaking it.
        _fsync_parent_entry(probe)
        for name in reversed(missing):
            try:
                os.mkdir(name, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass  # a concurrent creator made it first
            except OSError:
                raise BundleError("quarantine-not-writable") from None
            # Persist the entry that holds the new directory before
            # anything is written inside it.
            _fsync_fd(descriptor)
            try:
                next_descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError:
                raise BundleError("quarantine-not-writable") from None
            os.close(descriptor)
            descriptor = next_descriptor
        _require_private_directory(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)


def materialize_quarantine(*, bundle: FrozenBundle, directory: Path) -> None:
    """Write the frozen bytes into a fresh private quarantine directory.

    Every path is opened relative to the validated root through component
    dir_fds with O_NOFOLLOW, so a symlinked component cannot redirect a
    write outside the quarantine. Files are published by writing a private
    temporary file, fsyncing it, and linking it into place (an atomic,
    no-clobber create); the temporary name is exclusively created and
    cleaned up on every path, and a cleanup failure is never acknowledged
    as success. A target that already exists is kept only when it is a
    private regular file whose content still hashes to the frozen digest:
    the link count is not checked, because permissions live on the inode
    (a hardlink elsewhere cannot widen access) and our own publish
    protocol briefly holds a second link from its temporary name. Every
    created directory, every published file's parent, and the matching
    existing file's parent are fsynced, and any durability failure raises:
    a truncated or unverifiable quarantine is never reported as success.
    The quarantine is the durable private retention copy for unverified
    and unbound deliveries and for native reports, which stay opaque here:
    nothing in it is ever published or uploaded (W05/W13 own maintenance).
    """

    ensure_private_directory(directory=directory)
    root = _open_private_directory(directory)
    try:
        original = os.fstat(root)
        _require_private_directory(original)
        _write_quarantine_file(root, MANIFEST_NAME, 0, bundle.manifest_text.encode("utf-8"))
        for file in bundle.files:
            _write_quarantine_file(root, file.path, file.ordinal, file.data)
        _fsync_fd(root)
    finally:
        os.close(root)
    # Re-open the root by path for the final durability check: if anything
    # removed or replaced it during materialization (the original inode
    # renamed away, a fresh directory put in its place), the creation is
    # not acknowledged. Neither directory is modified or overwritten.
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise BundleError("quarantine-not-durable") from None
    try:
        final = os.fstat(descriptor)
        if (final.st_dev, final.st_ino) != (original.st_dev, original.st_ino):
            raise BundleError("quarantine-not-durable")
        _fsync_fd(descriptor)
    finally:
        os.close(descriptor)


# Manifest validation, before any listed file is opened.


def _validated_manifest(manifest_bytes: bytes) -> Mapping[str, object]:
    """Parse and fully validate the manifest: schema, declared bounds,
    canonical paths. Runs before any listed file is read, so hostile
    manifests are rejected without a single file open."""

    try:
        manifest = cast(Mapping[str, object], _loads_strict(manifest_bytes, "manifest"))
        validate_manifest(manifest=manifest)
    except ContractError as exc:
        raise BundleError("contract-violation", str(exc)) from None
    problems = _check_declared_bounds(manifest)
    seen: set[str] = set()
    for ordinal, entry in enumerate(_manifest_files(manifest), start=1):
        relative = cast(str, entry["path"])
        if _canonical(relative) is None:
            problems.append(f"file {ordinal}: path-not-canonical")
        elif relative in seen:
            problems.append(f"file {ordinal}: path-duplicate")
        seen.add(relative)
    if problems:
        raise BundleError("contract-violation", "; ".join(problems))
    return manifest


def _finish_freeze(
    *,
    manifest_bytes: bytes,
    manifest: Mapping[str, object],
    file_bytes: Mapping[str, bytes],
    present_paths: frozenset[str] | set[str],
) -> FrozenBundle:
    """File-level and journal checks on the exact frozen bytes, then the
    frozen bundle. The manifest is already fully validated."""

    problems: list[str] = []
    files = _collect_frozen_files(manifest, file_bytes, problems)
    listed = {file.path for file in files}
    unlisted = [path for path in present_paths if path != MANIFEST_NAME and path not in listed]
    if unlisted:
        problems.append(f"unlisted-file: {len(unlisted)}")
    if problems:
        raise BundleError("contract-violation", "; ".join(problems))
    source_states: dict[str, str] = {}
    total_records = _validate_journals(manifest, files, source_states)
    return FrozenBundle(
        manifest=dict(manifest),
        manifest_text=manifest_bytes.decode("utf-8"),
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        files=tuple(files),
        execution_id=cast(str, manifest["execution"]["execution_id"]),
        total_records=total_records,
        source_states=source_states,
    )


def _manifest_files(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    return cast(list[Mapping[str, object]], manifest["files"])


def _read_listed_files(bundle_dir: Path, manifest: Mapping[str, object]) -> dict[str, bytes]:
    """Read every manifest-listed file exactly once, safely and bounded."""

    file_bytes: dict[str, bytes] = {}
    total = 0
    for ordinal, entry in enumerate(_manifest_files(manifest), start=1):
        relative = cast(str, entry["path"])
        try:
            descriptor = _open_bundle_file(bundle_dir, relative, ordinal)
        except ContractError as exc:
            raise BundleError("contract-violation", str(exc)) from None
        try:
            data = _read_opened(descriptor, MAX_FILE_BYTES, f"file {ordinal}: file-oversize")
        finally:
            os.close(descriptor)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BundleError("contract-violation", f"file {ordinal}: total-bytes-oversize")
        file_bytes[relative] = data
    return file_bytes


def _collect_frozen_files(
    manifest: Mapping[str, object], file_bytes: Mapping[str, bytes], problems: list[str]
) -> list[FrozenFile]:
    """File-level contract checks on the exact frozen bytes."""

    files: list[FrozenFile] = []
    for ordinal, entry in enumerate(_manifest_files(manifest), start=1):
        relative = cast(str, entry["path"])
        data = file_bytes.get(relative)
        if data is None:
            problems.append(f"file {ordinal}: file-missing")
            continue
        if len(data) != entry["size_bytes"]:
            problems.append(f"file {ordinal}: size-mismatch")
        digest = hashlib.sha256(data).hexdigest()
        if digest != cast(str, entry["sha256"]):
            problems.append(f"file {ordinal}: checksum-mismatch")
        record_count: int | None = None
        complete: bool | None = None
        if entry["kind"] == "journal":
            record_count = cast(int, entry["record_count"])
            complete = cast(bool, entry["complete"])
        files.append(
            FrozenFile(
                path=relative,
                kind=cast(str, entry["kind"]),
                sha256=digest,
                size_bytes=len(data),
                data=data,
                ordinal=ordinal,
                record_count=record_count,
                complete=complete,
            )
        )
    return files


# Journal parsing: streaming, bounded, ordinal-only diagnostics.


def _validate_journals(manifest: Mapping[str, object], files: list[FrozenFile], source_states: dict[str, str]) -> int:
    """Validate every journal on the frozen bytes, one record at a time.

    Mirrors the contract's journal phase: per-record schema validation,
    the envelope checks (sequence contiguity, execution and journal
    identity echo, monotonic clock, single clock domain), the shared
    semantic state, then the cross-journal reference, identity-echo, and
    completeness checks. Records are streamed, never accumulated into a
    line list, so a maximal journal costs its own bytes plus one record.
    """

    by_path = {file.path: file for file in files}
    state = _BundleState()
    total_records = 0
    execution_id = manifest["execution"]["execution_id"]
    for ordinal, entry in enumerate(_manifest_files(manifest), start=1):
        if entry["kind"] != "journal":
            continue
        file = by_path[cast(str, entry["path"])]
        count = _validate_journal(file, execution_id, state, source_states)
        if count != file.record_count:
            raise BundleError("contract-violation", f"file {ordinal}: record-count-mismatch")
        total_records += count
        if total_records > MAX_TOTAL_RECORDS:
            raise BundleError("contract-violation", f"file {ordinal}: total-record-limit")
    problems: list[str] = []
    _check_references(state, problems)
    problems += _check_identity_echo(manifest, state)
    problems += _check_complete_bundle(manifest, state)
    if problems:
        raise BundleError("contract-violation", "; ".join(problems))
    return total_records


def _validate_journal(
    file: FrozenFile, execution_id: object, state: _BundleState, source_states: dict[str, str]
) -> int:
    """The contract's per-journal checks, streamed over the frozen bytes."""

    expected_journal_id = Path(file.path).stem
    last_mono: int | None = None
    clock_id: str | None = None
    open_intervals: dict[tuple[str, object], tuple[int, str]] = {}
    problems: list[str] = []
    count = 0
    for line in _iter_journal_lines(file.data):
        record = _parse_journal_line(line, file.ordinal, count)
        count += 1
        where = f"journal {file.ordinal} record {count}"
        try:
            validate_record(record=record)
        except ContractError as exc:
            raise BundleError("contract-violation", str(exc)) from None
        sequence = record.get("sequence")
        if sequence != count:
            problems.append(f"{where}: sequence-not-contiguous")
        if record.get("execution_id") != execution_id:
            problems.append(f"{where}: cross-execution-reference")
        if record.get("journal_id") != expected_journal_id:
            problems.append(f"{where}: journal-id-mismatch")
        mono = record.get("mono_ns")
        if isinstance(mono, int):
            if last_mono is not None and mono < last_mono:
                problems.append(f"{where}: monotonic-backwards")
            last_mono = mono
        record_clock = record.get("clock_id")
        if isinstance(record_clock, str):
            if clock_id is not None and record_clock != clock_id:
                problems.append(f"{where}: clock-id-changed")
            clock_id = record_clock
        event = cast(str, record["event"])
        invocation = cast(str, record.get("invocation_id", ""))
        if invocation:
            state.invocations_used.add(invocation)
        data = record.get("data", {})
        state.event_names.append(event)
        _gather_semantics(event, invocation, data, sequence, where, state, open_intervals, problems)
        if event == "collection_state":
            source_states[cast(str, data["source"])] = cast(str, data["status"])
    for _key, (start_sequence, label) in open_intervals.items():
        state.dangling.append(f"journal {file.ordinal} record {start_sequence}: dangling-{label}")
    if problems:
        raise BundleError("contract-violation", "; ".join(problems))
    return count


def _iter_journal_lines(data: bytes) -> Iterator[bytes]:
    """Yield one journal line at a time from the frozen bytes.

    Lines are produced by offset, so no full line list is ever allocated;
    a 32 MiB journal costs its own bytes plus one line at a time.
    """

    offset = 0
    while offset < len(data):
        end = data.find(b"\n", offset)
        if end == -1:
            yield data[offset:]
            return
        yield data[offset:end]
        offset = end + 1


def _parse_journal_line(line: bytes, ordinal: int, count: int) -> dict[str, JSONValue]:
    """Validate one journal line's shape and parse it strictly. count is
    the number of records already accepted from this journal, so the
    per-journal record cap holds for streaming callers too."""

    if not line.strip():
        raise BundleError("contract-violation", f"journal {ordinal}: blank-line")
    if len(line) > MAX_JSONL_LINE_BYTES:
        raise BundleError("contract-violation", f"journal {ordinal}: line-oversize")
    if count >= MAX_JOURNAL_RECORDS:
        raise BundleError("contract-violation", f"journal {ordinal}: record-limit")
    try:
        parsed = _loads_strict(line, f"journal {ordinal} record {count + 1}")
    except ContractError as exc:
        raise BundleError("contract-violation", str(exc)) from None
    if not isinstance(parsed, dict):
        raise BundleError("contract-violation", f"journal {ordinal} record {count + 1}: not-an-object")
    return cast(dict[str, JSONValue], parsed)


def _canonical(relative: str) -> str | None:
    """The canonical form of a bundle path, or None when it has none.

    Same rules as the contract, made explicit: no leading slash, no
    backslash, and no empty, dot, or double-dot segment, so a listed path
    can never walk out of the bundle through the dir_fd walk either.
    """

    if relative != posixpath.normpath(relative):
        return None
    if relative.startswith("/") or "\\" in relative:
        return None
    if any(part in ("", ".", "..") for part in relative.split("/")):
        return None
    return relative


def _read_opened(descriptor: int, cap: int, code: str) -> bytes:
    """Read one open descriptor fully, capped, rejecting growth mid-read."""

    parts: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise BundleError("contract-violation", code)
        parts.append(chunk)
    if os.fstat(descriptor).st_size != total:
        raise BundleError("contract-violation", "file-changed-during-read")
    return b"".join(parts)


def _walk_bundle_directory(bundle_dir: Path) -> set[str]:
    """Real files under the bundle root, following no links, bounded."""

    try:
        root = os.open(bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise BundleError("bundle-root-not-openable") from None
    try:
        paths: set[str] = set()
        _walk_bundle_fd(root, "", 0, [0], paths)
        return paths
    finally:
        os.close(root)


def _walk_bundle_fd(directory_fd: int, prefix: str, depth: int, counter: list[int], paths: set[str]) -> None:
    if depth >= MAX_DIRECTORY_DEPTH:
        raise BundleError("contract-violation", "directory-depth-exceeded")
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                counter[0] += 1
                if counter[0] > MAX_DIRECTORY_ENTRIES:
                    raise BundleError("contract-violation", "directory-entries-oversize")
                if entry.is_symlink():
                    raise BundleError("contract-violation", "symlink-in-bundle")
                if entry.is_dir(follow_symlinks=False):
                    subdirectory = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                    )
                    try:
                        _walk_bundle_fd(subdirectory, prefix + entry.name + "/", depth + 1, counter, paths)
                    finally:
                        os.close(subdirectory)
                elif entry.is_file(follow_symlinks=False):
                    paths.add(prefix + entry.name)
                else:
                    raise BundleError("contract-violation", "special-file-in-bundle")
    except OSError:
        raise BundleError("contract-violation", "directory-not-openable") from None


# Archives: preflight before any ZipInfo allocation.


def _read_archive_bytes(archive_path: Path) -> bytes:
    """Read the archive file once, bounded, without following links.

    The final component is opened with O_NOFOLLOW and O_NONBLOCK, and must
    be a regular file with a single link, so a symlinked spool entry or a
    FIFO cannot redirect or block the read; the received digest still pins
    the content.
    """

    try:
        descriptor = os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise BundleError("archive-not-readable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BundleError("archive-not-regular")
        if metadata.st_nlink != 1:
            raise BundleError("archive-hardlink")
        if metadata.st_size > MAX_ARCHIVE_BYTES:
            raise BundleError("archive-oversize")
        return _read_opened(descriptor, MAX_ARCHIVE_BYTES, "archive-oversize")
    finally:
        os.close(descriptor)


def _preflight_archive(archive_bytes: bytes) -> None:
    """Bound the central directory before zipfile allocates any ZipInfo.

    Parses the end-of-central-directory record (and the ZIP64 records when
    the classic fields are saturated) directly from the bytes and rejects
    archives whose entry count or central-directory size exceeds the
    bundle bounds, so a small forged directory cannot make ZipFile
    allocate gigabytes of objects first.
    """

    tail = archive_bytes[-_EOCD_SEARCH_TAIL:]
    eocd_at = tail.rfind(struct.pack("<I", _EOCD_SIGNATURE))
    if eocd_at < 0:
        raise BundleError("archive-not-readable")
    eocd = tail[eocd_at : eocd_at + 22]
    if len(eocd) < 22:
        raise BundleError("archive-not-readable")
    _signature, _disk, _cd_disk, entries_on_disk, total_entries, cd_size, _cd_offset, _comment = struct.unpack(
        "<IHHHHIIH", eocd
    )
    if total_entries == 0xFFFF or cd_size == 0xFFFFFFFF:
        locator_at = eocd_at - 20
        if locator_at < 0:
            raise BundleError("archive-not-readable")
        locator = tail[locator_at : locator_at + 20]
        if len(locator) < 20 or struct.unpack("<I", locator[:4])[0] != _ZIP64_LOCATOR_SIGNATURE:
            raise BundleError("archive-not-readable")
        zip64_offset = struct.unpack("<Q", locator[8:16])[0]
        if zip64_offset + 56 > len(archive_bytes):
            raise BundleError("archive-not-readable")
        zip64 = archive_bytes[zip64_offset : zip64_offset + 56]
        if struct.unpack("<I", zip64[:4])[0] != _ZIP64_EOCD_SIGNATURE:
            raise BundleError("archive-not-readable")
        total_entries = struct.unpack("<Q", zip64[32:40])[0]
        cd_size = struct.unpack("<Q", zip64[40:48])[0]
    if total_entries > MAX_ARCHIVE_MEMBERS or entries_on_disk > MAX_ARCHIVE_MEMBERS:
        raise BundleError("archive-member-limit")
    if cd_size > MAX_CENTRAL_DIRECTORY_BYTES or cd_size > len(archive_bytes):
        raise BundleError("archive-central-directory-oversize")


def _extract_archive_members(archive_bytes: bytes) -> dict[str, bytes]:
    """Extract every archive member as bytes, rejecting unsafe archives.

    Names must be canonical relative paths without duplicates; members
    must be regular files, unencrypted, and stored or deflated; declared
    and actual uncompressed sizes are capped per member and in total, so a
    zip bomb is cut off before or during decompression. Directory entries
    carry no bytes and are skipped. Archives are data: nothing here
    executes or fetches anything a member names.
    """

    _preflight_archive(archive_bytes)
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise BundleError("archive-not-readable") from None
    members: dict[str, bytes] = {}
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise BundleError("archive-member-limit")
        declared_total = 0
        read_total = 0
        for info in infos:
            if info.filename.endswith("/"):
                continue
            _check_member_name(info.filename)
            if info.filename in members:
                raise BundleError("archive-duplicate-member")
            mode = (info.external_attr >> 16) & 0o170000
            if mode != 0 and not stat.S_ISREG(mode):
                raise BundleError("archive-member-not-regular")
            if info.flag_bits & 0x1:
                raise BundleError("archive-member-encrypted")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise BundleError("archive-unsupported-compression")
            if info.file_size > MAX_FILE_BYTES:
                raise BundleError("archive-member-oversize")
            declared_total += info.file_size
            if declared_total > MAX_TOTAL_BYTES:
                raise BundleError("archive-total-oversize")
            data, read_total = _read_member(archive, info, read_total)
            members[info.filename] = data
    return members


def _check_member_name(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise BundleError("archive-member-path-unsafe")
    if name != posixpath.normpath(name):
        raise BundleError("archive-member-path-unsafe")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise BundleError("archive-member-path-unsafe")


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, read_total: int) -> tuple[bytes, int]:
    """Read one member with per-member and running-total caps enforced
    during decompression, in case the declared sizes lie."""

    parts: list[bytes] = []
    total = 0
    try:
        with archive.open(info) as member:
            while True:
                chunk = member.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                read_total += len(chunk)
                if total > MAX_FILE_BYTES or read_total > MAX_TOTAL_BYTES:
                    raise BundleError("archive-member-oversize")
                parts.append(chunk)
    except BundleError:
        raise
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError):
        raise BundleError("archive-not-readable") from None
    return b"".join(parts), read_total


# Quarantine: symlink-safe, durable, no-clobber writes through dir_fds.


def _open_private_directory(directory: Path) -> int:
    """Open a directory without following a final-component symlink."""

    try:
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise BundleError("quarantine-root-not-private") from None


def _require_private_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise BundleError("quarantine-root-not-private")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BundleError("quarantine-root-not-private")


def _open_component_directory(parent_fd: int, name: str) -> int:
    """Open or create one quarantine subdirectory without following links,
    validating that an existing one is a private directory we own. The
    descriptor is closed when validation fails, so a refusal leaks
    nothing. An adopted existing child also repairs the parent entry that
    holds it — an earlier or concurrent creator may have failed before
    flushing it — under the adopted descriptor's cleanup guard."""

    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        raise BundleError("quarantine-not-writable") from None
    else:
        try:
            _require_private_directory(os.fstat(descriptor))
            # The adopted child's entry may never have been synced: repair
            # the parent before returning, under this cleanup guard.
            _fsync_fd(parent_fd)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass  # a concurrent creator made it first
    except OSError:
        raise BundleError("quarantine-not-writable") from None
    # Persist the entry that holds this directory — whether this call or a
    # concurrent creator made it — before anything is written inside it.
    _fsync_fd(parent_fd)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        raise BundleError("quarantine-not-writable") from None
    try:
        _require_private_directory(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write_quarantine_file(root_fd: int, relative: str, ordinal: int, data: bytes) -> None:
    """Publish one frozen file atomically and without clobbering.

    The bytes go to a private temporary file (full write, fsync), which is
    then linked into place; link fails if the target exists, so an
    existing target is never replaced. The temporary name is exclusively
    created by this call and unlinked on every path — create, write,
    fsync, and publish — and an unlink failure after a successful publish
    is never acknowledged. An existing target is accepted only when it is
    a private regular file whose content still hashes to the frozen
    digest, read bounded and without following links; its parent is then
    fsynced too, because the earlier publisher may have crashed before
    syncing that entry. The ordinal, never the producer path, identifies
    failures.
    """

    parts = relative.split("/")
    opened: list[int] = []
    try:
        directory = root_fd
        for part in parts[:-1]:
            directory = _open_component_directory(directory, part)
            opened.append(directory)
        name = parts[-1]
        existing = _open_existing_quarantine_file(directory, name, len(data))
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != hashlib.sha256(data).hexdigest():
                raise BundleError("quarantine-conflict", f"file {ordinal}")
            # The entry may never have been synced by its publisher.
            _fsync_fd(directory)
            return
        temporary = f".{name}.{os.urandom(8).hex()}.tmp"
        # Acquire the name exclusively before arming its cleanup: a
        # refusal means a foreign file already holds this nonce, and a
        # refusal does not establish ownership — nothing is unlinked.
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        except OSError:
            raise BundleError("quarantine-not-writable") from None
        published = False
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            except OSError:
                raise BundleError("quarantine-not-durable") from None
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            except FileExistsError:
                _compare_after_link_race(directory, name, data, ordinal)
            except OSError:
                raise BundleError("quarantine-not-writable") from None
            published = True
        finally:
            # The temporary name is exclusively ours; clean it up on every
            # path. After a successful publish a failed cleanup is itself
            # a durability failure and is never acknowledged; on a path
            # that is already failing, the original error propagates.
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                if published:
                    raise BundleError("quarantine-not-durable") from None
        _fsync_fd(directory)
    finally:
        for descriptor in opened:
            os.close(descriptor)


def _open_existing_quarantine_file(parent_fd: int, name: str, expected_size: int) -> bytes | None:
    """Read an existing quarantine target bounded, without following
    links, only when it is a private regular file. The link count is
    deliberately not checked: permissions live on the inode, so a
    hardlink elsewhere cannot widen access, and our own publish protocol
    briefly holds a second link from its temporary name; a private
    regular file with matching bytes is accepted either way."""

    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise BundleError("quarantine-conflict") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BundleError("quarantine-conflict")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise BundleError("quarantine-conflict")
        if metadata.st_size != expected_size or metadata.st_size > MAX_FILE_BYTES:
            raise BundleError("quarantine-conflict")
        return _read_opened(descriptor, MAX_FILE_BYTES, "quarantine-conflict")
    finally:
        os.close(descriptor)


def _compare_after_link_race(parent_fd: int, name: str, data: bytes, ordinal: int) -> None:
    """Another writer published the target between the check and the
    link: accept it only when it still holds the frozen bytes."""

    existing = _open_existing_quarantine_file(parent_fd, name, len(data))
    if existing is None or hashlib.sha256(existing).hexdigest() != hashlib.sha256(data).hexdigest():
        raise BundleError("quarantine-conflict", f"file {ordinal}")


def _fsync_fd(descriptor: int) -> None:
    """Fsync an open descriptor, raising when durability cannot be
    established: a silently skipped fsync would acknowledge a quarantine
    that a crash could lose."""

    try:
        os.fsync(descriptor)
    except OSError:
        raise BundleError("quarantine-not-durable") from None


def _fsync_parent_entry(directory: Path) -> None:
    """Flush the directory entry that holds an adopted directory.

    A previous or concurrent creator may have made `directory` without
    syncing its parent; adopting it without this repair could acknowledge
    data whose containing entry was never durable. The parent is opened
    read-only by the caller's path semantics and only fsynced — never
    chmodded or validated — because ancestors such as the caller's
    scratch root are not private targets. A failed repair raises, so
    nothing is acknowledged.
    """

    parent = directory.parent
    if parent == directory:
        return  # the filesystem root has no parent entry to flush
    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise BundleError("quarantine-not-durable") from None
    try:
        _fsync_fd(descriptor)
    finally:
        os.close(descriptor)

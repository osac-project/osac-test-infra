#!/usr/bin/env python3
"""Replace every secret found by gitleaks with a literal [REDACTED]
marker across a copy of the scanned logs.

Usage: redact.py <gitleaks-findings.json> <dir-to-redact-in-place>

gitleaks --max-decode-depth (default 5 in v8.30) reports *decoded*
Secret/Match values (Tags: decoded:base64). The file often only has
that secret inside a base64 blob (SQL DEBUG JSON, etc.), so exact
Secret string replace no-ops and the uploaded tree still re-triggers
gitleaks. Columns help but do not reliably mark the real blob.

All wipe targets (encoded fields, plaintext secrets, hex-encoded secrets,
column spans) are computed against each file's pristine bytes, then
applied in one pass so earlier replacements cannot shift later column
offsets.
"""
from __future__ import annotations

import base64
import binascii
import json
import pathlib
import re
import sys
from collections import defaultdict

REDACTED_MARKER = b"[REDACTED]"

# Match gitleaks v8.30 default decode recursion when peeling nested b64.
_MAX_DECODE_DEPTH = 5

# gitleaks jwt Secret values sometimes include trailing backslashes copied
# from JSON string escapes in the scanned line (CaaS run 30568135525).
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# Match gitleaks default Base64 candidate floor ([\w/+-]{16,}={0,2}).
# Longer floors (e.g. 40) miss 16-39-char quoted fields when resolve_path
# returns None and column spans never land — those stay in the published tree.
_MIN_B64_LEN = 16
# Body alphabet: std + url-safe base64.
_B64_BODY = rb"[A-Za-z0-9+/_\-]{" + str(_MIN_B64_LEN).encode() + rb",}={0,2}"
# \"<base64>\" in JSON-escaped log lines (fulfillment SQL DEBUG).
_ESC_QUOTED_B64 = re.compile(rb'\\"(' + _B64_BODY + rb')\\"')
# "<base64>" raw double-quoted fields.
_RAW_QUOTED_B64 = re.compile(rb'"(' + _B64_BODY + rb')"')
# Unquoted assigned fields: token=<base64>, password:<base64>, etc.
# Leading delimiter + trailing non-b64 boundary avoid mid-token matches.
_UNQUOTED_ASSIGNED_B64 = re.compile(
    rb"(?i)(?:^|[\s;,|&\"'])(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|auth(?:orization)?)[:=]("
    + _B64_BODY
    + rb")(?![A-Za-z0-9+/_\-=])"
)


def secret_variants(secret: str) -> list[str]:
    """Return secret plus forms that strip gitleaks JSON-escape artifacts."""
    seen: set[str] = set()
    out: list[str] = []

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    add(secret)
    stripped = secret
    while stripped.endswith("\\"):
        stripped = stripped[:-1]
        add(stripped)
    if "eyJ" in secret:
        match = _JWT_RE.search(secret)
        if match:
            add(match.group(0))
    return out


def collect_secrets(findings: list[dict]) -> list[bytes]:
    """Unique secret byte strings, longest first, including escape variants."""
    variants: set[str] = set()
    for finding in findings:
        secret = finding.get("Secret") or ""
        for variant in secret_variants(secret):
            variants.add(variant)
    # Longest first: redacting a shorter secret first can leave a fragment
    # of a longer overlapping token behind.
    return [s.encode() for s in sorted(variants, key=len, reverse=True)]


def try_b64_decode(blob: bytes) -> bytes | None:
    """Decode std or url-safe base64; None if neither works.

    Prefer ``urlsafe_b64decode`` first: ``b64decode(..., validate=False)``
    can "succeed" on ``-``/``_`` inputs with corrupted bytes and then skip
    the url-safe path. ``urlsafe_b64decode`` does not accept ``validate=``.
    """
    padded = blob + b"==="
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        pass
    try:
        return base64.b64decode(padded, validate=False)
    except (binascii.Error, ValueError):
        return None


def blob_decodes_to_secret(blob: bytes, secrets: list[bytes]) -> bool:
    """True if any decode layer (up to _MAX_DECODE_DEPTH) contains a secret."""
    current = blob
    for _ in range(_MAX_DECODE_DEPTH):
        decoded = try_b64_decode(current)
        if decoded is None:
            return False
        if any(secret in decoded for secret in secrets):
            return True
        if decoded == current:
            return False
        current = decoded
    return False


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent half-open ranges."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def apply_ranges(data: bytes, ranges: list[tuple[int, int]]) -> bytes:
    """Replace each merged range with REDACTED_MARKER in one forward pass."""
    merged = merge_ranges(ranges)
    if not merged:
        return data

    result = bytearray()
    cursor = 0
    for start, end in merged:
        result.extend(data[cursor:start])
        result.extend(REDACTED_MARKER)
        cursor = end
    result.extend(data[cursor:])
    return bytes(result)


def _ranges_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


def encoded_fields_containing_secrets(
    content: bytes, secrets: list[bytes]
) -> list[tuple[int, int]]:
    """Half-open spans of base64 field *bodies* whose decode holds a secret."""
    if not secrets:
        return []
    ranges: list[tuple[int, int]] = []
    # Prefer escaped-quoted matches; raw-quoted can also match the inner
    # body of \"...\" (the " after \), so skip raw spans that overlap.
    esc_spans: list[tuple[int, int]] = []
    for match in _ESC_QUOTED_B64.finditer(content):
        if blob_decodes_to_secret(match.group(1), secrets):
            span = (match.start(1), match.end(1))
            esc_spans.append(span)
            ranges.append(span)

    for match in _RAW_QUOTED_B64.finditer(content):
        span = (match.start(1), match.end(1))
        if any(_ranges_overlap(span, esc) for esc in esc_spans):
            continue
        if blob_decodes_to_secret(match.group(1), secrets):
            ranges.append(span)

    # Unquoted token=<b64> (and similar). Skip spans already covered by quotes.
    quoted_spans = list(ranges)
    for match in _UNQUOTED_ASSIGNED_B64.finditer(content):
        span = (match.start(1), match.end(1))
        if any(_ranges_overlap(span, quoted) for quoted in quoted_spans):
            continue
        if blob_decodes_to_secret(match.group(1), secrets):
            ranges.append(span)
    return ranges


def plaintext_secret_ranges(content: bytes, secrets: list[bytes]) -> list[tuple[int, int]]:
    """Half-open spans of cleartext Secret variants in content."""
    ranges: list[tuple[int, int]] = []
    for secret in secrets:
        start = 0
        while True:
            idx = content.find(secret, start)
            if idx < 0:
                break
            ranges.append((idx, idx + len(secret)))
            start = idx + len(secret)
    return ranges


def hex_encoded_secret_ranges(content: bytes, secrets: list[bytes]) -> list[tuple[int, int]]:
    """Half-open spans of hex-encoded Secret bytes (gitleaks decoded:hex).

    Column spans usually cover encoded bounds when File resolves; this path
    still wipes hex forms tree-wide when path resolution fails. Search is
    case-insensitive so mixed-case hex (AbCd…) is wiped too.
    """
    ranges: list[tuple[int, int]] = []
    content_lower = content.lower()
    for secret in secrets:
        if not secret:
            continue
        form = binascii.hexlify(secret)  # lowercase
        start = 0
        while True:
            idx = content_lower.find(form, start)
            if idx < 0:
                break
            ranges.append((idx, idx + len(form)))
            start = idx + len(form)
    return ranges


def path_inside(root: pathlib.Path, path: pathlib.Path) -> bool:
    """True if path resolves inside root (blocks .. and symlink escape)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_path(redacted_dir: pathlib.Path, file_field: str) -> pathlib.Path | None:
    """Map a gitleaks File path onto redacted_dir.

    run_gitleaks mounts the scan root at /scan, so reports look like
    ``/scan/pod-….log``. Staging dirs are that root without the prefix.
    """
    if not file_field:
        return None
    rel = file_field.lstrip("/")
    if rel.startswith("scan/"):
        rel = rel[len("scan/") :]
    # Reject path traversal in the File field before joining.
    if ".." in pathlib.PurePosixPath(rel).parts:
        return None
    candidate = redacted_dir / rel
    if candidate.is_file() and path_inside(redacted_dir, candidate):
        return candidate
    name = pathlib.PurePosixPath(file_field).name
    if not name or name in {".", ".."}:
        return None
    matches = [
        p
        for p in redacted_dir.rglob(name)
        if p.is_file() and path_inside(redacted_dir, p)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def column_span(line: bytes, start_col: int, end_col: int) -> tuple[int, int] | None:
    """Convert 1-based inclusive gitleaks columns to a 0-based half-open span."""
    if start_col < 1 or end_col < start_col:
        return None
    start = start_col - 1
    end = min(end_col, len(line))
    if start >= len(line):
        return None
    return start, end


def line_start_offsets(raw: bytes) -> list[int]:
    """Byte offset of the start of each 1-based line (\\n-separated)."""
    offsets = [0]
    for idx, byte in enumerate(raw):
        if byte == 0x0A:
            offsets.append(idx + 1)
    return offsets


def location_ranges(
    content: bytes,
    line_cols: dict[int, list[tuple[int, int]]],
) -> list[tuple[int, int]]:
    """Absolute byte spans for gitleaks column hits on pristine content."""
    if not line_cols:
        return []
    offsets = line_start_offsets(content)
    lines = content.split(b"\n")
    # Trailing newline yields a final empty split entry that is not a line
    # gitleaks would number; drop it so indexes match StartLine.
    if content.endswith(b"\n") and lines and lines[-1] == b"":
        lines = lines[:-1]

    ranges: list[tuple[int, int]] = []
    for line_no, col_pairs in line_cols.items():
        if line_no < 1 or line_no > len(lines):
            continue
        line = lines[line_no - 1]
        base = offsets[line_no - 1]
        for start_col, end_col in col_pairs:
            span = column_span(line, start_col, end_col)
            if span is None:
                continue
            ranges.append((base + span[0], base + span[1]))
    return ranges


def redact_tree(findings: list[dict], redacted_dir: pathlib.Path) -> None:
    """Wipe secrets under redacted_dir using pristine-byte ranges per file."""
    secrets = collect_secrets(findings)

    # file -> line_number -> [(start_col, end_col), ...]
    pending_cols: dict[pathlib.Path, dict[int, list[tuple[int, int]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for finding in findings:
        path = resolve_path(redacted_dir, finding.get("File") or "")
        if path is None:
            continue
        start_line = finding.get("StartLine")
        end_line = finding.get("EndLine") or start_line
        start_col = finding.get("StartColumn")
        end_col = finding.get("EndColumn")
        if not start_line or not start_col or not end_col:
            continue
        if end_line != start_line:
            continue
        pending_cols[path][int(start_line)].append((int(start_col), int(end_col)))

    # Every file under the tree may hold plaintext/encoded secrets even when
    # gitleaks File path failed to resolve for a finding.
    paths = {
        p
        for p in redacted_dir.rglob("*")
        if p.is_file() and path_inside(redacted_dir, p)
    }
    paths.update(p for p in pending_cols if path_inside(redacted_dir, p))

    for path in sorted(paths):
        if not path_inside(redacted_dir, path):
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            print(f"redact.py: cannot read {path}, aborting: {exc}", file=sys.stderr)
            sys.exit(1)

        ranges: list[tuple[int, int]] = []
        if secrets:
            ranges.extend(encoded_fields_containing_secrets(content, secrets))
            ranges.extend(plaintext_secret_ranges(content, secrets))
            ranges.extend(hex_encoded_secret_ranges(content, secrets))
        ranges.extend(location_ranges(content, pending_cols.get(path, {})))
        if not ranges:
            continue
        path.write_bytes(apply_ranges(content, ranges))


def main() -> None:
    """Redact every finding in-place across redacted_dir."""
    findings_path, redacted_dir_arg = sys.argv[1], sys.argv[2]
    findings = json.loads(pathlib.Path(findings_path).read_text() or "[]")
    redact_tree(findings, pathlib.Path(redacted_dir_arg))


if __name__ == "__main__":
    main()

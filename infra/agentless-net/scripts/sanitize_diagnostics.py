#!/usr/bin/env python3
"""Copy diagnostic files with secrets removed.

The hosted run uploads only the directory this script writes. A private key
that survives redaction fails the script so the raw file is not published.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


_PRIVATE_KEY = re.compile(
    rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_AUTHORIZATION = re.compile(rb"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\r\n]+")
_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)"
    rb"(\"[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data)[A-Za-z0-9_.-]*\""
    rb"|[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data)[A-Za-z0-9_.-]*)"
    rb"(\s*[:=]\s*)"
    rb"(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n]+)"
)
_REDACTED = b"[REDACTED]"
# YAML block header: | or >, optional chomp, optional single-digit indent, in either order.
_BLOCK_HEADER = re.compile(rb"^[|>](?:[+-][1-9]?|[1-9][+-]?)?(?:[ \t]+#.*)?[ \t]*$")
# A secret key whose line has no value. The value is the indented block that follows.
_SECRET_KEY_ONLY = re.compile(
    rb"(?i)^[ \t]*"
    rb"(\"[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data)[A-Za-z0-9_.-]*\""
    rb"|[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data)[A-Za-z0-9_.-]*)"
    rb"(\s*[:=])"
    rb"[ \t]*$"
)


def _redact_assignment(match: re.Match[bytes]) -> bytes:
    value = match.group(3)
    if value.startswith(b'"') and value.endswith(b'"'):
        replacement = b'"' + _REDACTED + b'"'
    elif value.startswith(b"'") and value.endswith(b"'"):
        replacement = b"'" + _REDACTED + b"'"
    else:
        replacement = _REDACTED
    return match.group(1) + match.group(2) + replacement


def _line_indent(line: bytes) -> int:
    return len(line) - len(line.lstrip(b" \t"))


def _drop_indented_block(lines: list[bytes], index: int, key_indent: int) -> tuple[int, bool]:
    """Skip a nested value. Blank lines are discarded. Returns the next index and whether a value was present."""
    consumed_value = False
    while index < len(lines):
        follower = lines[index]
        if follower.strip() == b"":
            index += 1
            continue
        if _line_indent(follower) > key_indent:
            consumed_value = True
            index += 1
            continue
        break
    return index, consumed_value


def redact(data: bytes) -> bytes:
    data = _PRIVATE_KEY.sub(b"[REDACTED PRIVATE KEY]", data)
    data = _AUTHORIZATION.sub(lambda match: match.group(1) + _REDACTED, data)
    lines = data.splitlines(keepends=True)
    output: list[bytes] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _SECRET_ASSIGNMENT.search(line)
        value = match.group(3) if match else b""
        if match and value[:1] in (b"|", b">"):
            if _BLOCK_HEADER.fullmatch(value.strip()) is None:
                raise RuntimeError("unsupported secret block scalar")
            output.append(line[: match.start(3)] + _REDACTED + line[match.end(3) :])
            index, consumed_value = _drop_indented_block(lines, index + 1, _line_indent(line))
            if not consumed_value:
                raise RuntimeError("secret block scalar has no indented value")
            continue
        key_only = None if match else _SECRET_KEY_ONLY.search(line.rstrip(b"\r\n"))
        if key_only:
            ending = line[len(line.rstrip(b"\r\n")) :]
            output.append(key_only.group(1) + key_only.group(2) + b" " + _REDACTED + ending)
            index, consumed_value = _drop_indented_block(lines, index + 1, _line_indent(line))
            if not consumed_value:
                raise RuntimeError("secret key has no value")
            continue
        output.append(_SECRET_ASSIGNMENT.sub(_redact_assignment, line))
        index += 1
    return b"".join(output)


def sanitize_tree(source: Path, destination: Path) -> int:
    """Copy source into destination, redacting each file. Returns the file count."""
    if not source.is_dir():
        raise FileNotFoundError(f"diagnostic directory does not exist: {source}")
    count = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        cleaned = redact(path.read_bytes())
        if _PRIVATE_KEY_HEADER.search(cleaned):
            raise RuntimeError(f"private key material remains in {relative}")
        target.write_bytes(cleaned)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: sanitize_diagnostics.py SOURCE DESTINATION", file=sys.stderr)
        return 2
    count = sanitize_tree(Path(args[0]), Path(args[1]))
    print(f"sanitized {count} file(s) into {args[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

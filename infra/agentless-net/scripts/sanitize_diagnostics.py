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
_BEARER = re.compile(rb"Bearer\s+\S+")
_SECRET_NAME = rb"password|passwd|secret|token|api[_-]?key|client-key-data"
_JSON_FIELD = re.compile(
    rb'(?i)("(?:' + _SECRET_NAME + rb')")(\s*:\s*)("(?:\\.|[^"\\])*")'
)
_QUOTED_ASSIGNMENT = re.compile(
    rb"(?i)\b(" + _SECRET_NAME + rb""")(\s*[:=]\s*)(["'])(?:\\.|(?!\3).)*\3"""
)
_PLAIN_ASSIGNMENT = re.compile(
    rb"(?i)\b(" + _SECRET_NAME + rb""")(\s*[:=]\s*)([^\s#"']\S*)"""
)
_REDACTED = b"[REDACTED]"


def redact(data: bytes) -> bytes:
    data = _PRIVATE_KEY.sub(b"[REDACTED PRIVATE KEY]", data)
    data = _BEARER.sub(b"Bearer " + _REDACTED, data)
    data = _JSON_FIELD.sub(lambda match: match.group(1) + match.group(2) + b'"' + _REDACTED + b'"', data)
    data = _QUOTED_ASSIGNMENT.sub(
        lambda match: match.group(1) + match.group(2) + match.group(3) + _REDACTED + match.group(3),
        data,
    )
    data = _PLAIN_ASSIGNMENT.sub(lambda match: match.group(1) + match.group(2) + _REDACTED, data)
    return data


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
        if _PRIVATE_KEY.search(cleaned):
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

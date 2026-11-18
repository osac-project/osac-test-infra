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
    rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL,
)
_PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
_AUTHORIZATION = re.compile(rb"(?i)(Authorization\s*:\s*(?:Bearer|Basic)\s+)[^\r\n]+")
_SECRET_NAME = (
    rb"\"[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data|auth[_-]?config|authorization)[A-Za-z0-9_.-]*\""
    rb"|'[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data|auth[_-]?config|authorization)[A-Za-z0-9_.-]*'"
    rb"|(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_-]?key|client-key-data|auth[_-]?config|authorization)[A-Za-z0-9_.-]*"
)
# A comma ends a plain JSON member only when the next member's key follows it.
# "password: one, two" stays one value. "token": 123, "host": "edge" keeps host.
_PLAIN_SECRET_VALUE = (
    rb"(?:[^\r\n,]|,(?!\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'|[A-Za-z0-9_.-]+)\s*:))+"
)
_SECRET_ASSIGNMENT = re.compile(
    rb"(?i)(" + _SECRET_NAME + rb")"
    rb"(\s*[:=]\s*)"
    rb"(\"(?:\\.|[^\"\\])*\"|'(?:''|[^'])*'|" + _PLAIN_SECRET_VALUE + rb")"
)
# A secret key at the end of a line that already contains an earlier assignment.
_TRAILING_SECRET_KEY = re.compile(
    rb"(?i)(?:^|[,{\[]\s*)(" + _SECRET_NAME + rb")(\s*[:=])[ \t]*$"
)
_REDACTED = b"[REDACTED]"
_JSON_AUTH = re.compile(
    rb"""(?i)((?:"auth"|'auth')\s*:\s*)("(?:\\.|[^"\\])*"|'(?:''|[^'])*')"""
)
# YAML block header: | or >, optional chomp, optional single-digit indent, in either order.
_BLOCK_HEADER = re.compile(rb"^[|>](?:[+-][1-9]?|[1-9][+-]?)?(?:[ \t]+#.*)?[ \t]*$")
# A secret key whose line has no value. The value is the indented block that follows.
_SECRET_KEY_ONLY = re.compile(
    rb"(?i)^[ \t]*((?:[\[{][ \t]*)?(?:-[ \t]+)?)(" + _SECRET_NAME + rb")(\s*[:=])[ \t]*$"
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


def _redact_json_auth(match: re.Match[bytes]) -> bytes:
    value = match.group(2)
    quote = b'"' if value.startswith(b'"') else b"'"
    return match.group(1) + quote + _REDACTED + quote


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


def _close_container(stack: list[bytes], char: bytes) -> None:
    if not stack or stack[-1] != char:
        raise RuntimeError("unsupported json secret value")
    stack.pop()


def _consume_json_value(lines: list[bytes], index: int) -> tuple[int, int]:
    """Return the line and column just after a JSON object, array, or string.

    Indentation does not end the value. A quote starts only at a scalar
    boundary, so an apostrophe inside a plain value does not hide the closer.
    Delimiters inside real quotes do not end the value. An unclosed value is
    rejected. The caller keeps whatever follows the value on the closing line.
    """
    stack: list[bytes] = []
    quote = b""
    escape = False
    started = False
    in_plain = False
    at_boundary = True
    while index < len(lines):
        line = lines[index]
        pos = 0
        while pos < len(line):
            char = line[pos:pos + 1]
            if quote == b"'":
                if line[pos:pos + 2] == b"''":
                    pos += 2
                    continue
                if char == b"'":
                    quote = b""
                    if not stack:
                        return index, pos + 1
                    at_boundary = False
                pos += 1
                continue
            if quote == b'"':
                if escape:
                    escape = False
                elif char == b"\\":
                    escape = True
                elif char == b'"':
                    quote = b""
                    if not stack:
                        return index, pos + 1
                    at_boundary = False
                pos += 1
                continue
            if in_plain:
                if char == b":":
                    nxt = line[pos + 1:pos + 2]
                    if nxt in (b"", b" ", b"\t", b",", b"}", b"]"):
                        in_plain = False
                        at_boundary = True
                        pos += 1
                        continue
                    pos += 1
                    continue
                if char not in (b",", b"}", b"]"):
                    pos += 1
                    continue
                in_plain = False
                if char == b",":
                    at_boundary = True
                    pos += 1
                    continue
            if char in (b" ", b"\t", b"\r", b"\n"):
                pos += 1
                continue
            if not started:
                if char in (b'"', b"'"):
                    started = True
                    quote = char
                    pos += 1
                    continue
                if char == b"{":
                    started = True
                    stack.append(b"}")
                    at_boundary = True
                    pos += 1
                    continue
                if char == b"[":
                    started = True
                    stack.append(b"]")
                    at_boundary = True
                    pos += 1
                    continue
                raise RuntimeError("unsupported json secret value")
            if char in (b"}", b"]"):
                _close_container(stack, char)
                pos += 1
                if not stack:
                    return index, pos
                at_boundary = False
                continue
            if char in (b",", b":"):
                at_boundary = True
                pos += 1
                continue
            if not at_boundary:
                raise RuntimeError("unsupported json secret value")
            if char in (b'"', b"'"):
                quote = char
                pos += 1
                continue
            if char == b"{":
                stack.append(b"}")
                at_boundary = True
                pos += 1
                continue
            if char == b"[":
                stack.append(b"]")
                at_boundary = True
                pos += 1
                continue
            in_plain = True
            pos += 1
        index += 1
    raise RuntimeError("unsupported json secret value")


def _resume_after_json(lines: list[bytes], line_index: int, pos: int) -> int:
    """Keep members that share the line where a JSON secret value closed."""
    if line_index >= len(lines):
        return line_index
    remainder = lines[line_index][pos:]
    body = remainder.rstrip(b"\r\n")
    ending = remainder[len(body):]
    body = body.lstrip(b" \t")
    while body.startswith(b","):
        body = body[1:].lstrip(b" \t")
    if body == b"":
        return line_index + 1
    lines[line_index] = body + ending
    return line_index


def _is_yaml_quoted_key(line: bytes) -> bool:
    """A quoted YAML mapping key, such as \"username\": service, is not a JSON string."""
    text = line.lstrip(b" \t")
    if not text or text[:1] not in (b'"', b"'"):
        return False
    quote = text[:1]
    pos = 1
    if quote == b"'":
        while pos < len(text):
            if text[pos:pos + 2] == b"''":
                pos += 2
                continue
            if text[pos:pos + 1] == b"'":
                pos += 1
                break
            pos += 1
        else:
            return False
    else:
        escape = False
        while pos < len(text):
            char = text[pos:pos + 1]
            if escape:
                escape = False
            elif char == b"\\":
                escape = True
            elif char == b'"':
                pos += 1
                break
            pos += 1
        else:
            return False
    return text[pos:].lstrip(b" \t")[:1] == b":"


def _take_secret_value(lines: list[bytes], index: int, key_line: bytes) -> int:
    """Drop the value that follows a secret key, and keep later members."""
    probe = index + 1
    while probe < len(lines) and lines[probe].strip() == b"":
        probe += 1
    if probe < len(lines):
        first = lines[probe].lstrip(b" \t")[:1]
        json_value = first in (b"{", b"[") or (first == b'"' and not _is_yaml_quoted_key(lines[probe]))
    else:
        json_value = False
    if json_value:
        value_line, value_pos = _consume_json_value(lines, index + 1)
        return _resume_after_json(lines, value_line, value_pos)
    next_index, consumed_value = _drop_indented_block(lines, index + 1, _line_indent(key_line))
    if not consumed_value:
        raise RuntimeError("secret key has no value")
    return next_index


def _drop_plain_continuation(lines: list[bytes], index: int, key_indent: int) -> int:
    """Drop indented lines that continue a plain secret, including across blank lines."""
    while index < len(lines):
        probe = index
        while probe < len(lines) and lines[probe].strip() == b"":
            probe += 1
        if probe >= len(lines) or _line_indent(lines[probe]) <= key_indent:
            break
        index = probe + 1
    return index


def _closed_quote(value: bytes, quote: bytes) -> bool:
    if quote == b'"':
        pattern = rb'"(?:\\.|[^"\\])*"\Z'
    else:
        pattern = rb"'(?:''|[^'])*'\Z"
    return re.fullmatch(pattern, value) is not None


def _flow_closed(value: bytes) -> bool:
    try:
        line_index, pos = _consume_json_value([value], 0)
    except RuntimeError:
        return False
    return line_index == 0 and value[pos:].strip() == b""


def _multiline_secret(value: bytes) -> bool:
    """A quoted or flow value that continues past this line."""
    if value.startswith(b'"'):
        return not _closed_quote(value, b'"')
    if value.startswith(b"'"):
        return not _closed_quote(value, b"'")
    if value.startswith((b"{", b"[")):
        return not _flow_closed(value)
    return False


_JSON_KEY = re.compile(rb'"((?:\\.|[^"\\])*)"(\s*:)')


def _decode_json_keys(line: bytes) -> bytes:
    """Decode JSON \\u escapes in object keys so secret names are recognized."""

    def replace(match: re.Match[bytes]) -> bytes:
        key = match.group(1)
        if b"\\u" not in key.lower():
            return match.group(0)
        if re.search(rb"\\(?!u[0-9a-fA-F]{4})", key, re.IGNORECASE):
            raise RuntimeError("unsupported escaped secret key")

        def character(escape: re.Match[bytes]) -> bytes:
            try:
                return chr(int(escape.group(1), 16)).encode("utf-8")
            except (ValueError, UnicodeEncodeError) as exc:
                raise RuntimeError("unsupported escaped secret key") from exc

        decoded = re.sub(rb"\\u([0-9a-fA-F]{4})", character, key, flags=re.IGNORECASE)
        if b'"' in decoded or b"\\" in decoded or b"\n" in decoded:
            raise RuntimeError("unsupported escaped secret key")
        return b'"' + decoded + b'"' + match.group(2)

    return _JSON_KEY.sub(replace, line)


def redact(data: bytes) -> bytes:
    data = _PRIVATE_KEY.sub(b"[REDACTED PRIVATE KEY]", data)
    data = _AUTHORIZATION.sub(lambda match: match.group(1) + _REDACTED, data)
    data = _JSON_AUTH.sub(_redact_json_auth, data)
    lines = data.splitlines(keepends=True)
    output: list[bytes] = []
    index = 0
    while index < len(lines):
        line = _decode_json_keys(lines[index])
        matches = list(_SECRET_ASSIGNMENT.finditer(line))
        for candidate in matches:
            candidate_value = candidate.group(3)
            if candidate_value[:1] in (b"&", b"*", b"!"):
                raise RuntimeError("unsupported secret anchor or tag")
            if candidate_value[:1] in (b"|", b">") and _BLOCK_HEADER.fullmatch(candidate_value.strip()) is None:
                raise RuntimeError("unsupported secret block scalar")
            if _multiline_secret(candidate_value):
                raise RuntimeError("unsupported multiline secret value")
        match = matches[0] if matches else None
        value = match.group(3) if match else b""
        if match and value[:1] in (b"|", b">"):
            output.append(line[: match.start(3)] + _REDACTED + line[match.end(3) :])
            index, consumed_value = _drop_indented_block(lines, index + 1, _line_indent(line))
            if not consumed_value:
                raise RuntimeError("secret block scalar has no indented value")
            continue
        if match and _multiline_secret(value):
            raise RuntimeError("unsupported multiline secret value")
        body = line.rstrip(b"\r\n")
        ending = line[len(body):]
        trailing = _TRAILING_SECRET_KEY.search(body) if match else None
        if trailing and trailing.start(1) >= match.end():
            prefix = _SECRET_ASSIGNMENT.sub(_redact_assignment, body[:trailing.start()])
            output.append(prefix + trailing.group(1) + trailing.group(2) + b" " + _REDACTED + ending)
            index = _take_secret_value(lines, index, line)
            continue
        key_only = None if match else _SECRET_KEY_ONLY.search(body)
        if key_only:
            output.append(key_only.group(1) + key_only.group(2) + key_only.group(3) + b" " + _REDACTED + ending)
            index = _take_secret_value(lines, index, line)
            continue
        output.append(_SECRET_ASSIGNMENT.sub(_redact_assignment, line))
        plain = match is not None and value[:1] not in (b'"', b"'", b"{", b"[", b"|", b">", b"&", b"*", b"!")
        if plain:
            index = _drop_plain_continuation(lines, index + 1, _line_indent(line))
        else:
            index += 1
    return b"".join(output)


def _diagnostic_bytes(source_root: Path, path: Path) -> bytes:
    """Read one diagnostic file that is inside source_root."""
    if path.is_symlink():
        raise RuntimeError(f"diagnostic path is a symlink: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(source_root):
        raise RuntimeError(f"diagnostic path is outside the source directory: {path}")
    return resolved.read_bytes()


def sanitize_tree(source: Path, destination: Path) -> int:
    """Copy source into destination, redacting each file. Returns the file count."""
    if source.is_symlink():
        raise RuntimeError(f"diagnostic directory is a symlink: {source}")
    if not source.is_dir():
        raise FileNotFoundError(f"diagnostic directory does not exist: {source}")
    source_root = source.resolve()
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"diagnostic path is a symlink: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(source_root):
            raise RuntimeError(f"diagnostic path is outside the source directory: {path}")
        if resolved.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not resolved.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        cleaned = redact(_diagnostic_bytes(source_root, path))
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

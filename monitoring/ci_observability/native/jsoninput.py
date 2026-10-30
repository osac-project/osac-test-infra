"""Strict, bounded JSON loading for native report bytes.

Bytes in, parsed value out: no file paths, no URLs, no fetches. The
input byte cap is enforced before parsing, so allocation stays bounded
by the cap. Duplicate keys, non-finite constants, and overflowing
numbers are rejected by hooks during the parse itself. Depth and node
counts are checked after json.loads returns: a tree within the byte
cap is built first and then refused. The walks are iterative, and a
nesting level the JSON parser itself cannot descend surfaces as the
same constant error.
"""

from __future__ import annotations

import json

from .records import MAX_NATIVE_FILE_BYTES, AdapterError, JSONValue

MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 500_000


def load_json_bytes(*, raw: bytes, max_bytes: int = MAX_NATIVE_FILE_BYTES) -> JSONValue:
    """Decode strict UTF-8 JSON with duplicate-key, constant, depth, and
    node-count rejection."""

    if not isinstance(raw, (bytes, bytearray)):
        raise AdapterError("native report must be bytes")
    if len(raw) > max_bytes:
        raise AdapterError("native report exceeds the file byte bound")
    try:
        text = bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise AdapterError("native report must be strict UTF-8") from None
    if text.startswith("\ufeff"):
        raise AdapterError("native report must not start with a byte order mark")

    counter = [0]

    def count_node(value: object) -> object:
        counter[0] += 1
        if counter[0] > MAX_JSON_NODES:
            raise AdapterError("native report exceeds the node count bound")
        stack: list[object] = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, list):
                for item in current:
                    counter[0] += 1
                    if counter[0] > MAX_JSON_NODES:
                        raise AdapterError("native report exceeds the node count bound")
                    stack.append(item)
            elif isinstance(current, dict):
                for item in current.values():
                    counter[0] += 1
                    if counter[0] > MAX_JSON_NODES:
                        raise AdapterError("native report exceeds the node count bound")
                    stack.append(item)
        return value

    def reject_constant(name: str) -> object:
        raise AdapterError("native report must not contain non-finite constants")

    def parse_float_strict(text: str) -> float:
        value = float(text)
        if value != value or value in (float("inf"), float("-inf")):
            raise AdapterError("native report must not contain overflowing numbers")
        return value

    def strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("native report must not contain duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(
            text, object_pairs_hook=strict_pairs, parse_constant=reject_constant, parse_float=parse_float_strict
        )
    except AdapterError:
        raise
    except RecursionError:
        # A nest the parser itself cannot descend is the depth problem,
        # not an internal crash.
        raise AdapterError("native report exceeds the nesting depth bound") from None
    except (json.JSONDecodeError, ValueError):
        raise AdapterError("native report is not valid JSON") from None
    value = count_node(value)
    if _depth(value) > MAX_JSON_DEPTH:
        raise AdapterError("native report exceeds the nesting depth bound")
    return value


def _depth(value: object) -> int:
    """Iterative nesting depth: a recursive walk would turn a deep report
    into a RecursionError instead of a constant error."""
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, level = stack.pop()
        if level > deepest:
            deepest = level
        if isinstance(current, list):
            for item in current:
                stack.append((item, level + 1))
        elif isinstance(current, dict):
            for item in current.values():
                stack.append((item, level + 1))
    return deepest


def load_jsonl_bytes(
    *, raw: bytes, max_bytes: int = MAX_NATIVE_FILE_BYTES, max_lines: int = 500_000
) -> list[JSONValue]:
    """Decode strict UTF-8 JSON Lines: one bounded JSON document per line.

    Blank lines are rejected: a native stream is dense.
    """

    if not isinstance(raw, (bytes, bytearray)):
        raise AdapterError("native stream must be bytes")
    if len(raw) > max_bytes:
        raise AdapterError("native stream exceeds the file byte bound")
    try:
        text = bytes(raw).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise AdapterError("native stream must be strict UTF-8") from None
    lines = text.splitlines()
    if len(lines) > max_lines:
        raise AdapterError("native stream exceeds the line count bound")
    return [load_json_bytes(raw=line.encode("utf-8"), max_bytes=max_bytes) for line in lines]

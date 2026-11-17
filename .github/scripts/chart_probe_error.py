#!/usr/bin/env python3
"""Print only recognized diagnostic fragments, never arbitrary registry stderr."""

from __future__ import annotations

import re
import sys

DIAGNOSTICS = (
    "manifest unknown",
    "manifest not found",
    "name unknown",
    "unauthorized",
    "authentication required",
    "denied",
    "forbidden",
    "too many requests",
    "toomanyrequests",
    "no such host",
    "connection refused",
    "connection reset by peer",
    "i/o timeout",
    "context deadline exceeded",
    "tls handshake timeout",
    "certificate signed by unknown authority",
    "certificate has expired or is not yet valid",
    "unexpected eof",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)


def main() -> None:
    error = sys.stdin.read().lower()
    found = [phrase for phrase in DIAGNOSTICS if phrase in error]
    statuses = re.findall(
        r"\b(?:status code(?: from registry)?|statuscode|status|http(?:/[0-9.]+)?):?\s*([45][0-9]{2})\b", error
    )
    found.extend(f"HTTP {status}" for status in sorted(set(statuses)))
    diagnostic = "; ".join(found[:8]) if found else "no recognized diagnostic (raw stderr withheld)"
    print(f"Last chart manifest probe error (allowlisted): {diagnostic}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Censor secrets from a directory before it is published.

Usage:
  release_gate.py <dir> [--secret-file F]... [--env NAME]... [--k8s-secrets F]...
                        [--report PATH] [--check] [--fail-on-find]

Pattern scanners only find shapes somebody wrote a rule for. This gate works in
two independent layers so a secret has to evade both:

  1. Known values. Every secret the job already holds (files such as the registry
     pull secret, environment variables, and the data of the cluster's Kubernetes
     Secrets) is searched for in the artifact as read, JSON-escaped and base64
     encoded at every byte alignment. Each hit is replaced with
     [REDACTED:<name>]; a base64 hit replaces the whole enclosing base64 token.
  2. Shapes. Docker auth entries, private keys, JWTs, Authorization headers,
     well-known provider tokens and password fields are replaced even when no
     known value matches.

After censoring, the known values are searched for again. A file in which one
survives, a file that cannot be read, an archive that cannot be inspected and an
oversized file are all removed rather than published, and the report lists them.

Modes: by default the files are rewritten in place. --check changes nothing and
exits 1 when anything would have been censored. --fail-on-find censors and then
exits 1 if anything was found, so a CI canary or a strict workflow can treat a
leak as a failure while the published artifact is already clean.

Exit status: 0 clean, or censored without --fail-on-find; 1 found (--check or
--fail-on-find); 2 usage error or an inventory source that yields nothing. A
secret file that does not exist is skipped with a notice, because suites without
a pull secret share the upload action.

Only file names, rule names, counts and short hashes are printed or written,
never a value.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import bz2
import collections
import gzip
import hashlib
import io
import json
import lzma
import math
import os
import pathlib
import re
import sys
import zipfile

# Shorter strings (usernames, hostnames, placeholders) would flag ordinary text.
MIN_LEN = 16
# Kubernetes Secret values are often shorter random passwords; they are filtered
# by entropy instead.
K8S_MIN_LEN = 12
K8S_MIN_ENTROPY = 2.5
MAX_FILE_BYTES = 512 * 1024 * 1024
_MAX_NESTING = 4
REPORT_NAME = "redaction-report.json"

# Identity fields next to a credential (a Docker auth entry carries an email)
# appear in ordinary logs and are not secret.
_IDENTITY_KEYS = {"email", "username", "user"}
# Kubernetes Secret keys holding public material.
_K8S_PUBLIC_KEYS = {"ca.crt", "tls.crt", "ca-bundle.crt", "service-ca.crt", "namespace"}

_B64_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_"
)
_EQUALS = ord("=")


# ── inventory of known values ────────────────────────────────────────────────


def _entropy(text: str) -> float:
    counts = collections.Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _decode_b64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None


def collect_needles(value: object, label: str, out: dict[bytes, str], depth: int = 0) -> None:
    """Add every secret-like string under `value`, and what it decodes to."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _IDENTITY_KEYS:
                continue
            collect_needles(item, f"{label}.{key}" if label else str(key), out, depth)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            collect_needles(item, f"{label}[{index}]", out, depth)
    elif isinstance(value, str):
        _add_string(value, label, out, depth)


def _add_string(text: str, label: str, out: dict[bytes, str], depth: int, min_len: int = MIN_LEN) -> None:
    if len(text) < min_len:
        return
    out.setdefault(text.encode(), label)
    if depth >= _MAX_NESTING:
        return
    try:
        nested = json.loads(text)
    except ValueError:
        nested = None
    if isinstance(nested, (dict, list)):
        collect_needles(nested, label, out, depth + 1)
    decoded = _decode_b64(text)
    if not decoded:
        return
    try:
        decoded_text = decoded.decode()
    except UnicodeDecodeError:
        return
    # A Docker "auth" value is base64("user:password"); the password alone is
    # what gets copied around, so match it as well.
    _user, sep, password = decoded_text.partition(":")
    if sep:
        _add_string(password, f"{label}(decoded password)", out, depth + 1)
    _add_string(decoded_text, f"{label}(decoded)", out, depth + 1)


def needles_from_secret_file(path: pathlib.Path) -> dict[bytes, str]:
    raw = path.read_text(errors="replace")
    needles: dict[bytes, str] = {}
    try:
        collect_needles(json.loads(raw), "", needles)
    except ValueError:
        # Not JSON: treat each whitespace-separated token as a value.
        for index, token in enumerate(raw.split()):
            _add_string(token, f"token{index}", needles, 0)
    return needles


def needles_from_k8s_secrets(path: pathlib.Path) -> dict[bytes, str]:
    """Values of `oc get secrets -o json`, without public certificates."""
    document = json.loads(path.read_text())
    items = document.get("items", [document]) if isinstance(document, dict) else []
    needles: dict[bytes, str] = {}
    for item in items:
        meta = item.get("metadata", {})
        prefix = f"secret/{meta.get('namespace', '?')}/{meta.get('name', '?')}"
        values: dict[str, str] = {}
        for key, encoded in (item.get("data") or {}).items():
            decoded = _decode_b64(encoded) if isinstance(encoded, str) else None
            if decoded is not None:
                values[key] = decoded.decode(errors="replace")
        values.update({k: v for k, v in (item.get("stringData") or {}).items() if isinstance(v, str)})
        for key, text in values.items():
            if key in _K8S_PUBLIC_KEYS or text.startswith("-----BEGIN CERTIFICATE-----"):
                continue
            label = f"{prefix}/{key}"
            if text.lstrip().startswith(("{", "[")):
                # A .dockerconfigjson or other JSON document: its leaves are the secrets.
                try:
                    collect_needles(json.loads(text), label, needles)
                    continue
                except ValueError:
                    pass
            if len(text) >= K8S_MIN_LEN and _entropy(text) >= K8S_MIN_ENTROPY:
                _add_string(text, label, needles, 0, min_len=K8S_MIN_LEN)
    return needles


# ── how a value appears in a file ────────────────────────────────────────────


def variants(needle: bytes) -> list[tuple[str, bytes, bool]]:
    """(form, bytes to find, is-base64) for the ways a value shows up in a file."""
    found: list[tuple[str, bytes, bool]] = [("plain", needle, False)]
    escaped = json.dumps(needle.decode(errors="replace"))[1:-1].encode()
    if escaped != needle:
        found.append(("json-escaped", escaped, False))
    seen = set()
    for pad in range(3):
        for form, encode in (("base64", base64.b64encode), ("base64url", base64.urlsafe_b64encode)):
            # The first and last group depend on neighbouring bytes; the middle does not.
            middle = encode(b"\0" * pad + needle)[4:-4]
            if middle not in seen:
                seen.add(middle)
                found.append((f"{form}+{pad}", middle, True))
    return found


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:10]


def _marker(name: str, suffix: str = "") -> bytes:
    safe = re.sub(r"[^A-Za-z0-9._:/()\[\]-]", "_", name)
    return f"[REDACTED:{safe}{':' + suffix if suffix else ''}]".encode()


# ── shape rules ──────────────────────────────────────────────────────────────


def _docker_auth(match: re.Match[bytes]) -> bytes | None:
    key, value = match.group("key").lower(), match.group("val")
    if key == b"auth":
        decoded = _decode_b64(value.decode())
        if not decoded or b":" not in decoded:
            return None
    return match.group("pre") + _marker("docker-" + key.decode()) + match.group("post")


def _keep_prefix(group: str, name: str):
    def replace(match: re.Match[bytes]) -> bytes | None:
        return match.group(group) + _marker(name)
    return replace


def _password_field(match: re.Match[bytes]) -> bytes | None:
    value = match.group("val")
    if value.startswith((b"[REDACTED", b"<", b"*")) or value.upper().startswith(b"REDACTED"):
        return None
    return match.group("pre") + _marker("password-field") + match.group("post")


SHAPE_RULES: list[tuple[str, re.Pattern[bytes], object]] = [
    (
        "private-key",
        re.compile(rb"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY-----.*?-----END \1PRIVATE KEY-----", re.S),
        lambda m: _marker("private-key"),
    ),
    (
        # A block cut short by a log limit still carries key material.
        "private-key-truncated",
        re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[A-Za-z0-9+/=\s\\n]{40,}"),
        lambda m: _marker("private-key"),
    ),
    (
        # Docker config entries, also inside an escaped JSON string.
        "docker-auth",
        re.compile(
            rb'(?P<pre>\\*"(?P<key>auth|identitytoken|registrytoken)\\*"\s*:\s*\\*")'
            rb'(?P<val>[A-Za-z0-9+/=_.-]{20,})(?P<post>\\*")',
            re.I,
        ),
        _docker_auth,
    ),
    (
        "jwt",
        re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        lambda m: _marker("jwt"),
    ),
    (
        "authorization-header",
        re.compile(
            rb'(?P<pre>(?i:authorization)\\*"?\s*[:=]\s*\\*"?\s*(?i:bearer|basic|token)\s+)'
            rb"[A-Za-z0-9._~+/=-]{16,}"
        ),
        _keep_prefix("pre", "authorization-header"),
    ),
    (
        "provider-token",
        re.compile(
            rb"\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
            rb"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bglpat-[A-Za-z0-9_-]{20,}"
        ),
        lambda m: _marker("provider-token"),
    ),
    (
        # Only fields whose name says the value is a secret; fields that name a
        # Secret object ("secret", "secretName") are references, not values.
        "password-field",
        re.compile(
            rb'(?P<pre>\\*"(?i:password|passwd|api_?key|client_?secret|access_?token'
            rb'|auth_?token|bearer_?token|private_?key)\\*"\s*:\s*\\*")'
            rb'(?P<val>[^"\\]{8,})(?P<post>\\*")'
        ),
        _password_field,
    ),
]


# ── censoring one file ───────────────────────────────────────────────────────


class Patterns:
    """Known-value search patterns built once from the inventory."""

    def __init__(self, inventory: dict[bytes, str]) -> None:
        self.items = [
            (label, form, pattern, is_b64)
            for needle, label in sorted(inventory.items(), key=lambda kv: -len(kv[0]))
            for form, pattern, is_b64 in variants(needle)
        ]

    def present(self, data: bytes) -> list[str]:
        return sorted({label for label, _form, pattern, _b64 in self.items if pattern in data})


def censor_known(data: bytes, patterns: Patterns, hits: collections.Counter) -> bytes:
    for label, _form, pattern, is_b64 in patterns.items:
        if pattern not in data:
            continue
        if not is_b64:
            hits[f"known-value:{label}"] += data.count(pattern)
            data = data.replace(pattern, _marker(label))
            continue
        pieces, position = [], 0
        while (index := data.find(pattern, position)) >= 0:
            start, end = index, index + len(pattern)
            while start > position and data[start - 1] in _B64_CHARS:
                start -= 1
            while end < len(data) and (data[end] in _B64_CHARS or data[end] == _EQUALS):
                end += 1
            pieces += [data[position:start], _marker(label, "b64")]
            position = end
            hits[f"known-value:{label}"] += 1
        pieces.append(data[position:])
        data = b"".join(pieces)
    return data


def censor_shapes(data: bytes, hits: collections.Counter) -> bytes:
    for name, regex, replace in SHAPE_RULES:
        def substitute(match: re.Match[bytes], name=name, replace=replace) -> bytes:
            replacement = replace(match)
            if replacement is None:
                return match.group(0)
            hits[f"shape:{name}"] += 1
            return replacement
        data = regex.sub(substitute, data)
    return data


def censor_bytes(data: bytes, patterns: Patterns) -> tuple[bytes, collections.Counter]:
    hits: collections.Counter = collections.Counter()
    for _round in range(3):
        before = data
        data = censor_known(data, patterns, hits)
        data = censor_shapes(data, hits)
        if data == before:
            break
    return data, hits


def _archive_payload(data: bytes) -> bytes | None | bool:
    """Decompressed bytes of a known archive, None if not an archive, False if unreadable."""
    try:
        if data[:2] == b"\x1f\x8b":
            return gzip.GzipFile(fileobj=io.BytesIO(data)).read(MAX_FILE_BYTES + 1)
        if data[:3] == b"BZh":
            return bz2.decompress(data)
        if data[:6] == b"\xfd7zXZ\x00":
            return lzma.decompress(data)
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return b"\n".join(archive.read(name) for name in archive.namelist())
        if data[:4] in (b"\x28\xb5\x2f\xfd", b"7z\xbc\xaf", b"Rar!"):
            return False
    except Exception:
        return False
    return None


# ── whole directory ──────────────────────────────────────────────────────────


class Report:
    def __init__(self) -> None:
        self.censored: list[dict[str, object]] = []
        self.withheld: list[dict[str, str]] = []
        self.inventory: list[dict[str, object]] = []

    def found(self) -> bool:
        return bool(self.censored or self.withheld)

    def as_json(self) -> dict[str, object]:
        return {"censored": self.censored, "withheld": self.withheld, "inventory": self.inventory}


def process_directory(root: pathlib.Path, inventory: dict[bytes, str], check: bool = False) -> Report:
    patterns = Patterns(inventory)
    report = Report()
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if relative == REPORT_NAME or path.is_dir():
            continue
        if path.is_symlink():
            report.withheld.append({"file": relative, "reason": "symbolic link"})
            if not check:
                path.unlink()
            continue
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("larger than the inspection limit")
            data = path.read_bytes()
            payload = _archive_payload(data)
            if payload is False:
                raise ValueError("archive that could not be inspected")
            if payload is not None:
                if len(payload) > MAX_FILE_BYTES:
                    raise ValueError("archive larger than the inspection limit")
                _clean, hits = censor_bytes(payload, patterns)
                if hits:
                    report.withheld.append(
                        {"file": relative, "reason": "secret inside archive: " + ", ".join(sorted(hits))}
                    )
                    if not check:
                        path.unlink()
                continue
            cleaned, hits = censor_bytes(data, patterns)
            if hits:
                for rule, count in sorted(hits.items()):
                    report.censored.append({"file": relative, "rule": rule, "count": count})
                if check:
                    continue
                survivors = patterns.present(cleaned)
                if survivors:
                    path.unlink()
                    report.withheld.append(
                        {"file": relative, "reason": "known value survived censoring: " + ", ".join(survivors)}
                    )
                else:
                    path.write_bytes(cleaned)
        except (OSError, ValueError, MemoryError) as exc:
            report.withheld.append({"file": relative, "reason": f"{exc.__class__.__name__}: {exc}"})
            if not check:
                try:
                    path.unlink()
                except OSError:
                    pass
    return report


def build_inventory(args: argparse.Namespace) -> tuple[dict[bytes, str], list[dict[str, object]], int]:
    inventory: dict[bytes, str] = {}
    sources: list[dict[str, object]] = []
    for name in args.secret_file:
        path = pathlib.Path(name)
        if not path.is_file():
            print(f"{path}: not present, nothing to check against")
            continue
        try:
            found = needles_from_secret_file(path)
        except OSError as exc:
            print(f"{path}: unreadable ({exc.__class__.__name__})", file=sys.stderr)
            return {}, sources, 2
        if not found:
            print(f"{path}: no value of {MIN_LEN}+ characters to check against", file=sys.stderr)
            return {}, sources, 2
        inventory.update(found)
        sources.append({"source": path.name, "values": len(found)})
    for name in args.env:
        value = os.environ.get(name, "")
        if len(value) >= MIN_LEN:
            _add_string(value, f"env:{name}", inventory, 0)
            sources.append({"source": f"env:{name}", "values": 1})
        else:
            print(f"env {name}: unset or shorter than {MIN_LEN} characters, skipped")
    for name in args.k8s_secrets:
        path = pathlib.Path(name)
        if not path.is_file():
            print(f"{path}: not present, skipped")
            continue
        try:
            found = needles_from_k8s_secrets(path)
        except (OSError, ValueError) as exc:
            print(f"{path}: unreadable ({exc.__class__.__name__})", file=sys.stderr)
            return {}, sources, 2
        inventory.update(found)
        sources.append({"source": path.name, "values": len(found)})
    return inventory, sources, 0


def _write_summary(report: Report, check: bool) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary or not report.found():
        return
    lines = ["### Release gate: secrets " + ("found" if check else "censored before upload"), "",
             "| File | Rule | Count |", "|---|---|---|"]
    lines += [f"| `{c['file']}` | `{c['rule']}` | {c['count']} |" for c in report.censored]
    lines += [f"| `{w['file']}` | withheld | {w['reason']} |" for w in report.withheld]
    try:
        with open(summary, "a") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directory")
    parser.add_argument("--secret-file", action="append", default=[])
    parser.add_argument("--env", action="append", default=[], help="environment variable holding a secret")
    parser.add_argument("--k8s-secrets", action="append", default=[], help="`oc get secrets -o json` output")
    parser.add_argument("--report", help=f"report path (default: <directory>/{REPORT_NAME})")
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if anything is found")
    parser.add_argument("--fail-on-find", action="store_true", help="exit 1 if anything had to be censored")
    args = parser.parse_args(argv[1:])

    root = pathlib.Path(args.directory)
    if not root.is_dir():
        print(f"{root}: not a directory", file=sys.stderr)
        return 2
    inventory, sources, status = build_inventory(args)
    if status:
        return status

    report = process_directory(root, inventory, check=args.check)
    report.inventory = sources
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for entry in report.censored:
        print(f"{'::warning::' if in_actions else ''}{entry['file']}: {entry['rule']} x{entry['count']}"
              + (" (would be censored)" if args.check else " censored"))
    for entry in report.withheld:
        print(f"{'::warning::' if in_actions else ''}{entry['file']}: withheld: {entry['reason']}")
    _write_summary(report, args.check)
    if not args.check:
        report_path = pathlib.Path(args.report) if args.report else root / REPORT_NAME
        report_path.write_text(json.dumps(report.as_json(), indent=2) + "\n")
    values = sum(int(s["values"]) for s in sources)
    print(f"Release gate: {len(report.censored)} censoring(s), {len(report.withheld)} file(s) withheld, "
          f"{values} known value(s) checked")
    if report.found() and (args.check or args.fail_on_find):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

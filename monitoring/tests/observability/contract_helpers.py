"""Loaders and mutation helpers for the CI observability contract tests.

Validation itself lives in the contract package
(ci_observability.contracts.validation); the tests call those validators so
the contract is enforced by one implementation. What remains here is
loading example bundles and building deliberately broken copies of them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONTRACTS_DIR = TESTS_DIR.parent.parent / "ci_observability" / "contracts" / "v1"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"
NATIVE_DIR = EXAMPLES_DIR / "native"

MANIFEST_NAME = "bundle-manifest.json"

VALID_BUNDLES = sorted(path for path in EXAMPLES_DIR.iterdir() if path.is_dir() and (path / MANIFEST_NAME).is_file())


def bundle_path(name: str) -> Path:
    for bundle in VALID_BUNDLES:
        if bundle.name == name:
            return bundle
    raise KeyError(name)


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_journal(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_bundle_journal(bundle_name: str, journal_name: str) -> list[dict[str, object]]:
    return load_journal(bundle_path(bundle_name) / "journals" / f"{journal_name}.jsonl")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_bundle(bundle_name: str, destination: Path) -> Path:
    """Copy an example bundle into a test-owned directory."""
    bundle = destination / bundle_name
    shutil.copytree(bundle_path(bundle_name), bundle)
    return bundle


def rewrite_journal(bundle_dir: Path, relative: str, records: list[dict[str, object]]) -> None:
    """Replace a journal's records and write the manifest entry back.

    Tests that mutate journal content use this so a checksum or count
    mismatch never masks the specific violation under test.
    """
    path = bundle_dir / relative
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    manifest = load_json(bundle_dir / MANIFEST_NAME)
    for entry in manifest["files"]:
        if entry["path"] == relative:
            entry["sha256"] = sha256_file(path)
            entry["size_bytes"] = path.stat().st_size
            entry["record_count"] = len(records)
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def add_journal_entry(bundle_dir: Path, relative: str, raw_text: str) -> None:
    """Append a new journal file with raw bytes and list it in the manifest.

    raw_text is written verbatim, so tests can craft malformed lines,
    duplicate keys, non-finite numbers, and blank lines.
    """
    path = bundle_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw_text, encoding="utf-8")
    manifest = load_json(bundle_dir / MANIFEST_NAME)
    non_blank = len([line for line in raw_text.splitlines() if line.strip()])
    manifest["files"].append(
        {
            "path": relative,
            "kind": "journal",
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "record_count": non_blank,
            "complete": True,
        }
    )
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def replace_journal_with_records(tmp_path: Path, name: str, records: list[dict[str, object]]) -> Path:
    """A copy of the serial bundle whose whole journal is the given records.

    The manifest's job and suite stay, so an execution_started record built
    with manifest_echo keeps the identity echo consistent.
    """
    bundle = copy_bundle("serial", tmp_path / name)
    rewrite_journal(bundle, "journals/main.jsonl", records)
    return bundle


def manifest_echo(bundle_dir: Path) -> dict[str, object]:
    """The job and suite an execution_started record must echo."""
    manifest = load_json(bundle_dir / MANIFEST_NAME)
    return {"job": manifest["execution"]["job"], "suite": manifest["suite"]}


def set_manifest_field(bundle_dir: Path, keys: tuple[str | int, ...], value: object) -> None:
    """Set one manifest field, addressed by a path of keys and indices."""
    manifest = load_json(bundle_dir / MANIFEST_NAME)
    target = manifest
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def remove_manifest_field(bundle_dir: Path, keys: tuple[str | int, ...]) -> None:
    """Remove one manifest field, addressed by a path of keys and indices."""
    manifest = load_json(bundle_dir / MANIFEST_NAME)
    target = manifest
    for key in keys[:-1]:
        target = target[key]
    del target[keys[-1]]
    (bundle_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def make_record(event: str, data: dict[str, object], **overrides: object) -> dict[str, object]:
    """A minimal valid journal record for one event type."""
    record: dict[str, object] = {
        "schema": "osac.ci-observability.journal-record",
        "schema_version": 1,
        "execution_id": "11111111-2222-4333-8444-555555555555",
        "journal_id": "main",
        "sequence": 1,
        "recorded_at": "2026-10-27T10:00:00Z",
        "mono_ns": 1000,
        "event": event,
        "data": data,
    }
    record.update(overrides)
    return record


def make_manifest() -> dict[str, object]:
    """A minimal valid bundle manifest."""
    return {
        "schema": "osac.ci-observability.bundle-manifest",
        "schema_version": 1,
        "created_at": "2026-10-27T10:00:00Z",
        "producer": {"name": "test-producer", "version": "1.0.0"},
        "execution": {
            "execution_id": "11111111-2222-4333-8444-555555555555",
            "repository_id": 812345678,
            "repository": "osac-project/osac",
            "workflow": {"path": ".github/workflows/tests.yml", "revision": "0" * 40},
            "run_id": 1,
            "run_attempt": 1,
            "event": "push",
            "job": {"logical_job_key": "tests.yml#unit", "github_job_id": 1},
        },
        "suite": {"framework": "pytest", "tier": "unit", "component": "e2e", "variant": "unit"},
        "environment": {
            "sources": [
                {"role": "tested", "repository": "osac-project/osac", "revision": "0" * 40},
                {"role": "workflow", "repository": "osac-project/osac", "revision": "0" * 40},
            ]
        },
        "files": [
            {
                "path": "journals/main.jsonl",
                "kind": "journal",
                "sha256": "0" * 64,
                "size_bytes": 0,
                "record_count": 0,
                "complete": True,
            }
        ],
        "completeness": {"complete": True, "end_reason": "finished", "dropped_records": 0, "collection_errors": []},
        "redaction": {"applied": True, "findings": 0},
    }

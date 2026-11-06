#!/usr/bin/env python3
"""Replay downloaded, local run logs through the production shadow extractor.

Raw logs and artifacts stay outside the repository. This script copies only
the bounded files the extractor uses into a temporary directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import zipfile
from pathlib import Path
from types import ModuleType


def load_extractor(repo: Path) -> ModuleType:
    script = repo / ".github/scripts/laya-shadow.py"
    spec = importlib.util.spec_from_file_location("laya_shadow", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_artifact(archive: Path, target: Path, extractor: ModuleType) -> bool:
    if not archive.is_file():
        return False
    with zipfile.ZipFile(archive) as source:
        copied_bytes = 0
        copied_files = 0
        for member in source.infolist():
            name = member.filename
            if (
                not extractor.artifact_member_relevant(name)
                or member.file_size > extractor.MAX_SOURCE_BYTES
                or member.is_dir()
                or copied_bytes + member.file_size > extractor.MAX_ARTIFACT_BYTES
                or copied_files >= extractor.MAX_ARTIFACT_FILES
            ):
                continue
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as incoming, path.open("wb") as outgoing:
                remaining = member.file_size
                while remaining:
                    chunk = incoming.read(min(65536, remaining))
                    if not chunk:
                        break
                    outgoing.write(chunk)
                    remaining -= len(chunk)
            copied_bytes += member.file_size
            copied_files += 1
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    extractor = load_extractor(repo)
    cases = json.loads(args.cases.read_text())
    missing_logs = 0
    missing_artifacts = 0
    missing_evidence = 0
    for case in cases:
        by_pr = args.raw_root / str(case["pr"])
        by_run = args.raw_root / "new_candidates" / str(case["run_id"])
        source = by_pr if (by_pr / "job.log").is_file() else by_run
        if not (source / "job.log").is_file():
            missing_logs += 1
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory)
            if not copy_artifact(source / "artifact.zip", artifact_dir, extractor):
                missing_artifacts += 1
            evidence = extractor.extract_state(
                artifact_dir, artifact_dir / "junit.xml", source / "job.log", case["state"]["failed_step"]
            )
            case["state"] = evidence["state"]
            case["extractor_version"] = extractor.EXTRACTOR_VERSION
            case["evidence_available"] = evidence["evidence_available"]
            case["evidence_refs"] = evidence["evidence_refs"]
            if not evidence["evidence_available"]:
                missing_evidence += 1
    args.output.write_text(json.dumps(cases, indent=2) + "\n")
    print(
        f"runs={len(cases)} missing_logs={missing_logs} "
        f"missing_artifacts={missing_artifacts} missing_evidence={missing_evidence}"
    )


if __name__ == "__main__":
    main()

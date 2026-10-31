#!/usr/bin/env python3
"""Validate literal local COPY/ADD sources against a container build context."""

from __future__ import annotations

import argparse
import glob
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class LocalSource:
    path: str
    line: int


def logical_instructions(containerfile: Path) -> list[tuple[int, str]]:
    instructions: list[tuple[int, str]] = []
    parts: list[str] = []
    start_line = 0

    for line_number, raw_line in enumerate(containerfile.read_text().splitlines(), 1):
        stripped = raw_line.strip()
        if not parts and (not stripped or stripped.startswith("#")):
            continue
        if not parts:
            start_line = line_number

        continued = raw_line.rstrip().endswith("\\")
        parts.append(raw_line.rstrip()[:-1] if continued else raw_line)
        if not continued:
            instructions.append((start_line, "\n".join(parts).strip()))
            parts = []

    if parts:
        instructions.append((start_line, "\n".join(parts).strip()))
    return instructions


def _strip_options(arguments: str) -> tuple[list[str], str]:
    options: list[str] = []
    remainder = arguments.lstrip()
    while remainder.startswith("--"):
        lexer = shlex.shlex(remainder, posix=True)
        lexer.whitespace_split = True
        option = next(lexer)
        options.append(option)
        remainder = remainder[len(option) :].lstrip()
    return options, remainder


def local_sources(containerfile: Path) -> list[LocalSource]:
    sources: list[LocalSource] = []
    for line_number, instruction in logical_instructions(containerfile):
        fields = instruction.split(maxsplit=1)
        if len(fields) != 2:
            continue
        command, arguments = fields
        if command.upper() not in {"COPY", "ADD"}:
            continue

        options, remainder = _strip_options(arguments)
        if any(option == "--from" or option.startswith("--from=") for option in options):
            continue

        if remainder.startswith("["):
            try:
                paths = json.loads(remainder)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {line_number}: invalid JSON-form {command.upper()}: {error.msg}") from error
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                raise ValueError(f"line {line_number}: JSON-form {command.upper()} must be an array of strings")
        else:
            try:
                paths = shlex.split(remainder, posix=True)
            except ValueError as error:
                raise ValueError(f"line {line_number}: invalid {command.upper()}: {error}") from error

        if len(paths) < 2:
            raise ValueError(f"line {line_number}: {command.upper()} requires at least one source and a destination")
        for path in paths[:-1]:
            if path.startswith("<<") or (command.upper() == "ADD" and path.startswith(("http://", "https://"))):
                continue
            sources.append(LocalSource(path=path, line=line_number))
    return sources


def _context_relative(source: str) -> Path:
    # Container builders resolve parent components lexically and clamp them at
    # the context root.  Do not resolve against the filesystem: a preceding
    # symlink must not change how the source path is normalized.
    parts: list[str] = []
    for part in PurePosixPath(source).parts:
        if part in {"/", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return Path(*parts)


def _source_exists(context: Path, source: str) -> bool:
    candidate = context / _context_relative(source)
    if glob.has_magic(source):
        return bool(glob.glob(str(candidate), recursive=True, include_hidden=True))
    return candidate.exists()


def validate(repo_root: Path, containerfile_name: str, context_name: str) -> list[str]:
    repo_root = repo_root.resolve()
    containerfile = (repo_root / containerfile_name).resolve()
    context = (repo_root / context_name).resolve()

    try:
        containerfile.relative_to(repo_root)
        context.relative_to(repo_root)
    except ValueError as error:
        raise ValueError("containerfile and context must stay within the component repository") from error
    if not containerfile.is_file():
        raise ValueError(f"containerfile '{containerfile_name}' does not exist")
    if not context.is_dir():
        raise ValueError(f"build context '{context_name}' does not exist or is not a directory")

    errors: list[str] = []
    for source in local_sources(containerfile):
        # Build arguments can affect variable-containing sources, so leave
        # those to the builder rather than rejecting a potentially valid use.
        if "$" in source.path or _source_exists(context, source.path):
            continue
        message = f"COPY/ADD source '{source.path}' (line {source.line}) is missing from build context '{context_name}'"
        if _source_exists(repo_root, source.path) and context != repo_root:
            message += "; the source exists at the repository root, so use context '.'"
        errors.append(message)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--containerfile", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--label", default="component")
    args = parser.parse_args()

    try:
        errors = validate(args.repo_root, args.containerfile, args.context)
    except (OSError, ValueError) as error:
        print(f"::error::{args.label}: {error}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"::error::{args.label}: {args.containerfile}: {error}", file=sys.stderr)
        return 1

    print(f"Validated {args.label}: {args.containerfile} with context '{args.context}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

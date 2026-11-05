#!/usr/bin/env python3
"""Tests for the authorize-fork-pr action: who may run the E2E of a fork pull request.

Focus: the cherry-pick robot is not an organization member, so its pull requests were blocked; it is trusted for a pull
request from its own fork to a release-* branch, and for nothing else. The shell of the step runs here with a stand-in
`curl` that answers from the environment.

Run directly: python3 .github/scripts/test_authorize_fork_pr.py
Stdlib unittest only.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ACTION = Path(__file__).resolve().parents[1] / "actions" / "authorize-fork-pr" / "action.yml"
BOT = "openshift-cherrypick-robot"

CURL = r"""#!/bin/bash
out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) fmt="$2"; shift 2 ;;
    -H) shift 2 ;;
    -s) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "$url" in
  */issues/*/labels) body="$LABELS_JSON"; code=200 ;;
  */orgs/osac-project/members/*)
    login="${url##*/}"; body=""
    if [[ ",$MEMBERS," == *",$login,"* ]]; then code=204; else code=404; fi ;;
  */pulls/*) body="$PULL_JSON"; code=200 ;;
  *) body=""; code=404 ;;
esac
if [ -n "$out" ]; then printf '%s' "$body" > "$out"; elif [ -z "$fmt" ]; then printf '%s' "$body"; fi
if [ -n "$fmt" ]; then printf '%s' "$code"; fi
"""


def step_script() -> str:
    """The `run` block of the step "Validate fork PR authorization", as text (no YAML parser needed)."""
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "name: Validate fork PR authorization" in line)
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run + 1]) - len(lines[run + 1].lstrip())
    block: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        block.append(line[indent:] if line.strip() else "")
    return "\n".join(block)


def run(
    *,
    author: str,
    base: str = "release-0.4",
    head_owner: str = BOT,
    labels: tuple[str, ...] = (),
    members: tuple[str, ...] = (),
    trusted: str = BOT,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "curl"
        fake.write_text(CURL, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "RUNNER_TEMP": tmp,
            "PR_AUTHOR": author,
            "PR_NUMBER": "7",
            "PR_REPO": "osac-project/osac",
            "GH_TOKEN": "t",
            "TRUSTED_AUTHORS": trusted,
            "LABELS_JSON": json.dumps([{"name": n} for n in labels]),
            "MEMBERS": ",".join(members),
            "PULL_JSON": json.dumps({"base": {"ref": base}, "head": {"repo": {"owner": {"login": head_owner}}}}),
        }
        return subprocess.run(["bash", "-e", "-c", step_script()], env=env, capture_output=True, text=True, check=False)


class AuthorizeForkPr(unittest.TestCase):
    def test_the_step_is_found(self) -> None:
        self.assertIn("Check ok-to-test label", step_script())

    def test_the_cherry_pick_robot_is_trusted_for_its_own_fork_to_a_release_branch(self) -> None:
        for base in ("release-0.4", "release-1.12"):
            done = run(author=BOT, base=base)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("trusted cherry-pick author", done.stdout)

    def test_the_robot_is_not_trusted_for_anything_else(self) -> None:
        for kwargs in (
            {"base": "main"},
            {"base": "releases"},  # not a release-<number> branch
            {"head_owner": "somebody-else"},  # not its own fork
            {"trusted": ""},  # nothing is trusted
            {"trusted": "openshift-cherrypick-robot; echo hi"},  # a list that is not a list of logins
            {"trusted": "someone-else"},
        ):
            done = run(author=BOT, **kwargs)  # type: ignore[arg-type]
            self.assertEqual(done.returncode, 1, f"{kwargs}: {done.stdout}")
            self.assertIn("Fork PR blocked", done.stdout)

    def test_another_author_is_not_trusted_by_the_list(self) -> None:
        done = run(author="somebody", head_owner="somebody")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Fork PR blocked: 'somebody'", done.stdout)

    def test_the_ways_that_were_there_before_still_work(self) -> None:
        self.assertEqual(run(author="somebody", head_owner="somebody", labels=("ok-to-test",)).returncode, 0)
        self.assertEqual(run(author="member", head_owner="member", members=("member",)).returncode, 0)
        done = run(author="app[bot]", head_owner="osac-dev-bot", members=("osac-dev-bot",))
        self.assertEqual(done.returncode, 0)
        self.assertIn("fork owner", done.stdout)


if __name__ == "__main__":
    unittest.main()

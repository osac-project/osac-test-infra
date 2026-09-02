#!/usr/bin/env python3
"""Track recurring E2E failures as candidates for known-issues/ review.

Never writes to known-issues/ itself. Opens or bumps a tracking issue
(labeled ai-candidate-known-issue) in osac-test-infra so a human notices a
pattern and decides whether to promote it to a real known-issues/*.md
entry -- the AI diagnostic pipeline never gets write access to the trusted
corpus it reads from, only to this review queue.

Always tracks in osac-test-infra (TRACKING_REPO), regardless of which repo
(osac or osac-test-infra) the actual failing PR lives in, since that's
also where known-issues/ itself lives -- one review queue for the one
corpus, rather than splitting candidates across repos.
"""
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

TRACKING_REPO = "osac-project/osac-test-infra"
CANDIDATE_LABEL = "ai-candidate-known-issue"
REVIEW_LABEL = "needs-review"
PROMOTION_THRESHOLD = 3
MAX_EXCERPT_CHARS = 500
MAX_TEST_NAME_LEN = 200

JUNIT_PATH = os.environ.get("JUNIT_PATH", "")
DIAGNOSIS_FILE = os.environ.get("DIAGNOSIS_FILE", "")
SOURCE_REPO = os.environ.get("REPO", "")
PR_NUMBER = os.environ.get("PR_NUMBER", "")
RUN_URL = os.environ.get("RUN_URL", "")
WORKFLOW_NAME = os.environ.get("WORKFLOW_NAME", "")

OCCURRENCES_PATTERN = re.compile(r"\*\*Occurrences:\*\*\s*(\d+)")


def gh_api(method, path, payload=None):
    """Talk to the GitHub API via `gh api ... --input -` (JSON on stdin),
    never `-f`/`-F` fields: those treat any value starting with '@' as
    "read this local file", and the title/body built below embed a fork
    PR's own JUnit test name and diagnosis text -- both attacker-
    controlled for pull_request-triggered runs, since E2E tests execute
    from the fork's own checkout. JSON-via-stdin has no such magic-prefix
    behavior, so there's nothing for a crafted test name to trigger.
    """
    cmd = ["gh", "api", path]
    if method != "GET":
        cmd += ["-X", method, "--input", "-"]
    result = subprocess.run(
        cmd,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} failed: {result.stderr.strip()}")
    return result.stdout


def first_failing_test_name(path):
    """The only signature this script trusts: a specific pytest test name
    from junit.xml. Free-text log/AAP signals vary run to run (timestamps,
    job IDs) and would mint a fresh "signature" almost every time,
    defeating the point of recurrence tracking -- so if there's no JUnit
    failure, this intentionally tracks nothing rather than guessing.
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return None
    for testcase in tree.getroot().iter("testcase"):
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            return testcase.get("name", "")[:MAX_TEST_NAME_LEN]
    return None


def find_existing(signature_title):
    raw = gh_api(
        "GET",
        f"repos/{TRACKING_REPO}/issues?labels={CANDIDATE_LABEL}&state=open&per_page=100",
    )
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for issue in issues:
        if issue.get("title") == signature_title:
            return issue
    return None


def main():
    test_name = first_failing_test_name(JUNIT_PATH)
    if not test_name:
        print("No failing testcase name in junit.xml -- no stable signature, skipping recurrence tracking.")
        return

    signature = f"{WORKFLOW_NAME}: {test_name}"
    title = f"AI candidate known issue: {signature}"

    excerpt = "(no diagnosis text)"
    if DIAGNOSIS_FILE and os.path.isfile(DIAGNOSIS_FILE):
        with open(DIAGNOSIS_FILE, "r", errors="replace") as f:
            excerpt = f.read().strip()[:MAX_EXCERPT_CHARS]

    occurrence_note = (
        f"New occurrence:\n"
        f"- Repo/PR: {SOURCE_REPO}#{PR_NUMBER}\n"
        f"- Run: {RUN_URL}\n\n"
        f"> {excerpt}"
    )

    try:
        existing = find_existing(title)
    except RuntimeError as exc:
        print(f"Failed to list tracking issues, skipping: {exc}", file=sys.stderr)
        return

    try:
        if existing is None:
            body = (
                "Automatically tracked by the AI diagnostic pipeline -- opened "
                "when this test failed with no existing tracking issue found. "
                "Nothing here is auto-promoted: a human reviews and decides "
                "whether to add a real entry to this repo's known-issues/ "
                "(see known-issues/INDEX.md).\n\n"
                f"**Signature:** `{signature}`\n\n"
                "**Occurrences:** 1"
            )
            created = json.loads(gh_api(
                "POST",
                f"repos/{TRACKING_REPO}/issues",
                {"title": title, "body": body, "labels": [CANDIDATE_LABEL]},
            ))
            number = created["number"]
            gh_api("POST", f"repos/{TRACKING_REPO}/issues/{number}/comments", {"body": occurrence_note})
            print(f"Opened new tracking issue #{number} for signature: {signature}")
            return

        number = existing["number"]
        body = existing.get("body") or ""
        match = OCCURRENCES_PATTERN.search(body)
        count = int(match.group(1)) if match else 1
        new_count = count + 1
        if match:
            new_body = OCCURRENCES_PATTERN.sub(f"**Occurrences:** {new_count}", body, count=1)
        else:
            new_body = body + f"\n\n**Occurrences:** {new_count}"

        gh_api("PATCH", f"repos/{TRACKING_REPO}/issues/{number}", {"body": new_body})
        gh_api("POST", f"repos/{TRACKING_REPO}/issues/{number}/comments", {"body": occurrence_note})
        print(f"Bumped tracking issue #{number} to {new_count} occurrences for signature: {signature}")

        if new_count >= PROMOTION_THRESHOLD:
            # Static args only (repo/label constants, an int) -- safe as
            # plain subprocess argv, unlike the title/body payloads above.
            subprocess.run(
                [
                    "gh", "label", "create", REVIEW_LABEL,
                    "--repo", TRACKING_REPO,
                    "--color", "d93f0b",
                    "--description", "Flagged by the AI diagnostic pipeline for human review",
                    "--force",
                ],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", TRACKING_REPO, "--add-label", REVIEW_LABEL],
                capture_output=True, text=True,
            )
            print(f"Issue #{number} crossed {PROMOTION_THRESHOLD} occurrences -- flagged '{REVIEW_LABEL}'.")
    except RuntimeError as exc:
        print(f"Failed to update tracking issue, skipping: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

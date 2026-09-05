#!/usr/bin/env python3
"""Track recurring E2E failures as candidates for the known-issues memory.

The known-issues memory IS a set of GitHub Issues in osac-test-infra
labeled confirmed-known-issue (see ai-diagnose-failure.py's KNOWN_ISSUES
loading) -- there is no separate file corpus. This script only ever
manages the CANDIDATE_LABEL review queue below it: it opens or bumps a
tracking issue when the same JUnit test fails again, and flags it
REVIEW_LABEL once it crosses PROMOTION_THRESHOLD occurrences. It never
applies CONFIRMED_LABEL itself -- that's a deliberate human action (a
maintainer reviews the flagged issue, edits its body into a real
symptom/root-cause writeup if needed, and applies the label) -- so the AI
diagnostic pipeline never gets write access to the corpus it reads from,
only to this review queue.

Always tracks in osac-test-infra (TRACKING_REPO), regardless of which repo
(osac or osac-test-infra) the actual failing PR lives in, since that's
also where the confirmed-known-issue corpus itself lives -- one review
queue for the one corpus, rather than splitting candidates across repos.

Nothing here ever closes an issue it opens -- that's handled separately by
cleanup-ai-candidate-issues.yml, a daily scheduled job that closes
candidates gone quiet for 14+ days (skipping anything labeled needs-review
or confirmed-known-issue), so this review queue doesn't grow unbounded.
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
# Applied only by a human, never by this script -- see module docstring.
# Created proactively (ensure_labels()) purely so it's available to pick
# from the GitHub UI immediately, without needing separate repo setup.
CONFIRMED_LABEL = "confirmed-known-issue"
PROMOTION_THRESHOLD = 3
# Every auto-generated issue gets both this title prefix (visible at a
# glance in the Issues list, no filter needed) and the CANDIDATE_LABEL
# (the actual, code-level filter -- see find_existing() -- since a title
# string could in principle collide with something a human writes by
# hand). Author is also always the minted OSAC AI bot token's account
# (osac-ai[bot]) for free, as a third way to isolate these (`gh issue list
# --author <bot-login>`), with no extra code required.
TITLE_PREFIX = "[AI Diagnostic]"
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

    GET requests always add --paginate --slurp: without it, a candidate
    search silently misses any match beyond the first 100 open issues,
    risking a duplicate issue for a signature that already exists further
    back. --slurp does NOT flatten across pages the way its own docs
    imply -- confirmed empirically, it wraps each individual page's own
    JSON array as one element of an outer list, even for a single page --
    so callers must flatten the result themselves (see find_existing()).
    """
    cmd = ["gh", "api", path]
    if method == "GET":
        cmd += ["--paginate", "--slurp"]
    else:
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


def current_login():
    result = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api user failed: {result.stderr.strip()}")
    return result.stdout.strip()


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


def ensure_label(name, color, description):
    # Static args only (our own constants) -- safe as plain subprocess
    # argv, unlike the title/body payloads built from evidence below.
    # --force makes this idempotent (updates color/description if the
    # label already exists rather than erroring), so it's cheap to call
    # unconditionally every run rather than checking existence first.
    subprocess.run(
        ["gh", "label", "create", name, "--repo", TRACKING_REPO,
         "--color", color, "--description", description, "--force"],
        capture_output=True, text=True,
    )


def find_existing(signature_title, bot_login):
    # creator=bot_login is defense-in-depth, not the primary guard (only
    # write-access holders can apply CANDIDATE_LABEL at all, and a random
    # public user opening an issue in this public repo can't self-label
    # it) -- but it means even a maintainer mistakenly labeling someone
    # else's unrelated issue can never feed it into this automation.
    raw = gh_api(
        "GET",
        f"repos/{TRACKING_REPO}/issues?labels={CANDIDATE_LABEL}&creator={bot_login}&state=open&per_page=100",
    )
    try:
        pages = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for page in pages:
        for issue in page:
            if issue.get("title") != signature_title:
                continue
            # Never mutate an already-promoted record: if a maintainer
            # confirmed this exact issue (typically without also removing
            # CANDIDATE_LABEL), treat it as "not found" so a fresh
            # recurrence opens a new, separate candidate instead of
            # silently bumping/overwriting the confirmed writeup's body.
            label_names = [label.get("name") for label in issue.get("labels", [])]
            if CONFIRMED_LABEL in label_names:
                continue
            return issue
    return None


def main():
    test_name = first_failing_test_name(JUNIT_PATH)
    if not test_name:
        print("No failing testcase name in junit.xml -- no stable signature, skipping recurrence tracking.")
        return

    ensure_label(CANDIDATE_LABEL, "fbca04", "Auto-tracked by the AI diagnostic pipeline, pending human review")
    ensure_label(CONFIRMED_LABEL, "0e8a16", "Confirmed known issue -- fed into every AI diagnosis prompt")

    signature = f"{WORKFLOW_NAME}: {test_name}"
    title = f"{TITLE_PREFIX} {signature}"

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

    # Known, accepted race: this find-then-create is not atomic, so two
    # different PRs hitting the exact same failing test within the same
    # few seconds could each find nothing and both create an issue for
    # the same signature. Not fixed with a lock/CAS mechanism -- GitHub's
    # Issues API has no conditional-create support, this repo's own
    # concurrency group already serializes same-PR runs (the only
    # realistic repeat trigger), and the failure mode is a harmless
    # duplicate low-stakes tracking issue a human can merge/close, not a
    # correctness or security issue for the diagnoses themselves.
    try:
        bot_login = current_login()
        existing = find_existing(title, bot_login)
    except RuntimeError as exc:
        print(f"Failed to list tracking issues, skipping: {exc}", file=sys.stderr)
        return

    try:
        if existing is None:
            body = (
                "Automatically tracked by the AI diagnostic pipeline -- opened "
                "when this test failed with no existing tracking issue found. "
                "Nothing here is auto-promoted: if this is a real recurring "
                f"issue, edit this description into a proper symptom/root-"
                f"cause writeup and add the `{CONFIRMED_LABEL}` label -- every "
                "issue with that label is fed into future AI diagnoses. "
                "Leave unlabeled (or close) to ignore.\n\n"
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
            ensure_label(REVIEW_LABEL, "d93f0b", "Flagged by the AI diagnostic pipeline for human review")
            subprocess.run(
                ["gh", "issue", "edit", str(number), "--repo", TRACKING_REPO, "--add-label", REVIEW_LABEL],
                capture_output=True, text=True,
            )
            print(f"Issue #{number} crossed {PROMOTION_THRESHOLD} occurrences -- flagged '{REVIEW_LABEL}'.")
    except RuntimeError as exc:
        print(f"Failed to update tracking issue, skipping: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

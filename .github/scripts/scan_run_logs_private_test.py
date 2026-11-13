#!/usr/bin/env python3
"""scan-run-logs.sh with artifacts published to the private artifact repository.

Runs the real script with a stubbed curl (answers from a routes table and
logs every request) and a stubbed gitleaks container engine.
"""

from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPTS = pathlib.Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "scan-run-logs.sh"
API = "https://api.github.com"
REPO = "osac-project/osac"
PRIVATE = "osac-project/osac-ci-artifacts"
RUN = "100"
SOURCE_TOKEN = "source-token"
PRIVATE_TOKEN = "private-token"

CURL = r"""#!__PYTHON__
import json, os, sys
args = sys.argv[1:]
out, method, url, query, auth, i = None, "GET", None, [], "", 0
while i < len(args):
    a = args[i]
    if a == "-o": out = args[i + 1]; i += 2
    elif a in ("-w", "--connect-timeout", "--max-time"): i += 2
    elif a == "-X": method = args[i + 1]; i += 2
    elif a == "-H":
        if args[i + 1].startswith("Authorization:"): auth = args[i + 1].split("Bearer ", 1)[1]
        i += 2
    elif a == "--data-urlencode": query.append(args[i + 1]); i += 2
    elif a.startswith("-"): i += 1
    else: url = a; i += 1
path = url.replace("https://api.github.com", "")
with open(os.environ["REQUEST_LOG"], "a") as log:
    log.write(json.dumps({"method": method, "path": path, "query": query, "token": auth}) + "\n")
routes_file = os.environ["ROUTES"]
routes = json.load(open(routes_file))
route = routes.get(f"{method} {path}") or {"status": 404, "body": {}}
if isinstance(route, list):
    # Answered in turn, the last one repeating.
    key = f"{method} {path}"
    route = routes[key][0] if len(routes[key]) == 1 else routes[key].pop(0)
    json.dump(routes, open(routes_file, "w"))
if out and out != "/dev/null":
    if "zip_b64" in route:
        import base64
        open(out, "wb").write(base64.b64decode(route["zip_b64"]))
    else:
        open(out, "w").write(json.dumps(route.get("body", {})))
print(route["status"], end="")
"""

ENGINE = r"""#!/bin/bash
# Stub gitleaks: reports a finding for any scanned file that contains LEAK.
scan="" out="" report=""
for a in "$@"; do
  case "$a" in
    *:/scan:ro*) scan=${a%%:/scan*} ;;
    *:/out:rw*) out=${a%%:/out*} ;;
    --report-path=*) report=${a#--report-path=/out/} ;;
  esac
done
findings=()
while IFS= read -r f; do
  rel=${f#"$scan"}
  findings+=("{\"RuleID\":\"generic-api-key\",\"File\":\"${rel}\",\"StartLine\":2,\"Secret\":\"LEAKVALUE\"}")
done < <(grep -rl LEAK "$scan" 2>/dev/null || true)
printf '[%s]' "$(IFS=,; echo "${findings[*]}")" > "$out/$report"
"""


def zip_b64(files: dict[str, str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return base64.b64encode(buffer.getvalue()).decode()


class ScanPrivateArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.temp = pathlib.Path(self.temporary.name)
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(CURL.replace("__PYTHON__", sys.executable))
        (self.bin / "curl").chmod(0o755)
        (self.bin / "sleep").write_text('#!/bin/bash\necho "$*" >> "$SLEEP_LOG"\n')
        (self.bin / "sleep").chmod(0o755)
        self.sleeps = self.temp / "sleeps.log"
        (self.bin / "engine").write_text(ENGINE)
        (self.bin / "engine").chmod(0o755)
        self.requests = self.temp / "requests.log"
        self.routes_file = self.temp / "routes.json"
        self.output = self.temp / "out"
        # The run: no logs (zero jobs), no artifacts of its own, one collect
        # run in the private repository that published one artifact.
        self.routes = {
            f"GET /repos/{REPO}/actions/runs/{RUN}/logs": {"status": 404},
            f"GET /repos/{REPO}/actions/runs/{RUN}/jobs": {"status": 200, "body": {"total_count": 0}},
            f"GET /repos/{REPO}/actions/runs/{RUN}/artifacts": {"status": 200, "body": {"artifacts": []}},
            f"GET /repos/{REPO}/actions/runs/{RUN}": {"status": 200, "body": {"created_at": "2026-11-13T10:00:00Z"}},
            f"GET /repos/{PRIVATE}/actions/workflows/collect.yml/runs": {
                "status": 200,
                "body": {
                    "workflow_runs": [
                        {"id": 9001, "status": "completed", "display_title": f"collect {REPO}#{RUN}.1 - e2e vmaas / e2e"},
                        {"id": 9002, "status": "completed", "display_title": f"collect {REPO}#{RUN}1.1 - lookalike run id"},
                        {"id": 9003, "status": "completed", "display_title": f"collect other/repo#{RUN}.1 - other repository"},
                    ]
                },
            },
            f"GET /repos/{PRIVATE}/actions/runs/9001/artifacts": {
                "status": 200,
                "body": {"artifacts": [{"id": 77, "name": "osac-logs-100", "expired": False, "size_in_bytes": 10}]},
            },
            f"GET /repos/{PRIVATE}/actions/artifacts/77/zip": {
                "status": 200,
                "zip_b64": zip_b64({"log.txt": "all fine\n"}),
            },
            f"DELETE /repos/{PRIVATE}/actions/artifacts/77": {"status": 204},
        }

    def run_script(self, *, private_repo: str | None = PRIVATE, private_token: str | None = PRIVATE_TOKEN):
        self.routes_file.write_text(json.dumps(self.routes))
        environment = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.temp),
            "GH_TOKEN": SOURCE_TOKEN,
            "GITHUB_API_URL": API,
            "CONTAINER_ENGINE": str(self.bin / "engine"),
            "REQUEST_LOG": str(self.requests),
            "SLEEP_LOG": str(self.sleeps),
            "ROUTES": str(self.routes_file),
            "PRIVATE_COLLECT_WAIT_SECONDS": getattr(self, "wait_seconds", "300"),
        }
        if private_repo is not None:
            environment["PRIVATE_ARTIFACT_REPO"] = private_repo
        if private_token is not None:
            environment["PRIVATE_GH_TOKEN"] = private_token
        result = subprocess.run(
            ["bash", str(SCRIPT), RUN, str(self.output), REPO],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        status = dict(
            line.split("=", 1) for line in (self.output / "status.env").read_text().splitlines() if "=" in line
        )
        return result, status

    def calls(self) -> list[dict]:
        if not self.requests.exists():
            return []
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def calls_exact(self, path: str) -> list[dict]:
        return [call for call in self.calls() if call["path"] == path]

    def calls_to(self, fragment: str) -> list[dict]:
        return [call for call in self.calls() if fragment in call["path"]]

    def test_a_clean_private_artifact_is_scanned_with_the_private_token(self) -> None:
        result, status = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            (status["SCAN_OK"], status["LEAKS_FOUND"], status["PURGE_OK"], status["FINDINGS_COUNT"]),
            ("true", "false", "true", "0"),
        )
        download = self.calls_to("/artifacts/77/zip")
        self.assertEqual(len(download), 1)
        self.assertEqual(download[0]["token"], PRIVATE_TOKEN)
        # Only the run's own calls use the source token.
        for call in self.calls_to(f"/repos/{REPO}/"):
            self.assertEqual(call["token"], SOURCE_TOKEN, call)
        self.assertEqual([c for c in self.calls() if c["method"] == "DELETE"], [])

    def test_collect_runs_are_listed_since_the_run_started(self) -> None:
        self.run_script()
        listing = self.calls_to("/workflows/collect.yml/runs")[0]
        self.assertIn("created=>=2026-11-13T10:00:00Z", listing["query"])
        self.assertEqual(listing["token"], PRIVATE_TOKEN)

    def test_only_this_runs_collect_runs_are_read(self) -> None:
        self.run_script()
        # 9002 is another run whose id starts with this one's; 9003 another repository.
        self.assertEqual(self.calls_to("/runs/9002"), [])
        self.assertEqual(self.calls_to("/runs/9003"), [])
        self.assertEqual(len(self.calls_to("/runs/9001/artifacts")), 1)

    def test_a_leak_in_a_private_artifact_is_deleted_and_never_republished(self) -> None:
        self.routes[f"GET /repos/{PRIVATE}/actions/artifacts/77/zip"]["zip_b64"] = zip_b64(
            {"kubeconfig": "token: LEAKVALUE\n", "log.txt": "ordinary\n"}
        )
        result, status = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((status["LEAKS_FOUND"], status["PURGE_OK"], status["FINDINGS_COUNT"]), ("true", "true", "1"))
        deletes = [c for c in self.calls() if c["method"] == "DELETE"]
        self.assertEqual([(c["path"], c["token"]) for c in deletes], [(f"/repos/{PRIVATE}/actions/artifacts/77", PRIVATE_TOKEN)])
        # The caller uploads redacted/ as a public artifact: nothing of the
        # private artifact may be in it, and the secret is in no output.
        redacted = self.output / "redacted"
        self.assertFalse(redacted.exists() and any(redacted.rglob("*")), list(redacted.rglob("*")) if redacted.exists() else "")
        findings = (self.output / "findings.json").read_text()
        self.assertIn("generic-api-key", findings)
        self.assertIn("artifacts/", findings)
        self.assertNotIn("LEAKVALUE", findings)
        # The only place the value is printed is the runner's mask directive.
        for line in (result.stdout + result.stderr).splitlines():
            if "LEAKVALUE" in line:
                self.assertTrue(line.startswith("::add-mask::"), line)

    def test_a_failed_delete_leaves_the_exposure_open(self) -> None:
        self.routes[f"GET /repos/{PRIVATE}/actions/artifacts/77/zip"]["zip_b64"] = zip_b64({"kubeconfig": "LEAK\n"})
        self.routes[f"DELETE /repos/{PRIVATE}/actions/artifacts/77"] = {"status": 403}
        _, status = self.run_script()
        self.assertEqual((status["LEAKS_FOUND"], status["PURGE_OK"]), ("true", "false"))

    def test_the_repository_without_a_token_is_a_failed_scan(self) -> None:
        result, status = self.run_script(private_token=None)
        self.assertEqual(status["SCAN_OK"], "false", result.stdout)
        self.assertEqual(self.calls_to(PRIVATE), [])

    def test_a_listing_that_fails_is_a_failed_scan_not_a_clean_one(self) -> None:
        self.routes[f"GET /repos/{PRIVATE}/actions/workflows/collect.yml/runs"] = {"status": 403}
        result, status = self.run_script()
        self.assertEqual(status["SCAN_OK"], "false")
        self.assertIn("Could not list collect runs", result.stdout)
        self.assertEqual([c for c in self.calls() if c["method"] == "DELETE"], [])

    def test_without_the_private_repository_nothing_private_is_asked(self) -> None:
        _, status = self.run_script(private_repo=None, private_token=None)
        self.assertEqual(status["SCAN_OK"], "true")
        self.assertEqual(self.calls_to(PRIVATE), [])
        # The run itself is only read to date the private lookup, which was not made.
        self.assertEqual(self.calls_exact(f"/repos/{REPO}/actions/runs/{RUN}"), [])

    def test_a_collect_run_that_is_still_running_is_waited_for(self) -> None:
        self.routes[f"GET /repos/{PRIVATE}/actions/workflows/collect.yml/runs"]["body"]["workflow_runs"][0][
            "status"
        ] = "in_progress"
        self.routes[f"GET /repos/{PRIVATE}/actions/runs/9001"] = [
            {"status": 200, "body": {"status": "queued"}},
            {"status": 200, "body": {"status": "completed"}},
        ]
        result, status = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(status["SCAN_OK"], "true", result.stdout)
        # Two polls, a sleep before each, then the artifact was scanned.
        self.assertEqual(len(self.sleeps.read_text().splitlines()), 2)
        self.assertEqual(len(self.calls_exact(f"/repos/{PRIVATE}/actions/runs/9001")), 2)
        self.assertEqual(len(self.calls_to("/artifacts/77/zip")), 1)

    def test_a_collect_run_that_never_finishes_is_an_incomplete_scan(self) -> None:
        self.routes[f"GET /repos/{PRIVATE}/actions/workflows/collect.yml/runs"]["body"]["workflow_runs"][0][
            "status"
        ] = "in_progress"
        self.routes[f"GET /repos/{PRIVATE}/actions/runs/9001"] = {"status": 200, "body": {"status": "in_progress"}}
        self.wait_seconds = "30"
        result, status = self.run_script()
        self.assertEqual(status["SCAN_OK"], "false")
        self.assertIn("has not completed after 30s", result.stdout)
        # Bounded: 30s at 15s is two sleeps, not a loop.
        self.assertEqual(len(self.sleeps.read_text().splitlines()), 2)

    def test_the_wait_is_shared_by_every_pending_collect_run(self) -> None:
        runs = self.routes[f"GET /repos/{PRIVATE}/actions/workflows/collect.yml/runs"]["body"]["workflow_runs"]
        runs[0]["status"] = "in_progress"
        runs.append({"id": 9004, "status": "in_progress", "display_title": f"collect {REPO}#{RUN}.2 - e2e vmaas / e2e"})
        for run in (9001, 9004):
            self.routes[f"GET /repos/{PRIVATE}/actions/runs/{run}"] = {"status": 200, "body": {"status": "in_progress"}}
        self.routes[f"GET /repos/{PRIVATE}/actions/runs/9004/artifacts"] = {"status": 200, "body": {"artifacts": []}}
        self.wait_seconds = "30"
        result, status = self.run_script()
        self.assertEqual(status["SCAN_OK"], "false")
        # 30s at 15s is two sleeps in all, not two for each of the two runs.
        self.assertEqual(len(self.sleeps.read_text().splitlines()), 2)
        # Both are reported as unfinished; the second without waiting at all.
        self.assertIn("Collect run 9001", result.stdout)
        self.assertIn("Collect run 9004", result.stdout)

    def test_a_finished_collect_run_is_not_polled(self) -> None:
        self.run_script()
        self.assertEqual(self.calls_exact(f"/repos/{PRIVATE}/actions/runs/9001"), [])
        self.assertFalse(self.sleeps.exists())

    def test_withheld_is_set_only_when_every_leak_was_private(self) -> None:
        # No leak at all.
        _, status = self.run_script()
        self.assertEqual((status["LEAKS_FOUND"], status["REDACTED_WITHHELD"]), ("false", "false"))
        # A leak only in the private artifact: nothing to upload on purpose.
        self.routes[f"GET /repos/{PRIVATE}/actions/artifacts/77/zip"]["zip_b64"] = zip_b64({"kubeconfig": "LEAK\n"})
        _, status = self.run_script()
        self.assertEqual((status["LEAKS_FOUND"], status["REDACTED_WITHHELD"]), ("true", "true"))

    def test_a_public_leak_is_never_called_withheld(self) -> None:
        self.routes[f"GET /repos/{REPO}/actions/runs/{RUN}/artifacts"] = {
            "status": 200,
            "body": {"artifacts": [{"id": 5, "name": "osac-logs-100", "expired": False, "size_in_bytes": 10}]},
        }
        self.routes[f"GET /repos/{REPO}/actions/artifacts/5/zip"] = {
            "status": 200,
            "zip_b64": zip_b64({"kubeconfig": "token: LEAKVALUE\n"}),
        }
        self.routes[f"DELETE /repos/{REPO}/actions/artifacts/5"] = {"status": 204}
        # Public leak only, then public and private together.
        _, status = self.run_script(private_repo=None, private_token=None)
        self.assertEqual((status["LEAKS_FOUND"], status["REDACTED_WITHHELD"]), ("true", "false"))
        self.routes[f"GET /repos/{PRIVATE}/actions/artifacts/77/zip"]["zip_b64"] = zip_b64({"kubeconfig": "LEAK\n"})
        _, status = self.run_script()
        self.assertEqual((status["LEAKS_FOUND"], status["REDACTED_WITHHELD"]), ("true", "false"))

    def test_the_runs_own_artifacts_are_still_scanned_and_redacted(self) -> None:
        self.routes[f"GET /repos/{REPO}/actions/runs/{RUN}/artifacts"] = {
            "status": 200,
            "body": {"artifacts": [{"id": 5, "name": "osac-logs-100", "expired": False, "size_in_bytes": 10}]},
        }
        self.routes[f"GET /repos/{REPO}/actions/artifacts/5/zip"] = {
            "status": 200,
            "zip_b64": zip_b64({"kubeconfig": "token: LEAKVALUE\n"}),
        }
        self.routes[f"DELETE /repos/{REPO}/actions/artifacts/5"] = {"status": 204}
        result, status = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(status["LEAKS_FOUND"], "true")
        # The public artifact keeps today's path: a redacted copy is written.
        self.assertTrue(list((self.output / "redacted" / "artifacts").glob("*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)

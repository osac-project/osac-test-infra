#!/usr/bin/env python3
"""End-to-end canary for the release gate (run: python3 .github/scripts/release_gate_canary_test.py).

Runs the real scripts/gather-osac-logs.sh against a stubbed `oc` and `curl` that
serve a ClusterOrder and an AAP job list carrying a synthetic pull secret in the
shapes of a past leak: an escaped JSON string in spec.pullSecret, and the
same ClusterOrder inside a job's extra_vars string. Then checks the release gate:

  1. it detects the canary in what the collector produced (--check, exit 1),
  2. censoring removes it in every encoding while the JSON files still parse,
  3. the shape layer alone does the same when it is given no inventory.

Whether the collector itself leaks is reported but not asserted, so this test
passes before and after the collector is fixed. Set RELEASE_GATE_REQUIRE_CLEAN_COLLECTOR=1
to also require that the collector does not write the canary.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

SCRIPTS = pathlib.Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
GATE = SCRIPTS / "release_gate.py"
COLLECTOR = REPO / "scripts" / "gather-osac-logs.sh"

# Synthetic; not a credential.
USER = "robot+canary_access_00000000deadbeef"
TOKEN = "CANARYtokenCANARYtoken0123456789abcdefCANARYtokenCANARYtoken012345"
AUTH = base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()
PULL_SECRET = {
    "auths": {
        "quay.io": {"auth": AUTH, "email": "canary@example.com"},
        "cloud.openshift.com": {"auth": AUTH, "email": "canary@example.com"},
        "registry.redhat.io": {"auth": "cGxhY2U=", "email": "canary@example.com"},
    }
}

OC_STUB = """#!/bin/bash
case "$*" in
  *"get clusterorders"*"-o json"*) cat "$FIXTURES/clusterorders.json" ;;
  *"get route osac-aap"*) printf 'aap.canary.test' ;;
  *"get secret osac-aap-controller-admin-password"*) printf 'Y2FuYXJ5' ;;
esac
exit 0
"""
CURL_STUB = """#!/bin/bash
for last; do :; done
case "$last" in
  *"/jobs/?page="*) cat "$FIXTURES/jobs.json" ;;
  *"/stdout/"*) echo "job stdout" ;;
  *"project_updates"*) echo '{"results":[]}' ;;
esac
exit 0
"""


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _independent_needles() -> dict[str, bytes]:
    """The canary in every form a file could hold it; deliberately not the gate's own code."""
    needles = {"token": TOKEN.encode(), "auth": AUTH.encode()}
    for pad in range(3):
        for value, name in ((TOKEN, "token"), (AUTH, "auth")):
            encoded = base64.b64encode(b"\0" * pad + value.encode())
            needles[f"{name}-base64+{pad}"] = encoded[4:-4]
    return needles


def leaked_files(root: pathlib.Path) -> dict[str, list[str]]:
    needles = _independent_needles()
    found: dict[str, list[str]] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = path.read_bytes()
        forms = [name for name, needle in needles.items() if needle in data]
        if forms:
            found[str(path.relative_to(root))] = forms
    return found


def run_gate(root: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(GATE), str(root), *args],
                          capture_output=True, text=True, check=False)


def write_fixtures(base: pathlib.Path) -> pathlib.Path:
    fixtures = base / "fixtures"
    fixtures.mkdir()
    order = {
        "metadata": {"name": "canary-cluster", "namespace": "osac-e2e-ci"},
        "spec": {
            "pullSecret": json.dumps(PULL_SECRET),
            "templateParameters": json.dumps({"pull_secret": json.dumps(PULL_SECRET)}),
            "releaseImage": "quay.io/openshift-release-dev/ocp-release:4.18.0-x86_64",
        },
    }
    (fixtures / "clusterorders.json").write_text(json.dumps({"items": [order]}))
    extra_vars = json.dumps({"osac_job_vars": {"resource": order}})
    jobs = {"count": 1, "next": None,
            "results": [{"id": 7, "status": "successful", "name": "osac-create-cluster", "extra_vars": extra_vars}]}
    (fixtures / "jobs.json").write_text(json.dumps(jobs))
    bin_dir = base / "bin"
    bin_dir.mkdir()
    for name, body in (("oc", OC_STUB), ("curl", CURL_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "kubectl").symlink_to("oc")
    (base / "kubeconfig").write_text("")
    return fixtures


def main() -> None:
    for tool in ("bash", "jq"):
        _assert(shutil.which(tool) is not None, f"{tool} is required to run the collector")

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        fixtures = write_fixtures(base)
        vault_file = base / "pull-secret.json"  # what the workflow mounts as OSAC_PULL_SECRET_PATH
        vault_file.write_text(json.dumps(PULL_SECRET))

        collected = base / "collected"
        env = {**os.environ, "PATH": f"{base / 'bin'}{os.pathsep}{os.environ['PATH']}",
               "FIXTURES": str(fixtures), "KUBECONFIG": str(base / "kubeconfig")}
        result = subprocess.run(["bash", str(COLLECTOR), str(collected)], cwd=REPO, env=env,
                                capture_output=True, text=True, timeout=300, check=False)
        _assert(result.returncode == 0, f"collector failed: {result.stderr[-400:]}")
        _assert((collected / "mgmt-cluster" / "clusterorders.json").is_file(), "collector produced no ClusterOrder dump")
        _assert((collected / "mgmt-cluster" / "aap-jobs" / "jobs-page-1.json").is_file(), "collector produced no AAP job dump")

        keep = os.environ.get("RELEASE_GATE_CANARY_KEEP")
        if keep:  # lets a person run another scanner over exactly what the collector wrote
            shutil.copytree(collected, keep, dirs_exist_ok=True)

        before = leaked_files(collected)
        print("collector output, before the gate:")
        if before:
            for name, forms in before.items():
                print(f"  LEAK  {name}  ({', '.join(forms)})")
        else:
            print("  clean: the collector does not write the canary")
        if os.environ.get("RELEASE_GATE_REQUIRE_CLEAN_COLLECTOR") == "1":
            _assert(not before, f"collector wrote the canary: {sorted(before)}")

        # 1. Detection: --check changes nothing and fails exactly when the collector leaked.
        checked = run_gate(collected, "--secret-file", str(vault_file), "--check")
        print(f"gate --check: exit {checked.returncode}")
        _assert(checked.returncode == (1 if before else 0),
                f"gate --check exit {checked.returncode} does not match the collector output: {checked.stdout}")
        _assert(leaked_files(collected) == before, "--check modified the artifact")

        # 2. Censoring with the inventory.
        with_inventory = base / "with-inventory"
        shutil.copytree(collected, with_inventory)
        censored = run_gate(with_inventory, "--secret-file", str(vault_file), "--fail-on-find")
        print(f"gate censor (inventory): exit {censored.returncode}")
        _assert(censored.returncode == (1 if before else 0), f"unexpected exit: {censored.stdout}{censored.stderr}")
        _assert(not leaked_files(with_inventory), f"canary survived: {leaked_files(with_inventory)}")
        for path in with_inventory.rglob("*.json"):
            json.loads(path.read_text())  # censoring must not corrupt the JSON the collector wrote

        # 3. The shape layer alone, no inventory at all.
        shapes_only = base / "shapes-only"
        shutil.copytree(collected, shapes_only)
        run_gate(shapes_only)
        _assert(not leaked_files(shapes_only), f"shape layer missed the canary: {leaked_files(shapes_only)}")

        report = json.loads((with_inventory / "redaction-report.json").read_text())
        if before:
            reported = {entry["file"] for entry in report["censored"]}
            _assert(set(before) <= reported, f"report misses {set(before) - reported}")
            print("censored files: " + ", ".join(sorted(reported)))
        _assert(not report["withheld"], f"files were withheld: {report['withheld']}")
        print("release_gate_canary_test.py: ok")


if __name__ == "__main__":
    main()

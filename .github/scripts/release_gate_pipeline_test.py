#!/usr/bin/env python3
"""The collect, teardown, upload sequence of a CaaS job (run: python3 .github/scripts/release_gate_pipeline_test.py).

Teardown deletes RUNNER_TEMP/pull-secret.json and the registry auth files before the
upload action runs, so the release gate can only know those secrets if the collection
step saved a copy first. This runs the real collect-artifacts.sh against a stubbed `oc`,
deletes the secret files the way teardown.sh does, then runs the real "Release gate" step
of the upload-artifacts action.

The canary is a bare token in a ClusterOrder annotation. No shape rule can match that,
so only the known-value layer can censor it; if the snapshot is missing the token is
published. Set RELEASE_GATE_REPO_ROOT to run the same test against another checkout.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(os.environ.get("RELEASE_GATE_REPO_ROOT") or pathlib.Path(__file__).resolve().parents[2])

# Synthetic; not a credential.
USER = "robot+pipeline_access_00000000feedface"
TOKEN = "PIPELINEtokenPIPELINEtoken0123456789abcdefPIPELINEtokenPIPELINE012"
AUTH = base64.b64encode(f"{USER}:{TOKEN}".encode()).decode()
PULL_SECRET = {"auths": {"quay.io": {"auth": AUTH, "email": "pipeline@example.com"}}}

OC_STUB = """#!/bin/bash
case "$*" in
  *"get clusterorders"*"-o json"*) cat "$FIXTURES/clusterorders.json" ;;
  *"get secrets"*) echo '{"items": []}' ;;
  *"get route osac-aap"*) printf 'aap.pipeline.test' ;;
  *"get secret osac-aap-controller-admin-password"*) printf 'cGlwZWxpbmU=' ;;
esac
exit 0
"""
CURL_STUB = """#!/bin/bash
for last; do :; done
case "$last" in
  *"/jobs/?page="*) echo '{"count": 0, "next": null, "results": []}' ;;
  *"project_updates"*) echo '{"results": []}' ;;
esac
exit 0
"""


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def step_script(action_yml: pathlib.Path, step_name: str, needle: str | None = "release_gate.py") -> str:
    """The `run: |` block of one composite-action step, without needing a YAML parser."""
    lines = action_yml.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip().startswith("run:"))
    inline = lines[run].strip()[len("run:"):].strip()
    if inline and inline != "|":
        script = inline  # a one-line `run: command`
    else:
        indent = len(lines[run]) - len(lines[run].lstrip()) + 2
        block = []
        for line in lines[run + 1:]:
            if line.strip() and not line.startswith(" " * indent):
                break
            block.append(line[indent:] if line.strip() else "")
        script = "\n".join(block)
    _assert(script.strip() and (needle is None or needle in script), f"could not extract the {step_name!r} step from {action_yml.name}")
    return script


def contains_canary(root: pathlib.Path) -> list[str]:
    needles = [TOKEN.encode(), AUTH.encode()]
    for pad in range(3):
        needles.append(base64.b64encode(b"\0" * pad + TOKEN.encode())[4:-4])
    return [str(p.relative_to(root)) for p in sorted(root.rglob("*"))
            if p.is_file() and any(n in p.read_bytes() for n in needles)]


def world(base: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    """A runner temp dir holding what the workflow leaves behind, and an environment with stubbed oc and curl."""
    runner_temp = base / "runner-temp"
    runner_temp.mkdir()
    (base / "bin").mkdir()
    for name, body in (("oc", OC_STUB), ("curl", CURL_STUB)):
        (base / "bin" / name).write_text(body)
        (base / "bin" / name).chmod(0o755)
    (base / "bin" / "kubectl").symlink_to("oc")
    (base / "kubeconfig").write_text("")
    fixtures = base / "fixtures"
    fixtures.mkdir()
    order = {"metadata": {"name": "co1", "annotations": {"note": f"debug dump {TOKEN} end"}}, "spec": {"releaseImage": "x"}}
    (fixtures / "clusterorders.json").write_text(json.dumps({"items": [order]}))
    # What the workflow's "Write pull secret and registry auth" step leaves behind.
    (runner_temp / "pull-secret.json").write_text(json.dumps(PULL_SECRET))
    (runner_temp / "auth.json").write_text(json.dumps({"auths": {}}))  # legitimately empty
    env = {**os.environ, "PATH": f"{base / 'bin'}{os.pathsep}{os.environ['PATH']}", "FIXTURES": str(fixtures),
           "KUBECONFIG": str(base / "kubeconfig"), "RUNNER_TEMP": str(runner_temp),
           "GITHUB_WORKSPACE": str(REPO), "E2E_NAMESPACE": "osac-e2e-ci"}
    return runner_temp, env


def collect(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(REPO / ".github/scripts/collect-artifacts.sh")], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=300, check=False)


def run_step(env: dict[str, str], step: str, **extra: str) -> subprocess.CompletedProcess[str]:
    script = step_script(REPO / ".github/actions/upload-artifacts/action.yml", step, needle=None)
    script = script.replace("${{ github.workspace }}", str(REPO))
    step_env = {**env, "ARTIFACT_DIR": str(pathlib.Path(env["RUNNER_TEMP"]) / "osac-artifacts"), "KNOWN_SECRET_FILES": "",
                "KNOWN_SECRET_ENV": "", "K8S_SECRETS_FILE": "", "FAIL_ON_CENSOR": "false", **extra}
    return subprocess.run(["bash", "-c", script], env=step_env, capture_output=True, text=True, timeout=300, check=False)


def main() -> None:
    _assert(shutil.which("jq") is not None, "jq is required to run the collector")

    # 1. The sequence of a CaaS job: collect, teardown, release gate.
    with tempfile.TemporaryDirectory() as tmp:
        runner_temp, env = world(pathlib.Path(tmp))
        done = collect(env)
        _assert(done.returncode == 0, f"collection failed: {done.stderr[-300:]}")
        artifacts = runner_temp / "osac-artifacts"
        _assert((artifacts / ".redaction-complete").exists(), "collection did not complete")
        _assert(contains_canary(artifacts), "the collector did not write the canary; the test is not exercising anything")
        print("collected; canary is in:", ", ".join(contains_canary(artifacts)))
        for name in ("pull-secret.json", "auth.json"):  # Teardown (teardown.sh)
            (runner_temp / name).unlink()
        result = run_step(env, "Release gate - censor secrets")
        print(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "(no output)")
        _assert(result.returncode == 0, f"the gate step failed ({result.returncode}): {result.stdout[-300:]}{result.stderr[-300:]}")
        _assert(artifacts.is_dir(), "the artifact directory was removed; an empty auth file must not abort the upload")
        _assert(not contains_canary(artifacts),
                f"canary survived the gate: {contains_canary(artifacts)}\ngate output:\n{result.stdout}{result.stderr}")
        report = json.loads((artifacts / "redaction-report.json").read_text())
        rules = {c["rule"].split(":")[0] for c in report["censored"]}
        _assert("known-value" in rules, f"the known-value layer did not act (rules: {rules}, inventory: {report['inventory']})")
        _assert(any(s["source"] == "pull-secret.json" and s["values"] > 0 for s in report["inventory"]),
                f"the pull secret was not in the inventory: {report['inventory']}")
        _assert(not (runner_temp / "osac-secret-inventory").exists(), "the snapshots were left on the runner")

    # 2. The gate skips because the artifact directory is gone: the snapshots must still be removed.
    with tempfile.TemporaryDirectory() as tmp:
        runner_temp, env = world(pathlib.Path(tmp))
        _assert(collect(env).returncode == 0, "collection failed")
        _assert((runner_temp / "osac-secret-inventory/secret-files/pull-secret.json").exists(), "no snapshot was taken")
        shutil.rmtree(runner_temp / "osac-artifacts")  # what "Verify redaction complete" does on a bad collection
        _assert(run_step(env, "Release gate - censor secrets").returncode == 0, "the gate step failed on a missing directory")
        _assert(not (runner_temp / "osac-secret-inventory").exists(), "the gate left the snapshots when it skipped")

    # 3. The closing step removes them even when nothing earlier did.
    with tempfile.TemporaryDirectory() as tmp:
        runner_temp, env = world(pathlib.Path(tmp))
        _assert(collect(env).returncode == 0, "collection failed")
        _assert(run_step(env, "Remove secret snapshots").returncode == 0, "the cleanup step failed")
        _assert(not (runner_temp / "osac-secret-inventory").exists(), "the cleanup step left the snapshots")

    # 4. A secret file that cannot be copied must stop the upload, not be skipped silently.
    if hasattr(os, "geteuid") and os.geteuid() != 0:  # root ignores file modes
        with tempfile.TemporaryDirectory() as tmp:
            runner_temp, env = world(pathlib.Path(tmp))
            blocked = runner_temp / "osac-secret-inventory/secret-files/pull-secret.json"
            blocked.parent.mkdir(parents=True)
            blocked.write_text("")
            blocked.chmod(0o400)  # cp onto a read-only file fails
            done = collect(env)
            _assert(not (runner_temp / "osac-artifacts/.redaction-complete").exists(),
                    f"a failed snapshot still marked the artifacts complete\n{done.stdout[-300:]}{done.stderr[-300:]}")
            _assert("Could not save a copy" in done.stdout + done.stderr, "no error was reported for the failed copy")
    print("release_gate_pipeline_test.py: ok")


if __name__ == "__main__":
    main()

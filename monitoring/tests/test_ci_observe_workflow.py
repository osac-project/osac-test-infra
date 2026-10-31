"""Structural tests for the ci-observe CI integration (Milestone 2, C0).

The integration is the composite action
.github/actions/ci-observe-capture/action.yml and its wiring into the
reusable e2e-{vmaas,bmaas,caas}-full-install.yml workflows. These tests pin
the contract that makes the capture safe to turn on for a real E2E job:

- the capture is opt-in (the workflow input defaults to false),
- the action reuses the repo's collector configs instead of its own copies,
- the capture artifact name carries the run id and the attempt,
- the test command's exit code is preserved: the run step has no
  continue-on-error and exits with the captured return code, while every
  capture-side step degrades to a warning instead of failing the job.

The automatic backend wiring is pinned too: the scheduled
ci-observe-sync.yml workflow on the central monitoring machine and the
Grafana datasources that point at its fixed ports.

Runtime behavior (deploy, capture, close) is exercised on a live cluster by
the capture supervisor's own validation; see monitoring/ci-observe.md.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTION_PATH = REPO_ROOT / ".github" / "actions" / "ci-observe-capture" / "action.yml"
SYNC_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci-observe-sync.yml"
DATASOURCES_PATH = REPO_ROOT / "monitoring" / "config" / "grafana" / "datasources.yml"
SCRIPT = REPO_ROOT / "monitoring" / "scripts" / "ci-observe.py"
_spec = importlib.util.spec_from_file_location("ci_observe_workflow_mod", SCRIPT)
ci_observe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_observe)
CLUSTER_MANIFEST = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "cluster.yaml"
COLLECTOR_CONFIG = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "collector.yaml"

# The three full-install reusable workflows wired for capture, and the
# callers that own their workflow_dispatch inputs.
SUITES = ("vmaas", "bmaas", "caas")
FULL_INSTALL_WORKFLOWS = {
    suite: REPO_ROOT / ".github" / "workflows" / f"e2e-{suite}-full-install.yml" for suite in SUITES
}
FULL_INSTALL_CALLERS = {
    suite: REPO_ROOT / ".github" / "workflows" / f"e2e-{suite}-full-install-caller.yml" for suite in SUITES
}
PR_SUITE_STARTERS = {suite: REPO_ROOT / ".github" / "workflows" / f"e2e-pr-{suite}.yml" for suite in SUITES}

UPLOAD_ACTION = "./.github/actions/upload-artifacts"
CAPTURE_ACTION = "./.github/actions/ci-observe-capture"

# The -e flags the observed command adds so the action's OTEL exports reach
# the container under test. Trailing backslash: each is a continued podman
# argument line inside the invocation.
OTEL_PASS_THROUGH = ("-e OTEL_EXPORTER_OTLP_ENDPOINT \\", "-e OTEL_EXPORTER_OTLP_TIMEOUT \\", "-e PYTEST_ADDOPTS \\")


@pytest.fixture(scope="module")
def action() -> dict:
    return yaml.safe_load(ACTION_PATH.read_text())


@pytest.fixture(scope="module")
def workflow(request: pytest.FixtureRequest) -> dict:
    return yaml.safe_load(FULL_INSTALL_WORKFLOWS[request.param].read_text())


@pytest.fixture(scope="module")
def caller(request: pytest.FixtureRequest) -> dict:
    return yaml.safe_load(FULL_INSTALL_CALLERS[request.param].read_text())


@pytest.fixture(scope="module")
def pr_starter(request: pytest.FixtureRequest) -> dict:
    return yaml.safe_load(PR_SUITE_STARTERS[request.param].read_text())


@pytest.fixture(scope="module")
def sync_workflow() -> dict:
    return yaml.safe_load(SYNC_WORKFLOW_PATH.read_text())


@pytest.fixture(scope="module")
def datasources() -> dict:
    return yaml.safe_load(DATASOURCES_PATH.read_text())


def workflow_call(document: dict) -> dict:
    """The workflow_call trigger of a parsed workflow file.

    PyYAML parses the YAML 1.1 boolean-looking ``on`` key as True.
    """
    triggers = document.get("on") or document.get(True)
    assert isinstance(triggers, dict)
    return triggers["workflow_call"]


def workflow_dispatch_inputs(document: dict) -> dict:
    """The workflow_dispatch inputs of a parsed workflow file."""
    triggers = document.get("on") or document.get(True)
    assert isinstance(triggers, dict)
    return triggers["workflow_dispatch"]["inputs"]


def steps_of(document: dict) -> list[dict]:
    return document["jobs"]["e2e"]["steps"]


def step_by_id(steps: list[dict], step_id: str) -> dict:
    matches = [step for step in steps if step.get("id") == step_id]
    assert len(matches) == 1, f"expected exactly one step with id {step_id}"
    return matches[0]


def sync_steps(document: dict) -> list[dict]:
    return document["jobs"]["sync"]["steps"]


def called_workflow_with(document: dict) -> dict:
    """The `with:` block of the caller's single reusable-workflow job."""
    calling = [job for job in document["jobs"].values() if "uses" in job]
    assert len(calling) == 1, "expected exactly one job calling a reusable workflow"
    return calling[0]["with"]


class TestAction:
    def test_is_a_well_formed_composite_action(self, action: dict) -> None:
        assert action["runs"]["using"] == "composite"
        inputs = action["inputs"]
        assert inputs["kubeconfig-path"]["required"] is True
        assert inputs["namespace"]["required"] is True
        assert inputs["command"]["required"] is True
        assert inputs["capture-id"]["required"] is False
        assert inputs["capture-id"]["default"] == ""
        assert inputs["suite"]["required"] is False
        assert inputs["suite"]["default"] == ""
        assert inputs["pr"]["required"] is False
        assert inputs["pr"]["default"] == ""
        # Self-contained: every step is an inline run or an existing repo
        # action, never a script of its own.
        for step in action["runs"]["steps"]:
            assert "run" in step or "uses" in step, step.get("name")
            assert not str(step.get("run", "")).startswith("python3 "), step.get("name")

    def test_reuses_the_repo_collector_configs(self, action: dict) -> None:
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        script = deploy["run"]
        assert "monitoring/config/ci-observe/cluster.yaml" in script
        assert "monitoring/config/ci-observe/collector.yaml" in script
        assert CLUSTER_MANIFEST.is_file()
        assert COLLECTOR_CONFIG.is_file()

    def test_renders_suite_and_pr_into_the_cluster_manifest(self, action: dict) -> None:
        """The suite and PR inputs become the CI_OBSERVE_SUITE / CI_OBSERVE_PR
        env values of the gateway (the collector turns them into the
        osac.ci.suite / osac.ci.pr resource attributes), and are narrowed to
        safe charsets first: they land inside a sed replacement and a
        double-quoted manifest value."""
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        script = deploy["run"]
        assert deploy["env"]["SUITE_INPUT"] == "${{ inputs.suite }}"
        assert deploy["env"]["PR_INPUT"] == "${{ inputs.pr }}"
        for token in ("CI_OBSERVE_SUITE", "CI_OBSERVE_PR"):
            assert f'-e "s|\\${{{token}}}' in script, token
        assert "tr -cd 'a-z0-9-'" in script
        assert "tr -cd '0-9'" in script

    def test_capture_id_is_a_short_dns_label(self, action: dict) -> None:
        """The capture id names the PVC and the readout pod; the readout pod
        prepends 19 characters to it and Kubernetes caps pod names at 63, so
        the action must refuse anything longer than 44 characters instead of
        losing the capture at readout-pod creation."""
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        script = deploy["run"]
        assert '"${#capture_id}" -gt 44' in script

    def test_beyla_image_is_configurable_and_defaults_to_the_pinned_patch_build(self, action: dict) -> None:
        beyla = action["inputs"]["beyla-image"]
        assert beyla["required"] is False
        documents = [doc for doc in yaml.safe_load_all(CLUSTER_MANIFEST.read_text()) if doc]
        daemonset = next(doc for doc in documents if doc.get("kind") == "DaemonSet")
        pinned = daemonset["spec"]["template"]["spec"]["containers"][0]["image"]
        assert beyla["default"] == pinned

    def test_artifact_name_carries_run_id_and_attempt(self, action: dict) -> None:
        upload = next(step for step in action["runs"]["steps"] if step.get("uses") == UPLOAD_ACTION)
        name = upload["with"]["artifact-name"]
        assert "ci-observe-capture" in name
        assert "${{ github.run_id }}" in name
        assert "${{ github.run_attempt }}" in name

    def test_upload_goes_through_the_release_gate_path(self, action: dict) -> None:
        upload = next(step for step in action["runs"]["steps"] if step.get("uses") == UPLOAD_ACTION)
        stage = step_by_id(action["runs"]["steps"], "stage")
        # Staged inside the standard artifact directory the upload action
        # gates, and gated on that staging step's output.
        assert "${RUNNER_TEMP}/osac-artifacts" in stage["run"]
        assert upload["if"] == "always() && steps.stage.outputs.staged == 'true'"
        # The gitleaks pre-scan is opted out: ~1 GB of telemetry JSONL costs
        # ~24 minutes of pattern scanning. The release gate and the upload
        # still run (pinned by .github/scripts/upload_artifacts_test.py).
        assert upload["with"]["gitleaks"] == "false"

    def test_test_exit_code_is_preserved(self, action: dict) -> None:
        steps = action["runs"]["steps"]
        run = step_by_id(steps, "run")
        # The run step is the only one without continue-on-error: its outcome
        # is the test's own outcome.
        assert "continue-on-error" not in run
        assert 'bash -e -o pipefail "${ci_dir}/command.sh" || rc=$?' in run["run"]
        assert 'exit "${rc}"' in run["run"]
        for step in steps:
            if step.get("id") != "run":
                assert step.get("continue-on-error") is True, step.get("name", step)

    def test_closing_steps_run_always_and_delete_the_capture_volume(self, action: dict) -> None:
        close = step_by_id(action["runs"]["steps"], "close")
        # The full close runs for every non-cancelled outcome; a cancelled
        # job takes the quick-cleanup step instead (cancellation budget).
        assert close["if"].startswith("always() && !cancelled()")
        script = close["run"]
        assert "pvc/ci-observe-capture-" in script
        assert "--ignore-not-found" in script
        # The writers are stopped before the copy, so the capture files are
        # closed on disk when the readout pod reads them. Foreground cascade:
        # the delete must not return while the gateway pod still flushes.
        assert "delete deployment/ci-observe-gateway" in script
        assert "delete daemonset/ci-observe-beyla" in script
        assert "--cascade=foreground" in script

    def test_a_cancelled_job_takes_the_cheap_cleanup_path(self, action: dict) -> None:
        steps = action["runs"]["steps"]
        quick = step_by_id(steps, "quick-cancelled-cleanup")
        assert quick["if"] == "cancelled() && env.CI_OBSERVE_DEPLOYED == '1'"
        script = quick["run"]
        assert "--wait=false" in script
        assert "pvc/ci-observe-capture-" in script
        # The expensive steps are skipped for a cancelled job: the full close
        # (cluster waits, file copy) and the staged upload (artifact upload).
        assert step_by_id(steps, "close")["if"].startswith("always() && !cancelled()")
        assert step_by_id(steps, "stage")["if"].startswith("always() && !cancelled()")

    def test_run_step_sets_the_otel_environment(self, action: dict) -> None:
        run = step_by_id(action["runs"]["steps"], "run")
        script = run["run"]
        assert 'OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${CI_OBSERVE_OTLP_PORT}"' in script
        assert 'OTEL_EXPORTER_OTLP_TIMEOUT="2"' in script
        assert "--export-traces" in script
        # Only when the collectors came up: a failed deployment must leave
        # the command's environment untouched.
        assert 'if [[ -n "${CI_OBSERVE_OTLP_PORT:-}" ]]' in script

    def test_capture_metadata_matches_the_capture_schema(self, action: dict) -> None:
        close = step_by_id(action["runs"]["steps"], "close")
        script = close["run"]
        assert '"osac.ci-observe.capture"' in script
        assert '"cluster-objects"' in script
        assert '"cluster-metrics"' in script
        # A capture the test command never finished is interrupted, not
        # closed: only closed captures can be imported.
        assert '"interrupted"' in script


def _shell_scripts() -> list[tuple[str, str]]:
    """Every embedded shell script: all run: blocks of the action, the sync
    workflow and the wired workflows, plus the observed command inputs (they
    are bash too)."""
    scripts: list[tuple[str, str]] = []

    def walk(node: object, origin: str) -> None:
        if isinstance(node, dict):
            run = node.get("run")
            if isinstance(run, str):
                scripts.append((origin, run))
            command = node.get("with", {}).get("command") if isinstance(node.get("with"), dict) else None
            if isinstance(command, str):
                scripts.append((origin, command))
            for value in node.values():
                walk(value, origin)
        elif isinstance(node, list):
            for value in node:
                walk(value, origin)

    origins = [ACTION_PATH, SYNC_WORKFLOW_PATH, *FULL_INSTALL_WORKFLOWS.values(), *PR_SUITE_STARTERS.values()]
    for origin in origins:
        for doc in (d for d in yaml.safe_load_all(origin.read_text()) if d):
            walk(doc, str(origin))
    return scripts


@pytest.mark.parametrize(
    "origin,script", _shell_scripts(), ids=[f"{Path(o).name}:{i}" for i, (o, _) in enumerate(_shell_scripts())]
)
def test_every_embedded_shell_script_parses(origin: str, script: str) -> None:
    """bash -n on every embedded script. A review of an earlier revision found
    a `fi` and a `for` joined onto one line by an editing slip: syntactically
    valid YAML, a broken shell script, and a close step that would have lost
    every capture. This test keeps that class of mistake out."""
    result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, f"{origin}: {result.stderr}"


def test_every_workflow_using_the_action_gates_it_behind_the_input() -> None:
    referencing = [
        path for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml") if CAPTURE_ACTION in path.read_text()
    ]
    assert set(referencing) == set(FULL_INSTALL_WORKFLOWS.values()), (
        "exactly the three full-install reusable workflows must use the action"
    )
    for path in referencing:
        entry = workflow_call(yaml.safe_load(path.read_text()))["inputs"]["ci-observe"]
        assert entry.get("default") is False, path


@pytest.mark.parametrize(
    "workflow, suite_env",
    [
        ("vmaas", ("-e OSAC_VM_TEMPLATE", "-e OSAC_CLUSTER_TEMPLATE", "-e METERING_ADAPTER_URL")),
        ("bmaas", ("-e OSAC_BMI_TEMPLATE", "-e OSAC_BMH_NAMESPACE")),
        ("caas", ("-e OSAC_CLUSTER_TEMPLATE", "-e METERING_ADAPTER_URL", "caas_ssh_key.pub")),
    ],
    indirect=["workflow"],
)
def test_observed_command_carries_the_suite_specific_environment(
    workflow: dict, suite_env: tuple[str, str, ...]
) -> None:
    observed = step_by_id(steps_of(workflow), "test-observed")
    command = observed["with"]["command"]
    for fragment in suite_env:
        assert fragment in command
    # Every suite-specific value the plain step passes through its env block
    # is present on the observed step too (as data, not script text).
    plain_env = step_by_id(steps_of(workflow), "test")["env"]
    for name in plain_env:
        if name in command or f"${{{name}}}" in command:
            assert name in observed["env"], name


@pytest.mark.parametrize("workflow", SUITES, indirect=True)
class TestWorkflowWiring:
    def test_capture_is_opt_in_and_defaults_to_false(self, workflow: dict) -> None:
        entry = workflow_call(workflow)["inputs"]["ci-observe"]
        assert entry["type"] == "boolean"
        assert entry["required"] is False
        assert entry["default"] is False

    def test_observed_step_passes_a_flavored_capture_identity(self, workflow: dict) -> None:
        """The capture id is <suite>-pr<N|main>-<run id>-a<attempt>: readable
        in the Grafana capture dropdown, DNS-1123 by construction (fixed
        suite word, digits, "main"), unique through the full run id, and
        short enough (under 30 characters) that the PVC and readout pod names
        built from it fit the 63-character pod-name cap. Suite and PR reach
        the action separately and become the osac.ci.suite / osac.ci.pr
        resource attributes."""
        observed = step_by_id(steps_of(workflow), "test-observed")
        capture_id = observed["with"]["capture-id"]
        match = re.fullmatch(
            r"\$\{\{ format\('(?P<suite>[a-z]+)-\{0\}-\{1\}-a\{2\}', "
            r"inputs\.pr-number != '' && format\('pr\{0\}', inputs\.pr-number\) \|\| 'main', "
            r"github\.run_id, github\.run_attempt\) \}\}",
            capture_id,
        )
        assert match, f"capture-id is not the flavored format: {capture_id}"
        assert match.group("suite") in SUITES
        assert observed["with"]["suite"] == match.group("suite")
        assert observed["with"]["pr"] == "${{ inputs.pr-number }}"

    def test_exactly_one_test_step_runs_depending_on_the_input(self, workflow: dict) -> None:
        steps = steps_of(workflow)
        plain = step_by_id(steps, "test")
        observed = step_by_id(steps, "test-observed")
        assert plain["if"] == "${{ !inputs.ci-observe }}"
        assert observed["if"] == "${{ inputs.ci-observe }}"
        assert observed["uses"] == CAPTURE_ACTION
        assert steps.index(observed) == steps.index(plain) + 1
        assert plain["name"] == "Run E2E tests"

    def test_observed_command_mirrors_the_plain_test_invocation(self, workflow: dict) -> None:
        steps = steps_of(workflow)
        plain = step_by_id(steps, "test")
        observed = step_by_id(steps, "test-observed")
        command = observed["with"]["command"]

        # The observed script is the plain step's WHOLE script, line for
        # line -- preamble, test selection, podman invocation and pytest
        # args -- plus the three OTEL pass-through flags. Comparing only
        # the podman block would let a preamble edit drift silently and
        # make a labeled pull request run a different test selection than
        # the plain path.
        plain_lines = [line.strip() for line in plain["run"].splitlines() if line.strip()]
        command_lines = [line.strip() for line in command.splitlines() if line.strip()]
        observed_lines = [line for line in command_lines if line not in OTEL_PASS_THROUGH]
        assert observed_lines == plain_lines
        for flag in OTEL_PASS_THROUGH:
            assert flag in command_lines

        # Same container, same suite selection, same action inputs. The
        # image tag and every suite input reach the script as environment
        # variables -- the same data-only channel the plain step uses.
        assert observed["env"]["E2E_IMAGE"] == "osac-e2e-tests:${{ github.run_id }}"
        assert "tests/e2e/" in command
        assert observed["with"]["namespace"] == "${{ inputs.namespace }}"
        assert observed["env"]["OSAC_NAMESPACE"] == "${{ inputs.namespace }}"
        assert "${{ inputs." not in command
        assert "${{ steps." not in command

        # The plain step's own script stays the unwrapped podman run.
        for flag in OTEL_PASS_THROUGH:
            assert flag not in plain["run"]

    def test_job_result_covers_both_test_steps(self, workflow: dict) -> None:
        result = workflow["jobs"]["e2e"]["outputs"]["result"]
        assert "steps.test.outcome" in result
        assert "steps.test-observed.outcome" in result
        assert "'PASSED'" in result and "'FAILED'" in result


@pytest.mark.parametrize("caller", SUITES, indirect=True)
class TestCallerWiring:
    """The full-install callers own the workflow_dispatch input and pass it on."""

    def test_dispatch_input_is_opt_in_and_defaults_to_false(self, caller: dict) -> None:
        entry = workflow_dispatch_inputs(caller)["ci-observe"]
        assert entry["type"] == "boolean"
        assert entry["required"] is False
        assert entry["default"] is False

    def test_input_is_passed_to_the_reusable_workflow(self, caller: dict) -> None:
        assert called_workflow_with(caller)["ci-observe"] == "${{ inputs.ci-observe || false }}"


@pytest.mark.parametrize("pr_starter", SUITES, indirect=True)
class TestPrStarterWiring:
    """A pull request turns the capture on with the ci-observe label: the suite
    starters read it from the pull request (their runs are dispatches, whose
    events carry no labels) and pass it to the reusable workflow."""

    def test_the_suite_job_waits_for_a_labels_job(self, pr_starter: dict) -> None:
        suite_job = next(job for name, job in pr_starter["jobs"].items() if name != "verify" and name != "labels")
        assert suite_job["needs"] == ["verify", "labels"]

    def test_the_labels_job_reads_the_ci_observe_label(self, pr_starter: dict) -> None:
        labels = pr_starter["jobs"]["labels"]
        assert labels["permissions"] == {"pull-requests": "read"}
        step = labels["steps"][0]
        # The input reaches the shell through an environment variable, never
        # through the script text (the convention of the osac-ci e2e dispatch).
        assert step["env"]["PR_NUMBER"] == "${{ inputs.pr-number }}"
        script = step["run"]
        assert "pulls/" in script and "ci-observe" in script
        assert '"${PR_NUMBER}"' in script
        # A non-numeric input degrades to ci-observe=false instead of reaching
        # the API URL.
        assert "*[!0-9]*" in script
        # The pull request lives in this repository even for a fork's commit:
        # the label query must not use inputs.repo.
        assert "github.repository" in script

    def test_the_label_reaches_the_reusable_workflow(self, pr_starter: dict) -> None:
        suite_job = next(job for name, job in pr_starter["jobs"].items() if name != "verify" and name != "labels")
        assert suite_job["with"]["ci-observe"] == "${{ needs.labels.outputs.ci-observe == 'true' }}"


class TestSyncWorkflow:
    """The scheduled backend wiring: ci-observe-sync.yml on monitoring-central."""

    def test_runs_on_the_central_machine_every_15_minutes(self, sync_workflow: dict) -> None:
        job = sync_workflow["jobs"]["sync"]
        assert job["runs-on"] == "monitoring-central"
        triggers = sync_workflow.get("on") or sync_workflow.get(True)
        assert triggers["schedule"] == [{"cron": "*/15 * * * *"}]
        assert "workflow_dispatch" in triggers

    def test_serializes_runs_and_times_out(self, sync_workflow: dict) -> None:
        job = sync_workflow["jobs"]["sync"]
        assert job["concurrency"] == {"group": "ci-observe-sync", "cancel-in-progress": False}
        assert job["timeout-minutes"] == 30
        assert job["permissions"] == {"contents": "read"}

    def test_the_vault_github_token_authenticates_gh(self, sync_workflow: dict) -> None:
        steps = sync_steps(sync_workflow)
        approle = step_by_id(steps, "approle")["run"]
        assert "${HOME}/.vault-server/.approle" in approle
        vault = step_by_id(steps, "vault")
        assert vault["uses"].startswith("hashicorp/vault-action@")
        assert "secret/data/osac/monitoring/github-token token | PRIVATE_GITHUB_TOKEN" in vault["with"]["secrets"]
        for step_id in ("sync_osac", "sync_osac_test_infra"):
            assert step_by_id(steps, step_id)["env"]["GH_TOKEN"] == "${{ steps.vault.outputs.PRIVATE_GITHUB_TOKEN }}"

    def test_the_backend_uses_a_persistent_state_dir_and_a_quadlet(self, sync_workflow: dict) -> None:
        steps = sync_steps(sync_workflow)
        assert "${HOME}/monitoring/ci-observe" in step_by_id(steps, "prepare")["run"]
        up = step_by_id(steps, "up")["run"]
        assert 'python3 monitoring/scripts/ci-observe.py --state-dir "${CI_OBSERVE_STATE_DIR}" up --quadlet' in up
        # An ad-hoc container would lose its port forwarding when the runner
        # reaps the job's process group; the backend must be a systemd quadlet.
        assert "--ui-port" not in up

    def test_the_quadlet_ports_match_the_provisioned_datasources(self, sync_workflow: dict) -> None:
        """The rendered quadlet publishes the ports the Grafana datasources point at."""
        rendered = ci_observe.render_quadlet(Path("/home/github-runner/monitoring/ci-observe"))
        datasources = yaml.safe_load((REPO_ROOT / "monitoring" / "config" / "grafana" / "datasources.yml").read_text())
        by_uid = {ds["uid"]: ds for ds in datasources["datasources"]}
        for uid, attr in (
            ("ci-observe-traces", "tempo"),
            ("ci-observe-logs", "loki"),
            ("ci-observe-metrics", "metrics"),
        ):
            port = ci_observe.QUADLET_PORTS[attr]
            assert by_uid[uid]["url"] == f"http://127.0.0.1:{port}", uid
            assert f"PublishPort=127.0.0.1:{port}:" in rendered, uid
        # The stack record's ports are the quadlet's, so the import path and
        # the Grafana URL never drift apart.
        assert ci_observe.QUADLET_PORTS["otlp"] == 4318
        assert ci_observe.QUADLET_PORTS["ui"] == 3300

    def test_both_osac_repositories_are_synced(self, sync_workflow: dict) -> None:
        steps = sync_steps(sync_workflow)
        assert "sync --repository osac-project/osac --since 24h" in step_by_id(steps, "sync_osac")["run"]
        assert (
            "sync --repository osac-project/osac-test-infra --since 24h"
            in step_by_id(steps, "sync_osac_test_infra")["run"]
        )
        # Both syncs and the prune run against the same state dir as up.
        for step_id in ("sync_osac", "sync_osac_test_infra"):
            assert '"${CI_OBSERVE_STATE_DIR}"' in step_by_id(steps, step_id)["run"]

    def test_prunes_cached_data_older_than_14_days(self, sync_workflow: dict) -> None:
        prune = step_by_id(sync_steps(sync_workflow), "prune")
        assert prune["if"] == "always()"
        assert "-mtime +14" in prune["run"]
        assert "captures" in prune["run"]
        assert "artifacts" in prune["run"]

    def test_posts_each_repository_report_to_the_job_summary(self, sync_workflow: dict) -> None:
        summary = step_by_id(sync_steps(sync_workflow), "summary")
        assert summary["if"] == "always()"
        assert "$GITHUB_STEP_SUMMARY" in summary["run"]
        assert "osac-project/${repo}" in summary["run"]


class TestCiObserveDatasources:
    """The Grafana datasources point at the ports the sync workflow publishes."""

    def test_the_three_datasources_match_the_quadlet_ports(self, datasources: dict) -> None:
        entries = {ds["uid"]: ds for ds in datasources["datasources"]}
        for uid, ds_type, attr in (
            ("ci-observe-traces", "tempo", "tempo"),
            ("ci-observe-logs", "loki", "loki"),
            ("ci-observe-metrics", "prometheus", "metrics"),
        ):
            entry = entries[uid]
            assert entry["type"] == ds_type
            assert entry["url"] == f"http://127.0.0.1:{ci_observe.QUADLET_PORTS[attr]}"
            assert entry["editable"] is False
        assert entries["ci-observe-metrics"]["jsonData"]["timeInterval"] == "60s"

    def test_existing_datasources_are_unchanged(self, datasources: dict) -> None:
        entries = {ds["uid"]: ds for ds in datasources["datasources"]}
        assert entries["prometheus"]["url"] == "http://127.0.0.1:9091"
        assert entries["prometheus"]["isDefault"] is True
        assert entries["workflow-api"]["type"] == "yesoreyeram-infinity-datasource"

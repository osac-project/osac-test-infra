"""Structural tests for the ci-observe CI integration (Milestone 2, C0).

The integration is the composite action
.github/actions/ci-observe-capture/action.yml and its wiring into the
reusable e2e-{vmaas,bmaas,caas}-full-install.yml workflows. These tests pin
the contract that makes the telemetry safe to turn on for a real E2E job:

- the telemetry is opt-in (the workflow input defaults to false),
- the action reuses the repo's collector configs instead of its own copies,
- the action pushes telemetry live to the central backend (basic-auth
  OTLP/HTTP from the collector gateway) and uploads no artifact,
- the test command's exit code is preserved: the run step has no
  continue-on-error and exits with the captured return code, while every
  telemetry-side step before the test degrades to a warning instead of
  failing the job,
- an empty OTLP password degrades to running the test without telemetry,
  never to an unauthenticated push,
- the post-test verification step makes a telemetry failure impossible to
  miss: when telemetry was requested (a password was passed), it scrapes the
  gateway's self-metrics and FAILS the job on refused input, failed sends,
  undelivered data or nothing-collected -- the 2026-11-01 incident (a dead
  central endpoint under 30 green runs) must not repeat.

The backend wiring is pinned too: the ci-observe-sync.yml workflow on the
central monitoring machine (manual dispatch only -- the schedule is
disabled because live pushes made the artifact sync unnecessary and its
backend restart would drop in-flight live-ingest batches), the new
ci-observe-backend.yml that redeploys the backend on config changes (never
on a schedule, same reason), the Grafana datasources that point at its
fixed ports, and the Caddy ingest vhost whose basic-auth hash is rendered
at deploy time from a machine-local secret (a credential is never
committed).

Runtime behavior (deploy, live push) is exercised on a live cluster by the
capture supervisor's own validation; see monitoring/ci-observe.md.
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
BACKEND_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci-observe-backend.yml"
CADDYFILE_PATH = REPO_ROOT / "monitoring" / "config" / "caddy" / "Caddyfile"
MONITORING_SETUP_PATH = REPO_ROOT / "monitoring" / "scripts" / "monitoring-setup.sh"
DATASOURCES_PATH = REPO_ROOT / "monitoring" / "config" / "grafana" / "datasources.yml"
ROUTER_CONFIG_PATH = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "otelcol-router" / "config.yaml"
ROUTER_SERVICE_PATH = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "otelcol-router" / "otelcol-router.service"
ROUTER_DEPLOY_SCRIPT_PATH = REPO_ROOT / "monitoring" / "scripts" / "deploy-otelcol-router.sh"
TEMPO210_QUADLET_PATH = REPO_ROOT / "monitoring" / "quadlet" / "ci-observe-tempo210.container"
TEMPO210_CONFIG_PATH = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "tempo210" / "tempo.yaml"
SCRIPT = REPO_ROOT / "monitoring" / "scripts" / "ci-observe.py"
_spec = importlib.util.spec_from_file_location("ci_observe_workflow_mod", SCRIPT)
ci_observe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_observe)
CLUSTER_MANIFEST = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "cluster.yaml"
COLLECTOR_LIVE_CONFIG = REPO_ROOT / "monitoring" / "config" / "ci-observe" / "collector-live.yaml"

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
def backend_workflow() -> dict:
    return yaml.safe_load(BACKEND_WORKFLOW_PATH.read_text())


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
        # The ingest password is a secret callers pass; empty is valid and
        # degrades to running the test without telemetry.
        assert inputs["otlp-password"]["required"] is False
        assert inputs["otlp-password"]["default"] == ""
        # Self-contained: every step is an inline run, never a script of its
        # own or a nested action.
        for step in action["runs"]["steps"]:
            assert "run" in step, step.get("name")
            assert not str(step.get("run", "")).startswith("python3 "), step.get("name")

    def test_reuses_the_repo_collector_configs(self, action: dict) -> None:
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        script = deploy["run"]
        assert "monitoring/config/ci-observe/cluster.yaml" in script
        assert "monitoring/config/ci-observe/collector-live.yaml" in script
        assert CLUSTER_MANIFEST.is_file()
        assert COLLECTOR_LIVE_CONFIG.is_file()

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

    def test_renders_the_otlp_endpoint_and_auth_into_the_cluster_manifest(self, action: dict) -> None:
        """The deploy step computes the basic-auth header from the
        otlp-password secret input ("Basic <base64 of ci-observe:password>",
        so the password itself never lands in the manifest) and renders the
        endpoint and header into the gateway env, which collector-live.yaml
        reads for the live push."""
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        script = deploy["run"]
        assert deploy["env"]["OTLP_PASSWORD_INPUT"] == "${{ inputs.otlp-password }}"
        assert 'printf \'%s:%s\' "ci-observe" "${OTLP_PASSWORD_INPUT}"' in script
        assert "| base64 -w0)" in script
        for token in ("CI_OBSERVE_OTLP_ENDPOINT", "CI_OBSERVE_OTLP_AUTH"):
            assert f'-e "s|\\${{{token}}}' in script, token
        assert "https://osac-ci-1.redhat.com" in script

    def test_missing_otlp_password_degrades_to_no_telemetry(self, action: dict) -> None:
        """An empty password (or endpoint) must never become an
        unauthenticated push: the deploy step warns, marks the deployment
        failed for the summary and exits success so the test still runs."""
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        script = deploy["run"]
        assert '-z "${OTLP_PASSWORD_INPUT}"' in script
        assert "running the test without telemetry" in script
        assert 'touch "${ci_dir}/deploy-failed"' in script

    def test_capture_id_is_a_short_dns_label(self, action: dict) -> None:
        """The capture id becomes the osac.ci.capture_id attribute and a
        label in the central backend; the action refuses anything that is
        not a DNS-1123 label of at most 44 characters."""
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

    def test_test_exit_code_is_preserved(self, action: dict) -> None:
        steps = action["runs"]["steps"]
        run = step_by_id(steps, "run")
        # The run step is the test's own outcome, and the verify step's
        # failure is the telemetry's own outcome: neither may be softened.
        # Every other step is a warning-only side show.
        assert "continue-on-error" not in run
        assert 'bash -e -o pipefail "${ci_dir}/command.sh" || rc=$?' in run["run"]
        assert 'exit "${rc}"' in run["run"]
        for step in steps:
            # The run step is the test's own outcome and stays gating;
            # everything else, verify included, is opt-in telemetry or
            # cleanup and never changes the job's conclusion.
            if step.get("id") != "run":
                assert step.get("continue-on-error") is True, step.get("name", step)

    def test_a_cancelled_job_takes_the_cheap_cleanup_path(self, action: dict) -> None:
        steps = action["runs"]["steps"]
        quick = step_by_id(steps, "quick-cancelled-cleanup")
        assert quick["if"] == "cancelled() && env.CI_OBSERVE_DEPLOYED == '1'"
        script = quick["run"]
        assert "--wait=false" in script
        assert "deployment/ci-observe-gateway" in script
        assert "daemonset/ci-observe-beyla" in script
        # Best-effort cleanup, but never silent: every failure warns.
        assert "|| true" not in script
        assert script.count("::warning::") == 2

    def test_there_is_no_post_test_artifact_tail(self, action: dict) -> None:
        """The action is deploy, run, cheap cleanup, summary, verify --
        nothing else. The old file-capture tail (close and copy, readout
        pod, staging, AAP license preparation, artifact upload, staged-file
        cleanup) is gone entirely: telemetry is pushed live during the run."""
        steps = action["runs"]["steps"]
        ids = {step["id"] for step in steps if step.get("id")}
        names = {step.get("name") for step in steps}
        assert ids == {"deploy", "run", "quick-cancelled-cleanup", "verify"}
        assert names == {
            "Deploy collectors",
            "Run test command",
            "Quick cleanup (job cancelled)",
            "Write capture summary",
            "Verify telemetry delivery",
        }
        assert not any(step.get("uses") for step in steps), "no nested actions: every step is inline"
        for gone in ("readout", "metadata.json", "osac-artifacts", "aap-license", "ci-observe-capture-"):
            for step in steps:
                assert gone not in step.get("run", ""), (gone, step.get("name"))

    def test_summary_defers_the_delivery_verdict_to_the_verify_step(self, action: dict) -> None:
        """The summary step writes the header and the capture id, and never
        claims delivery: that claim is the verify step's to make (or
        refute), so a green summary line can no longer sit on top of a dead
        endpoint."""
        steps = action["runs"]["steps"]
        summary = next(step for step in steps if step.get("name") == "Write capture summary")
        script = summary["run"]
        assert "capture id" in script
        assert "artifact" in script
        assert "telemetry not requested" in script
        assert "Verify telemetry delivery" in script
        # The old unconditional success line is exactly what hid the
        # 2026-11-01 incident; it must not come back.
        assert "telemetry pushed live to the central backend" not in script

    def test_run_step_sets_the_otel_environment(self, action: dict) -> None:
        run = step_by_id(action["runs"]["steps"], "run")
        script = run["run"]
        assert 'OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${CI_OBSERVE_OTLP_PORT}"' in script
        assert 'OTEL_EXPORTER_OTLP_TIMEOUT="2"' in script
        assert "--export-traces" in script
        # Only when the collectors came up: a failed deployment must leave
        # the command's environment untouched.
        assert 'if [[ -n "${CI_OBSERVE_OTLP_PORT:-}" ]]' in script
        # The verify step reads the run's duration to tell a dead capture
        # from a command that ended within seconds.
        assert "CI_OBSERVE_TEST_DURATION" in script


class TestVerifyStep:
    """The post-test verification: the loud backstop that makes a telemetry
    failure impossible to miss (2026-11-01: 30 green runs pushed into a dead
    central endpoint because every telemetry failure degraded to a
    warning)."""

    @pytest.fixture(scope="class")
    def verify(self, action: dict) -> dict:
        return step_by_id(action["runs"]["steps"], "verify")

    def test_runs_after_the_test_and_is_loud_but_never_gates(self, verify: dict) -> None:
        assert verify["if"] == "always() && !cancelled()"
        # The owner's rule: ci-observe is opt-in telemetry, so a delivery
        # failure must be visible (::error, red step) but must never change
        # the job's conclusion or block the required E2E check.
        assert verify["continue-on-error"] is True
        # It needs the cluster (port-forward to the gateway), like deploy.
        assert verify["env"]["KUBECONFIG"] == "${{ inputs.kubeconfig-path }}"

    def test_error_message_never_implies_the_test_failed(self, verify: dict) -> None:
        script = verify["run"]
        assert "telemetry was requested but did not reach the central backend" in script
        assert "the test result above is unaffected" in script
        assert "::error::" in script
        assert "${GITHUB_STEP_SUMMARY}" in script

    def test_fails_when_telemetry_was_requested_but_never_started(self, verify: dict, action: dict) -> None:
        """An empty OTLP password is an intentional telemetry-less run and
        exits success; a password that was passed (telemetry-requested
        marker) followed by a failed deployment fails the job."""
        script = verify["run"]
        assert '[[ ! -f "${ci_dir}/telemetry-requested" ]]' in script
        assert '[[ -f "${ci_dir}/deploy-failed" ]]' in script
        deploy = step_by_id(action["runs"]["steps"], "deploy")
        # The marker is written from the password input alone, BEFORE any
        # validation can fail (an invalid capture id with a password set is
        # a requested-but-never-started run, not a telemetry-less one).
        marker = deploy["run"].index('touch "${ci_dir}/telemetry-requested"')
        password_guard = deploy["run"].index('if [[ -n "${OTLP_PASSWORD_INPUT}" ]]; then')
        capture_id_assignment = deploy["run"].index('capture_id="${CAPTURE_ID_INPUT:-')
        assert marker > password_guard
        assert marker < capture_id_assignment

    def test_reads_the_gateway_self_metrics_over_the_standard_port(self, verify: dict) -> None:
        script = verify["run"]
        assert "svc/ci-observe-gateway :8888" in script
        assert "curl -sf" in script
        # The parser tolerates the optional _total suffix a manually
        # configured prometheus reader appends (collector v0.119+).
        assert 'sub(/_total$/, "", name)' in script

    def test_reads_every_delivery_counter_family(self, verify: dict) -> None:
        """The four counter families, one per signal: receiver
        accepted/refused and exporter sent/send_failed."""
        script = verify["run"]
        assert "for signal in spans log_records metric_points; do" in script
        for family in (
            "otelcol_receiver_accepted_${signal}",
            "otelcol_receiver_refused_${signal}",
            "otelcol_exporter_sent_${signal}",
            "otelcol_exporter_send_failed_${signal}",
        ):
            assert f'"{family}"' in script, family
        # The per-signal sent values for the summary line are read by their
        # full names.
        for counter in (
            "otelcol_exporter_sent_spans",
            "otelcol_exporter_sent_log_records",
            "otelcol_exporter_sent_metric_points",
        ):
            assert f"counter_sum {counter}" in script, counter

    def test_failure_conditions(self, verify: dict) -> None:
        script = verify["run"]
        # Any refused input or failed send is a hard failure.
        assert "otelcol_receiver_refused_${signal}" in script
        assert "otelcol_exporter_send_failed_${signal}" in script
        # Accepted but never delivered: the deficit (accepted - sent) is
        # tolerated while small (in-flight batches) or shrinking (queue
        # draining); a large, non-shrinking deficit is the failure.
        assert "(( deficit = accepted - sent ))" in script
        assert "(( tolerance = accepted / 50 + 1000 ))" in script
        assert "(( deficit > tolerance ))" in script
        assert "(( deficit_min < deficit_first ))" in script
        # Nothing collected during a non-trivial run.
        assert "(( total_sent == 0 ))" in script
        assert "(( duration > 60 ))" in script
        # The dead local path (the deploy step's port-forward) is caught too.
        assert "kill -0" in script

    def test_green_path_writes_the_delivered_line_to_the_summary(self, verify: dict) -> None:
        script = verify["run"]
        delivered = "telemetry delivered: ${sent_spans} spans, ${sent_log_records} logs, ${sent_metric_points} metrics"
        assert delivered in script

    def test_no_swallowed_failures(self, verify: dict) -> None:
        """The verify step itself must never swallow an error: no `|| true`,
        no stderr to /dev/null, every command's output lands in the log."""
        script = verify["run"]
        assert "|| true" not in script
        assert "2>/dev/null" not in script

    def test_stops_its_own_port_forward_with_a_warning_on_failure(self, verify: dict) -> None:
        script = verify["run"]
        assert "trap cleanup_verify_port_forward EXIT" in script
        assert "::warning::ci-observe: failed to stop the verify port-forward" in script


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

    origins = [
        ACTION_PATH,
        SYNC_WORKFLOW_PATH,
        BACKEND_WORKFLOW_PATH,
        *FULL_INSTALL_WORKFLOWS.values(),
        *PR_SUITE_STARTERS.values(),
    ]
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

    def test_the_otlp_password_secret_is_declared_and_passed_to_the_action(self, workflow: dict) -> None:
        """The reusable workflow declares the ingest-password secret and
        hands it to the capture action; callers that pass nothing leave it
        empty, which the action degrades to no telemetry."""
        secret = workflow_call(workflow)["secrets"]["ci-observe-otlp-password"]
        assert secret["required"] is False
        observed = step_by_id(steps_of(workflow), "test-observed")
        assert observed["with"]["otlp-password"] == "${{ secrets.ci-observe-otlp-password }}"

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
    """The backend wiring: ci-observe-sync.yml on monitoring-central."""

    def test_runs_on_the_central_machine_and_is_manual_only(self, sync_workflow: dict) -> None:
        """The schedule is disabled: live pushes made the artifact sync
        unnecessary for new runs, and the workflow's quadlet restart would
        drop in-flight live-ingest batches. Manual dispatch stays for
        backfilling historical artifacts."""
        job = sync_workflow["jobs"]["sync"]
        assert job["runs-on"] == "monitoring-central"
        triggers = sync_workflow.get("on") or sync_workflow.get(True)
        assert "schedule" not in triggers
        assert "workflow_dispatch" in triggers
        text = SYNC_WORKFLOW_PATH.read_text()
        assert "Schedule disabled" in text, "the disabled schedule must say why"

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
        for uid, attr in (("ci-observe-logs", "loki"), ("ci-observe-metrics", "metrics")):
            port = ci_observe.QUADLET_PORTS[attr]
            assert by_uid[uid]["url"] == f"http://127.0.0.1:{port}", uid
            assert f"PublishPort=127.0.0.1:{port}:" in rendered, uid
        # Since the 2026-11-01 cutover, new traces go to the separate Tempo
        # 2.10 container, not the LGTM bundle: the traces datasource points
        # at its 3210 API port, published by the tempo210 quadlet, and the
        # LGTM bundle's Tempo port 3200 serves the read-only archive
        # datasource for traces ingested before the cutover.
        assert by_uid["ci-observe-traces"]["url"] == "http://127.0.0.1:3210"
        assert by_uid["ci-observe-traces-archive"]["url"] == f"http://127.0.0.1:{ci_observe.QUADLET_PORTS['tempo']}"
        assert f"PublishPort=127.0.0.1:{ci_observe.QUADLET_PORTS['tempo']}:" in rendered
        tempo210_quadlet = (REPO_ROOT / "monitoring" / "quadlet" / "ci-observe-tempo210.container").read_text()
        assert "PublishPort=127.0.0.1:3210:3210" in tempo210_quadlet
        assert "PublishPort=127.0.0.1:14418:4418" in tempo210_quadlet
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


class TestBackendWorkflow:
    """ci-observe-backend.yml: the backend's deploy path, left open when the
    sync workflow's schedule was disabled. Exactly the sync workflow's
    vault-auth + `up --quadlet` steps, on the central machine, and nothing
    else."""

    def test_triggers_on_config_changes_and_manual_dispatch_only(self, backend_workflow: dict) -> None:
        """Never a schedule: a backend restart drops in-flight live-ingest
        batches (the same reason the sync schedule was disabled). No
        pull_request trigger either, so it never shows in a PR's checks."""
        triggers = backend_workflow.get("on") or backend_workflow.get(True)
        assert set(triggers) == {"push", "workflow_dispatch"}
        assert triggers["push"]["branches"] == ["main"]
        assert triggers["push"]["paths"] == ["monitoring/config/ci-observe/**"]
        text = BACKEND_WORKFLOW_PATH.read_text()
        assert "NO schedule" in text, "the missing schedule must say why"

    def test_single_job_on_the_central_machine(self, backend_workflow: dict) -> None:
        assert set(backend_workflow["jobs"]) == {"backend"}
        job = backend_workflow["jobs"]["backend"]
        assert job["runs-on"] == "monitoring-central"
        assert job["permissions"] == {"contents": "read"}
        assert job["concurrency"] == {"group": "ci-observe-backend", "cancel-in-progress": False}
        assert job["timeout-minutes"] == 15
        # A dispatch from any branch but main must not touch the machine.
        assert "github.event_name == 'push'" in job["if"]
        assert "github.ref == 'refs/heads/main'" in job["if"]

    def test_does_the_vault_auth_and_up_quadlet_and_nothing_else(self, backend_workflow: dict) -> None:
        steps = backend_workflow["jobs"]["backend"]["steps"]
        ids = [step.get("id") for step in steps]
        assert ids == ["checkout", "approle", "vault", "prepare", "up"]
        approle = step_by_id(steps, "approle")["run"]
        assert "${HOME}/.vault-server/.approle" in approle
        vault = step_by_id(steps, "vault")
        assert vault["uses"].startswith("hashicorp/vault-action@")
        up = step_by_id(steps, "up")["run"]
        assert 'python3 monitoring/scripts/ci-observe.py --state-dir "${CI_OBSERVE_STATE_DIR}" up --quadlet' in up
        assert "unset CONTAINERS_STORAGE_CONF" in up
        # The sync workflow's other work must not be here.
        for absent in ("sync --repository", "prune", "-mtime", "GITHUB_STEP_SUMMARY"):
            assert absent not in up, absent


class TestIngestVhost:
    """The Caddy vhost that terminates the runner fleet's live OTLP push
    (2026-11-01 incident: it existed only live on the machine, a deploy
    wiped it, and 30 runs pushed into a dead endpoint). The vhost lives in
    the repo; the credential never does."""

    def test_vhost_is_committed_with_a_placeholder_hash(self) -> None:
        text = CADDYFILE_PATH.read_text()
        assert "https://osac-ci-1.redhat.com:4319 {" in text
        assert "tls internal" in text
        # The vhost proxies to the host otelcol-router service, not directly
        # to a backend container (see TestOtelcolRouter below).
        assert "reverse_proxy 127.0.0.1:14318" in text
        # The basic_auth user line carries the render-time placeholder, and
        # it is the only occurrence of the token in the file.
        assert "\t\tci-observe CI_OBSERVE_OTLP_HASH\n" in text
        assert text.count("CI_OBSERVE_OTLP_HASH") == 1
        # Rotation is documented on the vhost itself.
        assert "caddy hash-password" in text
        assert "CI_OBSERVE_OTLP_PASSWORD" in text

    def test_the_deploy_renders_the_hash_and_never_clobbers_the_live_one(self) -> None:
        """monitoring-setup.sh's deploy_caddyfile: env var or machine-local
        secret file, falling back to the hash already deployed live; with
        no hash at all the deployed file stays untouched (deploying the
        placeholder would break Caddy's config load)."""
        script = MONITORING_SETUP_PATH.read_text()
        assert "deploy_caddyfile()" in script
        # The three hash sources.
        assert "CI_OBSERVE_OTLP_HASH:-" in script
        assert "secrets/ci-observe-otlp-hash" in script
        assert "'$1 == \"ci-observe\" { print $2; exit }'" in script
        # The placeholder is validated as a bcrypt hash before rendering.
        assert "bcrypt" in script
        # Both deploy paths go through the renderer, never a plain cp.
        assert 'cp "${MONITORING_REPO_DIR}/config/caddy/Caddyfile"' not in script
        assert script.count("deploy_caddyfile") >= 3, "definition plus both deploy sites (central + update-central)"

    def test_haproxy_route_is_documented_as_system_config(self) -> None:
        readme = (REPO_ROOT / "monitoring" / "README.md").read_text()
        assert "otlp-ci-observe" in readme
        assert "127.0.0.1:4319" in readme
        assert "req.ssl_sni -i osac-ci-1.redhat.com" in readme
        # The README must say this route is NOT deployed by the repo.
        assert "NOT deployed by this repo" in readme


class TestOtelcolRouter:
    """The host OTLP signal router behind the :4319 vhost, and the Tempo
    2.10 traces backend it feeds (both added live on 2026-11-01 and
    committed here so a deploy-monitoring run cannot revert them). The
    router is a root-managed system service, like the HAProxy route; the
    Tempo 2.10 container is a user quadlet, like the rest of the central
    stack."""

    def test_router_config_splits_the_signals_on_loopback(self) -> None:
        config = yaml.safe_load(ROUTER_CONFIG_PATH.read_text())
        receiver = config["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
        assert receiver == "127.0.0.1:14318", "Caddy proxies the :4319 vhost here"
        exporters = config["exporters"]
        # Traces -> Tempo 2.10 (14418); logs + metrics -> the LGTM bundle (4318).
        assert exporters["otlphttp/tempo210"]["endpoint"] == "http://127.0.0.1:14418"
        assert exporters["otlphttp/lgtm"]["endpoint"] == "http://127.0.0.1:4318"
        pipelines = config["service"]["pipelines"]
        assert pipelines["traces"]["exporters"] == ["otlphttp/tempo210"]
        assert pipelines["logs"]["exporters"] == ["otlphttp/lgtm"]
        assert pipelines["metrics"]["exporters"] == ["otlphttp/lgtm"]
        # Retries must be on: a central deploy must not drop in-flight
        # batches (the same contract as the gateway's live exporter).
        for exporter in exporters.values():
            assert exporter["retry_on_failure"]["enabled"] is True

    def test_router_self_metrics_are_scraped_by_central_prometheus(self) -> None:
        config = yaml.safe_load(ROUTER_CONFIG_PATH.read_text())
        readers = config["service"]["telemetry"]["metrics"]["readers"]
        prometheus = readers[0]["pull"]["exporter"]["prometheus"]
        assert prometheus["host"] == "127.0.0.1"
        assert prometheus["port"] == 18888
        prometheus_config = yaml.safe_load((REPO_ROOT / "monitoring" / "config" / "prometheus.yml").read_text())
        jobs = {job["job_name"]: job for job in prometheus_config["scrape_configs"]}
        targets = jobs["otelcol-router"]["static_configs"][0]["targets"]
        assert targets == ["127.0.0.1:18888"]

    def test_router_service_is_a_hardened_system_unit(self) -> None:
        text = ROUTER_SERVICE_PATH.read_text()
        assert "ExecStart=/usr/local/bin/otelcol-contrib --config=/etc/otelcol-router/config.yaml" in text
        assert "User=otelcol-router" in text
        # The router needs no privileges; the hardening lines must stay.
        for hardening in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true"):
            assert hardening in text, hardening

    def test_router_deploy_script_extracts_the_pinned_binary(self) -> None:
        """deploy-otelcol-router.sh installs the binary, config and unit
        from this repo as root -- the router's only deploy path."""
        script = ROUTER_DEPLOY_SCRIPT_PATH.read_text()
        # The same digest ci-observe.py pins (COLLECTOR_IMAGE).
        assert "340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b" in script
        assert "podman cp" in script
        assert "/usr/local/bin/otelcol-contrib" in script
        assert "useradd --system" in script

    def test_tempo210_quadlet_publishes_the_router_and_grafana_ports(self) -> None:
        text = TEMPO210_QUADLET_PATH.read_text()
        assert "Image=docker.io/grafana/tempo:2.10.10" in text
        # 3210 is the API port the ci-observe-traces datasource points at;
        # 14418 is the OTLP receiver the router exports to.
        assert "PublishPort=127.0.0.1:3210:3210" in text
        assert "PublishPort=127.0.0.1:14418:4418" in text
        assert "Volume=ci-observe-tempo210-data:/data" in text
        # The config is the repo's, deployed into the ci-observe state dir.
        assert "Volume=%h/monitoring/ci-observe/config/tempo210/tempo.yaml:/etc/tempo.yaml:ro,Z" in text

    def test_monitoring_setup_deploys_the_tempo210_unit_and_config(self) -> None:
        """The tempo210 quadlet rides the normal central deploy paths
        (--central and --update-central); without this, a deploy-monitoring
        run leaves the live quadlet stale and a fresh machine without it."""
        script = MONITORING_SETUP_PATH.read_text()
        assert "ci-observe-tempo210.container" in script
        assert "ci-observe-tempo210.service" in script
        assert "deploy_tempo210_config()" in script
        # Both deploy modes install the unit and the config.
        assert script.count("deploy_tempo210_config") >= 3, "definition plus both deploy sites"
        # The config the quadlet mounts is the repo's file.
        assert "config/ci-observe/tempo210/tempo.yaml" in script

    def test_tempo210_config_serves_search_and_keeps_30_days(self) -> None:
        config = yaml.safe_load(TEMPO210_CONFIG_PATH.read_text())
        assert config["server"]["http_listen_port"] == 3210
        assert config["storage"]["trace"]["backend"] == "local"
        # 720h retention: matches the 30-day window the CI Observe
        # dashboard's default time range assumes.
        assert config["compactor"]["compaction"]["block_retention"] == "720h"


class TestCiObserveDatasources:
    """The Grafana datasources point at the ports the backend publishes."""

    def test_the_datasources_match_the_backend_ports(self, datasources: dict) -> None:
        entries = {ds["uid"]: ds for ds in datasources["datasources"]}
        for uid, ds_type, attr in (
            ("ci-observe-logs", "loki", "loki"),
            ("ci-observe-metrics", "prometheus", "metrics"),
        ):
            entry = entries[uid]
            assert entry["type"] == ds_type
            assert entry["url"] == f"http://127.0.0.1:{ci_observe.QUADLET_PORTS[attr]}"
            assert entry["editable"] is False
        # Traces are split since the 2026-11-01 cutover: Tempo 2.10 (API
        # 3210, its own container) serves new traces; the LGTM bundle's
        # Tempo 3.1.1 keeps port 3200 as the read-only archive for traces
        # ingested before the cutover.
        assert entries["ci-observe-traces"]["type"] == "tempo"
        assert entries["ci-observe-traces"]["url"] == "http://127.0.0.1:3210"
        assert entries["ci-observe-traces"]["editable"] is False
        assert entries["ci-observe-traces-archive"]["type"] == "tempo"
        assert entries["ci-observe-traces-archive"]["url"] == f"http://127.0.0.1:{ci_observe.QUADLET_PORTS['tempo']}"
        assert entries["ci-observe-traces-archive"]["editable"] is False
        assert entries["ci-observe-metrics"]["jsonData"]["timeInterval"] == "60s"

    def test_existing_datasources_are_unchanged(self, datasources: dict) -> None:
        entries = {ds["uid"]: ds for ds in datasources["datasources"]}
        assert entries["prometheus"]["url"] == "http://127.0.0.1:9091"
        assert entries["prometheus"]["isDefault"] is True
        assert entries["workflow-api"]["type"] == "yesoreyeram-infinity-datasource"

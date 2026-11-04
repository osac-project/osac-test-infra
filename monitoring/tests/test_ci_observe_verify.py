"""Tests for monitoring/scripts/ci_observe_verify.py.

The verify script replaced a shell block that marked a perfect capture red:
`(( deficit = accepted - sent ))` returns 1 when the deficit is 0 under
`set -e`. These tests pin the contract that a delivery problem is a warning
and every path exits 0, plus the structure of the action step that calls it.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ACTION = Path(__file__).resolve().parents[2] / ".github" / "actions" / "ci-observe-capture" / "action.yml"
SCRIPT = SCRIPTS / "ci_observe_verify.py"


@pytest.fixture(scope="module")
def verify_module() -> ModuleType:
    import importlib.util

    specification = importlib.util.spec_from_file_location("ci_observe_verify", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def exposition(
    *,
    accepted: dict[str, float] | None = None,
    sent: dict[str, float] | None = None,
    refused: dict[str, float] | None = None,
    send_failed: dict[str, float] | None = None,
    enqueue_failed: dict[str, float] | None = None,
    queue: float | None = 0,
) -> str:
    lines = ["# HELP otelcol_exporter_sent spans", "# TYPE otelcol_exporter_sent counter"]
    for signal in ("spans", "log_records", "metric_points"):
        lines.append(f'otelcol_receiver_accepted_{signal}{{receiver="otlp"}} {(accepted or {}).get(signal, 0)}')
        lines.append(f'otelcol_receiver_refused_{signal}{{receiver="otlp"}} {(refused or {}).get(signal, 0)}')
        lines.append(f"otelcol_exporter_sent_{signal}_total {(sent or {}).get(signal, 0)}")
        lines.append(
            f'otelcol_exporter_send_failed_{signal}{{exporter="otlp_http/live"}} {(send_failed or {}).get(signal, 0)}'
        )
        lines.append(
            f'otelcol_exporter_enqueue_failed_{signal}{{exporter="otlp_http/live"}} '
            f"{(enqueue_failed or {}).get(signal, 0)}"
        )
    if queue is not None:
        lines.append(f'otelcol_exporter_queue_size{{exporter="otlp_http/live"}} {queue}')
    return "\n".join(lines) + "\n"


class Harness:
    def __init__(self, module: ModuleType, tmp_path: Path) -> None:
        self.module = module
        self.ci_dir = tmp_path / "ci-observe"
        self.ci_dir.mkdir()
        (self.ci_dir / "telemetry-requested").write_text("")
        self.output = io.StringIO()
        self.reporter = module.Reporter(stream=self.output)
        self.scrapes: list[str] = []
        self.sleeps: list[float] = []
        self.url = ""

    def scrape(self, url: str) -> str:
        self.url = url
        if not self.scrapes:
            raise AssertionError("no more scrapes configured")
        return self.scrapes.pop(0)

    def run(
        self,
        *,
        deployed: bool = True,
        metrics_port: int | None = 4123,
        duration: int = 600,
        drain_timeout: float = 20,
        pid: int | None = None,
    ) -> int:
        (self.ci_dir / "forwarder.pid").write_text(str(pid or __import__("os").getpid()))
        return self.module.verify_delivery(
            ci_dir=self.ci_dir,
            metrics_port=metrics_port,
            deployed=deployed,
            duration_seconds=duration,
            reporter=self.reporter,
            scrape=self.scrape,
            sleep=self.sleeps.append,
            drain_timeout=drain_timeout,
            drain_interval=10,
        )

    @property
    def log(self) -> str:
        return self.output.getvalue()


@pytest.fixture
def harness(verify_module: ModuleType, tmp_path: Path) -> Harness:
    return Harness(verify_module, tmp_path)


def test_complete_capture_is_green(harness: Harness) -> None:
    """accepted == sent and an empty queue: the old shell marked this red."""
    harness.scrapes = [
        exposition(
            accepted={"spans": 10, "log_records": 5, "metric_points": 3},
            sent={"spans": 10, "log_records": 5, "metric_points": 3},
        )
    ]
    assert harness.run() == 0
    assert "::warning::" not in harness.log
    assert "telemetry delivered: 10 spans, 5 logs, 3 metrics" in harness.log
    assert harness.url == "http://127.0.0.1:4123/metrics"
    # The queue was already empty, so no sleep was needed.
    assert harness.sleeps == []


def test_in_flight_items_are_reported_not_judged(harness: Harness) -> None:
    """accepted > sent with a queue that drains is the normal steady state."""
    harness.scrapes = [
        exposition(accepted={"spans": 12}, sent={"spans": 10}, queue=2),
        exposition(accepted={"spans": 12}, sent={"spans": 12}, queue=2),
        exposition(accepted={"spans": 12}, sent={"spans": 12}, queue=0),
    ]
    assert harness.run() == 0
    # The counters are read after the queue drained, not before: the batch
    # that was in flight has been exported by the last scrape.
    assert "0 reported in flight" in harness.log
    assert "telemetry delivered: 12 spans" in harness.log
    assert "batches are still in flight" not in harness.log
    assert "dropped telemetry" not in harness.log
    assert harness.sleeps == [10]


def test_transient_send_failed_is_information(harness: Harness) -> None:
    """send_failed means retries happened; it is not a drop and not a verdict."""
    harness.scrapes = [
        exposition(
            accepted={"spans": 10, "log_records": 4}, sent={"spans": 10, "log_records": 4}, send_failed={"spans": 3}
        )
    ]
    assert harness.run() == 0
    assert "3 spans send attempts failed" in harness.log
    assert "information rather than a drop" in harness.log
    assert "the gateway dropped telemetry" not in harness.log


def test_real_drops_warn_about_refused_and_unqueueable_data(harness: Harness) -> None:
    harness.scrapes = [
        exposition(accepted={"spans": 1}, sent={"spans": 1}, refused={"spans": 2}, enqueue_failed={"log_records": 1})
    ]
    assert harness.run() == 0
    assert "the gateway dropped telemetry" in harness.log
    assert "2 spans refused (otelcol_receiver_refused_spans)" in harness.log
    assert "1 log_records could not be queued (otelcol_exporter_enqueue_failed_log_records)" in harness.log


def test_gateway_gone_is_a_warning(harness: Harness) -> None:
    def scrape(url: str) -> str:
        raise OSError("connection refused")

    assert (
        harness.module.verify_delivery(
            ci_dir=harness.ci_dir,
            metrics_port=4123,
            deployed=True,
            duration_seconds=600,
            reporter=harness.reporter,
            scrape=scrape,
            sleep=harness.sleeps.append,
        )
        == 0
    )
    assert "could not scrape the gateway self-metrics endpoint" in harness.log
    assert "connection refused" in harness.log


def test_stuck_queue_reports_residue_as_in_flight(harness: Harness) -> None:
    stuck = exposition(accepted={"spans": 20}, sent={"spans": 2}, queue=7)
    harness.scrapes = [stuck, stuck, stuck]
    assert harness.run(drain_timeout=10) == 0
    assert "7 batches are still in flight after 10s" in harness.log
    assert "otelcol_exporter_queue_size" in harness.log
    assert "telemetry delivered" not in harness.log


def test_missing_otelcol_metrics_is_a_warning(harness: Harness) -> None:
    harness.scrapes = ["# nothing here\n"]
    assert harness.run() == 0
    assert "served no otelcol_* metrics" in harness.log


def test_missing_queue_metric_is_a_warning(harness: Harness) -> None:
    harness.scrapes = [exposition(accepted={"spans": 3}, sent={"spans": 3}, queue=None)]
    assert harness.run() == 0
    assert "exposes no otelcol_exporter_queue_size" in harness.log


def test_deploy_failure_is_a_warning(harness: Harness) -> None:
    (harness.ci_dir / "deploy-failed").write_text("")
    assert harness.run() == 0
    assert "the collector deployment failed" in harness.log
    assert "the test result is unaffected" in harness.log


def test_missing_deployed_marker_is_a_warning(harness: Harness) -> None:
    assert harness.run(deployed=False) == 0
    assert "never recorded a deployed gateway" in harness.log


def test_missing_metrics_port_is_a_warning(harness: Harness) -> None:
    assert harness.run(metrics_port=None) == 0
    assert "never reported the gateway self-metrics port" in harness.log


def test_not_requested_is_not_a_warning(harness: Harness) -> None:
    (harness.ci_dir / "telemetry-requested").unlink()
    assert harness.run() == 0
    assert "telemetry not requested" in harness.log
    assert "::warning::" not in harness.log


def test_nothing_collected_is_a_warning(harness: Harness) -> None:
    harness.scrapes = [exposition()]
    assert harness.run(duration=600) == 0
    assert "nothing was collected" in harness.log


def test_short_run_with_no_telemetry_is_not_a_warning(harness: Harness) -> None:
    harness.scrapes = [exposition()]
    assert harness.run(duration=5) == 0
    assert "nothing was collected" not in harness.log


def test_dead_forwarder_is_a_warning(harness: Harness) -> None:
    harness.scrapes = [exposition(accepted={"spans": 1}, sent={"spans": 1})]
    assert harness.run(pid=999999999) == 0
    assert "local forwarder is no longer running" in harness.log


def test_metric_samples_sums_families_and_strips_the_total_suffix(verify_module: ModuleType) -> None:
    text = (
        "# a comment\n"
        'otelcol_exporter_sent_spans{exporter="a"} 2\n'
        'otelcol_exporter_sent_spans{exporter="b"} 3\n'
        "otelcol_exporter_sent_spans_total 4\n"
        "otelcol_exporter_queue_size 0\n"
    )
    samples = verify_module.metric_samples(text)
    assert samples["otelcol_exporter_sent_spans"] == 9
    assert samples["otelcol_exporter_queue_size"] == 0


def test_identity_round_trip(verify_module: ModuleType, tmp_path: Path) -> None:
    capture = {
        "capture_id": "caas-main-41709318840-a1",
        "ci_run_id": "41709318840",
        "ci_run_attempt": "1",
        "ci_suite": "caas",
        "ci_pr": "752",
        "ci_workflow": "CaaS E2E",
        "ci_test_ref": "main",
        "ci_test_sha": "a" * 40,
        "ci_test_infra_ref": "main",
        "ci_test_infra_sha": "b" * 40,
        "ci_test_infra_workflow_sha": "c" * 40,
        "ci_test_suite": "tests/e2e/caas/",
        "ci_test_filter": "none",
        "ci_test_marker": "none",
    }
    path = tmp_path / "identity.json"
    verify_module.write_identity(path, capture)
    assert verify_module.read_identity(path) == capture


def test_info_payload_carries_capture_id_and_identity(verify_module: ModuleType) -> None:
    capture = {
        "capture_id": "caas-main-41709318840-a1",
        "ci_run_id": "41709318840",
        "ci_run_attempt": "2",
        "ci_suite": "caas",
        "ci_pr": "752",
        "ci_workflow": "CaaS E2E",
        "ci_test_ref": "main",
        "ci_test_sha": "a" * 40,
        "ci_test_infra_ref": "main",
        "ci_test_infra_sha": "b" * 40,
        "ci_test_infra_workflow_sha": "c" * 40,
        "ci_test_suite": "tests/e2e/caas/",
        "ci_test_filter": "none",
        "ci_test_marker": "none",
    }
    payload = verify_module.capture_info_payload(capture, now_ns=1700000000000000000)
    resource = payload["resourceMetrics"][0]
    assert resource["resource"]["attributes"] == [
        {"key": "osac.ci.capture_id", "value": {"stringValue": "caas-main-41709318840-a1"}}
    ]
    metric = resource["scopeMetrics"][0]["metrics"][0]
    assert metric["name"] == "osac_ci_capture_info"
    datapoint = metric["gauge"]["dataPoints"][0]
    assert datapoint["asInt"] == "1"
    assert datapoint["timeUnixNano"] == "1700000000000000000"
    attributes = {item["key"]: item["value"]["stringValue"] for item in datapoint["attributes"]}
    assert attributes["ci_run_id"] == "41709318840"
    assert attributes["ci_run_attempt"] == "2"
    assert attributes["ci_suite"] == "caas"
    assert attributes["ci_pr"] == "752"
    assert attributes["ci_workflow"] == "CaaS E2E"
    assert attributes["ci_test_ref"] == "main"
    assert attributes["ci_test_sha"] == "a" * 40
    assert attributes["ci_test_infra_ref"] == "main"
    assert attributes["ci_test_infra_sha"] == "b" * 40
    assert attributes["ci_test_infra_workflow_sha"] == "c" * 40
    assert attributes["ci_test_suite"] == "tests/e2e/caas/"
    assert attributes["ci_test_filter"] == "none"
    assert attributes["ci_test_marker"] == "none"


def test_info_post_uses_the_central_endpoint_and_basic_auth(verify_module: ModuleType) -> None:
    sent: dict[str, object] = {}

    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        sent.update(url=url, body=body, headers=headers, timeout=timeout)

    capture = {"capture_id": "cap-1", "ci_run_id": "41709318840", "ci_test_sha": "a" * 40}
    posted, detail = verify_module.post_capture_info(
        endpoint=verify_module.INFO_ENDPOINT, auth="Basic Y2k6b2JzZXJ2ZQ==", capture=capture, transport=transport
    )
    assert posted is True
    assert detail == ""
    assert sent["url"] == "https://osac-ci-1.redhat.com/v1/metrics"
    assert sent["headers"] == {"Content-Type": "application/json", "Authorization": "Basic Y2k6b2JzZXJ2ZQ=="}
    assert sent["timeout"] == 10
    body = json.loads(sent["body"])
    assert body["resourceMetrics"][0]["resource"]["attributes"][0]["value"]["stringValue"] == "cap-1"


def test_info_post_failure_never_raises(verify_module: ModuleType) -> None:
    def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        raise OSError("network down")

    posted, detail = verify_module.post_capture_info(
        endpoint=verify_module.INFO_ENDPOINT, auth="", capture={"capture_id": "cap-1"}, transport=transport
    )
    assert posted is False
    assert "network down" in detail


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module writes to GITHUB_STEP_SUMMARY when it is set. Running the
    suite inside a GitHub Actions job must not append ci-observe lines to
    that job's own summary."""
    for name in (
        "GITHUB_STEP_SUMMARY",
        "CI_OBSERVE_DEPLOYED",
        "CI_OBSERVE_METRICS_PORT",
        "CI_OBSERVE_TEST_DURATION",
        "CI_OBSERVE_OTLP_AUTH",
        "CI_OBSERVE_INFO_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_main_never_exits_non_zero(verify_module: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert verify_module.main([]) == 0
    assert verify_module.main(["not-a-command"]) == 0
    assert verify_module.main(["verify", "--ci-dir", "/nonexistent"]) == 0


def test_write_identity_command_writes_every_field(verify_module: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    argv = ["write-identity", "--file", str(path), "--capture-id", "cap-1"]
    for field in verify_module.IDENTITY_FIELDS:
        argv.extend(["--" + field.replace("_", "-"), field.upper()])
    assert verify_module.main(argv) == 0
    identity = json.loads(path.read_text())
    assert identity["capture_id"] == "cap-1"
    for field in verify_module.IDENTITY_FIELDS:
        assert identity[field] == field.upper()


def test_post_info_command_reads_the_identity_and_warns_on_failure(
    verify_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    identity = ci_dir / "identity.json"
    verify_module.write_identity(identity, {"capture_id": "cap-1"})
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("CI_OBSERVE_DEPLOYED", "1")
    calls: dict[str, object] = {}

    def failing_post(**kwargs: object) -> tuple[bool, str]:
        calls.update(kwargs)
        return False, "endpoint down"

    monkeypatch.setattr(verify_module, "post_capture_info", failing_post)
    assert verify_module.main(["post-info", "--ci-dir", str(ci_dir), "--identity", str(identity)]) == 0
    assert calls["capture"] == {"capture_id": "cap-1"}
    assert "the capture info metric was not posted" in summary.read_text()


def test_post_info_posts_nothing_when_the_deploy_failed(
    verify_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture in the registry with no data would read as a real capture
    that lost its telemetry; a failed deploy must leave no info series."""
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    identity = ci_dir / "identity.json"
    verify_module.write_identity(identity, {"capture_id": "cap-1"})
    (ci_dir / "deploy-failed").write_text("")
    monkeypatch.setenv("CI_OBSERVE_DEPLOYED", "1")
    posted: list[dict[str, str]] = []
    monkeypatch.setattr(verify_module, "post_capture_info", lambda **kw: posted.append(kw["capture"]) or (True, ""))
    assert verify_module.main(["post-info", "--ci-dir", str(ci_dir), "--identity", str(identity)]) == 0
    assert posted == []


def test_post_info_posts_nothing_without_the_deployed_marker(
    verify_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    identity = ci_dir / "identity.json"
    verify_module.write_identity(identity, {"capture_id": "cap-1"})
    monkeypatch.delenv("CI_OBSERVE_DEPLOYED", raising=False)
    posted: list[dict[str, str]] = []
    monkeypatch.setattr(verify_module, "post_capture_info", lambda **kw: posted.append(kw["capture"]) or (True, ""))
    assert verify_module.main(["post-info", "--ci-dir", str(ci_dir), "--identity", str(identity)]) == 0
    assert posted == []


def test_post_info_posts_once_when_the_gateway_deployed(
    verify_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    identity = ci_dir / "identity.json"
    verify_module.write_identity(identity, {"capture_id": "cap-1"})
    monkeypatch.setenv("CI_OBSERVE_DEPLOYED", "1")
    posted: list[dict[str, str]] = []
    monkeypatch.setattr(verify_module, "post_capture_info", lambda **kw: posted.append(kw["capture"]) or (True, ""))
    assert verify_module.main(["post-info", "--ci-dir", str(ci_dir), "--identity", str(identity)]) == 0
    assert posted == [{"capture_id": "cap-1"}]


@pytest.mark.parametrize(
    ("marker", "failed", "deployed"),
    [("1", False, True), ("1", True, False), ("", False, False), ("", True, False), (None, False, False)],
)
def test_telemetry_deployed_requires_the_marker_and_no_failure(
    verify_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str | None,
    failed: bool,
    deployed: bool,
) -> None:
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    if failed:
        (ci_dir / "deploy-failed").write_text("")
    if marker is None:
        monkeypatch.delenv("CI_OBSERVE_DEPLOYED", raising=False)
    else:
        monkeypatch.setenv("CI_OBSERVE_DEPLOYED", marker)
    assert verify_module.telemetry_deployed(ci_dir) is deployed


def test_verify_command_wires_env_and_posts_the_info_metric(
    verify_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    (ci_dir / "telemetry-requested").write_text("")
    verify_module.write_identity(ci_dir / "capture-identity.json", {"capture_id": "cap-1"})
    monkeypatch.setenv("CI_OBSERVE_METRICS_PORT", "4123")
    monkeypatch.setenv("CI_OBSERVE_DEPLOYED", "1")
    monkeypatch.setenv("CI_OBSERVE_TEST_DURATION", "600")
    posted: list[str] = []

    def fake_post(**kwargs: object) -> tuple[bool, str]:
        capture = kwargs["capture"]
        posted.append(capture["capture_id"])
        return True, ""

    monkeypatch.setattr(verify_module, "post_capture_info", fake_post)
    monkeypatch.setattr(verify_module, "scrape_text", lambda url: exposition(accepted={"spans": 1}, sent={"spans": 1}))
    assert verify_module.main(["verify", "--ci-dir", str(ci_dir)]) == 0
    assert posted == ["cap-1"]


def test_verify_posts_info_only_when_the_gateway_deployed(
    verify_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ci_dir = tmp_path / "ci-observe"
    ci_dir.mkdir()
    (ci_dir / "telemetry-requested").write_text("")
    verify_module.write_identity(ci_dir / "capture-identity.json", {"capture_id": "cap-1"})
    monkeypatch.setenv("CI_OBSERVE_METRICS_PORT", "4123")
    monkeypatch.setenv("CI_OBSERVE_TEST_DURATION", "600")
    monkeypatch.setattr(verify_module, "scrape_text", lambda url: exposition(accepted={"spans": 1}, sent={"spans": 1}))
    posted: list[str] = []

    def fake_post(**kwargs: object) -> tuple[bool, str]:
        posted.append(kwargs["capture"]["capture_id"])
        return True, ""

    monkeypatch.setattr(verify_module, "post_capture_info", fake_post)

    # A failed deploy: marker absent, failure marker present.
    (ci_dir / "deploy-failed").write_text("")
    monkeypatch.delenv("CI_OBSERVE_DEPLOYED", raising=False)
    assert verify_module.main(["verify", "--ci-dir", str(ci_dir)]) == 0
    assert posted == [], "a failed deploy must not create a registry capture"

    # Marker present but the deploy still failed: still no post.
    monkeypatch.setenv("CI_OBSERVE_DEPLOYED", "1")
    assert verify_module.main(["verify", "--ci-dir", str(ci_dir)]) == 0
    assert posted == []

    # A completed deploy: exactly one post.
    (ci_dir / "deploy-failed").unlink()
    assert verify_module.main(["verify", "--ci-dir", str(ci_dir)]) == 0
    assert posted == ["cap-1"]


class TestActionStructure:
    @pytest.fixture(scope="class")
    def action(self) -> dict:
        return yaml.safe_load(ACTION.read_text())

    @staticmethod
    def step(action: dict, step_id: str) -> dict:
        return next(step for step in action["runs"]["steps"] if step.get("id") == step_id)

    def test_verify_never_exits_non_zero_and_has_no_arithmetic_or_forwarder(self, action: dict) -> None:
        script = self.step(action, "verify")["run"]
        assert "exit 1" not in script
        assert "exit 0" in script
        # No bare arithmetic command: `(( x = y ))` returns 1 when the result
        # is 0 under `set -e`, which is the bug this step must not repeat.
        assert re.search(r"(?<!\$)\(\(", script) is None
        assert "port-forward" not in script
        assert "oc " not in script

    def test_verify_calls_the_tested_script(self, action: dict) -> None:
        script = self.step(action, "verify")["run"]
        assert "monitoring/scripts/ci_observe_verify.py" in script
        assert re.search(r"ci_observe_verify\.py.*verify", script, re.S) is not None
        assert "--ci-dir" in script
        assert self.step(action, "verify")["continue-on-error"] is True

    def test_deploy_opens_one_forward_for_both_ports(self, action: dict) -> None:
        script = self.step(action, "deploy")["run"]
        forward = re.findall(r"oc port-forward[^\n]*", script)
        assert len(forward) == 1, forward
        assert ":4317 :8888" in forward[0]
        assert "CI_OBSERVE_METRICS_PORT=" in script
        assert "CI_OBSERVE_OTLP_PORT=" in script

    def test_only_the_deploy_step_mentions_port_forward(self, action: dict) -> None:
        for step in action["runs"]["steps"]:
            if step.get("id") == "verify":
                assert "port-forward" not in step.get("run", "")
        assert "port-forward" not in SCRIPT.read_text()

    def test_info_metric_is_posted_after_deploy_and_in_verify(self, action: dict) -> None:
        deploy = self.step(action, "deploy")["run"]
        verify = self.step(action, "verify")["run"]
        assert "write-identity" in deploy
        assert "post-info" in deploy
        assert "post-info" not in verify, "verify posts through its own subcommand, not a second shell call"
        # The identity is recorded once, before the deploy attempt (it is a
        # diagnostic file on a failed deploy, never posted).
        assert deploy.index("write-identity") < deploy.index("if ! deploy_collectors")

    def test_the_info_post_is_gated_on_the_deployed_marker(self, action: dict) -> None:
        deploy = self.step(action, "deploy")["run"]
        # The deploy step's post carries the marker (GITHUB_ENV values only
        # reach later steps, so the shell copy is set next to it) and the ci
        # dir that holds the deploy-failed marker.
        assert 'CI_OBSERVE_DEPLOYED="${CI_OBSERVE_DEPLOYED:-}"' in deploy
        assert '--ci-dir "${ci_dir}"' in deploy
        assert "CI_OBSERVE_DEPLOYED=1" in deploy
        script = SCRIPT.read_text()
        assert "def telemetry_deployed(" in script
        assert 'ci_dir / "deploy-failed"' in script
        assert "the gateway did not deploy; the capture info metric was not posted" in script

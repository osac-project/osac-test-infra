"""Post-test delivery verification and capture identity for the ci-observe action.

Two jobs, both non-gating:

- ``post-info`` posts one ``osac_ci_capture_info`` gauge to the central OTLP
  ingest so the capture registry has an identity series for this run. It runs
  after deploy and again in verify. A failed post is a warning.
- ``verify`` reads the gateway's self-metrics and reports whether the accepted
  telemetry drained to the central backend. It always returns 0: ci-observe is
  opt-in telemetry and must never fail a test or the required E2E check.

The script never exits non-zero, including on a crash or bad arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

# Runners reach the central ingest through the HAProxy SNI route on :443,
# which forwards to the Caddy vhost on :4319. They cannot reach :4319
# directly, so posting there would never deliver the identity.
INFO_ENDPOINT = "https://osac-ci-1.redhat.com/v1/metrics"
INFO_METRIC = "osac_ci_capture_info"
INFO_TIMEOUT_SECONDS = 10
INFO_SCOPE = "ci-observe-capture"

SIGNALS = ("spans", "log_records", "metric_points")
QUEUE_SIZE = "otelcol_exporter_queue_size"
DRAIN_TIMEOUT_SECONDS = 100
DRAIN_INTERVAL_SECONDS = 10
SCRAPE_TIMEOUT_SECONDS = 30
TRIVIAL_RUN_SECONDS = 60

# Datapoint attributes. The resource attribute is only the capture id: these
# identity values must not be promoted to every series in the backend.
IDENTITY_FIELDS = (
    "ci_run_id",
    "ci_run_attempt",
    "ci_suite",
    "ci_pr",
    "ci_workflow",
    "ci_test_ref",
    "ci_test_sha",
    "ci_test_infra_ref",
    "ci_test_infra_sha",
    "ci_test_infra_workflow_sha",
    "ci_test_suite",
    "ci_test_filter",
    "ci_test_marker",
)


class Reporter:
    """Prints to the log and mirrors the important lines to the job summary."""

    def __init__(self, stream: IO[str] | None = None, summary_path: Path | None = None) -> None:
        self.stream = stream or sys.stdout
        self.summary_path = summary_path

    def line(self, text: str) -> None:
        print(text, file=self.stream)

    def summary(self, text: str) -> None:
        print(text, file=self.stream)
        if self.summary_path is None:
            return
        try:
            with self.summary_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{text}\n")
        except OSError as error:
            print(f"ci-observe: could not write the job summary: {error}", file=self.stream)

    def warning(self, text: str) -> None:
        self.line(f"::warning::ci-observe: {text}")
        self.summary(f"- :warning: ci-observe: {text}")


def _ssl_context() -> ssl.SSLContext:
    # The central ingest serves a certificate from the internal CA, which the
    # runners do not trust. The gateway collectors skip verification the same
    # way; TLS still protects the basic auth header in transit.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _post_bytes(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        response.read()


def _string_attributes(values: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": key, "value": {"stringValue": value}} for key, value in values.items()]


def capture_info_payload(capture: dict[str, str], now_ns: int | None = None) -> dict[str, Any]:
    """The OTLP/JSON body for one identity series: value 1, resource capture id,
    every run identity field as a datapoint attribute."""
    moment = time.time_ns() if now_ns is None else now_ns
    datapoint = {
        "asInt": "1",
        "timeUnixNano": str(moment),
        "attributes": _string_attributes({field: capture.get(field, "") for field in IDENTITY_FIELDS}),
    }
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _string_attributes({"osac.ci.capture_id": capture.get("capture_id", "")})},
                "scopeMetrics": [
                    {
                        "scope": {"name": INFO_SCOPE},
                        "metrics": [{"name": INFO_METRIC, "gauge": {"dataPoints": [datapoint]}}],
                    }
                ],
            }
        ]
    }


def post_capture_info(
    *,
    endpoint: str,
    auth: str,
    capture: dict[str, str],
    timeout: float = INFO_TIMEOUT_SECONDS,
    transport: Callable[[str, bytes, dict[str, str], float], None] | None = None,
) -> tuple[bool, str]:
    """Post the identity metric once. Returns (posted, detail); never raises."""
    body = json.dumps(capture_info_payload(capture)).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    send = _post_bytes if transport is None else transport
    try:
        send(endpoint, body, headers, timeout)
    except Exception as error:  # a failed post is a warning, never an exit code
        return False, str(error) or type(error).__name__
    return True, ""


def write_identity(path: Path, capture: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(capture, sort_keys=True) + "\n", encoding="utf-8")


def read_identity(path: Path) -> dict[str, str] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    return {str(key): str(value) for key, value in document.items()}


def metric_samples(text: str) -> dict[str, float]:
    """Sum every sample of each metric family in a Prometheus exposition dump.

    Lines are ``name{labels} value``. Comment lines carry no sample and are
    skipped. The collector's prometheus reader appends ``_total`` to counters
    unless without_type_suffix is set, so that suffix is stripped before the
    comparison.
    """
    totals: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("{", 1)[0]
        if name.endswith("_total"):
            name = name[: -len("_total")]
        try:
            value = float(parts[-1])
        except ValueError:
            continue
        totals[name] = totals.get(name, 0.0) + value
    return totals


def scrape_text(url: str, timeout: float = SCRAPE_TIMEOUT_SECONDS) -> str:
    with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as response:
        return response.read().decode("utf-8", "replace")


def _process_alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for_queue_drain(
    *,
    scrape: Callable[[str], str],
    url: str,
    timeout: float = DRAIN_TIMEOUT_SECONDS,
    interval: float = DRAIN_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, float | None, str, dict[str, float] | None]:
    """Poll until the exporter queue reports 0.

    Returns (state, queue size, detail, samples): ("drained", 0, "", last
    scrape), ("residue", size, "", last scrape) when the bound expires,
    ("absent", None, "", last scrape) when the collector exposes no queue
    size, or ("unreachable", None, detail, last scrape) when a scrape
    failed. The samples come back because the counters move while the queue
    drains: judging delivery on the first scrape would report a backlog that
    has already been delivered.
    """
    attempts = max(1, int(timeout / interval) + 1)
    latest: float | None = None
    samples: dict[str, float] | None = None
    for attempt in range(attempts):
        try:
            samples = metric_samples(scrape(url))
        except Exception as error:  # reported as a warning
            return "unreachable", None, str(error) or type(error).__name__, samples
        if QUEUE_SIZE not in samples:
            return "absent", None, "", samples
        latest = samples[QUEUE_SIZE]
        if latest == 0:
            return "drained", 0.0, "", samples
        if attempt + 1 < attempts:
            sleep(interval)
    return "residue", latest, "", samples


def _format_count(value: float) -> str:
    return f"{value:.0f}"


def verify_delivery(
    *,
    ci_dir: Path,
    metrics_port: int | None,
    deployed: bool,
    duration_seconds: int,
    reporter: Reporter,
    scrape: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
    drain_interval: float = DRAIN_INTERVAL_SECONDS,
) -> int:
    """Read the gateway's self-metrics and report the delivery outcome.

    Every outcome is a warning, never a failure: the return value is always 0.
    """
    if not (ci_dir / "telemetry-requested").exists():
        reporter.summary("telemetry not requested (no OTLP ingest password); the tests ran without telemetry capture")
        return 0
    if (ci_dir / "deploy-failed").exists():
        reporter.warning(
            "telemetry was requested but the collector deployment failed; the test result is unaffected "
            "and the Grafana run page will be incomplete (see the warning in the Deploy collectors step)"
        )
        return 0
    if not deployed:
        reporter.warning(
            "telemetry was requested but the collector deployment never recorded a deployed gateway; "
            "the test result is unaffected and the Grafana run page will be incomplete"
        )
        return 0
    if not _process_alive(ci_dir / "forwarder.pid"):
        reporter.warning(
            "the deploy step's local forwarder is no longer running; the test command's OTEL endpoint "
            "was dead during the run"
        )
    if not metrics_port:
        reporter.warning(
            "the deploy step's forwarder never reported the gateway self-metrics port; delivery could not be verified"
        )
        return 0

    url = f"http://127.0.0.1:{metrics_port}/metrics"
    reader = scrape_text if scrape is None else scrape
    try:
        text = reader(url)
    except Exception as error:  # a dead gateway is a warning
        reporter.warning(
            f"could not scrape the gateway self-metrics endpoint ({url}); the gateway died after the test: "
            f"{error or type(error).__name__}"
        )
        return 0

    samples = metric_samples(text)
    if not any(name.startswith("otelcol_") for name in samples):
        reporter.warning(f"the gateway self-metrics endpoint served no otelcol_* metrics ({url})")
        return 0

    # The queue is the only counter that moves after the test, so wait for it
    # before reading any counter. accepted, sent and the drop counters all
    # come from the last scrape: the first one can predate a drain that has
    # already delivered everything.
    if QUEUE_SIZE not in samples:
        queue_state, residue, detail, final_samples = "absent", None, "", samples
    elif samples[QUEUE_SIZE] == 0:
        queue_state, residue, detail, final_samples = "drained", 0.0, "", samples
    else:
        queue_state, residue, detail, final_samples = wait_for_queue_drain(
            scrape=reader, url=url, timeout=drain_timeout, interval=drain_interval, sleep=sleep
        )
    if queue_state == "unreachable":
        reporter.warning(f"could not re-scrape the gateway self-metrics endpoint ({url}) while waiting: {detail}")
        return 0
    if final_samples:
        samples = final_samples
    if queue_state == "absent":
        reporter.warning(f"the gateway exposes no {QUEUE_SIZE}; the exporter queue drain was not checked")

    # Real drops: input the gateway refused, and batches its exporter could not
    # even enqueue. send_failed is retried by the exporter and reported as
    # information below, never as a drop.
    dropped: list[str] = []
    for signal in SIGNALS:
        refused = samples.get(f"otelcol_receiver_refused_{signal}", 0.0)
        if refused:
            dropped.append(f"{_format_count(refused)} {signal} refused (otelcol_receiver_refused_{signal})")
        enqueue_failed = samples.get(f"otelcol_exporter_enqueue_failed_{signal}", 0.0)
        if enqueue_failed:
            dropped.append(
                f"{_format_count(enqueue_failed)} {signal} could not be queued "
                f"(otelcol_exporter_enqueue_failed_{signal})"
            )
    if dropped:
        reporter.warning(
            "the gateway dropped telemetry: "
            + "; ".join(dropped)
            + "; the test result is unaffected, and the Grafana run page will be incomplete"
        )

    for signal in SIGNALS:
        send_failed = samples.get(f"otelcol_exporter_send_failed_{signal}", 0.0)
        if send_failed:
            reporter.warning(
                f"{_format_count(send_failed)} {signal} send attempts failed "
                f"(otelcol_exporter_send_failed_{signal}); the exporter retries them, so this is "
                "information rather than a drop"
            )

    accepted = {signal: samples.get(f"otelcol_receiver_accepted_{signal}", 0.0) for signal in SIGNALS}
    sent = {signal: samples.get(f"otelcol_exporter_sent_{signal}", 0.0) for signal in SIGNALS}
    accepted_total = sum(accepted.values())
    sent_total = sum(sent.values())
    in_flight = max(accepted_total - sent_total, 0.0)
    reporter.line(
        f"ci-observe: accepted {_format_count(accepted_total)} items, exported {_format_count(sent_total)}; "
        f"{_format_count(in_flight)} reported in flight"
    )

    if queue_state == "residue":
        reporter.warning(
            f"{_format_count(residue or 0.0)} batches are still in flight after {int(drain_timeout)}s "
            f"({QUEUE_SIZE}); the gateway keeps sending until the cluster teardown"
        )
    elif queue_state == "drained":
        reporter.summary(
            "telemetry delivered: "
            f"{_format_count(sent['spans'])} spans, {_format_count(sent['log_records'])} logs, "
            f"{_format_count(sent['metric_points'])} metrics"
        )

    if sent_total == 0 and duration_seconds > TRIVIAL_RUN_SECONDS:
        reporter.warning(
            "nothing was collected: the gateway exported 0 spans, 0 logs and 0 metrics during a "
            f"{duration_seconds}s test run"
        )
    return 0


def _int_or_none(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _summary_path() -> Path | None:
    value = os.environ.get("GITHUB_STEP_SUMMARY", "")
    return Path(value) if value else None


def telemetry_deployed(ci_dir: Path) -> bool:
    """Whether the collectors actually deployed.

    The info metric is posted only when the gateway reached Available and the
    collectors applied: the deploy step wrote CI_OBSERVE_DEPLOYED=1 and did
    not mark the deployment failed. A capture with no data must never enter
    the registry, or the run index and the Capture page would show an empty
    capture that cannot be explained.
    """
    if os.environ.get("CI_OBSERVE_DEPLOYED") != "1":
        return False
    return not (ci_dir / "deploy-failed").exists()


def _post_info_command(args: argparse.Namespace) -> int:
    reporter = Reporter(summary_path=_summary_path())
    if not telemetry_deployed(args.ci_dir):
        reporter.line("ci-observe: the gateway did not deploy; the capture info metric was not posted")
        return 0
    identity = read_identity(args.identity)
    if identity is None:
        reporter.warning(f"no capture identity at {args.identity}; the capture info metric was not posted")
        return 0
    posted, detail = post_capture_info(
        endpoint=args.endpoint, auth=os.environ.get("CI_OBSERVE_OTLP_AUTH", ""), capture=identity
    )
    if not posted:
        reporter.warning(f"the capture info metric was not posted to {args.endpoint}: {detail}")
    else:
        reporter.line(f"ci-observe: posted {INFO_METRIC} for capture {identity.get('capture_id', '')}")
    return 0


def _write_identity_command(args: argparse.Namespace) -> int:
    capture = {"capture_id": args.capture_id}
    for field in IDENTITY_FIELDS:
        capture[field] = getattr(args, field)
    write_identity(args.file, capture)
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    ci_dir = args.ci_dir
    reporter = Reporter(summary_path=_summary_path())

    deployed = telemetry_deployed(ci_dir)
    identity = read_identity(ci_dir / "capture-identity.json") if deployed else None
    if deployed and identity is None:
        reporter.warning("the capture info metric was not posted: no capture identity was recorded after deploy")
    elif identity is not None:
        posted, detail = post_capture_info(
            endpoint=args.endpoint, auth=os.environ.get("CI_OBSERVE_OTLP_AUTH", ""), capture=identity
        )
        if not posted:
            reporter.warning(f"the capture info metric was not posted to {args.endpoint}: {detail}")

    return verify_delivery(
        ci_dir=ci_dir,
        metrics_port=_int_or_none(os.environ.get("CI_OBSERVE_METRICS_PORT")),
        deployed=deployed,
        duration_seconds=_int_or_none(os.environ.get("CI_OBSERVE_TEST_DURATION")) or 0,
        reporter=reporter,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    post_info = commands.add_parser("post-info", help="post the capture identity metric once")
    post_info.add_argument("--ci-dir", type=Path, required=True)
    post_info.add_argument("--identity", type=Path, required=True)
    post_info.add_argument("--endpoint", default=os.environ.get("CI_OBSERVE_INFO_ENDPOINT", INFO_ENDPOINT))
    post_info.set_defaults(handler=_post_info_command)

    write_identity_command = commands.add_parser("write-identity", help="record the run identity for the info metric")
    write_identity_command.add_argument("--file", type=Path, required=True)
    write_identity_command.add_argument("--capture-id", required=True)
    for field in IDENTITY_FIELDS:
        write_identity_command.add_argument("--" + field.replace("_", "-"), default="")
    write_identity_command.set_defaults(handler=_write_identity_command)

    verify = commands.add_parser("verify", help="report the gateway's delivery outcome")
    verify.add_argument("--ci-dir", type=Path, required=True)
    verify.add_argument("--endpoint", default=os.environ.get("CI_OBSERVE_INFO_ENDPOINT", INFO_ENDPOINT))
    verify.set_defaults(handler=_verify_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return int(args.handler(args))
    except SystemExit:
        # argparse reports a bad command line through SystemExit; the step must
        # still end green.
        return 0
    except Exception as error:  # the script must never fail the job
        print(f"::warning::ci-observe: verification failed unexpectedly: {error}")
        return 0


if __name__ == "__main__":
    main()
    sys.exit(0)

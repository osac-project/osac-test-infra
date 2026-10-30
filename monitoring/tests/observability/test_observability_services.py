"""Tests for the W05a CI observability service stack (PostgreSQL 17,
single-process Loki, isolated CI Prometheus, Alloy shipping redacted
spool logs).

Two tiers:

* Static tests always run: file presence, image digest pins, loopback-only
  listeners, config invariants, script syntax, secret-file guard rails,
  and the no-raw-journald / no-secrets-in-argv mechanisms.
* Real-stack tests run only with CI_OBSERVABILITY_REAL_STACK=1. They start
  ephemeral ``ci-observability-selftest-*`` containers with clearly
  disposable passwords and the shipped configs (copied into private temp
  directories -- tracked files are never bind-mounted), and exercise the
  real boundaries: PostgreSQL role and schema bootstrap over SCRAM, the
  no-secrets-in-argv property during bootstrap, backup and restore
  through container exec including refusal, rollback, and concurrent
  runs, Loki ingest/query, time-based retention deletion, CI Prometheus
  targets, runtime resource limits, and Alloy's redacted-spool path into
  Loki with sanitized fixture input.

Boundaries that are NOT covered by this file and must not be reported as
covered: systemd deployment of the quadlet units (needs the trusted
monitoring host), filesystem byte quotas (a W05/W13 rollout gate), and
anything requiring the real W06 collector. Without the environment
variable the real-container cases are SKIPPED, not counted as passing.

Run with (the ``-o addopts=`` clears pytest.ini's coverage flags, which
would otherwise fail a partial run that only executes this file):

    cd monitoring/tests
    CI_OBSERVABILITY_REAL_STACK=1 pytest -o addopts= observability -x
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests

from monitoring.ci_observability import api, ledger

MONITORING_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = MONITORING_DIR / "scripts"
CONFIG_DIR = MONITORING_DIR / "config" / "observability"
QUADLET_DIR = MONITORING_DIR / "quadlet"

STACK_SCRIPT = SCRIPTS_DIR / "observability-stack.sh"
BACKUP_SCRIPT = SCRIPTS_DIR / "ci-observability-backup.sh"
RESTORE_SCRIPT = SCRIPTS_DIR / "ci-observability-restore.sh"

POSTGRES_IMAGE = "docker.io/library/postgres@sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"
LOKI_IMAGE = "docker.io/grafana/loki@sha256:0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28"
PROMETHEUS_IMAGE = (
    "quay.io/prometheus/prometheus@sha256:fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2"
)
ALLOY_IMAGE = "docker.io/grafana/alloy@sha256:89a5a320168387336b9ac3c850609c3ba7757eadd4ae5f1d819deb35e9854b18"

UNIT_FILES = [
    "ci-observability-postgres.container",
    "ci-observability-loki.container",
    "ci-observability-prometheus.container",
    "ci-observability-alloy.container",
]

# The deployment names these tests must never touch (they belong to the
# fleet monitoring stack deployed by monitoring-setup.sh).
FLEET_UNIT_NAMES = ["prometheus.container", "grafana.container", "alertmanager.container", "node-exporter.container"]

REAL_STACK_REQUESTED = os.environ.get("CI_OBSERVABILITY_REAL_STACK") == "1"

real_stack = pytest.mark.skipif(
    not REAL_STACK_REQUESTED,
    reason="opt-in local tier not requested: set CI_OBSERVABILITY_REAL_STACK=1 to run the "
    "real-container boundary tests (PostgreSQL, Loki, Prometheus, Alloy)",
)


@dataclass(frozen=True, kw_only=True)
class ContainerFixture:
    name: str
    work: Path


def run(
    command: list[str], *, env: dict[str, str] | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(command, capture_output=True, text=True, env=merged, timeout=timeout, check=False)


def podman(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["podman", *args], timeout=timeout)


def make_temp_dir(prefix: str) -> Path:
    """A stable directory for container bind mounts.

    pytest's tmp_path_factory dirs sit behind a symlink and are deleted by
    later pytest sessions, which breaks mounts of still-running containers.
    These dirs live under /tmp/opencode (the approved local scratch area)
    when available, are owned by this run, and are removed by teardown."""
    base = "/tmp/opencode" if os.access("/tmp/opencode", os.W_OK) else None
    return Path(tempfile.mkdtemp(prefix=f"ci-observability-selftest-{prefix}-", dir=base))


def copy_config(name: str, destination: Path) -> Path:
    """Copy a tracked config into a disposable directory and return the copy.

    Tests never bind-mount tracked files: a :Z mount would relabel the
    checkout."""
    target = destination / name
    shutil.copy(CONFIG_DIR / name, target)
    return target


def http_get(
    url: str, *, params: dict[str, str | int] | None = None, headers: dict[str, str] | None = None, timeout: int = 15
) -> requests.Response:
    return requests.get(url, params=params, headers=headers, timeout=timeout)


def http_ready(url: str) -> bool:
    """Readiness polling: tolerate refused connections and slow startups."""
    try:
        return http_get(url, timeout=5).status_code == 200
    except requests.RequestException:
        return False


def wait_until(description: str, fn: Callable[[], object], *, attempts: int = 30, delay: float = 1.0) -> None:
    for _ in range(attempts):
        if fn():
            return
        time.sleep(delay)
    raise AssertionError(f"timed out waiting for {description}")


def require_free_ports(ports: list[int], *, for_container: str) -> None:
    """Fail (not skip) when a required fixed port is already taken: a real
    deployment or another listener owns it, and the test must not collide."""
    for port in ports:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                pytest.fail(f"port {port} is already in use; {for_container} cannot start")


def remove_container(name: str) -> None:
    result = podman(["rm", "-f", name], timeout=180)
    assert result.returncode == 0, f"failed to clean up container {name}: {result.stderr}"


def remove_volume(name: str) -> None:
    result = podman(["volume", "rm", "--force", name])
    assert result.returncode == 0, f"failed to clean up volume {name}: {result.stderr}"


def assert_container_limits(name: str, *, memory_mb: int, cpus: float, pids: int) -> None:
    """The quadlets pin memory/cpu/pids; the test containers run with the
    same flags, and this proves the limits are actually applied."""
    result = podman(
        ["inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}}", name]
    )
    assert result.returncode == 0, result.stderr
    memory, nanocpus, pids_limit = result.stdout.split()
    assert int(memory) == memory_mb * 1024 * 1024, f"{name}: memory {memory}"
    assert int(nanocpus) == int(cpus * 1e9), f"{name}: cpus {nanocpus}"
    assert int(pids_limit) == pids, f"{name}: pids {pids_limit}"


# Static tests


def test_quadlet_units_exist_and_are_namespaced() -> None:
    for name in UNIT_FILES:
        unit = (QUADLET_DIR / name).read_text()
        assert f"ContainerName={name.removesuffix('.container')}" in unit
        # The stack's units must never carry the fleet units' names.
        for fleet in FLEET_UNIT_NAMES:
            assert f"ContainerName={fleet.removesuffix('.container')}" not in unit


def test_images_are_digest_pinned() -> None:
    pins = {}
    for name in UNIT_FILES:
        unit_lines = (QUADLET_DIR / name).read_text().splitlines()
        image = next(line.removeprefix("Image=") for line in unit_lines if line.startswith("Image="))
        assert re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}", image), f"{name}: {image}"
        pins[name] = image
    assert pins["ci-observability-postgres.container"] == POSTGRES_IMAGE
    assert pins["ci-observability-loki.container"] == LOKI_IMAGE
    assert pins["ci-observability-prometheus.container"] == PROMETHEUS_IMAGE
    assert pins["ci-observability-alloy.container"] == ALLOY_IMAGE


def test_units_expose_loopback_only() -> None:
    postgres = (QUADLET_DIR / "ci-observability-postgres.container").read_text()
    assert "PublishPort=127.0.0.1:5433:5432" in postgres
    for name in UNIT_FILES:
        unit = (QUADLET_DIR / name).read_text()
        assert "0.0.0.0:" not in unit, f"{name} publishes a non-loopback port"
    loki = (QUADLET_DIR / "ci-observability-loki.container").read_text()
    prometheus = (QUADLET_DIR / "ci-observability-prometheus.container").read_text()
    alloy = (QUADLET_DIR / "ci-observability-alloy.container").read_text()
    assert "Network=host" in loki and "Network=host" in prometheus and "Network=host" in alloy
    # Host-network units must pin their listeners to loopback themselves.
    assert "--web.listen-address=127.0.0.1:9095" in prometheus
    assert "--server.http.listen-addr=127.0.0.1:12345" in alloy
    # Loki's listeners are pinned in loki.yml, asserted below.


def test_resource_limits_are_set() -> None:
    for name in UNIT_FILES:
        unit = (QUADLET_DIR / name).read_text()
        podman_args = next(line for line in unit.splitlines() if line.startswith("PodmanArgs="))
        assert re.search(r"--memory=\d+[mg]", podman_args), name
        assert "--pids-limit=" in podman_args, name
        assert "--cpus=" in podman_args, name
    prometheus = (QUADLET_DIR / "ci-observability-prometheus.container").read_text()
    assert "--storage.tsdb.retention.time=7d" in prometheus
    assert "--storage.tsdb.retention.size=4GB" in prometheus
    # The quota language must stay honest: retention.size is a soft
    # target, not a hard filesystem cap.
    assert "not a hard filesystem cap" in prometheus


def test_loki_config_invariants() -> None:
    config = (CONFIG_DIR / "loki.yml").read_text()
    assert "http_listen_address: 127.0.0.1" in config
    assert "http_listen_port: 3100" in config
    # 9095 belongs to the CI Prometheus; Loki's gRPC must stay off it.
    assert "grpc_listen_port: 9097" in config
    assert "auth_enabled: false" in config
    assert "retention_enabled: true" in config
    assert "delete_request_store: filesystem" in config
    assert "retention_period: 336h" in config
    assert "allow_structured_metadata: true" in config
    assert "reject_old_samples: true" in config
    assert "reject_old_samples_max_age: 168h" in config
    assert "schema: v13" in config
    assert "object_store: filesystem" in config
    assert "max_query_lookback: 336h" in config
    assert "ingestion_rate_mb: 4" in config
    # Loopback advertisement must match the loopback listeners.
    assert "instance_addr: 127.0.0.1" in config
    assert "address: 127.0.0.1" in config


def test_ci_prometheus_config_invariants() -> None:
    config = (CONFIG_DIR / "ci-prometheus.yml").read_text()
    assert "scrape_interval: 15s" in config
    targets = re.findall(r"- 127\.0\.0\.1:(\d+)", config)
    assert sorted(targets) == ["12345", "9095", "9185"]
    assert "0.0.0.0" not in config
    # W01's ledger endpoint is part of the interface even though W05a does
    # not implement it.
    assert "ci-observability-ledger" in config and "9185" in config


def test_alloy_config_has_no_raw_log_source() -> None:
    config = (CONFIG_DIR / "alloy-config.alloy").read_text()
    unit = (QUADLET_DIR / "ci-observability-alloy.container").read_text()
    # Redaction happens before any storage: no journald or other raw-log
    # source, and no host journal mounts, by design.
    for forbidden in ("loki.source.journal", "/var/log/journal", "/run/log/journal"):
        assert forbidden not in config, forbidden
        assert forbidden not in unit, forbidden
    # The only input is the private redacted spool, mounted read-only.
    assert '__path__ = "/spool/redacted/*.ndjson"' in config
    assert "Volume=%h/.ci-observability/spool/redacted:/spool/redacted:ro,Z" in unit
    # Static, bounded stream labels only; the auto filename label is
    # dropped, and run/test identity rides structured metadata.
    replacements = re.findall(r'replacement\s*=\s*"([^"]+)"', config)
    assert sorted(replacements) == ["ci-observability-pilot", "redacted-spool"]
    assert 'values = ["filename"]' in config
    assert "run_id" in config and "test_id" in config
    # Malformed (non-JSON) spool lines are dropped, never shipped
    # verbatim; strict schema and redaction stay W06's job.
    assert "drop_malformed = true" in config
    assert 'url = "http://127.0.0.1:3100/loki/api/v1/push"' in config


def test_postgres_init_uses_private_secret_mechanism() -> None:
    script = (CONFIG_DIR / "postgres-init-roles.sh").read_text()
    # psql \getenv pulls each password from the environment inside the
    # psql process; nothing is interpolated by the shell.
    assert "\\getenv ingest_password CI_OBSERVABILITY_INGEST_PASSWORD" in script
    assert "\\getenv reader_password CI_OBSERVABILITY_READER_PASSWORD" in script
    # No secrets on any command line, and no SQL literals.
    assert "-v ingest_password" not in script
    assert "-v reader_password" not in script
    assert "<<'EOSQL'" in script
    assert not re.search(r"PASSWORD\s+'[^:]", script)


def test_postgres_init_roles_invariants() -> None:
    script = (CONFIG_DIR / "postgres-init-roles.sh").read_text()
    assert "CREATE ROLE ci_observability LOGIN" in script
    assert "CREATE ROLE ci_observability_reader LOGIN" in script
    assert "CREATE SCHEMA IF NOT EXISTS ci_observability" in script
    assert "GRANT SELECT ON TABLES TO ci_observability_reader" in script
    assert "search_path TO ci_observability" in script
    # The dedicated database inherits nothing from PUBLIC: only the two
    # stack roles connect, only ingest gets TEMP, nobody creates in
    # schema public.
    assert 'REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC' in script
    assert 'GRANT CONNECT ON DATABASE :"dbname" TO ci_observability, ci_observability_reader' in script
    assert 'GRANT TEMP ON DATABASE :"dbname" TO ci_observability' in script
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in script
    for var in ("CI_OBSERVABILITY_INGEST_PASSWORD", "CI_OBSERVABILITY_READER_PASSWORD"):
        assert f': "${{{var}:?' in script


def test_scripts_pass_bash_n() -> None:
    for script in (STACK_SCRIPT, BACKUP_SCRIPT, RESTORE_SCRIPT, CONFIG_DIR / "postgres-init-roles.sh"):
        result = run(["bash", "-n", str(script)])
        assert result.returncode == 0, f"{script}: {result.stderr}"


def test_scripts_stay_off_the_fleet_stack() -> None:
    scripts = [STACK_SCRIPT, BACKUP_SCRIPT, RESTORE_SCRIPT]
    for script in scripts:
        # Comments may explain the separation; code must not reference the
        # fleet stack's files.
        code = "\n".join(line for line in script.read_text().splitlines() if not line.lstrip().startswith("#"))
        assert ".monitoring-server" not in code, script
    units_block = re.search(r"UNITS=\((.*?)\)", STACK_SCRIPT.read_text(), re.DOTALL)
    assert units_block is not None
    units = re.findall(r"(ci-observability-[\w-]+)", units_block.group(1))
    assert units == [
        "ci-observability-postgres",
        "ci-observability-loki",
        "ci-observability-prometheus",
        "ci-observability-alloy",
    ]


def test_status_fails_unhealthy_and_offline_reports_gap() -> None:
    script = STACK_SCRIPT.read_text()
    # --status exits nonzero when anything is unhealthy, and --check
    # --offline says explicitly that it is not full acceptance.
    assert "stack is not healthy" in script
    assert "NOT a full acceptance check" in script


def test_deploy_never_enables_and_down_never_disables() -> None:
    """Generated quadlet units cannot be enabled or disabled through
    systemctl ("transient or generated"); the generator's own
    default.target.wants links are the enablement. The script must rely
    on daemon-reload + restart for deploy and on removing the installed
    definitions for teardown."""
    code = "\n".join(line for line in STACK_SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    assert "systemctl --user enable" not in code
    assert "systemctl --user disable" not in code
    assert "systemctl --user daemon-reload" in code
    assert 'systemctl --user restart "${unit}.service"' in code


def test_docs_file_exists() -> None:
    doc = (MONITORING_DIR / "observability-services.md").read_text()
    for needle in (
        "127.0.0.1:5433",
        "127.0.0.1:3100",
        "127.0.0.1:9095",
        "127.0.0.1:12345",
        "pilot",
        "spool/redacted",
        "query-only",
        "not a hard filesystem cap",
    ):
        assert needle in doc


# Script behavior tests (no long-running containers)


@pytest.fixture()
def stack_home(tmp_path: Path) -> Path:
    home = tmp_path / "ci-observability"
    # Real deployments create the stack home with mode 700; the strict
    # parent checks require the same here.
    home.mkdir(mode=0o700)
    return home


def stack_env(stack_home: Path) -> dict[str, str]:
    return {"CI_OBSERVABILITY_HOME": str(stack_home)}


def write_secrets(stack_home: Path, *, super_pw: str, ingest_pw: str, reader_pw: str) -> Path:
    secrets_dir = stack_home / "secrets"
    if not secrets_dir.exists():
        secrets_dir.mkdir(mode=0o700)
    secrets = secrets_dir / "postgres.env"
    # Reset whatever a previous test section left behind, including
    # symlinks.
    if secrets.is_symlink() or secrets.exists():
        secrets.unlink()
    secrets.write_text(
        f"POSTGRES_PASSWORD={super_pw}\n"
        f"CI_OBSERVABILITY_INGEST_PASSWORD={ingest_pw}\n"
        f"CI_OBSERVABILITY_READER_PASSWORD={reader_pw}\n"
    )
    secrets.chmod(0o600)
    return secrets


def check_offline(stack_home: Path) -> subprocess.CompletedProcess[str]:
    return run(["bash", str(STACK_SCRIPT), "--check", "--offline"], env=stack_env(stack_home))


def test_init_secrets_creates_protected_file(stack_home: Path) -> None:
    result = run(["bash", str(STACK_SCRIPT), "--init-secrets"], env=stack_env(stack_home))
    assert result.returncode == 0, result.stderr
    secrets = stack_home / "secrets" / "postgres.env"
    assert secrets.exists()
    assert (secrets.stat().st_mode & 0o777) == 0o600
    values = {}
    for line in secrets.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    assert set(values) == {"POSTGRES_PASSWORD", "CI_OBSERVABILITY_INGEST_PASSWORD", "CI_OBSERVABILITY_READER_PASSWORD"}
    assert all(len(value) >= 32 for value in values.values())
    # Refuses to overwrite.
    before = secrets.read_text()
    result = run(["bash", str(STACK_SCRIPT), "--init-secrets"], env=stack_env(stack_home))
    assert result.returncode != 0
    assert "refusing to overwrite" in result.stderr
    assert secrets.read_text() == before


def test_concurrent_init_secrets_create_exactly_one_file(stack_home: Path) -> None:
    env = dict(os.environ)
    env["CI_OBSERVABILITY_HOME"] = str(stack_home)
    processes = [
        subprocess.Popen(
            ["bash", str(STACK_SCRIPT), "--init-secrets"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    returncodes = [process.wait(timeout=60) for process in processes]
    assert sorted(returncode == 0 for returncode in returncodes) == [False, True]
    secrets = stack_home / "secrets" / "postgres.env"
    assert secrets.exists()
    # The winner's file is complete and valid.
    assert check_offline(stack_home).returncode == 0
    # No leftover temporary files from the loser.
    assert list((stack_home / "secrets").iterdir()) == [secrets]


def test_check_and_plan_with_generated_secrets(stack_home: Path) -> None:
    assert run(["bash", str(STACK_SCRIPT), "--init-secrets"], env=stack_env(stack_home)).returncode == 0
    plan = run(["bash", str(STACK_SCRIPT), "--plan"], env=stack_env(stack_home))
    assert plan.returncode == 0, plan.stderr
    for needle in (
        "127.0.0.1:5433",
        "127.0.0.1:3100",
        "127.0.0.1:9095",
        "127.0.0.1:12345",
        "ci-observability-postgres",
        "spool/redacted",
    ):
        assert needle in plan.stdout
    check = check_offline(stack_home)
    assert check.returncode == 0, check.stderr + check.stdout
    # The offline gap is reported explicitly, never silently accepted.
    assert "NOT a full acceptance check" in check.stdout


def test_check_fails_closed_on_bad_secrets(stack_home: Path) -> None:
    good_super = "a" * 32
    good_ingest = "b" * 32
    good_reader = "c" * 32

    def checked(content: str | None, *, chmod: int = 0o600) -> subprocess.CompletedProcess[str]:
        secrets = write_secrets(stack_home, super_pw=good_super, ingest_pw=good_ingest, reader_pw=good_reader)
        if content is not None:
            secrets.write_text(content)
        secrets.chmod(chmod)
        return check_offline(stack_home)

    # Missing file.
    result = check_offline(stack_home)
    assert result.returncode != 0
    assert "init-secrets" in result.stderr

    good_content = (
        f"POSTGRES_PASSWORD={good_super}\n"
        f"CI_OBSERVABILITY_INGEST_PASSWORD={good_ingest}\n"
        f"CI_OBSERVABILITY_READER_PASSWORD={good_reader}\n"
    )

    # World-readable file.
    result = checked(None, chmod=0o644)
    assert result.returncode != 0
    assert "mode 600" in result.stderr

    # Too short.
    result = checked(good_content.replace(good_super, "short"))
    assert result.returncode != 0
    assert "shorter than 16" in result.stderr

    # Known weak value, long enough to pass the length check.
    result = checked(good_content.replace(good_super, "changeme-changeme-changeme"))
    assert result.returncode != 0
    assert "weak" in result.stderr

    # Duplicate key.
    result = checked(good_content + f"POSTGRES_PASSWORD={good_super}\n")
    assert result.returncode != 0
    assert "duplicate key POSTGRES_PASSWORD" in result.stderr

    # Unexpected key.
    result = checked(good_content + "EXTRA_KEY=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
    assert result.returncode != 0
    assert "EXTRA_KEY is not in the allowlist" in result.stderr

    # Malformed line.
    result = checked(good_content + "JUST_A_KEY_WITHOUT_VALUE\n")
    assert result.returncode != 0
    assert "malformed line" in result.stderr

    # Value outside the EnvironmentFile-safe charset.
    result = checked(good_content.replace(good_super, "has space and $hell"))
    assert result.returncode != 0
    assert "outside the safe set" in result.stderr

    # Symlinked secrets file.
    stash_file = stack_home / "secrets" / "stash.env"
    write_secrets(stack_home, super_pw=good_super, ingest_pw=good_ingest, reader_pw=good_reader)
    shutil.move(str(stack_home / "secrets" / "postgres.env"), str(stash_file))
    (stack_home / "secrets" / "postgres.env").symlink_to(stash_file)
    result = check_offline(stack_home)
    assert result.returncode != 0
    assert "symlink" in result.stderr

    # Group-accessible parent directory.
    write_secrets(stack_home, super_pw=good_super, ingest_pw=good_ingest, reader_pw=good_reader)
    (stack_home / "secrets").chmod(0o750)
    result = check_offline(stack_home)
    assert result.returncode != 0
    assert "group- or world-accessible" in result.stderr
    (stack_home / "secrets").chmod(0o700)

    # Symlinked secrets directory.
    secrets_dir = stack_home / "secrets"
    stash = stack_home / "stash"
    shutil.move(str(secrets_dir), str(stash))
    secrets_dir.symlink_to(stash)
    result = check_offline(stack_home)
    assert result.returncode != 0
    assert "symlink" in result.stderr


@real_stack
def test_backup_requires_a_running_container(stack_home: Path) -> None:
    result = run(
        ["bash", str(BACKUP_SCRIPT)],
        env={
            "CI_OBSERVABILITY_HOME": str(stack_home),
            "CI_OBSERVABILITY_POSTGRES_CONTAINER": "ci-observability-selftest-absent",
        },
    )
    assert result.returncode != 0
    assert "not running" in result.stderr


def test_restore_argument_guard_rails(tmp_path: Path) -> None:
    env = {"CI_OBSERVABILITY_POSTGRES_CONTAINER": "ci-observability-selftest-absent"}
    # No dump argument.
    result = run(["bash", str(RESTORE_SCRIPT)], env=env)
    assert result.returncode != 0
    assert "Usage" in result.stderr or "Usage" in result.stdout
    # Symlinked dump file is refused outright.
    real_dump = tmp_path / "real.dump"
    real_dump.write_bytes(b"PGDMP" + b"\x00" * 64)
    link = tmp_path / "link.dump"
    link.symlink_to(real_dump)
    result = run(["bash", str(RESTORE_SCRIPT), str(link)], env=env)
    assert result.returncode != 0
    assert "symlink" in result.stderr


def test_backup_refuses_symlinked_output_dir(tmp_path: Path) -> None:
    real_dir = tmp_path / "real-backups"
    real_dir.mkdir()
    link = tmp_path / "link-backups"
    link.symlink_to(real_dir)
    result = run(
        ["bash", str(BACKUP_SCRIPT), "--output-dir", str(link)],
        env={"CI_OBSERVABILITY_POSTGRES_CONTAINER": "ci-observability-selftest-absent"},
    )
    assert result.returncode != 0
    assert "output dir" in result.stderr and "symlink" in result.stderr


def test_down_rehearsal_with_fake_systemctl(tmp_path: Path) -> None:
    """Rehearsal only: a private HOME and a fake systemctl that records
    every call. No home or live systemd is touched. Proves --down stops
    only the four units, never calls enable or disable, moves exactly
    the four installed definitions to a protected directory, and leaves
    everything else untouched."""
    home = tmp_path / "home"
    quadlet_dir = home / ".config" / "containers" / "systemd"
    quadlet_dir.mkdir(parents=True)
    stack_home = tmp_path / "stack"
    stack_home.mkdir(mode=0o700)

    def install_unit_definitions() -> None:
        for name in UNIT_FILES:
            shutil.copy(QUADLET_DIR / name, quadlet_dir / name)

    install_unit_definitions()
    # An unrelated unit definition that must survive untouched.
    unrelated = quadlet_dir / "unrelated-fleet.container"
    unrelated.write_text("# someone else's unit definition\n")
    # State that must survive --down.
    (stack_home / "data" / "loki").mkdir(parents=True)
    (stack_home / "backups").mkdir()
    (stack_home / "backups" / "keep.dump").write_bytes(b"PGDMP")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    fake_systemctl = fake_bin / "systemctl"
    fake_systemctl.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> {shlex.quote(str(log))}\n'
        'if [[ "$2" == "show" ]]; then echo loaded; fi\n'
        'if [[ "$2" == "is-active" ]]; then exit 3; fi\n'
        "exit 0\n"
    )
    fake_systemctl.chmod(0o755)
    env = {"CI_OBSERVABILITY_HOME": str(stack_home), "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    result = run(["bash", str(STACK_SCRIPT), "--down"], env=env)
    assert result.returncode == 0, result.stderr + result.stdout

    calls = log.read_text().splitlines()
    expected = {"--user daemon-reload"}
    for unit in (
        "ci-observability-postgres",
        "ci-observability-loki",
        "ci-observability-prometheus",
        "ci-observability-alloy",
    ):
        expected.add(f"--user show --property=LoadState --value {unit}.service")
        expected.add(f"--user stop {unit}.service")
    assert set(calls) == expected, calls
    assert not any("enable" in call or "disable" in call for call in calls)

    # Exactly the four definitions moved; the unrelated one untouched.
    disabled_dir = stack_home / "disabled-quadlets"
    assert sorted(path.name for path in disabled_dir.iterdir()) == sorted(UNIT_FILES)
    assert list(quadlet_dir.iterdir()) == [unrelated]
    assert (disabled_dir.stat().st_mode & 0o777) == 0o700
    # Data, secrets area, and backups preserved.
    assert (stack_home / "data" / "loki").is_dir()
    assert (stack_home / "backups" / "keep.dump").read_bytes() == b"PGDMP"

    # A second --down after restoring the definitions keeps both copies
    # (timestamped suffix), never overwriting the saved ones.
    install_unit_definitions()
    result = run(["bash", str(STACK_SCRIPT), "--down"], env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    saved = sorted(path.name for path in disabled_dir.iterdir())
    assert len(saved) == 8, saved
    assert sorted(name for name in saved if name in UNIT_FILES) == sorted(UNIT_FILES)
    assert all(name not in UNIT_FILES for name in saved if name.count(".") > 1)
    assert list(quadlet_dir.iterdir()) == [unrelated]


def quadlet_binary() -> str | None:
    for candidate in (
        "/usr/libexec/podman/quadlet",
        "/usr/lib/podman/quadlet",
        "/usr/lib/systemd/system-generators/podman-system-generator",
        "/usr/libexec/systemd/system-generators/podman-system-generator",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


QUADLET_BIN = quadlet_binary()


@pytest.mark.skipif(
    QUADLET_BIN is None or shutil.which("systemctl") is None,
    reason="offline enablement proof needs the podman quadlet generator and systemctl",
)
def test_quadlet_units_enable_offline_via_generator(tmp_path: Path) -> None:
    """Prove, offline, that the shipped units' [Install] sections create
    the default.target.wants enablement: first with the real podman
    quadlet generator against private copied units and a private output
    directory, then with systemd's offline `--root --global enable`. No
    live systemd is involved."""
    home = tmp_path / "home"
    units_src = home / ".config" / "containers" / "systemd"
    units_src.mkdir(parents=True)
    run_dir = home / "run"
    run_dir.mkdir()
    out = tmp_path / "generated"
    out.mkdir()
    for name in UNIT_FILES:
        shutil.copy(QUADLET_DIR / name, units_src / name)

    result = run([QUADLET_BIN, "-user", str(out)], env={"HOME": str(home), "XDG_RUNTIME_DIR": str(run_dir)})
    assert result.returncode == 0, result.stderr + result.stdout
    service_names = []
    for name in UNIT_FILES:
        service_name = name.removesuffix(".container") + ".service"
        service_names.append(service_name)
        service = out / service_name
        assert service.is_file(), service_name
        assert "WantedBy=default.target" in service.read_text()
        # The generator itself creates the enablement links -- this is
        # why systemctl enable must not be used on these units.
        wants_link = out / "default.target.wants" / service_name
        assert wants_link.is_symlink(), wants_link

    # Independent offline proof of the [Install] section with systemd.
    root = tmp_path / "systemd-root"
    user_units = root / "usr" / "lib" / "systemd" / "user"
    user_units.mkdir(parents=True)
    for service_name in service_names:
        shutil.copy(out / service_name, user_units / service_name)
    result = run(["systemctl", f"--root={root}", "--global", "enable", *service_names])
    assert result.returncode == 0, result.stderr + result.stdout
    for service_name in service_names:
        link = root / "etc" / "systemd" / "user" / "default.target.wants" / service_name
        assert link.is_symlink(), link


# Real-container tests (CI_OBSERVABILITY_REAL_STACK=1)

PG_SUPER_PW = "disposable-local-fixture-superuser-password"
PG_INGEST_PW = "disposable-local-fixture-ingest-password"
PG_READER_PW = "disposable-local-fixture-reader-password"


def test_real_stack_environment_is_usable() -> None:
    if not REAL_STACK_REQUESTED:
        pytest.skip("opt-in local tier not requested")
    # Requested but unusable must FAIL, not skip.
    assert shutil.which("podman") is not None, "CI_OBSERVABILITY_REAL_STACK=1 requires podman"
    assert QUADLET_BIN is not None, "CI_OBSERVABILITY_REAL_STACK=1 requires the Quadlet generator"
    assert shutil.which("systemctl") is not None, "CI_OBSERVABILITY_REAL_STACK=1 requires offline systemctl checks"


def start_postgres(*, name: str, volume: str, port_args: list[str], init_script: Path | None) -> None:
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--memory=768m",
        "--memory-swap=768m",
        "--cpus=1.0",
        "--pids-limit=160",
        *port_args,
        "-e",
        f"POSTGRES_PASSWORD={PG_SUPER_PW}",
        "-e",
        f"CI_OBSERVABILITY_INGEST_PASSWORD={PG_INGEST_PW}",
        "-e",
        f"CI_OBSERVABILITY_READER_PASSWORD={PG_READER_PW}",
        "-e",
        "POSTGRES_USER=postgres",
        "-e",
        "POSTGRES_DB=ci_observability",
        "-e",
        "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
        "-v",
        f"{volume}:/var/lib/postgresql/data",
    ]
    if init_script is not None:
        # Mount the copy; tracked files are never bind-mounted.
        args += ["-v", f"{init_script}:/docker-entrypoint-initdb.d/10-roles.sh:ro,Z"]
    # Same server tuning as the unit's Exec line.
    args += [
        POSTGRES_IMAGE,
        "postgres",
        "-c",
        "shared_buffers=128MB",
        "-c",
        "max_connections=40",
        "-c",
        "work_mem=8MB",
        "-c",
        "maintenance_work_mem=64MB",
    ]
    result = podman(args, timeout=180)
    assert result.returncode == 0, result.stderr


def wait_postgres_ready(container: str) -> list[str]:
    """Wait for first-boot completion while sampling every process's
    command line, so the argv checks below cover the init window."""
    samples: list[str] = []
    for _ in range(60):
        procs = podman(
            [
                "exec",
                container,
                "bash",
                "-c",
                "for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < \"$f\" 2>/dev/null; echo; done",
            ],
            timeout=30,
        )
        if procs.returncode == 0:
            samples.append(procs.stdout)
        if podman(["exec", container, "pg_isready", "-U", "postgres", "-d", "ci_observability"]).returncode == 0:
            return samples
        time.sleep(1)
    raise AssertionError(f"{container} never became ready")


def psql(container: str, sql: str, *, user: str = "postgres", password: str | None = None) -> str:
    args = ["exec"]
    if password is not None:
        # TCP through the container's loopback, exercising SCRAM auth.
        args += ["-e", f"PGPASSWORD={password}"]
    # Without a password, use the in-container unix socket (trust).
    args += [container, "psql"]
    if password is not None:
        args += ["-h", "127.0.0.1"]
    args += ["-U", user, "-d", "ci_observability", "--tuples-only", "--no-align", "--command", sql]
    result = podman(args)
    assert result.returncode == 0, f"psql {sql!r}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture(scope="module")
def pg_container() -> Iterator[ContainerFixture]:
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    name = f"ci-observability-selftest-pg-{suffix}"
    volume = f"ci-observability-selftest-pgdata-{suffix}"
    work = make_temp_dir("pg")
    assert podman(["volume", "create", volume]).returncode == 0
    # The init script is a tracked file: mount a copy, not the original.
    init_copy = copy_config("postgres-init-roles.sh", work)
    start_postgres(name=name, volume=volume, port_args=["-p", "127.0.0.1::5432"], init_script=init_copy)
    samples = wait_postgres_ready(name)
    # The secrets must never appear on any process's command line.
    for sample in samples:
        assert PG_SUPER_PW not in sample, "superuser password leaked into argv"
        assert PG_INGEST_PW not in sample, "ingest password leaked into argv"
        assert PG_READER_PW not in sample, "reader password leaked into argv"
    assert samples, "no process samples were collected"
    assert any("postgres" in sample for sample in samples), "sampling did not cover the init window"
    assert_container_limits(name, memory_mb=768, cpus=1.0, pids=160)
    yield ContainerFixture(name=name, work=work)
    remove_container(name)
    remove_volume(volume)
    shutil.rmtree(work, ignore_errors=True)


@real_stack
def test_postgres_bootstrap_roles_and_privileges(pg_container: ContainerFixture) -> None:
    container = pg_container.name
    # Database, schema, both roles, and the search_path default exist.
    assert psql(container, "SELECT count(*) FROM pg_database WHERE datname = 'ci_observability'") == "1"
    assert psql(container, "SELECT count(*) FROM pg_namespace WHERE nspname = 'ci_observability'") == "1"
    roles = psql(container, "SELECT rolname FROM pg_roles WHERE rolname LIKE 'ci_observability%' ORDER BY 1")
    assert roles.splitlines() == ["ci_observability", "ci_observability_reader"]
    assert "search_path=ci_observability, public" in psql(
        container,
        "SELECT setconfig FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase "
        "WHERE datname = 'ci_observability'",
    )
    # The dedicated database inherits nothing from PUBLIC.
    acl = psql(
        container,
        "SELECT count(*) FROM aclexplode((SELECT datacl FROM pg_database WHERE datname = 'ci_observability')) "
        "WHERE grantee = 0",
    )
    assert acl == "0", "PUBLIC still holds privileges on the dedicated database"
    # Ingest role over TCP (SCRAM): can create and write, has TEMP.
    psql(
        container,
        "CREATE TABLE w05a_selftest (id int, note text); INSERT INTO w05a_selftest VALUES (1, 'bootstrap')",
        user="ci_observability",
        password=PG_INGEST_PW,
    )
    assert psql(container, "SELECT has_database_privilege('ci_observability', 'ci_observability', 'TEMP')") == "t"
    # Reader role over TCP (SCRAM): can read (including the new table via
    # default privileges), cannot write, cannot create anywhere, and has
    # no TEMP.
    note = psql(container, "SELECT note FROM w05a_selftest", user="ci_observability_reader", password=PG_READER_PW)
    assert note == "bootstrap"
    assert (
        psql(container, "SELECT has_database_privilege('ci_observability_reader', 'ci_observability', 'TEMP')") == "f"
    )
    for denied_sql in (
        "INSERT INTO w05a_selftest VALUES (2, 'nope')",
        "CREATE TEMP TABLE probe(x int)",
        "CREATE TABLE public.probe(x int)",
    ):
        denied = podman(
            [
                "exec",
                "-e",
                f"PGPASSWORD={PG_READER_PW}",
                container,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "ci_observability_reader",
                "-d",
                "ci_observability",
                "-c",
                denied_sql,
            ]
        )
        assert denied.returncode != 0, denied_sql
        assert "permission denied" in denied.stderr


@real_stack
def test_backup_and_restore_through_container_exec(pg_container: ContainerFixture, tmp_path: Path) -> None:
    container = pg_container.name
    env = {"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": container}
    backup = run(["bash", str(BACKUP_SCRIPT)], env=env, timeout=180)
    assert backup.returncode == 0, backup.stderr
    dump = next((tmp_path / "backups").glob("ci_observability_*.dump"))
    assert dump.stat().st_mode & 0o777 == 0o600
    assert dump.read_bytes().startswith(b"PGDMP")

    # Restore over live data is refused without --force.
    refused = run(["bash", str(RESTORE_SCRIPT), str(dump)], env=env, timeout=180)
    assert refused.returncode != 0
    assert "refusing to restore" in refused.stderr

    # Data loss path: drop the table, restore without --force into the
    # now-empty schema, and the row comes back readable by the reader.
    psql(container, "DROP TABLE w05a_selftest")
    restored = run(["bash", str(RESTORE_SCRIPT), str(dump)], env=env, timeout=180)
    assert restored.returncode == 0, restored.stderr
    note = psql(container, "SELECT note FROM w05a_selftest", user="ci_observability_reader", password=PG_READER_PW)
    assert note == "bootstrap"

    # --force explicitly replaces live data.
    forced = run(["bash", str(RESTORE_SCRIPT), str(dump), "--force"], env=env, timeout=180)
    assert forced.returncode == 0, forced.stderr
    assert psql(container, "SELECT count(*) FROM pg_tables WHERE schemaname = 'ci_observability'") == "1"


@real_stack
def test_concurrent_backups_both_succeed(pg_container: ContainerFixture, tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update({"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": pg_container.name})
    processes = [
        subprocess.Popen(
            ["bash", str(BACKUP_SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=180) for process in processes]
    returncodes = [process.returncode for process in processes]
    assert returncodes == [0, 0], outputs
    dumps = list((tmp_path / "backups").glob("ci_observability_*.dump"))
    assert len(dumps) == 2, "a concurrent backup was lost or clobbered"
    for dump in dumps:
        assert dump.read_bytes().startswith(b"PGDMP")
    # No leftover temporary files.
    assert sorted(path.name for path in (tmp_path / "backups").iterdir()) == sorted(path.name for path in dumps)


@real_stack
def test_restore_rejects_invalid_archives(pg_container: ContainerFixture, tmp_path: Path) -> None:
    container = pg_container.name
    env = {"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": container}
    backup = run(["bash", str(BACKUP_SCRIPT)], env=env, timeout=180)
    assert backup.returncode == 0, backup.stderr
    dump = next((tmp_path / "backups").glob("ci_observability_*.dump"))

    # Complete garbage is refused by the pre-validation.
    garbage = tmp_path / "garbage.dump"
    garbage.write_bytes(bytes(range(256)) * 8)
    refused = run(["bash", str(RESTORE_SCRIPT), str(garbage)], env=env, timeout=180)
    assert refused.returncode != 0
    assert "not a readable pg_dump custom-format archive" in refused.stderr

    # A corrupted middle (header and table of contents intact) also fails
    # validation, and the database is untouched.
    corrupt = tmp_path / "corrupt.dump"
    data = bytearray(dump.read_bytes())
    middle = len(data) // 2
    for i in range(middle, min(middle + 400, len(data))):
        data[i] ^= 0xFF
    corrupt.write_bytes(data)
    refused = run(["bash", str(RESTORE_SCRIPT), str(corrupt), "--force"], env=env, timeout=180)
    assert refused.returncode != 0
    assert psql(container, "SELECT count(*) FROM pg_tables WHERE schemaname = 'ci_observability'") == "1"


@real_stack
def test_restore_failure_rolls_back(pg_container: ContainerFixture, tmp_path: Path) -> None:
    """A valid archive that fails mid-restore (here: the stack roles do
    not exist in the target) must leave the previous data unchanged --
    the drop and recreate run inside one transaction."""
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    name = f"ci-observability-selftest-pg-noroles-{suffix}"
    volume = f"ci-observability-selftest-pgdata-noroles-{suffix}"
    try:
        assert podman(["volume", "create", volume]).returncode == 0
        start_postgres(name=name, volume=volume, port_args=[], init_script=None)
        wait_postgres_ready(name)
        psql(
            name,
            "CREATE SCHEMA ci_observability; "
            "CREATE TABLE ci_observability.before_restore(v text); "
            "INSERT INTO ci_observability.before_restore VALUES ('must-survive')",
        )
        env = {"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": pg_container.name}
        backup = run(["bash", str(BACKUP_SCRIPT)], env=env, timeout=180)
        assert backup.returncode == 0, backup.stderr
        dump = next((tmp_path / "backups").glob("ci_observability_*.dump"))
        failed = run(
            ["bash", str(RESTORE_SCRIPT), str(dump), "--force"],
            env={"CI_OBSERVABILITY_POSTGRES_CONTAINER": name},
            timeout=180,
        )
        assert failed.returncode != 0
        assert "left unchanged" in failed.stderr
        # The single transaction rolled the drop back.
        assert psql(name, "SELECT v FROM ci_observability.before_restore") == "must-survive"
    finally:
        remove_container(name)
        remove_volume(volume)


@real_stack
def test_backup_restores_only_the_stack_schema(pg_container: ContainerFixture, tmp_path: Path) -> None:
    container = pg_container.name
    # An unrelated schema in the same database must be outside every
    # backup and restore.
    psql(
        container,
        "CREATE SCHEMA IF NOT EXISTS unrelated_schema; "
        "DROP TABLE IF EXISTS unrelated_schema.keepout; "
        "CREATE TABLE unrelated_schema.keepout AS SELECT 42 AS answer",
    )
    env = {"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": container}
    backup = run(["bash", str(BACKUP_SCRIPT)], env=env, timeout=180)
    assert backup.returncode == 0, backup.stderr
    dump = next((tmp_path / "backups").glob("ci_observability_*.dump"))
    forced = run(["bash", str(RESTORE_SCRIPT), str(dump), "--force"], env=env, timeout=180)
    assert forced.returncode == 0, forced.stderr
    assert psql(container, "SELECT answer FROM unrelated_schema.keepout") == "42"
    # And the stack schema came back with its own data.
    note = psql(container, "SELECT note FROM w05a_selftest", user="ci_observability_reader", password=PG_READER_PW)
    assert note == "bootstrap"
    psql(container, "DROP TABLE unrelated_schema.keepout; DROP SCHEMA unrelated_schema")


@real_stack
def test_restore_into_fresh_container(pg_container: ContainerFixture, tmp_path: Path) -> None:
    """Restore into a second, freshly-initialized container (empty
    schema): the default, non-forced path must succeed there."""
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    name = f"ci-observability-selftest-pg2-{suffix}"
    volume = f"ci-observability-selftest-pgdata2-{suffix}"
    try:
        assert podman(["volume", "create", volume]).returncode == 0
        init_copy = copy_config("postgres-init-roles.sh", tmp_path)
        start_postgres(name=name, volume=volume, port_args=[], init_script=init_copy)
        wait_postgres_ready(name)
        env = {"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": pg_container.name}
        backup = run(["bash", str(BACKUP_SCRIPT)], env=env, timeout=180)
        assert backup.returncode == 0, backup.stderr
        dump = next((tmp_path / "backups").glob("ci_observability_*.dump"))
        restored = run(
            ["bash", str(RESTORE_SCRIPT), str(dump)], env={"CI_OBSERVABILITY_POSTGRES_CONTAINER": name}, timeout=180
        )
        assert restored.returncode == 0, restored.stderr
        assert psql(name, "SELECT note FROM w05a_selftest") == "bootstrap"
    finally:
        remove_container(name)
        remove_volume(volume)


@pytest.fixture(scope="module")
def loki_container() -> Iterator[ContainerFixture]:
    require_free_ports([3100, 9097], for_container="the selftest Loki")
    name = f"ci-observability-selftest-loki-{os.getpid()}"
    work = make_temp_dir("loki")
    data = work / "data"
    data.mkdir()
    config = copy_config("loki.yml", work)
    result = podman(
        [
            "run",
            "-d",
            "--name",
            name,
            "--network=host",
            "--userns=keep-id",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "--memory=512m",
            "--memory-swap=512m",
            "--cpus=0.5",
            "--pids-limit=128",
            "-v",
            f"{config}:/etc/loki/loki.yml:ro,Z",
            "-v",
            f"{data}:/data:Z",
            LOKI_IMAGE,
            "-config.file=/etc/loki/loki.yml",
        ],
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    wait_until("loki ready", lambda: http_ready("http://127.0.0.1:3100/ready"))
    assert_container_limits(name, memory_mb=512, cpus=0.5, pids=128)
    yield ContainerFixture(name=name, work=work)
    remove_container(name)
    shutil.rmtree(work, ignore_errors=True)


@real_stack
def test_loki_ingest_and_query(loki_container: ContainerFixture) -> None:
    now_ns = f"{time.time_ns()}"
    push = requests.post(
        "http://127.0.0.1:3100/loki/api/v1/push",
        json={
            "streams": [
                {
                    "stream": {"osac_stack": "ci-observability-pilot", "selftest": "ingest"},
                    "values": [[now_ns, "w05a selftest line"]],
                }
            ]
        },
        timeout=15,
    )
    assert push.status_code == 204, push.text
    end = time.time()
    query = http_get(
        "http://127.0.0.1:3100/loki/api/v1/query_range",
        params={
            "query": '{osac_stack="ci-observability-pilot", selftest="ingest"}',
            "start": int((end - 300) * 1e9),
            "end": int((end + 60) * 1e9),
        },
    )
    body = query.json()
    assert body["status"] == "success"
    lines = [value[1] for result in body["data"]["result"] for value in result["values"]]
    assert "w05a selftest line" in lines


@real_stack
def test_loki_retention_deletes_expired_chunks() -> None:
    """Real time-based retention: a stream older than the (shortened)
    retention period is marked, physically deleted from the chunk store,
    and stops being queryable; a fresh stream survives.

    The retention period stays above Loki's documented 24h minimum, so
    the expired stream is timestamped ~26h in the past instead. Restart
    #1 is required because the TSDB shipper only uploads the index
    partition once the ingester recovers its WAL; restart #2 drops the
    in-memory copy the ingester kept, so the final query can only be
    served from the store."""
    require_free_ports([3101, 9098], for_container="the short-retention Loki")
    name = f"ci-observability-selftest-loki-ret-{os.getpid()}"
    work = make_temp_dir("loki-retention")
    data = work / "data"
    data.mkdir()
    # Variant of the shipped config: 25h retention, fast compaction, no
    # deletion delay, and non-default ports so the main test instance can
    # keep running.
    variant = work / "loki-short.yml"
    variant.write_text(
        (CONFIG_DIR / "loki.yml")
        .read_text()
        .replace("retention_period: 336h", "retention_period: 25h")
        .replace("retention_enabled: true", "retention_enabled: false")
        .replace("http_listen_port: 3100", "http_listen_port: 3101")
        .replace("grpc_listen_port: 9097", "grpc_listen_port: 9098")
        .replace(
            "  working_directory: /data/compactor",
            "  working_directory: /data/compactor\n  compaction_interval: 10s\n  retention_delete_delay: 0s",
        )
    )
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--network=host",
        "--userns=keep-id",
        "-u",
        f"{os.getuid()}:{os.getgid()}",
        "--memory=512m",
        "--memory-swap=512m",
        "--cpus=0.5",
        "--pids-limit=128",
        "-v",
        f"{variant}:/etc/loki/loki.yml:ro,Z",
        "-v",
        f"{data}:/data:Z",
        LOKI_IMAGE,
        "-config.file=/etc/loki/loki.yml",
    ]
    try:
        assert podman(args, timeout=180).returncode == 0
        assert_container_limits(name, memory_mb=512, cpus=0.5, pids=128)
        wait_until("short-retention loki ready", lambda: http_ready("http://127.0.0.1:3101/ready"))
        old_ns = int((time.time() - 26 * 3600) * 1e9)
        new_ns = time.time_ns()
        # Separate streams: a chunk's lifetime covers its newest line, so
        # the expired line must not share a stream with the fresh one.
        push = requests.post(
            "http://127.0.0.1:3101/loki/api/v1/push",
            json={
                "streams": [
                    {"stream": {"selftest": "retention-old"}, "values": [[str(old_ns), "expired line"]]},
                    {"stream": {"selftest": "retention-new"}, "values": [[str(new_ns), "fresh line"]]},
                ]
            },
            timeout=15,
        )
        assert push.status_code == 204, push.text
        assert requests.post("http://127.0.0.1:3101/flush", timeout=15).status_code == 204
        # Restart #1 seals the WAL partition and ships the index table to
        # the store, where the compactor can apply retention.
        podman(["stop", "-t", "30", name], timeout=120)
        assert podman(["start", name], timeout=120).returncode == 0
        wait_until("loki ready after restart", lambda: http_ready("http://127.0.0.1:3101/ready"))

        def old_stream_is_queryable() -> bool:
            observed = http_get(
                "http://127.0.0.1:3101/loki/api/v1/query_range",
                params={
                    "query": '{selftest="retention-old"}',
                    "start": old_ns - 3600 * 1_000_000_000,
                    "end": old_ns + 3600 * 1_000_000_000,
                },
            ).json()
            return bool(observed["data"]["result"])

        wait_until("old stream stored before retention starts", old_stream_is_queryable, attempts=30, delay=2)

        # The retention sweeper's real deletion: the expired chunk file
        # disappears from the store while the fresh stream's stays.
        def store_chunk_count() -> int:
            chunk_dir = data / "chunks" / "fake"
            return len(list(chunk_dir.glob("*/*"))) if chunk_dir.exists() else 0

        assert store_chunk_count() >= 2, "both stored chunks must exist before enabling deletion"
        assert podman(["stop", "-t", "30", name], timeout=120).returncode == 0
        variant.write_text(variant.read_text().replace("retention_enabled: false", "retention_enabled: true"))
        assert podman(["start", name], timeout=120).returncode == 0
        wait_until("retention-enabled Loki ready", lambda: http_ready("http://127.0.0.1:3101/ready"))
        wait_until("expired chunk deleted from the store", lambda: store_chunk_count() == 1, attempts=40, delay=5.0)

        # Restart #2 drops the in-memory copy the ingester kept from WAL
        # recovery (it also acts as a chunk cache), so the query below can
        # only be served from the store.
        podman(["stop", "-t", "30", name], timeout=120)
        assert podman(["start", name], timeout=120).returncode == 0
        wait_until("loki ready after second restart", lambda: http_ready("http://127.0.0.1:3101/ready"))

        end = time.time()
        body = http_get(
            "http://127.0.0.1:3101/loki/api/v1/query_range",
            params={
                "query": '{selftest="retention-old"}',
                "start": int((end - 26.5 * 3600) * 1e9),
                "end": int(end * 1e9),
            },
        ).json()
        assert len(body["data"]["result"]) == 0, "expired stream must be gone from queries"
        body = http_get(
            "http://127.0.0.1:3101/loki/api/v1/query_range",
            params={"query": '{selftest="retention-new"}', "start": int((end - 600) * 1e9), "end": int(end * 1e9)},
        ).json()
        assert len(body["data"]["result"]) == 1, "fresh stream must survive retention"
    finally:
        remove_container(name)
        shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def prometheus_container() -> Iterator[ContainerFixture]:
    require_free_ports([9095], for_container="the selftest CI Prometheus")
    name = f"ci-observability-selftest-prom-{os.getpid()}"
    work = make_temp_dir("prom")
    data = work / "data"
    data.mkdir()
    config = copy_config("ci-prometheus.yml", work)
    try:
        result = podman(
            [
                "run",
                "-d",
                "--name",
                name,
                "--network=host",
                "--userns=keep-id",
                "-u",
                f"{os.getuid()}:{os.getgid()}",
                "--memory=768m",
                "--memory-swap=768m",
                "--cpus=0.5",
                "--pids-limit=128",
                "-v",
                f"{config}:/etc/prometheus/ci-prometheus.yml:ro,Z",
                "-v",
                f"{data}:/prometheus:Z",
                PROMETHEUS_IMAGE,
                "--config.file=/etc/prometheus/ci-prometheus.yml",
                "--storage.tsdb.path=/prometheus",
                "--storage.tsdb.retention.time=7d",
                "--storage.tsdb.retention.size=4GB",
                "--web.listen-address=127.0.0.1:9095",
            ],
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        wait_until("prometheus ready", lambda: http_ready("http://127.0.0.1:9095/-/ready"))
        assert_container_limits(name, memory_mb=768, cpus=0.5, pids=128)
        yield ContainerFixture(name=name, work=work)
    finally:
        remove_container(name)
        shutil.rmtree(work, ignore_errors=True)


@real_stack
def test_ci_prometheus_targets(prometheus_container: ContainerFixture) -> None:
    # Wait for at least one completed scrape of itself.
    def self_up() -> bool:
        body = http_get("http://127.0.0.1:9095/api/v1/query", params={"query": "up"}).json()
        results = body["data"]["result"]
        return any(r["metric"].get("job") == "ci-observability-prometheus" and r["value"][1] == "1" for r in results)

    wait_until("prometheus self-scrape up", self_up, attempts=10, delay=3.0)
    targets = http_get("http://127.0.0.1:9095/api/v1/targets").json()
    jobs = {target["labels"]["job"] for target in targets["data"]["activeTargets"]}
    assert jobs == {"ci-observability-prometheus", "ci-observability-alloy", "ci-observability-ledger"}
    scrape_urls = {target["scrapeUrl"] for target in targets["data"]["activeTargets"]}
    assert all(url.startswith("http://127.0.0.1:") for url in scrape_urls), scrape_urls


@real_stack
def test_ci_prometheus_scrapes_the_read_api_health_metric(
    pg_container: ContainerFixture, prometheus_container: ContainerFixture
) -> None:
    require_free_ports([9185], for_container="the read API metrics fixture")
    mapping = podman(["port", pg_container.name, "5432/tcp"])
    assert mapping.returncode == 0, mapping.stderr
    port = int(mapping.stdout.strip().rsplit(":", 1)[1])
    database = f"127.0.0.1:{port}/ci_observability"
    ledger.apply_migrations(dsn=f"postgresql://ci_observability:{PG_INGEST_PW}@{database}")
    server = api.ReadAPIServer(dsn=f"postgresql://ci_observability_reader:{PG_READER_PW}@{database}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def health_metric_is_present() -> bool:
        response = http_get(
            "http://127.0.0.1:9095/api/v1/query", params={"query": "ci_observability_ledger_healthcheck_success"}
        )
        response.raise_for_status()
        results = response.json()["data"]["result"]
        return any(result["value"][1] == "1" for result in results)

    try:
        wait_until("read API health metric in CI Prometheus", health_metric_is_present, attempts=12, delay=3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@real_stack
def test_postgres_source_privacy_survives_backup_restore(*, pg_container: ContainerFixture, tmp_path: Path) -> None:
    """Exercise the deployed roles and restore scripts, not synthetic grants."""
    container = pg_container.name
    mapping = podman(["port", container, "5432/tcp"])
    assert mapping.returncode == 0, mapping.stderr
    port = int(mapping.stdout.strip().rsplit(":", 1)[1])
    ledger.apply_migrations(dsn=f"postgresql://ci_observability:{PG_INGEST_PW}@127.0.0.1:{port}/ci_observability")

    permissions_query = (
        "SELECT has_table_privilege(current_user, table_name, 'SELECT'),"
        " has_any_column_privilege(current_user, table_name, 'SELECT')"
        " FROM (VALUES ('ci_observability.source_receipt'),"
        " ('ci_observability.source_execution'),"
        " ('ci_observability.source_github_observation'),"
        " ('ci_observability.source_redaction_proof')) AS sources(table_name)"
    )
    before = psql(container, permissions_query, user="ci_observability_reader", password=PG_READER_PW)
    assert before.splitlines() == ["f|f"] * 4
    assert (
        psql(container, "SELECT count(*) FROM ledger_execution", user="ci_observability_reader", password=PG_READER_PW)
        == "0"
    )

    env = {"CI_OBSERVABILITY_HOME": str(tmp_path), "CI_OBSERVABILITY_POSTGRES_CONTAINER": container}
    backup = run(["bash", str(BACKUP_SCRIPT)], env=env, timeout=180)
    assert backup.returncode == 0, backup.stderr
    dump = next((tmp_path / "backups").glob("ci_observability_*.dump"))
    restored = run(["bash", str(RESTORE_SCRIPT), str(dump), "--force"], env=env, timeout=180)
    assert restored.returncode == 0, restored.stderr

    after = psql(container, permissions_query, user="ci_observability_reader", password=PG_READER_PW)
    assert after.splitlines() == ["f|f"] * 4
    assert (
        psql(container, "SELECT count(*) FROM ledger_execution", user="ci_observability_reader", password=PG_READER_PW)
        == "0"
    )


@real_stack
def test_stack_script_full_check(tmp_path: Path) -> None:
    """The shipped configs validated by the real upstream binaries
    (loki -verify-config, promtool check config, alloy validate) through
    the stack script's own --check."""
    env = {"CI_OBSERVABILITY_HOME": str(tmp_path)}
    assert run(["bash", str(STACK_SCRIPT), "--init-secrets"], env=env).returncode == 0
    result = run(["bash", str(STACK_SCRIPT), "--check"], env=env, timeout=300)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "accepted by loki -verify-config" in result.stdout
    assert "accepted by promtool check config" in result.stdout
    assert "accepted by alloy validate" in result.stdout


@real_stack
def test_alloy_ships_redacted_spool_to_loki(
    loki_container: ContainerFixture, prometheus_container: ContainerFixture
) -> None:
    """The shipped Alloy config, unchanged, with sanitized fixture input
    shaped like the documented W06 spool contract. No raw-log source is
    involved anywhere in this path."""
    require_free_ports([12345], for_container="the selftest Alloy")
    name = f"ci-observability-selftest-alloy-{os.getpid()}"
    work = make_temp_dir("alloy")
    spool = work / "spool" / "redacted"
    spool.mkdir(parents=True)
    state = work / "state"
    state.mkdir()
    config = copy_config("alloy-config.alloy", work)
    try:
        result = podman(
            [
                "run",
                "-d",
                "--name",
                name,
                "--network=host",
                "--memory=512m",
                "--memory-swap=512m",
                "--cpus=0.5",
                "--pids-limit=128",
                "-v",
                f"{config}:/etc/alloy/config.alloy:ro,Z",
                "-v",
                f"{state}:/var/lib/alloy/data:Z",
                "-v",
                f"{spool}:/spool/redacted:ro,Z",
                ALLOY_IMAGE,
                "run",
                "--server.http.listen-addr=127.0.0.1:12345",
                "/etc/alloy/config.alloy",
            ],
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        assert_container_limits(name, memory_mb=512, cpus=0.5, pids=128)
        wait_until("alloy ready", lambda: http_ready("http://127.0.0.1:12345/-/ready"))

        def spool_lines() -> list[str]:
            end = time.time()
            body = http_get(
                "http://127.0.0.1:3100/loki/api/v1/query_range",
                params={
                    "query": '{osac_stack="ci-observability-pilot", source="redacted-spool"}',
                    "start": int((end - 900) * 1e9),
                    "end": int((end + 60) * 1e9),
                },
            ).json()
            return [value[1] for r in body["data"]["result"] for value in r["values"]]

        # Source availability stays missing until files arrive: an empty
        # spool ships nothing.
        time.sleep(3)
        assert spool_lines() == []

        # Sanitized fixture input: redacted message, allowlisted
        # structured metadata, one unexpected key that must be dropped
        # by the allowlist, and malformed non-JSON plaintext carrying a
        # secret marker that must never reach Loki.
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{time.time_ns() % 1_000_000_000:09d}Z"
        malformed_marker = "THIS IS NOT JSON AT ALL selftest-secret-marker-9f2c"
        fixture = (
            f'{{"timestamp": "{timestamp}", "message": "redacted spool line one", '
            '"run_id": "run-42", "test_id": "test-a"}\n'
            f'{{"timestamp": "{timestamp}", "message": "redacted spool line two", '
            '"run_id": "run-42", "extra_key": "must-not-appear"}\n'
            f"{malformed_marker}\n"
        )
        (spool / "w06-fixture.ndjson").write_text(fixture)

        wait_until(
            "alloy spool lines in loki", lambda: "redacted spool line two" in spool_lines(), attempts=12, delay=5.0
        )
        assert "redacted spool line one" in spool_lines()
        # Malformed plaintext is dropped by drop_malformed; its secret
        # marker never reaches Loki.
        all_lines = spool_lines()
        assert malformed_marker not in all_lines
        assert not any("THIS IS NOT JSON" in line for line in all_lines)
        assert not any("selftest-secret-marker" in line for line in all_lines)

        # Categorized response: run/test identity must be structured
        # metadata, never stream labels, and the stream labels must be the
        # bounded static set (no filename, no run_id).
        end = time.time()
        body = http_get(
            "http://127.0.0.1:3100/loki/api/v1/query_range",
            params={
                "query": '{osac_stack="ci-observability-pilot", source="redacted-spool"}',
                "start": int((end - 900) * 1e9),
                "end": int((end + 60) * 1e9),
            },
            headers={"X-Loki-Response-Encoding-Flags": "categorize-labels"},
        ).json()
        for result_entry in body["data"]["result"]:
            stream = result_entry["stream"]
            assert "run_id" not in stream, "run_id must not be a stream label"
            assert "filename" not in stream, "filename must not be a stream label"
            assert set(stream) <= {"osac_stack", "source", "service_name"}, stream
        metas = [
            value[2].get("structuredMetadata", {})
            for result_entry in body["data"]["result"]
            for value in result_entry["values"]
        ]
        by_line = {
            value[1]: value[2].get("structuredMetadata", {})
            for result_entry in body["data"]["result"]
            for value in result_entry["values"]
        }
        assert by_line["redacted spool line one"].get("run_id") == "run-42"
        assert by_line["redacted spool line one"].get("test_id") == "test-a"
        assert by_line["redacted spool line two"].get("run_id") == "run-42"
        for meta in metas:
            assert "extra_key" not in meta, "non-allowlisted metadata must be dropped"

        # The endpoint the CI Prometheus scrapes answers, and the
        # prometheus target for alloy flips to up.
        assert http_get("http://127.0.0.1:12345/metrics").status_code == 200

        def alloy_up() -> bool:
            query = http_get("http://127.0.0.1:9095/api/v1/query", params={"query": "up"}).json()
            return any(
                r["metric"].get("job") == "ci-observability-alloy" and r["value"][1] == "1"
                for r in query["data"]["result"]
            )

        wait_until("alloy target up in ci prometheus", alloy_up, attempts=12, delay=5.0)
    finally:
        remove_container(name)
        shutil.rmtree(work, ignore_errors=True)

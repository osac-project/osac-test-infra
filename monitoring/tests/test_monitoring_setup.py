"""The provisioning behavior of monitoring-setup.sh.

Two behaviors are pinned here because both have broken silently before:

- Quadlet-generated services cannot be enabled -- systemd refuses
  ("transient or generated"); their boot activation is the generator's
  WantedBy symlink. The enable loop used to exit 1 on the first generated
  unit of a healthy host and never enabled the plain units after it.
- --update-central used to exit 0 at "Central update complete." before the
  script's final otelcol-router check, so a deployed Caddyfile proxying
  OTLP to a dead upstream read as a green job.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

MONITORING = Path(__file__).resolve().parents[1]
SCRIPT = MONITORING / "scripts" / "monitoring-setup.sh"


def update_central_block() -> str:
    setup = SCRIPT.read_text()
    return setup.split('if [[ "${MODE}" == "update-central" ]]', 1)[1].split(
        'if [[ "${MODE}" == "update-agent" ]]', 1
    )[0]


def test_the_enable_loop_skips_quadlet_generated_units_and_fails_on_real_ones(tmp_path):
    """enable_units runs against a fake systemctl: the generated units are
    skipped (their boot activation is the generator's), the plain ones are
    enabled, and a plain unit that cannot be enabled fails the setup."""
    function = re.search(r"(?ms)^enable_units\(\) \{.*?^\}", SCRIPT.read_text())
    assert function, "enable_units must stay a top-level function of monitoring-setup.sh"

    calls = tmp_path / "calls"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${2}" == "is-enabled" ]]; then\n'
        "    case \"${3}\" in\n"
        "        node-exporter.service|prometheus.service) echo generated; exit 0 ;;\n"
        "        *) echo disabled; exit 1 ;;\n"
        "    esac\n"
        "fi\n"
        'if [[ "${2}" == "enable" ]]; then\n'
        '    echo "enable ${3}" >> ' + shlex.quote(str(calls)) + "\n"
        '    [[ "${3}" == "bad.timer" ]] && exit 1\n'
        "    exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    systemctl.chmod(0o755)
    probe = function.group(0) + '\nenable_units "$@"\n'

    def run(*units):
        return subprocess.run(
            ["bash", "-c", probe, "probe", *units],
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        )

    # A generated unit is not an enable call; the plain ones after it still
    # are -- the loop must not stop or fail before reaching them.
    result = run("node-exporter.service", "service-health-textfile.timer")
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["enable service-health-textfile.timer"]

    # A plain unit that cannot be enabled fails the setup and names it.
    result = run("node-exporter.service", "bad.timer")
    assert result.returncode == 1
    assert "bad.timer" in result.stderr
    assert "will not come back" in result.stderr


def test_update_central_exits_nonzero_over_a_dead_otelcol_router():
    """The unhealthy-router guard must run inside --update-central, before
    its own exit 0: the mode never reaches the script's final check, and a
    deployed Caddyfile proxying OTLP to a dead upstream must not read as a
    green update."""
    block = update_central_block()

    complete = block.index('info "Central update complete."')
    guard = block.index("OTEL_ROUTER_UNHEALTHY", complete)
    exit_zero = block.index("exit 0", guard)
    assert "exit 1" in block[guard:exit_zero], "the guard must fail the update, not warn"
    assert "deployed Caddyfile" in block[guard:exit_zero], "the error says what is wrong"


# -- every piece the units need reaches the host, in both central modes ------------------------

QUADLETS = MONITORING / "quadlet"
TIMERS = MONITORING / "systemd"


def full_install_block() -> str:
    """The script after the update modes: the first --central install."""
    rest = SCRIPT.read_text().split('if [[ "${MODE}" == "update-agent" ]]', 1)[1]
    return rest.split("\n    exit 0\nfi\n", 1)[1]


def bash_array(text: str, name: str) -> list[str]:
    match = re.search(rf"(?ms)^{name}=\((.*?)^\)", text)
    assert match, f"{name} is not an array of monitoring-setup.sh"
    return re.findall(r'"([^"]+)"', match.group(1))


def mounted_scripts(quadlet: Path) -> list[str]:
    return re.findall(r"^Volume=%h/\.monitoring-server/scripts/([^:]+):", quadlet.read_text(), re.M)


def test_every_script_a_container_mounts_is_copied_by_both_central_modes():
    """podman refuses a missing bind-mount source: a script a quadlet mounts
    but an install does not copy is a container that does not start (a first
    install lacked ci_observe_logs.py, so it had no exporter)."""
    update, full = update_central_block(), full_install_block()
    for quadlet in sorted(QUADLETS.glob("*.container")):
        for script in mounted_scripts(quadlet):
            source = f'"${{MONITORING_REPO_DIR}}/scripts/{script}"'
            assert source in update, f"--update-central does not copy {script} ({quadlet.name} mounts it)"
            assert source in full, f"--central does not copy {script} ({quadlet.name} mounts it)"


def test_the_incidents_file_reaches_the_exporter_container():
    """The incidents layer is a config file the exporter reads, not a script:
    the quadlet mounts it (podman refuses a missing source, so a mode that
    does not copy it stops the exporter on restart) and points the reader at
    it, and the image carries the YAML parser it reads with."""
    quadlet = (QUADLETS / "workflow-exporter.container").read_text()
    assert "config/ci-observe/incidents.yml:/app/incidents.yml:ro,Z" in quadlet
    assert "Environment=CI_OBSERVE_INCIDENTS_FILE=/app/incidents.yml" in quadlet
    copy = 'cp "${MONITORING_REPO_DIR}/config/ci-observe/incidents.yml"'
    for name, block in (("--update-central", update_central_block()),
                        ("--central", full_install_block())):
        assert copy in block, f"{name} does not copy the incidents file"
    containerfile = (MONITORING / "containers" / "Containerfile.workflow-exporter").read_text()
    assert "pyyaml" in containerfile, "the exporter reads YAML incidents records"


def test_both_central_modes_install_the_same_containers():
    update = update_central_block()
    loop = re.search(r"(?ms)for unit in (.*?); do", update).group(1)
    updated = set(re.findall(r"[\w.-]+\.container", loop))
    setup = SCRIPT.read_text()
    installed = set(bash_array(setup, "CENTRAL_UNITS")) | set(bash_array(setup, "COMMON_UNITS"))
    assert updated == installed, f"only one mode installs: {sorted(updated ^ installed)}"
    assert "ci-observe-mcp.container" in installed


def test_every_ci_observe_timer_runs_after_both_central_modes():
    """A timer copied but never enabled never runs, and nothing says so."""
    update = update_central_block()
    started = set(bash_array(SCRIPT.read_text(), "CENTRAL_SERVICES"))
    for timer in sorted(path.name for path in TIMERS.glob("ci-observe-*.timer")):
        assert f"systemctl --user enable --now {timer}" in update, f"--update-central does not enable {timer}"
        assert timer in started, f"--central does not start and enable {timer}"


def test_every_container_of_the_exporter_image_runs_its_own_program():
    """The image's default command is the workflow exporter: a second
    container without Exec= ran a second exporter (the MCP container did)."""
    for quadlet in sorted(QUADLETS.glob("*.container")):
        text = quadlet.read_text()
        if "Image=localhost/osac-workflow-exporter" not in text or "ContainerName=workflow-exporter\n" in text:
            continue
        program = re.search(r"^Exec=/app/(\S+)", text, re.M)
        assert program, f"{quadlet.name} runs the image's default program, the exporter"
        assert program.group(1) in mounted_scripts(quadlet), f"{quadlet.name} runs a script it does not mount"


def test_the_mcp_container_receives_only_its_own_keys():
    """The one container the VPN reaches without the fleet's basic auth gets
    its MCP_* keys from the .env, not the whole file with PRIVATE_GITHUB_TOKEN."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("ci_observe_mcp_for_setup",
                                                  MONITORING / "scripts" / "ci_observe_mcp.py")
    mcp = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mcp  # its dataclasses resolve their annotations through it
    spec.loader.exec_module(mcp)
    text = (QUADLETS / "ci-observe-mcp.container").read_text()
    container = text.split("\n[Container]\n", 1)[1].split("\n[Service]\n", 1)[0]
    service = text.split("\n[Service]\n", 1)[1]
    assert "EnvironmentFile=" not in container
    assert "EnvironmentFile=%h/.monitoring-server/.env" in service
    passed = set(re.findall(r"--env (\w+)", container))
    assert set(mcp.Config.REQUIRED) <= passed
    assert "PRIVATE_GITHUB_TOKEN" not in passed


def test_the_mcp_check_warns_for_a_missing_or_empty_key_and_the_setup_goes_on(tmp_path):
    """start_mcp_if_configured runs under the script's own set -euo pipefail.
    On 2026-11-22 its grep for an absent key failed the pipeline and ended
    the whole setup there, silently, before the ci-observe timers were
    enabled. A missing key, an empty one, a quoted empty one and no .env at
    all warn and go on; the last assignment wins, as systemd reads it."""
    function = re.search(r"(?ms)^start_mcp_if_configured\(\) \{.*?^\}", SCRIPT.read_text())
    assert function, "start_mcp_if_configured must stay a top-level function of monitoring-setup.sh"
    assert "set -euo pipefail" in SCRIPT.read_text()
    systemctl = tmp_path / "systemctl"
    systemctl.write_text('#!/usr/bin/env bash\necho "systemctl $*"\n')
    systemctl.chmod(0o755)
    keys = ("MCP_GITHUB_CLIENT_ID", "MCP_GITHUB_CLIENT_SECRET", "MCP_GRAFANA_URL", "MCP_GRAFANA_SERVICE_TOKEN",
            "MCP_EXTERNAL_URL")
    base = "".join(f"{key}=v\n" for key in keys)
    cases = {
        "complete": (base + "MCP_SESSION_SECRET=s\n", True),
        "missing": (base, False),
        "empty": (base + "MCP_SESSION_SECRET=\n", False),
        "double-quoted empty": (base + 'MCP_SESSION_SECRET=""\n', False),
        "single-quoted empty": (base + "MCP_SESSION_SECRET=''\n", False),
        "emptied by a later line": (base + "MCP_SESSION_SECRET=s\nMCP_SESSION_SECRET=\n", False),
        "set by a later line": (base + "MCP_SESSION_SECRET=\nMCP_SESSION_SECRET=s\n", True),
        "no .env": (None, False),
    }
    for name, (env, starts) in cases.items():
        home = tmp_path / name.replace(" ", "-")
        home.mkdir()
        if env is not None:
            (home / ".env").write_text(env)
        probe = ("set -euo pipefail\n" + function.group(0)
                 + f"\nMONITORING_HOME={shlex.quote(str(home))}\nstart_mcp_if_configured\necho the-setup-goes-on\n")
        result = subprocess.run(["bash", "-c", probe], capture_output=True, text=True,
                                env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
        assert result.returncode == 0, (name, result.stderr)
        assert "the-setup-goes-on" in result.stdout, name
        assert ("systemctl --user restart ci-observe-mcp.service" in result.stdout) is starts, name
        assert ("is NOT started" in result.stderr) is (not starts), name

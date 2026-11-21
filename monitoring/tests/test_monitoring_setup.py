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

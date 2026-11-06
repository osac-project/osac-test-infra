#!/usr/bin/env python3
"""Shared fake binaries for the script tests.

The script tests exercise the .github/scripts tools against fake oc,
virsh and qemu-img binaries installed on PATH. This module owns the
fakes so they stay consistent across the suites. Behavior is driven by
environment variables (set through FakeBins.env):

  CAPTURE_DIR     directory where `oc apply` and `virsh define` capture
                  their stdin, one numbered file per invocation
  CALLS_FILE      log of every virsh call and of oc annotate/delete/label
  UNDEFINED_FILE  virsh state: domains recorded there no longer resolve
                  (`domuuid` fails); `undefine` records into it unless
                  VIRSH_UNDEFINE_BROKEN=true
  FAKE_DISK_DIR   disk paths reported by `virsh dumpxml`
  NS_MARKER       when set, the list form of `oc get bmh` fails until a
                  Namespace manifest has been applied (models namespace
                  existence)
  BMH_LIST_FILE   names returned by the list form of `oc get bmh`
  OC_WAIT_EXIT    exit code for `oc wait`

Run indirectly: imported by the test_*.py suites.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

FAKE_OC = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    cmd="${1}"
    if [[ "${cmd}" == "apply" ]]; then
      n=$(ls "${CAPTURE_DIR}" 2>/dev/null | wc -l)
      cat > "${CAPTURE_DIR}/apply-${n}.yaml"
      # A namespace must exist before BMHs can be listed in it.
      if [[ -n "${NS_MARKER:-}" ]] && grep -q "kind: Namespace" "${CAPTURE_DIR}/apply-${n}.yaml"; then
        touch "${NS_MARKER}"
      fi
      exit 0
    fi
    if [[ "${cmd}" == "wait" ]]; then
      exit "${OC_WAIT_EXIT:-0}"
    fi
    if [[ "${cmd}" == "get" ]]; then
      if [[ "${2}" == "endpoints" ]]; then
        echo "192.0.2.10"
        exit 0
      fi
      if [[ "${2}" == "bmh" && "${3}" == "-n" ]]; then
        if [[ -n "${NS_MARKER:-}" && ! -f "${NS_MARKER}" ]]; then
          echo "Error from server (NotFound): namespace not found" >&2
          exit 1
        fi
        cat "${BMH_LIST_FILE:-/dev/null}" 2>/dev/null || true
        exit 0
      fi
      echo "available"
      exit 0
    fi
    if [[ "${cmd}" == "annotate" || "${cmd}" == "delete" || "${cmd}" == "label" || "${cmd}" == "patch" ]]; then
      echo "$*" >> "${CALLS_FILE}"
      exit 0
    fi
    exit 87
    """
)

FAKE_VIRSH = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    shift 2
    cmd="${1}"
    shift
    echo "${cmd} $*" >> "${CALLS_FILE}"
    case "${cmd}" in
      net-info) exit 0 ;;
      dumpxml)
        # dumpxml: the owned disk, plus a shared CD-ROM that must survive
        # deletion.
        echo "<domain type='kvm'>"
        echo "  <name>${1}</name>"
        echo "  <os>"
        echo "    <nvram>${FAKE_DISK_DIR:-/tmp}/${1}-VARS.fd</nvram>"
        echo "  </os>"
        echo "  <devices>"
        echo "    <disk type='file' device='disk'>"
        echo "      <source file='${FAKE_DISK_DIR:-/tmp}/${1}.qcow2'/>"
        echo "    </disk>"
        echo "    <disk type='file' device='cdrom'>"
        echo "      <source file='${FAKE_DISK_DIR:-/tmp}/shared-install.iso'/>"
        echo "    </disk>"
        echo "  </devices>"
        echo "</domain>"
        exit 0
        ;;
      define)
        n=$(ls "${CAPTURE_DIR}" 2>/dev/null | grep -c '^vm-' || true)
        cat > "${CAPTURE_DIR}/vm-${n}.xml"
        exit 0
        ;;
      start) exit 0 ;;
      domuuid)
        # Undefined domains no longer resolve.
        if grep -qx "${1}" "${UNDEFINED_FILE}" 2>/dev/null; then
          exit 1
        fi
        echo "uuid-of-${1}"
        exit 0
        ;;
      undefine)
        if [[ "${VIRSH_UNDEFINE_BROKEN:-}" != "true" ]]; then
          echo "${1}" >> "${UNDEFINED_FILE}"
        fi
        exit 0
        ;;
      destroy) exit 0 ;;
      *) exit 64 ;;
    esac
    """
)

FAKE_QEMU_IMG = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "${1}" == "create" ]]; then
      : > "${4}"
      exit 0
    fi
    exit 64
    """
)


class FakeBins:
    """Fake oc/virsh/qemu-img binaries plus the state they capture."""

    def __init__(self, temp: Path, binaries: list[str]) -> None:
        self.temp = temp
        self.bin_dir = temp / "bin"
        self.bin_dir.mkdir()
        contents = {"oc": FAKE_OC, "virsh": FAKE_VIRSH, "qemu-img": FAKE_QEMU_IMG}
        for binary in binaries:
            path = self.bin_dir / binary
            path.write_text(contents[binary])
            path.chmod(0o755)
        self.calls_file = temp / "fake-bin-calls.log"
        self.capture_dir = temp / "capture"
        self.capture_dir.mkdir()
        self.undefined_file = temp / "virsh-undefined.log"
        self.undefined_file.touch()

    def env(self, **extra: str) -> dict[str, str]:
        """A test-process environment with the fakes on PATH."""
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin_dir}:{env['PATH']}",
                "CALLS_FILE": str(self.calls_file),
                "CAPTURE_DIR": str(self.capture_dir),
                "UNDEFINED_FILE": str(self.undefined_file),
            }
        )
        env.update(extra)
        return env

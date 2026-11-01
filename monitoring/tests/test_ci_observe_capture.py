from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_observe_capture.py"
MANIFEST = Path(__file__).resolve().parents[1] / "config" / "ci-observe" / "cluster.yaml"


@pytest.fixture
def capture_module() -> ModuleType:
    specification = importlib.util.spec_from_file_location("ci_observe_capture", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize("returncode", [0, 1, 3, 17, 127])
def test_native_returncode(*, capture_module: ModuleType, tmp_path: Path, returncode: int) -> None:
    result = capture_module.run_test(
        command=[sys.executable, "-c", f"raise SystemExit({returncode})"],
        environment=os.environ.copy(),
        directory=tmp_path,
    )
    assert result.returncode == returncode
    assert result.exit_code == returncode
    assert result.signal_number is None


def test_arguments_environment_and_directory(*, capture_module: ModuleType, tmp_path: Path) -> None:
    output = tmp_path / "child.json"
    environment = os.environ.copy()
    environment["CI_OBSERVE_CHILD_VALUE"] = "unchanged"
    result = capture_module.run_test(
        command=[
            sys.executable,
            "-c",
            "import json, os, pathlib, sys; "
            "pathlib.Path('child.json').write_text(json.dumps([os.getcwd(), "
            "os.environ['CI_OBSERVE_CHILD_VALUE'], sys.argv[1:]]))",
            "--filter",
            "two words",
        ],
        environment=environment,
        directory=tmp_path,
    )
    assert result.exit_code == 0
    assert json.loads(output.read_text()) == [str(tmp_path), "unchanged", ["--filter", "two words"]]


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_cancel_reaches_child(*, tmp_path: Path, number: int) -> None:
    child = tmp_path / "child.py"
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    child.write_text(
        "import pathlib, signal, time\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        f"stopped = pathlib.Path({str(stopped)!r})\n"
        "def stop(number, frame):\n"
        "    stopped.write_text(str(number))\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "ready.write_text('ready')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import json, os, pathlib, sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "from ci_observe_capture import run_test\n"
        f"result = run_test(command=[sys.executable, {str(child)!r}], "
        f"environment=os.environ.copy(), directory=pathlib.Path({str(tmp_path)!r}))\n"
        "print(json.dumps({'native': result.returncode, 'signal': result.signal_number}), flush=True)\n"
        "raise SystemExit(result.exit_code)\n"
    )
    process = subprocess.Popen([sys.executable, str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("child did not become ready")
            time.sleep(0.01)
        process.send_signal(number)
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 128 + number
        assert json.loads(output) == {"native": 0, "signal": number}
        assert stopped.read_text() == str(number)
        assert errors == b""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_render_manifest_resolves_every_token(*, capture_module: ModuleType, tmp_path: Path) -> None:
    source = MANIFEST.parent
    original = MANIFEST.read_text()
    (tmp_path / "cluster.yaml").write_text(original)
    capture_module.MANIFEST = tmp_path / "cluster.yaml"
    rendered = capture_module.render_manifest(
        capture_id="cap123",
        namespace="osac-e2e-ci",
        cluster_uid="uid-1",
        kubelet_endpoint="https://192.0.2.1:10250",
        suite="vmaas",
        pr="2051",
        otlp_endpoint="https://osac-ci-1.redhat.com",
        otlp_auth="Basic dXNlcjpwYXNz",
    )
    assert "${CI_OBSERVE_" not in rendered
    assert "namespace: osac-e2e-ci" in rendered
    assert "k8s_namespace: osac-e2e-ci" in rendered
    # Suite and PR become the gateway env values the collector's
    # ${env:...} placeholders read.
    assert 'name: CI_OBSERVE_SUITE\n              value: "vmaas"' in rendered
    assert 'name: CI_OBSERVE_PR\n              value: "2051"' in rendered
    # The live-push tokens become the gateway env values collector-live.yaml
    # reads for the central OTLP export.
    assert 'name: CI_OBSERVE_OTLP_ENDPOINT\n              value: "https://osac-ci-1.redhat.com"' in rendered
    assert 'name: CI_OBSERVE_OTLP_AUTH\n              value: "Basic dXNlcjpwYXNz"' in rendered
    capture_module.MANIFEST = source / "cluster.yaml"


def test_render_manifest_leaves_the_live_tokens_empty_by_default(*, capture_module: ModuleType) -> None:
    """The local file-capture path renders the shared manifest without live
    push: the endpoint and auth tokens coalesce to empty values, which the
    local collector config (collector.yaml) never reads."""
    rendered = capture_module.render_manifest(
        capture_id="cap123", namespace="osac-e2e-ci", cluster_uid="uid-1", kubelet_endpoint="https://192.0.2.1:10250"
    )
    assert "${CI_OBSERVE_" not in rendered
    assert 'name: CI_OBSERVE_OTLP_ENDPOINT\n              value: ""' in rendered
    assert 'name: CI_OBSERVE_OTLP_AUTH\n              value: ""' in rendered


def test_render_manifest_coalesces_suite_and_pr_to_none_when_unset(
    *, capture_module: ModuleType, tmp_path: Path
) -> None:
    """A local capture without suite/PR context still renders: the tokens
    coalesce to "none", because the gateway's collector config cannot expand
    an empty value (its resource processor rejects it at load)."""
    rendered = capture_module.render_manifest(
        capture_id="cap123", namespace="osac-e2e-ci", cluster_uid="uid-1", kubelet_endpoint="https://192.0.2.1:10250"
    )
    assert "${CI_OBSERVE_" not in rendered
    assert 'name: CI_OBSERVE_SUITE\n              value: "none"' in rendered
    assert 'name: CI_OBSERVE_PR\n              value: "none"' in rendered


def test_render_manifest_narrows_suite_and_pr_to_safe_charsets(*, capture_module: ModuleType, tmp_path: Path) -> None:
    """The values land inside the manifest's double-quoted env values (and,
    in the CI action, inside a sed replacement), so anything outside the
    safe charset is dropped rather than corrupting the manifest."""
    rendered = capture_module.render_manifest(
        capture_id="cap123",
        namespace="osac-e2e-ci",
        cluster_uid="uid-1",
        kubelet_endpoint="https://192.0.2.1:10250",
        suite="vmaas/x&",
        pr="20 51",
    )
    assert "${CI_OBSERVE_" not in rendered
    assert 'name: CI_OBSERVE_SUITE\n              value: "vmaasx"' in rendered
    assert 'name: CI_OBSERVE_PR\n              value: "2051"' in rendered


def test_render_manifest_rejects_unresolved_tokens(*, capture_module: ModuleType, tmp_path: Path) -> None:
    capture_module.MANIFEST = tmp_path / "broken.yaml"
    (tmp_path / "broken.yaml").write_text("name: ${CI_OBSERVE_UNKNOWN_TOKEN}\n")
    with pytest.raises(ValueError, match="CI_OBSERVE_UNKNOWN_TOKEN"):
        capture_module.render_manifest(
            capture_id="x", namespace="y", cluster_uid="z", kubelet_endpoint="https://e:10250"
        )
    capture_module.MANIFEST = MANIFEST


def test_kubelet_endpoint_selects_internal_ip(*, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kubelet endpoint must be the node IP, not addresses[0]: that entry
    is the InternalDNS hostname on OpenShift and need not resolve from the
    gateway pod, which would silently starve metrics.jsonl."""

    def fake_kubectl(kubeconfig: Path, *arguments: str, stdin: str | None = None) -> str:
        assert any('type=="InternalIP"' in argument for argument in arguments), arguments
        return "192.0.2.10\n"

    monkeypatch.setattr(capture_module, "_kubectl", fake_kubectl)
    assert capture_module._kubelet_endpoint(Path("/fake/kubeconfig")) == "https://192.0.2.10:10250"


def test_kubelet_endpoint_requires_internal_ip(*, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: "\n")
    with pytest.raises(ValueError, match="InternalIP"):
        capture_module._kubelet_endpoint(Path("/fake/kubeconfig"))


def test_capture_metadata_files_and_hashes(*, capture_module: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "traces.jsonl").write_text("{}\n")
    (tmp_path / "metrics.jsonl").write_text("")
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "offsets.db").write_text("internal")
    capture_module._write_capture_metadata(directory=tmp_path, capture_id="cap9", state="closed", exit_code=0)
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["schema"] == "osac.ci-observe.capture"
    assert metadata["state"] == "closed" and metadata["child_exit_code"] == 0
    assert [f["path"] for f in metadata["files"]] == ["metrics.jsonl", "traces.jsonl"]
    assert metadata["files"][1]["sha256"] == hashlib.sha256(b"{}\n").hexdigest()
    assert metadata["sources"]["pytest"] == "delivered"
    assert metadata["sources"]["cluster-metrics"] == "missing"


def test_capture_metadata_marks_interrupted(*, capture_module: ModuleType, tmp_path: Path) -> None:
    capture_module._write_capture_metadata(directory=tmp_path, capture_id="cap10", state="interrupted", exit_code=143)
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    assert metadata["state"] == "interrupted" and metadata["child_exit_code"] == 143
    assert metadata["sources"]["pytest"] == "missing"


def test_missing_command_is_an_error(*, capture_module: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        capture_module.run_test(
            command=[str(tmp_path / "missing-command")], environment=os.environ.copy(), directory=tmp_path
        )


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGKILL])
def test_native_child_signal(*, capture_module: ModuleType, tmp_path: Path, number: int) -> None:
    result = capture_module.run_test(
        command=[sys.executable, "-c", f"import os; os.kill(os.getpid(), {number})"],
        environment=os.environ.copy(),
        directory=tmp_path,
    )
    assert result.returncode == -number
    assert result.exit_code == 128 + number
    assert result.signal_number is None


def test_unresponsive_child_is_killed_after_cancel(*, capture_module: ModuleType, tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    wrapper = tmp_path / "wrapper.py"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    wrapper.write_text(
        "import json, os, pathlib, sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "from ci_observe_capture import run_test\n"
        f"result = run_test(command=[sys.executable, {str(child)!r}], "
        f"environment=os.environ.copy(), directory=pathlib.Path({str(tmp_path)!r}), "
        "escalation_delay=2.0)\n"
        "print(json.dumps({'native': result.returncode, 'signal': result.signal_number}), flush=True)\n"
        "raise SystemExit(result.exit_code)\n"
    )
    process = subprocess.Popen([sys.executable, str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("child did not become ready")
            time.sleep(0.01)
        start = time.monotonic()
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=20)
        assert process.returncode == 143
        assert json.loads(output) == {"native": -9, "signal": 15}
        assert time.monotonic() - start >= 2
        assert errors == b""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _spawn_with_grandchild(*, tmp_path: Path) -> tuple[subprocess.Popen, int]:
    wrapper = tmp_path / "wrapper.py"
    child = tmp_path / "child.py"
    grandchild = tmp_path / "grandchild.py"
    ready = tmp_path / "grandchild-ready"
    grandchild.write_text(
        "import os, pathlib, signal, sys, time\n"
        "if '--ignore' in sys.argv:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        "ready.write_text(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    child.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, " + repr(str(grandchild)) + ", " + repr(str(ready)) + "])\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    wrapper.write_text(
        "import json, os, pathlib, sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "from ci_observe_capture import run_test\n"
        f"result = run_test(command=[sys.executable, {str(child)!r}], "
        f"environment=os.environ.copy(), directory=pathlib.Path({str(tmp_path)!r}), "
        "escalation_delay=3.0)\n"
        "print(json.dumps({'native': result.returncode, 'signal': result.signal_number}), flush=True)\n"
        "raise SystemExit(result.exit_code)\n"
    )
    process = subprocess.Popen([sys.executable, str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("grandchild did not become ready")
            time.sleep(0.01)
        if process.poll() is not None:
            pytest.fail("wrapper exited before the grandchild started")
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    grandchild_pid = int(ready.read_text())
    os.kill(grandchild_pid, 0)
    return process, grandchild_pid


def test_forwarded_signal_terminates_descendant_group(*, tmp_path: Path) -> None:
    process, grandchild_pid = _spawn_with_grandchild(tmp_path=tmp_path)
    try:
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=20)
        assert json.loads(output) == {"native": -15, "signal": 15}
        assert process.returncode == 143
        assert errors == b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        pytest.fail("grandchild survived the forwarded group signal")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)


def _spawn_ignoring_group(*, tmp_path: Path) -> tuple[subprocess.Popen, int]:
    wrapper = tmp_path / "wrapper.py"
    child = tmp_path / "child.py"
    grandchild = tmp_path / "grandchild.py"
    ready = tmp_path / "grandchild-ready"
    grandchild.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"ready = pathlib.Path({str(ready)!r})\n"
        "ready.write_text(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    child.write_text(
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "subprocess.Popen([sys.executable, " + repr(str(grandchild)) + ", " + repr(str(ready)) + "])\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    wrapper.write_text(
        "import json, os, pathlib, sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "from ci_observe_capture import run_test\n"
        f"result = run_test(command=[sys.executable, {str(child)!r}], "
        f"environment=os.environ.copy(), directory=pathlib.Path({str(tmp_path)!r}), "
        "escalation_delay=3.0)\n"
        "print(json.dumps({'native': result.returncode, 'signal': result.signal_number}), flush=True)\n"
        "raise SystemExit(result.exit_code)\n"
    )
    process = subprocess.Popen([sys.executable, str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail("grandchild did not become ready")
            time.sleep(0.01)
        if process.poll() is not None:
            pytest.fail("wrapper exited before the grandchild started")
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    grandchild_pid = int(ready.read_text())
    os.kill(grandchild_pid, 0)
    return process, grandchild_pid


def test_escalation_kills_signal_ignoring_group(*, tmp_path: Path) -> None:
    process, grandchild_pid = _spawn_ignoring_group(tmp_path=tmp_path)
    try:
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=20)
        assert json.loads(output) == {"native": -9, "signal": 15}
        assert process.returncode == 143
        assert errors == b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        pytest.fail("grandchild survived the escalated group kill")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)


def test_second_signal_does_not_replace_the_first(*, capture_module: ModuleType, tmp_path: Path) -> None:
    wrapper = tmp_path / "wrapper.py"
    child = tmp_path / "child.py"
    child.write_text(
        "import signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
        "while True:\n"
        "    signal.pause()\n"
    )
    wrapper.write_text(
        "import json, os, pathlib, sys\n"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
        "from ci_observe_capture import run_test\n"
        f"result = run_test(command=[sys.executable, {str(child)!r}], "
        f"environment=os.environ.copy(), directory=pathlib.Path({str(tmp_path)!r}))\n"
        "print(json.dumps({'native': result.returncode, 'signal': result.signal_number}), flush=True)\n"
        "raise SystemExit(result.exit_code)\n"
    )
    process = subprocess.Popen([sys.executable, str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(1.0)
        process.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(timeout=20)
        assert process.returncode == 143
        assert json.loads(output) == {"native": 0, "signal": 15}
        assert errors == b""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

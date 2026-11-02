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
import yaml

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


def test_render_manifest_resolves_the_run_identity_tokens(*, capture_module: ModuleType) -> None:
    """The run identity values become the gateway env values the
    collector's ${env:...} placeholders read for the osac.ci.test_ref,
    osac.ci.test_sha, osac.ci.test_infra_ref, immutable test-infra SHAs,
    workflow, osac_images, runtime image IDs and test selection resource
    attributes."""
    test_filter = 'not TestBmaasNetworking | "edge" & slash\\path\nsecond line'
    rendered = capture_module.render_manifest(
        capture_id="cap123",
        namespace="osac-e2e-ci",
        cluster_uid="uid-1",
        kubelet_endpoint="https://192.0.2.1:10250",
        test_ref="9f3eeeb2c9a4b6f4e5b7f3a3b4c5d6e7f8a9b0c1",
        test_sha="5d" * 32,
        test_infra_ref="a902331b0b11c390a56381ad16ebd13914f8d5",
        test_infra_sha="a" * 40,
        test_infra_workflow_sha="b" * 40,
        workflow="local",
        run_attempt="2",
        osac_images="fulfillment-service=quay.io/osac/fulfillment@sha256:abc;osac-operator=quay.io/osac/operator:1.2",
        osac_image_ids="fulfillment-service/grpc-server=quay.io/osac/fulfillment@sha256:abc",
        osac_image_ids_complete="true",
        test_suite="tests/e2e/bmaas/sanity/",
        test_filter=test_filter,
        test_marker="sanity and not requires_caas",
    )
    assert "${CI_OBSERVE_" not in rendered
    assert 'name: CI_OBSERVE_TEST_REF\n              value: "9f3eeeb2c9a4b6f4e5b7f3a3b4c5d6e7f8a9b0c1"' in rendered
    # Both SHA lengths the validators accept: 40-byte SHA-1 and 64-byte
    # SHA-256 object ids.
    assert f'name: CI_OBSERVE_TEST_SHA\n              value: "{"5d" * 32}"' in rendered
    assert 'name: CI_OBSERVE_TEST_INFRA_REF\n              value: "a902331b0b11c390a56381ad16ebd13914f8d5"' in rendered
    infra_sha = "a" * 40
    workflow_sha = "b" * 40
    assert f'name: CI_OBSERVE_TEST_INFRA_SHA\n              value: "{infra_sha}"' in rendered
    assert f'name: CI_OBSERVE_TEST_INFRA_WORKFLOW_SHA\n              value: "{workflow_sha}"' in rendered
    assert 'name: CI_OBSERVE_WORKFLOW\n              value: "local"' in rendered
    assert 'name: CI_OBSERVE_RUN_ATTEMPT\n              value: "2"' in rendered
    assert (
        "name: CI_OBSERVE_OSAC_IMAGES\n              value: "
        '"fulfillment-service=quay.io/osac/fulfillment@sha256:abc;osac-operator=quay.io/osac/operator:1.2"'
    ) in rendered
    assert (
        "name: CI_OBSERVE_OSAC_IMAGE_IDS\n              value: "
        '"fulfillment-service/grpc-server=quay.io/osac/fulfillment@sha256:abc"'
    ) in rendered
    documents = [document for document in yaml.safe_load_all(rendered) if document]
    gateway = next(document for document in documents if document.get("kind") == "Deployment")
    containers = gateway["spec"]["template"]["spec"]["containers"]
    environment = {entry["name"]: entry["value"] for entry in containers[0]["env"]}
    assert environment["CI_OBSERVE_TEST_SUITE"] == "tests/e2e/bmaas/sanity/"
    assert environment["CI_OBSERVE_TEST_FILTER"] == test_filter
    assert environment["CI_OBSERVE_TEST_MARKER"] == "sanity and not requires_caas"
    assert environment["CI_OBSERVE_OSAC_IMAGE_IDS_COMPLETE"] == "true"


def test_render_manifest_degrades_an_invalid_test_sha_to_unknown_whole(*, capture_module: ModuleType) -> None:
    """The resolved osac commit is a 40/64 lowercase hex SHA or "unknown":
    a branch name, an uppercase or short SHA, or anything hostile degrades
    whole, and never falls back to the requested test_ref -- a moving ref
    is exactly what test_sha exists to disambiguate."""
    for hostile in ("main", "a" * 39, "A" * 40, "a" * 41, "a" * 63, "a" * 65, "main\nsha", "repo@main"):
        rendered = capture_module.render_manifest(
            capture_id="cap123",
            namespace="osac-e2e-ci",
            cluster_uid="uid-1",
            kubelet_endpoint="https://192.0.2.1:10250",
            test_ref="main",
            test_sha=hostile,
        )
        assert 'name: CI_OBSERVE_TEST_SHA\n              value: "unknown"' in rendered, hostile
        assert 'name: CI_OBSERVE_TEST_REF\n              value: "main"' in rendered, hostile


def test_render_manifest_coalesces_run_identity_to_non_empty_fallbacks(*, capture_module: ModuleType) -> None:
    """A local capture without run identity context still renders: the
    tokens coalesce to "unknown" (attempt: "1"), never to an empty string,
    because the gateway's collector config cannot expand an empty value
    (its resource processor rejects it at load). A local capture has no
    osac ref, so its test_ref is always "unknown"."""
    rendered = capture_module.render_manifest(
        capture_id="cap123", namespace="osac-e2e-ci", cluster_uid="uid-1", kubelet_endpoint="https://192.0.2.1:10250"
    )
    assert "${CI_OBSERVE_" not in rendered
    assert 'name: CI_OBSERVE_TEST_REF\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_TEST_SHA\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_TEST_INFRA_REF\n              value: "unspecified"' in rendered
    assert 'name: CI_OBSERVE_TEST_INFRA_SHA\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_TEST_INFRA_WORKFLOW_SHA\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_WORKFLOW\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_RUN_ATTEMPT\n              value: "1"' in rendered
    assert 'name: CI_OBSERVE_OSAC_IMAGES\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_OSAC_IMAGE_IDS\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_OSAC_IMAGE_IDS_COMPLETE\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_TEST_SUITE\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_TEST_FILTER\n              value: "none"' in rendered
    assert 'name: CI_OBSERVE_TEST_MARKER\n              value: "none"' in rendered


def test_render_manifest_marks_overlong_selection_unknown(*, capture_module: ModuleType) -> None:
    rendered = capture_module.render_manifest(
        capture_id="cap123",
        namespace="osac-e2e-ci",
        cluster_uid="uid-1",
        kubelet_endpoint="https://192.0.2.1:10250",
        test_suite="s" * (capture_module.MAX_TEST_SUITE_LENGTH + 1),
        test_filter="f" * (capture_module.MAX_TEST_FILTER_LENGTH + 1),
        test_marker="m" * (capture_module.MAX_TEST_MARKER_LENGTH + 1),
    )
    documents = [document for document in yaml.safe_load_all(rendered) if document]
    gateway = next(document for document in documents if document.get("kind") == "Deployment")
    containers = gateway["spec"]["template"]["spec"]["containers"]
    environment = {entry["name"]: entry["value"] for entry in containers[0]["env"]}
    assert environment["CI_OBSERVE_TEST_SUITE"] == "unknown"
    assert environment["CI_OBSERVE_TEST_FILTER"] == "unknown"
    assert environment["CI_OBSERVE_TEST_MARKER"] == "unknown"


def test_render_manifest_degrades_unparseable_refs_to_unknown_whole(*, capture_module: ModuleType) -> None:
    """A git ref is either a real ref or "unknown": characters outside the
    SHA/branch/tag set degrade the WHOLE ref to "unknown", never a
    stripped remainder (like "featurex") that matches no real ref. The
    workflow name is a display string and keeps the denylist strip:
    newlines, backslashes, double quotes, the sed-special | and &, and $
    (an embedded ${CI_OBSERVE_...} token must not survive into a renderer
    that substitutes tokens) are dropped rather than corrupting the
    manifest."""
    rendered = capture_module.render_manifest(
        capture_id="cap123",
        namespace="osac-e2e-ci",
        cluster_uid="uid-1",
        kubelet_endpoint="https://192.0.2.1:10250",
        test_ref="feature!(x)",
        test_infra_ref="main\nsha",
        workflow='E2E "full &| install" ${CI_OBSERVE_TEST_REF}',
        run_attempt="attempt-3",
        osac_images="a=b\nc=d",
    )
    assert "${CI_OBSERVE_" not in rendered
    assert 'name: CI_OBSERVE_TEST_REF\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_TEST_INFRA_REF\n              value: "unknown"' in rendered
    assert 'name: CI_OBSERVE_WORKFLOW\n              value: "E2E full  install {CI_OBSERVE_TEST_REF}"' in rendered
    assert 'name: CI_OBSERVE_RUN_ATTEMPT\n              value: "3"' in rendered
    assert 'name: CI_OBSERVE_OSAC_IMAGES\n              value: "a=bc=d"' in rendered


def test_test_infra_sha_requires_a_clean_capture_checkout(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local SHA is reported only when the supervisor and its configs are
    tracked at HEAD and match the worktree copies."""
    calls = []

    def clean_checkout(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(capture_module.subprocess, "run", clean_checkout)
    assert capture_module._test_infra_sha() == "a" * 40
    # One tracked-at-HEAD probe per source file, then the worktree diff,
    # then the SHA itself.
    assert len(calls) == 5
    for probe in calls[:3]:
        assert probe[1:4] == ["-C", str(SCRIPT.parents[2]), "cat-file"]
        assert probe[4] == "-e"
        assert probe[5].startswith("HEAD:")
    assert calls[3][1:4] == ["-C", str(SCRIPT.parents[2]), "diff"]
    assert calls[4][-2:] == ["--verify", "HEAD"]


def test_test_infra_sha_is_unknown_when_a_source_file_is_not_tracked_at_head(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git diff reports no difference for a path HEAD does not contain, so
    an untracked replacement running instead of the committed file would
    silently claim a commit that never had it: the tracked-at-HEAD probe is
    what closes that gap."""

    def untracked_source(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[3:5] == ["cat-file", "-e"]:
            return subprocess.CompletedProcess(arguments, 1, "", "")
        return subprocess.CompletedProcess(arguments, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(capture_module.subprocess, "run", untracked_source)
    assert capture_module._test_infra_sha() == "unknown"


def test_test_infra_sha_is_unknown_when_capture_sources_are_dirty(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dirty_checkout(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if arguments[3:5] == ["cat-file", "-e"]:
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 1, "", "")

    monkeypatch.setattr(capture_module.subprocess, "run", dirty_checkout)
    assert capture_module._test_infra_sha() == "unknown"


def test_osac_images_collapses_the_deployment_lines(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed-images read returns one deployment=image line per
    deployment; the token value is the same lines collapsed to a single
    ";"-separated line, because neither the manifest's double-quoted env
    values nor the CI action's sed rendering can carry a newline."""
    monkeypatch.setattr(
        capture_module, "_kubectl", lambda *args, **kwargs: "fulfillment-service=img1,img2\nosac-operator=img3\n"
    )
    assert (
        capture_module._osac_images(Path("/fake/kubeconfig"), "osac-e2e-ci")
        == "fulfillment-service=img1,img2;osac-operator=img3"
    )


def test_osac_images_degrades_to_unknown_on_any_failure(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kubectl failure or an empty namespace must never render an empty
    env value: the collector rejects an empty value at config load."""

    def failing_kubectl(kubeconfig: Path, *arguments: str, stdin: str | None = None) -> str:
        raise subprocess.CalledProcessError(1, "kubectl")

    monkeypatch.setattr(capture_module, "_kubectl", failing_kubectl)
    assert capture_module._osac_images(Path("/fake/kubeconfig"), "osac-e2e-ci") == "unknown"
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: "\n")
    assert capture_module._osac_images(Path("/fake/kubeconfig"), "osac-e2e-ci") == "unknown"


def test_osac_image_ids_include_only_running_osac_deployment_pods(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = [
        {
            "kind": "Deployment",
            "metadata": {"name": "fulfillment", "labels": {"app.kubernetes.io/part-of": "osac"}},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "grpc_server"},
                            {"name": "side_car"},
                            {"name": "not_started"},
                            {"name": "invalid"},
                        ]
                    }
                },
            },
        },
        {
            "kind": "Deployment",
            "metadata": {"name": "metrics-adapter", "labels": {"app.kubernetes.io/part-of": "other"}},
        },
        {
            "kind": "ReplicaSet",
            "metadata": {
                "name": "fulfillment-abc123",
                "ownerReferences": [{"kind": "Deployment", "name": "fulfillment", "controller": True}],
            },
        },
        {
            "kind": "ReplicaSet",
            "metadata": {
                "name": "metrics-adapter-abc123",
                "ownerReferences": [{"kind": "Deployment", "name": "metrics-adapter", "controller": True}],
            },
        },
        {
            "kind": "Pod",
            "metadata": {
                "name": "fulfillment-abc123-pod1",
                "ownerReferences": [{"kind": "ReplicaSet", "name": "fulfillment-abc123", "controller": True}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"name": "grpc_server", "imageID": "quay.io/osac/my_repository@sha256:abc"},
                    {"name": "side_car", "imageID": "cri-o://sha256:def"},
                    {"name": "invalid", "imageID": "sha256:bad|suffix"},
                ],
            },
        },
        {
            "kind": "Pod",
            "metadata": {
                "name": "fulfillment-abc123-pending",
                "ownerReferences": [{"kind": "ReplicaSet", "name": "fulfillment-abc123", "controller": True}],
            },
            "status": {"phase": "Pending", "containerStatuses": [{"name": "grpc-server", "imageID": "pending"}]},
        },
        {
            "kind": "Pod",
            "metadata": {
                "name": "metrics-adapter-abc123-pod1",
                "ownerReferences": [{"kind": "ReplicaSet", "name": "metrics-adapter-abc123", "controller": True}],
            },
            "status": {"phase": "Running", "containerStatuses": [{"name": "adapter", "imageID": "other"}]},
        },
    ]
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: json.dumps({"items": resources}))

    result = capture_module.osac_image_ids(Path("/fake/kubeconfig"), "osac-e2e-ci")
    assert result.image_ids == (
        "fulfillment/grpc_server=quay.io/osac/my_repository@sha256:abc;"
        "fulfillment/invalid=unknown;fulfillment/not_started=unknown;fulfillment/side_car=cri-o://sha256:def"
    )
    assert result.complete == "false"


def test_osac_image_ids_marks_a_missing_replica(*, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    resources = [
        {
            "kind": "Deployment",
            "metadata": {"name": "osac-api", "labels": {"app.kubernetes.io/part-of": "osac"}},
            "spec": {"replicas": 2, "template": {"spec": {"containers": [{"name": "api"}]}}},
        },
        {
            "kind": "ReplicaSet",
            "metadata": {
                "name": "osac-api-abc123",
                "ownerReferences": [{"kind": "Deployment", "name": "osac-api", "controller": True}],
            },
        },
        {
            "kind": "Pod",
            "metadata": {
                "name": "osac-api-abc123-pod1",
                "ownerReferences": [{"kind": "ReplicaSet", "name": "osac-api-abc123", "controller": True}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "api", "imageID": "quay.io/osac/api@sha256:abc"}],
            },
        },
    ]
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: json.dumps({"items": resources}))

    result = capture_module.osac_image_ids(Path("/fake/kubeconfig"), "osac-e2e-ci")
    assert result.image_ids == "osac-api/api=quay.io/osac/api@sha256:abc;osac-api/api=unknown"
    assert result.complete == "false"


def test_osac_image_ids_bounds_output_and_uses_unknown_fallback(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_kubectl(kubeconfig: Path, *arguments: str, stdin: str | None = None) -> str:
        raise subprocess.CalledProcessError(1, "kubectl")

    monkeypatch.setattr(capture_module, "_kubectl", failing_kubectl)
    result = capture_module.osac_image_ids(Path("/fake/kubeconfig"), "osac-e2e-ci")
    assert result.image_ids == "unknown"
    assert result.complete == "unknown"
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: json.dumps({"items": []}))
    result = capture_module.osac_image_ids(Path("/fake/kubeconfig"), "osac-e2e-ci")
    assert result.image_ids == "unknown"
    assert result.complete == "unknown"

    resources = [
        {
            "kind": "Deployment",
            "metadata": {"name": "osac", "labels": {"app.kubernetes.io/part-of": "osac"}},
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"name": f"container-{number:03d}"} for number in range(80)]}},
            },
        },
        {
            "kind": "ReplicaSet",
            "metadata": {
                "name": "osac-abc123",
                "ownerReferences": [{"kind": "Deployment", "name": "osac", "controller": True}],
            },
        },
        {
            "kind": "Pod",
            "metadata": {
                "name": "osac-abc123-pod",
                "ownerReferences": [{"kind": "ReplicaSet", "name": "osac-abc123", "controller": True}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {"name": f"container-{number:03d}", "imageID": f"sha256:{number:064x}"} for number in range(80)
                ],
            },
        },
    ]
    monkeypatch.setattr(capture_module, "MAX_OSAC_IMAGE_IDS_LENGTH", 256)
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: json.dumps({"items": resources}))
    result = capture_module.osac_image_ids(Path("/fake/kubeconfig"), "osac-e2e-ci")
    assert result.image_ids.endswith(capture_module.IMAGE_IDS_TRUNCATION_MARKER)
    assert result.complete == "false"
    assert len(result.image_ids) <= 256
    assert "\n" not in result.image_ids


def test_osac_image_ids_degrade_to_unknown_for_hostile_deployment_names(
    *, capture_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deployment names are DNS-1123 in a real cluster; a hostile API
    response with an unsafe name must degrade the whole value to
    "unknown" rather than being rewritten (or breaking the sed/YAML
    rendering the value crosses in the CI action)."""
    resources = [
        {
            "kind": "Deployment",
            "metadata": {"name": 'evil${CI_OBSERVE_TEST_REF}|&"', "labels": {"app.kubernetes.io/part-of": "osac"}},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "api"}]}}},
        }
    ]
    monkeypatch.setattr(capture_module, "_kubectl", lambda *args, **kwargs: json.dumps({"items": resources}))
    result = capture_module.osac_image_ids(Path("/fake/kubeconfig"), "osac-e2e-ci")
    assert result.image_ids == "unknown"
    assert result.complete == "unknown"


def test_safe_image_part_keeps_underscores_and_drops_rendering_unsafe_characters(*, capture_module: ModuleType) -> None:
    """Container names carry underscores; image IDs carry colons, slashes,
    at-signs and digest runs. Everything that would break the manifest's
    double-quoted env values or the CI action's sed rendering (newline,
    quote, backslash, pipe, ampersand, dollar) is dropped."""
    assert capture_module._safe_image_part("grpc_server") == "grpc_server"
    assert capture_module._safe_image_part("cri-o://quay.io/osac/x@sha256:abc+def-1") == (
        "cri-o://quay.io/osac/x@sha256:abc+def-1"
    )
    assert capture_module._safe_image_part('bad|&"\\$`\nname') == "badname"
    assert capture_module._safe_image_part(None) == ""
    assert capture_module._safe_image_part(42) == ""


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

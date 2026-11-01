"""Configuration tests for the ci-observe collectors (lane A of the
collector-first CI observability plan).

These are structural tests on monitoring/config/ci-observe/*.yaml: they pin
both collector paths -- the live-push config the CI capture action deploys
(OTLP/HTTP to the central backend) and the local file-capture config the
capture supervisor deploys (separate append-only OTLP JSONL files under
/capture) -- plus the shared cluster manifest, the narrow read-only RBAC,
the image digests and the placeholder conventions. Runtime behavior of the
same statements and operators is validated against the pinned collector
image in the lane A validation runbook; see the plan for the recorded
evidence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "ci-observe"

CONTRIB_IMAGE = (
    "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib"
    "@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b"
)
BEYLA_IMAGE = "quay.io/osac-project/beyla@sha256:4896c8963342919a51dd403a7bdb8a58ad44de04bd455e9f641e91b1d9c6881b"
NAMESPACE = "ci-observability-canary"
SIGNALS = ("traces", "metrics", "logs")
UNSAFE_TRACE_KEYS = (
    "http.request.header.authorization",
    "http.request.body",
    "http.response.body",
    "db.query.text",
    "db.statement",
    "url.query",
    "url.full",
)


@pytest.fixture(scope="module")
def collector() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "collector.yaml").read_text())


@pytest.fixture(scope="module")
def collector_live() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "collector-live.yaml").read_text())


@pytest.fixture(scope="module")
def replay() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "replay.yaml").read_text())


@pytest.fixture(scope="module")
def cluster_docs() -> list[dict[str, Any]]:
    text = (CONFIG_DIR / "cluster.yaml").read_text()
    docs = [doc for doc in yaml.safe_load_all(text) if doc]
    rendered = yaml.safe_dump(docs)
    assert "${env:" not in rendered, "cluster.yaml is rendered by the coordinator, not by the collector"
    return docs


def by_kind(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [doc for doc in docs if doc.get("kind") == kind]


class TestGateway:
    def test_otlp_listens_on_standard_ports(self, collector: dict[str, Any]) -> None:
        protocols = collector["receivers"]["otlp"]["protocols"]
        assert protocols["grpc"]["endpoint"] == "0.0.0.0:4317"
        assert protocols["http"]["endpoint"] == "0.0.0.0:4318"

    def test_k8s_objects_snapshot_then_watch(self, collector: dict[str, Any]) -> None:
        k8s = collector["receivers"]["k8s_objects"]
        assert k8s["auth_type"] == "serviceAccount"
        assert k8s["include_initial_state"] is True
        assert k8s["storage"] == "file_storage/k8sobjects"
        assert k8s["error_mode"] == "ignore"
        names = {obj["name"] for obj in k8s["objects"]}
        # Metadata/status only, never Secret or ConfigMap content.
        assert names == {"pods", "events", "computeinstances", "virtualnetworks", "subnets", "clusterorders"}
        for obj in k8s["objects"]:
            assert obj["mode"] == "watch"
            assert obj["namespaces"] == ["${env:CI_OBSERVE_NAMESPACE}"]
            assert (obj.get("group") == "osac.openshift.io") == (obj["name"] != "pods" and obj["name"] != "events")

    def test_filelog_restricted_to_observed_namespace(self, collector: dict[str, Any]) -> None:
        filelog = collector["receivers"]["file_log"]
        assert filelog["include"] == ["/var/log/pods/${env:CI_OBSERVE_NAMESPACE}_*/*/*.log"]
        assert filelog["start_at"] == "beginning"
        assert filelog["storage"] == "file_storage/filelog"

    def test_filelog_regexes_match_cri_lines_and_paths(self, collector: dict[str, Any]) -> None:
        operators = {op["id"]: op for op in collector["receivers"]["file_log"]["operators"] if "id" in op}
        cri = re.match(operators["parse-crio"]["regex"], "2026-10-29T21:00:00.123456789Z stdout F message text")
        assert cri and cri.group("time") == "2026-10-29T21:00:00.123456789Z"
        assert cri.group("log") == "message text"
        # The widely copied "[^ Z]+" time pattern drops every Z-suffixed CRI
        # line; these regexes must not reintroduce it.
        assert "[^ Z]" not in operators["parse-crio"]["regex"]
        for route in operators["get-format"]["routes"]:
            assert "$$" not in route["expr"], "the $$body form never matches at 0.164.0"
        path_re = operators["extract-metadata-from-filepath"]["regex"]
        meta = re.match(path_re, "/var/log/pods/osac-e2e-ci_pod-7d9f8b6c5-x2k4q_1a2b3c4d-uid/api-container/0.log")
        assert meta and meta.group("namespace") == "osac-e2e-ci"
        assert meta.group("pod") == "pod-7d9f8b6c5-x2k4q"
        assert meta.group("container") == "api-container"

    def test_kubelet_stats_uses_service_account(self, collector: dict[str, Any]) -> None:
        kubelet = collector["receivers"]["kubelet_stats"]
        assert kubelet["auth_type"] == "serviceAccount"
        assert kubelet["endpoint"] == "${env:CI_OBSERVE_KUBELET_ENDPOINT}"
        assert kubelet["insecure_skip_verify"] is True

    def test_kubelet_stats_needs_no_kubelet_pods_permission(self, collector: dict[str, Any]) -> None:
        """extra_metadata_labels makes every scrape call the kubelet /pods
        endpoint, which the kubelet authorizes as nodes/proxy — outside the
        nodes/stats grant — and the 403 aborts the whole scrape (kubeletstats
        0.164): metrics.jsonl stays empty. Pod/container identity comes from
        /stats/summary itself."""
        kubelet = collector["receivers"]["kubelet_stats"]
        assert "extra_metadata_labels" not in kubelet
        assert "metric_groups" not in kubelet, "defaults are node, pod and container: the bounded set"

    def test_capture_files_are_append_only_jsonl(self, collector: dict[str, Any]) -> None:
        for signal in SIGNALS:
            exporter = collector["exporters"][f"file/{signal}"]
            assert exporter["path"] == f"/capture/{signal}.jsonl"
            assert exporter["append"] is True, "append must be explicit: the file exporter default is truncate"
            assert exporter["format"] == "json"
            assert "rotation" not in exporter, "rotation would break the append-only capture contract"
            assert "compression" not in exporter, "replay reads uncompressed JSONL only"

    def test_state_lives_on_the_capture_volume(self, collector: dict[str, Any]) -> None:
        extensions = collector["extensions"]
        assert extensions["file_storage/k8sobjects"]["directory"] == "/capture/.state/k8sobjects"
        assert extensions["file_storage/filelog"]["directory"] == "/capture/.state/filelog"

    def test_trace_sanitization_removes_unsafe_fields(self, collector: dict[str, Any]) -> None:
        blocks = collector["processors"]["transform/sanitize_traces"]["trace_statements"]
        contexts = {block["context"]: block["statements"] for block in blocks}
        assert set(contexts) == {"resource", "span", "spanevent"}
        joined = "\n".join(contexts["span"])
        # Header values (including authorization) are removed by prefix.
        assert 'delete_matching_keys(attributes, "http.request.header.*")' in joined
        assert 'delete_matching_keys(attributes, "http.response.header.*")' in joined
        for key in UNSAFE_TRACE_KEYS:
            assert f'delete_key(attributes, "{key}")' in joined or key.startswith("http.request.header."), key

    def test_long_lived_secret_scrubbing(self, collector: dict[str, Any]) -> None:
        """Policy (user decision, 2026-10-30): debuggability first. Only the
        container registry pull secret and the AAP license are scrubbed from
        captures; raw parameter values, exception text, stack traces and
        command lines are kept for investigation, and the release gate and
        the post-upload scanner on the artifact upload path are the second
        layer for these two secrets."""
        blocks = collector["processors"]["transform/sanitize_traces"]["trace_statements"]
        contexts = {block["context"]: block["statements"] for block in blocks}
        assert set(contexts) == {"resource", "span", "spanevent"}
        pull = '(\\"auth\\"\\\\s*:\\\\s*\\")[A-Za-z0-9+/=]{8,}(\\")|eyJhdXRocyI6[A-Za-z0-9+/=]{40,}'
        aap = "(?i)(license|entitlement|subscription)([^A-Za-z0-9+/=]{0,8})[A-Za-z0-9+/=]{200,}"
        # Both secret rules run over every trace field that holds raw data.
        for context, statements in contexts.items():
            joined = "\n".join(statements)
            assert pull in joined, f"pull-secret rule missing from {context}"
            assert aap in joined, f"AAP-license rule missing from {context}"
            assert "[registry-secret]" in joined and "[aap-license]" in joined
        # The reverted aggressive rules must stay gone: raw test data is kept.
        span = "\n".join(contexts["span"])
        assert "pytest.nodeid" not in span, "bracket stripping was reverted"
        assert 'replace_pattern(name, "\\\\[' not in span, "bracket stripping was reverted"
        assert "set(status.message" not in span, "status truncation was reverted"
        assert "process.command_line" not in "\n".join(contexts["resource"]), "command line deletion was reverted"
        assert "exception." not in "\n".join(contexts["spanevent"]), "exception attribute deletion was reverted"
        # The same two rules scrub raw string log bodies (pod logs).
        log_statements = collector["processors"]["transform/k8s_objects"]["log_statements"][0]["statements"]
        log_joined = "\n".join(log_statements)
        assert pull in log_joined and aap in log_joined
        assert "where IsString(body)" in log_joined

    def test_object_projection_keeps_metadata_and_status_only(self, collector: dict[str, Any]) -> None:
        statements = collector["processors"]["transform/k8s_objects"]["log_statements"][0]["statements"]
        joined = "\n".join(statements)
        assert 'keep_keys(body["object"], ["metadata", "status"])' in joined
        assert 'delete_key(body["object"]["metadata"], "managedFields")' in joined
        # Both projections must be guarded so file_log records pass through.
        assert joined.count('where IsMap(body["object"])') == 3
        events = next(s for s in statements if '== "events"' in s)
        assert "involvedObject" in events and "reason" in events and "message" in events

    def test_capture_identity_attributes(self, collector: dict[str, Any]) -> None:
        actions = {a["key"]: a for a in collector["processors"]["resource/capture"]["attributes"]}
        assert actions["osac.ci.capture_id"]["value"] == "${env:CI_OBSERVE_CAPTURE_ID}"
        assert actions["osac.ci.observed_namespace"]["value"] == "${env:CI_OBSERVE_NAMESPACE}"
        assert actions["osac.ci.cluster_uid"]["value"] == "${env:CI_OBSERVE_CLUSTER_UID}"
        # Suite and PR make captures filterable in Grafana. They are upserted
        # on every signal the gateway exports; empty for non-PR runs (Loki and
        # Prometheus drop empty label values, so the labels are just absent).
        assert actions["osac.ci.suite"]["value"] == "${env:CI_OBSERVE_SUITE}"
        assert actions["osac.ci.pr"]["value"] == "${env:CI_OBSERVE_PR}"
        for action in actions.values():
            assert action["action"] == "upsert"

    def test_pipelines_and_budgets(self, collector: dict[str, Any]) -> None:
        service = collector["service"]
        pipelines = service["pipelines"]
        assert pipelines["traces"]["receivers"] == ["otlp"]
        assert set(pipelines["metrics"]["receivers"]) == {"otlp", "kubelet_stats"}
        assert set(pipelines["logs"]["receivers"]) == {"otlp", "k8s_objects", "file_log"}
        for signal in SIGNALS:
            assert pipelines[signal]["exporters"] == [f"file/{signal}"]
            assert "memory_limiter" in pipelines[signal]["processors"]
            assert "batch" in pipelines[signal]["processors"]
        batch = collector["processors"]["batch"]
        # One JSONL line is one batch; the cap keeps lines under the 16 MiB
        # replay limit.
        assert batch["send_batch_max_size"] <= 256
        assert collector["processors"]["memory_limiter"]["limit_mib"] <= 384

    def test_health_and_data_counters(self, collector: dict[str, Any]) -> None:
        assert collector["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"
        readers = collector["service"]["telemetry"]["metrics"]["readers"]
        assert readers[0]["pull"]["exporter"]["prometheus"]["port"] == 8888


class TestLiveGateway:
    """The live-push config the CI capture action deploys: the same
    collection as the local file-capture collector.yaml, but every pipeline
    exports to the central backend over OTLP/HTTP with basic auth instead
    of appending to capture files."""

    def test_file_capture_paths_are_gone(self, collector_live: dict[str, Any]) -> None:
        assert set(collector_live["exporters"]) == {"otlphttp/live"}
        for signal in SIGNALS:
            assert collector_live["service"]["pipelines"][signal]["exporters"] == ["otlphttp/live"]
        dumped = yaml.safe_dump(collector_live)
        assert "/capture/" not in dumped, "the live config must not write to the capture volume"
        assert not re.search(r"\bfile/", dumped), "the live config must not use file exporters"

    def test_exports_live_with_auth_and_insecure_tls(self, collector_live: dict[str, Any]) -> None:
        exporter = collector_live["exporters"]["otlphttp/live"]
        assert exporter["endpoint"] == "${env:CI_OBSERVE_OTLP_ENDPOINT}"
        assert exporter["headers"]["Authorization"] == "${env:CI_OBSERVE_OTLP_AUTH}"
        # The central endpoint serves an internal-CA certificate; the
        # collector skips verification until the CA is mounted (follow-up).
        assert exporter["tls"]["insecure"] is True

    def test_queue_and_retry_survive_a_brief_central_outage(self, collector_live: dict[str, Any]) -> None:
        """A central deploy or restart must not drop in-flight batches: the
        sending queue holds them and the exporter keeps retrying."""
        exporter = collector_live["exporters"]["otlphttp/live"]
        assert exporter["sending_queue"]["queue_size"] >= 5000
        assert exporter["retry_on_failure"]["enabled"] is True

    def test_collects_and_stamps_the_same_signals(
        self, collector_live: dict[str, Any], collector: dict[str, Any]
    ) -> None:
        """Same receivers, same capture identity processor, same memory
        budget as the file-capture path: only the transport differs."""
        for name in ("otlp", "k8s_objects", "file_log", "kubelet_stats"):
            assert collector_live["receivers"][name] == collector["receivers"][name], name
        for name in ("resource/capture", "memory_limiter", "transform/sanitize_traces", "transform/k8s_objects"):
            assert collector_live["processors"][name] == collector["processors"][name], name

    def test_state_lives_on_the_pod_local_volume(self, collector_live: dict[str, Any]) -> None:
        """Watch resourceVersions and filelog offsets move off the capture
        PVC to the pod-local state volume: they only need to survive
        container restarts."""
        extensions = collector_live["extensions"]
        assert extensions["file_storage/k8sobjects"]["directory"] == "/state/k8sobjects"
        assert extensions["file_storage/filelog"]["directory"] == "/state/filelog"
        assert collector_live["receivers"]["k8s_objects"]["storage"] == "file_storage/k8sobjects"
        assert collector_live["receivers"]["file_log"]["storage"] == "file_storage/filelog"

    def test_local_file_capture_config_is_unchanged(self, collector: dict[str, Any]) -> None:
        """collector.yaml keeps the local supervisor's append-only capture
        contract; only its header comment changed."""
        for signal in SIGNALS:
            exporter = collector["exporters"][f"file/{signal}"]
            assert exporter["path"] == f"/capture/{signal}.jsonl"
            assert exporter["append"] is True


class TestReplay:
    def test_reads_capture_interface_from_beginning(self, replay: dict[str, Any]) -> None:
        for signal in SIGNALS:
            receiver = replay["receivers"][f"otlp_json_file/{signal}"]
            assert receiver["include"] == [f"/capture/{signal}.jsonl"]
            assert receiver["start_at"] == "beginning"
            assert receiver["storage"] == f"file_storage/{signal}"
            assert receiver["max_log_size"] == "16MiB"
            assert receiver["max_log_size_behavior"] == "truncate"
            assert "compression" not in receiver, "capture files are uncompressed"

    def test_offsets_persist_per_signal(self, replay: dict[str, Any]) -> None:
        for signal in SIGNALS:
            extension = replay["extensions"][f"file_storage/{signal}"]
            assert extension["directory"] == f"${{env:CI_OBSERVE_REPLAY_STATE}}/{signal}"
        assert set(replay["service"]["extensions"]) == {f"file_storage/{s}" for s in SIGNALS}

    def test_exports_unchanged_to_env_endpoint(self, replay: dict[str, Any]) -> None:
        assert replay["exporters"]["otlp_http"]["endpoint"] == "${env:CI_OBSERVE_OTLP_ENDPOINT}"
        for signal in SIGNALS:
            pipeline = replay["service"]["pipelines"][signal]
            assert pipeline["receivers"] == [f"otlp_json_file/{signal}"]
            assert pipeline["exporters"] == ["otlp_http"]
            assert "processors" not in pipeline, "replay must export data exactly as captured"


class TestCluster:
    def test_images_are_pinned_to_verified_digests(self, cluster_docs: list[dict[str, Any]]) -> None:
        docs = cluster_docs
        images = set()
        for doc in docs:
            if doc.get("kind") == "DaemonSet":
                images |= {c["image"] for c in doc["spec"]["template"]["spec"]["containers"]}
            if doc.get("kind") == "Deployment":
                images |= {c["image"] for c in doc["spec"]["template"]["spec"]["containers"]}
        assert images == {CONTRIB_IMAGE, BEYLA_IMAGE}
        for image in images:
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), image

    def test_owned_namespace_and_no_capture_volume(self, cluster_docs: list[dict[str, Any]]) -> None:
        namespaces = {doc["metadata"].get("namespace") for doc in cluster_docs if doc.get("kind")}
        namespaces.discard(None)
        assert namespaces == {NAMESPACE, "${CI_OBSERVE_NAMESPACE}"}
        # Live push made the capture PVC obsolete: the gateway streams
        # telemetry to the central backend as it is collected.
        assert not by_kind(cluster_docs, "PersistentVolumeClaim")
        deployment = by_kind(cluster_docs, "Deployment")[0]
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        mounts = {mount["name"] for mount in container["volumeMounts"]}
        volumes = {volume["name"] for volume in deployment["spec"]["template"]["spec"]["volumes"]}
        assert "capture" not in mounts and "capture" not in volumes
        # The only extra volume is the pod-local collector state.
        assert volumes == {"config", "state", "pod-logs"}
        spec = deployment["spec"]["template"]["spec"]
        state = next(volume for volume in spec["volumes"] if volume["name"] == "state")
        assert state["emptyDir"] == {}

    def test_rbac_is_narrow_and_read_only(self, cluster_docs: list[dict[str, Any]]) -> None:
        read_only = {"get", "list", "watch"}
        for doc in by_kind(cluster_docs, "Role") + by_kind(cluster_docs, "ClusterRole"):
            for rule in doc["rules"]:
                assert set(rule["verbs"]) <= read_only, (doc["metadata"]["name"], rule)
                for resource in rule["resources"]:
                    assert resource not in ("secrets", "configmaps"), (doc["metadata"]["name"], resource)
        # Namespaced object watch happens in the observed namespace only.
        role = by_kind(cluster_docs, "Role")[0]
        assert role["metadata"]["namespace"] == "${CI_OBSERVE_NAMESPACE}"
        core = next(rule for rule in role["rules"] if rule["apiGroups"] == [""])
        assert set(core["resources"]) == {"pods", "events"}
        crds = next(rule for rule in role["rules"] if rule["apiGroups"] == ["osac.openshift.io"])
        assert set(crds["resources"]) == {"computeinstances", "virtualnetworks", "subnets", "clusterorders"}
        # The only cluster-scoped permission is the kubelet stats subresource.
        cluster_role = by_kind(cluster_docs, "ClusterRole")[0]
        assert cluster_role["rules"] == [{"apiGroups": [""], "resources": ["nodes/stats"], "verbs": ["get"]}]

    def test_gateway_is_single_replica_and_unprivileged(self, cluster_docs: list[dict[str, Any]]) -> None:
        deployment = by_kind(cluster_docs, "Deployment")[0]
        assert deployment["spec"]["replicas"] == 1, "one writer and one watch; duplicates would corrupt evidence"
        assert deployment["metadata"]["namespace"] == NAMESPACE
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert container["args"] == ["--config=/etc/ci-observe/config.yaml"]
        env = {e["name"]: e["value"] for e in container["env"]}
        assert set(env) == {
            "CI_OBSERVE_CAPTURE_ID",
            "CI_OBSERVE_NAMESPACE",
            "CI_OBSERVE_CLUSTER_UID",
            "CI_OBSERVE_KUBELET_ENDPOINT",
            "CI_OBSERVE_SUITE",
            "CI_OBSERVE_PR",
            "CI_OBSERVE_OTLP_ENDPOINT",
            "CI_OBSERVE_OTLP_AUTH",
        }
        assert all(value.startswith("${CI_OBSERVE_") for value in env.values())
        security = container["securityContext"]
        assert security["runAsNonRoot"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"]["drop"] == ["ALL"]
        assert container["resources"]["limits"]["memory"] == "512Mi"

    def test_beyla_runs_in_guest_with_minimum_privileges(self, cluster_docs: list[dict[str, Any]]) -> None:
        daemonset = by_kind(cluster_docs, "DaemonSet")[0]
        assert daemonset["metadata"]["namespace"] == NAMESPACE
        pod_spec = daemonset["spec"]["template"]["spec"]
        assert pod_spec["hostPID"] is True, "required for node process discovery"
        container = pod_spec["containers"][0]
        security = container["securityContext"]
        assert security.get("privileged") is not True
        assert security["runAsUser"] == 0
        assert security["readOnlyRootFilesystem"] is True
        assert set(security["capabilities"]["add"]) == {
            "BPF",
            "SYS_PTRACE",
            "NET_RAW",
            "CHECKPOINT_RESTORE",
            "DAC_READ_SEARCH",
            "PERFMON",
            "SYS_ADMIN",
        }
        assert security["capabilities"]["drop"] == ["ALL"]
        assert container["env"] == [{"name": "BEYLA_CONFIG_PATH", "value": "/etc/beyla/beyla.yml"}]
        beyla_config = yaml.safe_load(by_kind(cluster_docs, "ConfigMap")[0]["data"]["beyla.yml"])
        assert beyla_config["discovery"]["instrument"] == [{"k8s_namespace": "${CI_OBSERVE_NAMESPACE}"}]
        assert beyla_config["otel_traces_export"]["endpoint"].endswith(":4317")

    def test_scc_requires_explicit_parent_approval(self, cluster_docs: list[dict[str, Any]]) -> None:
        assert not by_kind(cluster_docs, "SecurityContextConstraints"), (
            "the Beyla SCC must stay commented out until the parent approves it"
        )
        text = (CONFIG_DIR / "cluster.yaml").read_text()
        assert "SCC GATE" in text and "add-scc-to-user" in text


class TestLgtmOverrides:
    """The local LGTM stack must promote the capture's osac.ci.* resource
    attributes to queryable labels, or suite/PR filtering silently shows no
    data. Both lists replace the curated defaults, so they must keep repeating
    them while adding the osac.ci attributes."""

    # The stock grafana/otel-lgtm 0.38.1 defaults each list replaces.
    LOKI_DEFAULTS = (
        "service.name",
        "service.namespace",
        "service.instance.id",
        "deployment.environment",
        "deployment.environment.name",
        "cloud.region",
        "cloud.availability_zone",
        "k8s.cluster.name",
        "k8s.namespace.name",
        "k8s.pod.name",
        "k8s.container.name",
        "container.name",
        "k8s.replicaset.name",
        "k8s.deployment.name",
        "k8s.statefulset.name",
        "k8s.daemonset.name",
        "k8s.cronjob.name",
        "k8s.job.name",
    )

    def test_loki_promotes_the_osac_ci_attributes_to_index_labels(self) -> None:
        config = yaml.safe_load((CONFIG_DIR / "lgtm-overrides" / "loki-config.yaml").read_text())
        attributes = config["limits_config"]["otlp_config"]["resource_attributes"]["attributes_config"][0]["attributes"]
        for name in self.LOKI_DEFAULTS:
            assert name in attributes, name
        for name in ("osac.ci.capture_id", "osac.ci.observed_namespace", "osac.ci.suite", "osac.ci.pr"):
            assert name in attributes, name

    def test_prometheus_promotes_the_osac_ci_attributes(self) -> None:
        config = yaml.safe_load((CONFIG_DIR / "lgtm-overrides" / "prometheus.yaml").read_text())
        promoted = config["otlp"]["promote_resource_attributes"]
        for name in ("osac.ci.capture_id", "osac.ci.observed_namespace", "osac.ci.suite", "osac.ci.pr"):
            assert name in promoted, name
        # The list replaces the curated defaults; they must stay repeated.
        assert "service.name" in promoted and "k8s.pod.name" in promoted

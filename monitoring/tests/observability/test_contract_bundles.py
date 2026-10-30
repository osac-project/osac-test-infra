"""Bundle-level contract tests over the sanitized v1 example bundles.

Every example must pass the production bundle validator, and each bundle
carries scenario assertions that pin the behavior it exists to demonstrate.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from contract_helpers import VALID_BUNDLES, bundle_path, load_bundle_journal, load_json

from monitoring.ci_observability.contracts.derivation import derive_attempt_result
from monitoring.ci_observability.contracts.validation import validate_bundle


@pytest.mark.parametrize("bundle", VALID_BUNDLES, ids=lambda path: path.name)
def test_every_example_bundle_validates(bundle: Path) -> None:
    validate_bundle(bundle_dir=bundle)


@pytest.mark.parametrize("bundle", VALID_BUNDLES, ids=lambda path: path.name)
def test_bundle_native_reports_are_well_formed_xml(bundle: Path) -> None:
    for report in bundle.glob("native-report/*.xml"):
        ElementTree.parse(report)


def test_serial_bundle_links_operation_progress_and_resource() -> None:
    records = load_bundle_journal("serial", "main")
    started = next(r for r in records if r["event"] == "operation_started")
    progress = next(r for r in records if r["event"] == "operation_progress")
    observed = next(r for r in records if r["event"] == "resource_observed")

    # The operation names the test attempt it belongs to; no time-proximity
    # inference is needed or allowed.
    assert started["data"]["test"]["test_id"].endswith("test_bare_metal_instance_lifecycle")
    assert started["data"]["resource"]["cluster_role"] == "lab"
    assert progress["data"]["operation_id"] == started["data"]["operation_id"]
    # The observation carries only allowlisted identity, metadata, and
    # status fields, with the owner chain and finalizers explicit.
    assert set(observed["data"]) == {
        "resource",
        "generation",
        "ready",
        "status",
        "finalizers",
        "conditions",
        "warning_events",
    }
    assert observed["data"]["resource"]["owner_uids"]
    assert observed["data"]["finalizers"] == ["baremetalhost.metal3.io"]
    assert observed["data"]["warning_events"][0]["reason"] == "ProvisioningRegistrationError"
    # A deselected test is collected but never executed.
    collected = [r for r in records if r["event"] == "test_collected"]
    assert {c["data"]["test_id"] for c in collected if c["data"]["deselected"]} == {
        "tests/e2e/bmaas/test_bare_metal_inventory.py::test_bare_metal_inventory_drift"
    }


def test_parallel_bundle_measures_fixture_phases_and_use_edges() -> None:
    gw0 = load_bundle_journal("parallel", "gw0")
    fixture_events = [r for r in gw0 if r["event"].startswith("fixture") and "default_subnet" in str(r["data"])]
    phases = [(r["event"], r["data"]["phase"]) for r in fixture_events if "phase" in r["data"]]
    assert phases == [
        ("fixture_started", "setup"),
        ("fixture_finished", "setup"),
        ("fixture_started", "teardown"),
        ("fixture_finished", "teardown"),
    ]
    used = [r for r in gw0 if r["event"] == "fixture_used"]
    assert len(used) == 2
    setup = next(r for r in fixture_events if r["event"] == "fixture_finished" and r["data"]["phase"] == "setup")
    teardown = next(r for r in fixture_events if r["event"] == "fixture_finished" and r["data"]["phase"] == "teardown")
    assert setup["data"]["duration_ns"] != teardown["data"]["duration_ns"]

    gw1 = load_bundle_journal("parallel", "gw1")
    instances = {
        r["data"]["fixture_instance_id"]: r["data"].get("params_fingerprint")
        for r in gw1
        if r["event"] == "fixture_started" and r["data"]["phase"] == "setup"
    }
    assert instances == {"fx-tenant-b": "b" * 64, "fx-tenant-a": "a" * 64}
    # Operation resources carry the tenant annotation and the API id as
    # explicit known-safe fields.
    operation = next(r for r in gw0 if r["event"] == "operation_started")
    assert operation["data"]["resource"]["tenant"] == "tenant-1"
    assert operation["data"]["resource"]["api_id"]


def test_parallel_bundle_keeps_body_and_cleanup_failures_separate() -> None:
    gw1 = load_bundle_journal("parallel", "gw1")
    vm_exists = "tests/e2e/vmaas/test_vm_instance_exists.py::test_vm_instance_exists"
    phases = [
        {"phase": r["data"]["phase"], "outcome": r["data"]["outcome"], "failure": r["data"].get("failure")}
        for r in gw1
        if r["event"] == "test_phase_finished" and r["data"]["test_id"] == vm_exists
    ]
    # A passing body with a failing teardown: both outcomes survive, and
    # the derived attempt outcome is failed, never displayed as passed.
    derived = derive_attempt_result(phases=phases)
    assert derived.outcome == "failed"
    assert derived.cleanup_failure is not None
    assert derived.cleanup_failure["signature"].startswith("NamespaceCleanupError")
    assert derived.first_failure == derived.cleanup_failure

    controller = load_bundle_journal("parallel", "controller")
    finished = next(r for r in controller if r["event"] == "execution_finished")
    assert finished["data"]["outcome"] == "failed"
    assert finished["data"]["failure"]["phase"] == "call"


def test_rerun_bundle_is_a_second_attempt_with_an_in_job_retry() -> None:
    manifest = load_json(bundle_path("rerun") / "bundle-manifest.json")
    assert manifest["execution"]["run_attempt"] == 2
    assert manifest["execution"]["job"]["github_job_id"] is not None
    records = load_bundle_journal("rerun", "main")
    calls = [r for r in records if r["event"] == "test_phase_finished" and r["data"]["phase"] == "call"]
    assert [(c["data"]["attempt_index"], c["data"]["outcome"]) for c in calls] == [(1, "failed"), (2, "passed")]
    # Only the re-run job exists in this bundle; inherited jobs produce none.
    assert [entry["path"] for entry in manifest["files"]] == ["journals/main.jsonl"]


def test_campaign_bundle_repeats_one_test_under_campaign_identity() -> None:
    manifest = load_json(bundle_path("campaign") / "bundle-manifest.json")
    assert manifest["suite"]["campaign"] == {
        "campaign_id": "flake-20261027-caas-lifecycle",
        "group": "caas",
        "repetition": 1,
        "repetitions_total": 3,
    }
    assert manifest["environment"]["sources"][0]["revision"] == "0190bcbec275469b39d996207d46b35126f3216e"
    assert manifest["suite"]["selection"]["worker_count"] == 0
    records = load_bundle_journal("campaign", "main")
    calls = [r for r in records if r["event"] == "test_phase_finished" and r["data"]["phase"] == "call"]
    assert [c["data"]["attempt_index"] for c in calls] == [1, 2, 3]
    assert [c["data"]["outcome"] for c in calls] == ["failed", "passed", "passed"]


def test_partial_bundle_keeps_unknowns_explicit() -> None:
    manifest = load_json(bundle_path("partial") / "bundle-manifest.json")
    assert manifest["execution"]["job"]["github_job_id"] is None
    assert manifest["completeness"]["complete"] is False
    assert manifest["completeness"]["end_reason"] == "interrupted"
    assert manifest["completeness"]["dropped_records"] > 0
    orchestration = [s for s in manifest["environment"]["sources"] if s["role"] == "orchestration"]
    assert orchestration[0]["revision"] is None

    controller = load_bundle_journal("partial", "controller")
    assert not any(r["event"] == "execution_finished" for r in controller)
    lost = next(r for r in controller if r["event"] == "worker_stopped")
    assert lost["data"]["outcome"] == "interrupted"
    # The interrupted observer still delivered one pod observation with
    # per-container state and warning-event classification only.
    observed = next(r for r in controller if r["event"] == "resource_observed")
    containers = observed["data"]["containers"]
    assert {(c["name"], c["init"]) for c in containers} == {("manager", False), ("install-verification", True)}
    assert containers[0]["reason"] == "CrashLoopBackOff"
    assert observed["data"]["warning_events"][0]["reason"] == "BackOff"

    worker = load_bundle_journal("partial", "gw0")
    assert worker[-1]["event"] == "test_phase_started"
    assert worker[-1]["data"]["phase"] == "call"
    assert worker[0]["event"] == "fixture_started"
    entry = next(e for e in manifest["files"] if e["path"].endswith("gw0.jsonl"))
    assert entry["complete"] is False


def test_unit_ginkgo_bundle_converts_real_reports_faithfully() -> None:
    manifest = load_json(bundle_path("unit-ginkgo") / "bundle-manifest.json")
    assert manifest["execution"]["cohort"] == "pull-request"
    assert manifest["completeness"]["complete"] is True
    controller = load_bundle_journal("unit-ginkgo", "controller")
    finished = next(r for r in controller if r["event"] == "execution_finished")
    assert finished["data"]["outcome"] == "failed"

    # Two native invocations in one job, sharing identical test names, with
    # no fabricated phase boundaries and no fabricated monotonic clock.
    shared_ids: set[str] | None = None
    for invocation, attempts_expected in (("ginkgo-run-1", 2), ("ginkgo-run-2", 3)):
        records = load_bundle_journal("unit-ginkgo", invocation)
        assert all(r["invocation_id"] == invocation for r in records)
        assert all(r["mono_ns"] is None for r in records)
        assert not any(r["event"].startswith("test_phase") for r in records)
        results = [r for r in records if r["event"] == "test_result"]
        aggregates = [r for r in results if r["data"]["result_scope"] == "aggregate"]
        failing = [r for r in aggregates if r["data"]["test_id"].endswith("fails on purpose")]
        assert failing[0]["data"]["attempt_count"] == attempts_expected
        attempts = [
            r
            for r in results
            if r["data"]["result_scope"] == "attempt" and r["data"]["test_id"].endswith("fails on purpose")
        ]
        assert [a["data"]["attempt_index"] for a in attempts] == list(range(1, attempts_expected + 1))
        # The aggregate duration is the total; each attempt carries its own
        # measured span, so the total is never charged to the final retry.
        assert failing[0]["data"]["duration_ns"] > attempts[-1]["data"]["duration_ns"]
        ids = {r["data"]["test_id"] for r in aggregates}
        if shared_ids is None:
            shared_ids = ids
        else:
            assert ids == shared_ids
        started = next(r for r in records if r["event"] == "invocation_started")
        assert started["data"]["framework"] == "ginkgo"
        ended = next(r for r in records if r["event"] == "invocation_finished")
        assert ended["data"]["outcome"] == "failed"


def test_unit_build_fail_bundle_completes_without_collection_evidence() -> None:
    manifest = load_json(bundle_path("unit-build-fail") / "bundle-manifest.json")
    assert manifest["completeness"]["complete"] is True
    records = load_bundle_journal("unit-build-fail", "go-build")
    events = [r["event"] for r in records]
    # The build failed before any test collection: no collection records
    # exist, none are fabricated, and the capture is still complete.
    assert "collection_finished" not in events
    assert "test_collected" not in events
    assert "test_result" not in events
    finished = next(r for r in records if r["event"] == "invocation_finished")
    assert finished["data"]["outcome"] == "failed"
    assert finished["data"]["exit_code"] == 1


def test_mixed_aap_bundle_declares_cohort_and_invocation_frameworks() -> None:
    manifest = load_json(bundle_path("mixed-aap") / "bundle-manifest.json")
    assert manifest["suite"]["framework"] == "mixed"
    controller = load_bundle_journal("mixed-aap", "controller")
    finished = next(r for r in controller if r["event"] == "execution_finished")
    assert finished["data"]["outcome"] == "failed"

    pytest_records = load_bundle_journal("mixed-aap", "pytest-aap")
    pytest_started = next(r for r in pytest_records if r["event"] == "invocation_started")
    assert pytest_started["data"]["framework"] == "pytest"
    assert pytest_started["data"]["selection"]["worker_count"] == 0
    pytest_finished = next(r for r in pytest_records if r["event"] == "invocation_finished")
    assert pytest_finished["data"]["outcome"] == "passed"

    ansible_records = load_bundle_journal("mixed-aap", "ansible-targets")
    ansible_started = next(r for r in ansible_records if r["event"] == "invocation_started")
    assert ansible_started["data"]["framework"] == "ansible"
    assert ansible_started["data"]["dependencies"] == ["kind"]
    # Native aggregate results, with unknown workers left null.
    results = [r for r in ansible_records if r["event"] == "test_result"]
    assert all(r["data"]["worker_id"] is None for r in results)
    outcomes = {r["data"]["test_id"]: r["data"]["outcome"] for r in results}
    assert outcomes == {"targets/deploy_registry.yml": "failed", "targets/cleanup.yml": "passed"}

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path

import pytest

from tests.catalog.conftest import unique_name
from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_cluster_deletion,
    wait_for_cluster_grpc_removal,
    wait_for_cluster_order_cr,
    wait_for_cluster_progressing,
    wait_for_cluster_ready,
)
from tests.core.k8s_client import K8sClient
from tests.core.osac_cli import OsacCLI
from tests.core.runner import poll_until

logger = logging.getLogger(__name__)

OPERATOR_SELECTOR_LABEL = "control-plane=controller-manager"


def _count_provision_jobs(k8s: K8sClient, *, name: str) -> int:
    data = k8s.get_json(resource="clusterorder", name=name)
    jobs = data.get("status", {}).get("jobs", [])
    return len([j for j in jobs if j["type"] == "provision"])


@pytest.mark.disruptive
def test_operator_mid_provision_restart(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    namespace: str,
    cluster_template: str,
    pull_secret_path: str,
    ssh_public_key_path: str,
) -> None:
    """Kill operator during cluster provisioning, verify no duplicate AAP job and provision completes."""
    operator_deploy = k8s_hub_client.get_deployment_name_by_label(label=OPERATOR_SELECTOR_LABEL, namespace=namespace)

    name = unique_name("e2e-cluster")
    uuid = cli.create_cluster(
        name=name,
        template=cluster_template,
        template_parameter_files={"pull_secret": pull_secret_path},
        template_parameters={"ssh_public_key": Path(ssh_public_key_path).read_text().strip()},
    )

    try:
        co_name = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=uuid)

        poll_until(
            fn=lambda: k8s_hub_client.get_cluster_order_latest_job_state(
                name=co_name, job_type="provision", checked=False
            ),
            until=lambda v: v in ("Pending", "Running"),
            retries=60,
            delay=2,
            description=f"{co_name} provision job started",
        )

        provision_job_id_before = k8s_hub_client.get_cluster_order_latest_job_id(name=co_name, job_type="provision")
        provision_count_before = _count_provision_jobs(k8s_hub_client, name=co_name)
        logger.info(
            "Provision job %s running, killing operator (job count: %d)",
            provision_job_id_before,
            provision_count_before,
        )

        k8s_hub_client.rollout_restart(deployment=operator_deploy, namespace=namespace)
        k8s_hub_client.wait_for_rollout(deployment=operator_deploy, namespace=namespace)
        logger.info("Operator pod recovered")

        wait_for_cluster_progressing(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_ready(k8s=k8s_hub_client, name=co_name)
        logger.info("Cluster %s reached Ready after operator restart", co_name)

        provision_job_id_after = k8s_hub_client.get_cluster_order_latest_job_id(name=co_name, job_type="provision")
        provision_count_after = _count_provision_jobs(k8s_hub_client, name=co_name)

        assert provision_job_id_before == provision_job_id_after, (
            f"Provision job ID changed after operator restart: {provision_job_id_before} -> {provision_job_id_after}"
        )
        assert provision_count_before == provision_count_after, (
            f"Duplicate provision jobs created: {provision_count_before} -> {provision_count_after}"
        )

        cli.delete_cluster(uuid=uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_cluster(uuid=uuid)

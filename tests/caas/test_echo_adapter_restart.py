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
)
from tests.core.k8s_client import K8sClient
from tests.core.metering import MeteringCollector
from tests.core.osac_cli import OsacCLI

logger = logging.getLogger(__name__)

ECHO_ADAPTER_LABEL = "app.kubernetes.io/component=echo-adapter"


@pytest.mark.disruptive
@pytest.mark.metering
def test_echo_adapter_restart_recovery(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    namespace: str,
    cluster_template: str,
    pull_secret_path: str,
    ssh_public_key_path: str,
    metering: MeteringCollector,
) -> None:
    """Kill echo-adapter, verify Kafka consumer rebalances and cluster metering events resume."""
    adapter_deploy = k8s_hub_client.get_deployment_name_by_label(label=ECHO_ADAPTER_LABEL, namespace=namespace)

    name = unique_name("e2e-cluster")
    uuid = cli.create_cluster(
        name=name,
        template=cluster_template,
        template_parameter_files={"pull_secret": pull_secret_path},
        template_parameters={"ssh_public_key": Path(ssh_public_key_path).read_text().strip()},
    )

    try:
        co_name = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=uuid)
        wait_for_cluster_progressing(k8s=k8s_hub_client, name=co_name)

        metering.expect("osac.resource.created.v1", resource_id=uuid)
        metering.expect("osac.resource.started.v1", resource_id=uuid)
        metering.verify()
        logger.info("Cluster %s progressing, initial metering events verified", uuid)

        logger.info("Killing echo-adapter pod")
        k8s_hub_client.rollout_restart(deployment=adapter_deploy, namespace=namespace)
        k8s_hub_client.wait_for_rollout(deployment=adapter_deploy, namespace=namespace)
        logger.info("Echo-adapter recovered")

        metering.expect("osac.resource.heartbeat.v1", resource_id=uuid, timeout=180)
        metering.verify()
        logger.info("Metering events resumed after echo-adapter restart")

        cli.delete_cluster(uuid=uuid)
        metering.expect("osac.resource.deleted.v1", resource_id=uuid)

        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_cluster(uuid=uuid)

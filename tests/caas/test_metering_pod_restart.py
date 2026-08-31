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
from tests.core.runner import poll_until

logger = logging.getLogger(__name__)

METERING_COMPONENT_LABEL = "app.kubernetes.io/component=metering"
RECONCILER_TIMEOUT = 10 * 60


@pytest.mark.disruptive
@pytest.mark.metering
def test_metering_pod_restart_recovery(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    namespace: str,
    cluster_template: str,
    pull_secret_path: str,
    ssh_public_key_path: str,
    metering: MeteringCollector,
) -> None:
    """Kill metering pod, delete a cluster during downtime, verify reconciler emits correction events."""
    metering_deploy = k8s_hub_client.get_deployment_name_by_label(label=METERING_COMPONENT_LABEL, namespace=namespace)

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

        logger.info("Scaling metering to 0 replicas")
        k8s_hub_client.scale_deployment(deployment=metering_deploy, namespace=namespace, replicas=0)
        poll_until(
            fn=lambda: k8s_hub_client.count_by_label_all_namespaces(resource="pod", label=METERING_COMPONENT_LABEL),
            until=lambda count: count == 0,
            retries=30,
            delay=2,
            description="metering pods terminated",
        )

        logger.info("Deleting cluster while metering is down")
        cli.delete_cluster(uuid=uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
        logger.info("Cluster %s deleted while metering was down", uuid)

        logger.info("Scaling metering back to 1 replica")
        k8s_hub_client.scale_deployment(deployment=metering_deploy, namespace=namespace, replicas=1)
        k8s_hub_client.wait_for_rollout(deployment=metering_deploy, namespace=namespace)
        logger.info("Metering pod recovered, waiting for reconciler to detect missed deletion")

        metering.expect("osac.resource.correction.v1", resource_id=uuid, timeout=RECONCILER_TIMEOUT)
        metering.verify()
        logger.info("Reconciler emitted correction event for missed deletion")
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_cluster(uuid=uuid)

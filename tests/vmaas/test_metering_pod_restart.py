from __future__ import annotations

import logging

import pytest

from tests.catalog.conftest import unique_name
from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_cr,
    wait_for_deletion,
    wait_for_grpc_removal,
    wait_for_provision,
    wait_for_running,
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
    vm_template: str,
    default_subnet: str,
    metering: MeteringCollector,
) -> None:
    """Kill metering pod, delete a VM during downtime, verify reconciler emits correction events."""
    metering_deploy = k8s_hub_client.get_deployment_name_by_label(label=METERING_COMPONENT_LABEL, namespace=namespace)

    name = unique_name("e2e-ci")
    uuid: str = cli.create_compute_instance(
        name=name, template=vm_template, network_attachments=[{"subnet": default_subnet}]
    )

    ci_name: str = wait_for_cr(k8s=k8s_hub_client, uuid=uuid)
    wait_for_provision(k8s=k8s_hub_client, name=ci_name)
    wait_for_running(k8s=k8s_hub_client, name=ci_name)

    metering.expect("osac.resource.created.v1", resource_id=uuid)
    metering.expect("osac.resource.started.v1", resource_id=uuid)
    metering.verify()
    logger.info("VM %s running, initial metering events verified", uuid)

    logger.info("Scaling metering to 0 replicas")
    k8s_hub_client.scale_deployment(deployment=metering_deploy, namespace=namespace, replicas=0)
    try:
        poll_until(
            fn=lambda: k8s_hub_client.count_by_label_all_namespaces(resource="pod", label=METERING_COMPONENT_LABEL),
            until=lambda count: count == 0,
            retries=30,
            delay=2,
            description="metering pods terminated",
        )

        logger.info("Deleting VM while metering is down")
        cli.delete_compute_instance(uuid=uuid)
        wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
        wait_for_grpc_removal(grpc=grpc, uuid=uuid)
        logger.info("VM %s deleted while metering was down", uuid)
    finally:
        logger.info("Scaling metering back to 1 replica")
        k8s_hub_client.scale_deployment(deployment=metering_deploy, namespace=namespace, replicas=1)
        k8s_hub_client.wait_for_rollout(deployment=metering_deploy, namespace=namespace)

    logger.info("Metering pod recovered, waiting for reconciler to detect missed deletion")

    metering.expect("osac.resource.correction.v1", resource_id=uuid, timeout=RECONCILER_TIMEOUT)
    metering.verify()
    logger.info("Reconciler emitted correction event for missed deletion")

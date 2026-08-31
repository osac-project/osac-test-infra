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

logger = logging.getLogger(__name__)

ECHO_ADAPTER_LABEL = "app.kubernetes.io/component=echo-adapter"


@pytest.mark.disruptive
@pytest.mark.metering
def test_echo_adapter_restart_recovery(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    namespace: str,
    vm_template: str,
    default_subnet: str,
    metering: MeteringCollector,
) -> None:
    """Kill echo-adapter, verify Kafka consumer rebalances and events resume after recovery."""
    adapter_deploy = k8s_hub_client.get_deployment_name_by_label(label=ECHO_ADAPTER_LABEL, namespace=namespace)

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

    logger.info("Killing echo-adapter pod")
    k8s_hub_client.rollout_restart(deployment=adapter_deploy, namespace=namespace)
    k8s_hub_client.wait_for_rollout(deployment=adapter_deploy, namespace=namespace)
    logger.info("Echo-adapter recovered")

    logger.info("Triggering new metering event (stop VM)")
    grpc.update_compute_instance_run_strategy(ci_id=uuid, run_strategy="Halted")

    metering.expect("osac.resource.suspended.v1", resource_id=uuid, timeout=120)
    metering.verify()
    logger.info("Events resumed after echo-adapter restart")

    cli.delete_compute_instance(uuid=uuid)
    metering.expect("osac.resource.deleted.v1", resource_id=uuid)

    wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
    wait_for_grpc_removal(grpc=grpc, uuid=uuid)

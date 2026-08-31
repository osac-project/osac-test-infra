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

logger = logging.getLogger(__name__)

OPERATOR_SELECTOR_LABEL = "control-plane=controller-manager"


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
    """Kill operator during cluster provisioning, verify cluster reaches Ready."""
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
        wait_for_cluster_progressing(k8s=k8s_hub_client, name=co_name)
        logger.info("Cluster %s progressing, killing operator", co_name)

        k8s_hub_client.rollout_restart(deployment=operator_deploy, namespace=namespace)
        k8s_hub_client.wait_for_rollout(deployment=operator_deploy, namespace=namespace)
        logger.info("Operator pod recovered")

        wait_for_cluster_ready(k8s=k8s_hub_client, name=co_name)
        logger.info("Cluster %s reached Ready after operator restart", co_name)

        cli.delete_cluster(uuid=uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_cluster(uuid=uuid)

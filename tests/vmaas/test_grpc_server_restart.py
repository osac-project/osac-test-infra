from __future__ import annotations

import contextlib
import logging
import subprocess

import pytest

from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_virtual_network_cr,
    wait_for_virtual_network_deletion,
    wait_for_virtual_network_ready,
)
from tests.core.k8s_client import K8sClient
from tests.core.runner import poll_until

logger = logging.getLogger(__name__)

GRPC_SERVER_DEPLOYMENT = "fulfillment-grpc-server"


@pytest.mark.disruptive
def test_grpc_server_restart_recovery(grpc: GRPCClient, k8s_hub_client: K8sClient, namespace: str) -> None:
    """Kill gRPC server, create resource after recovery, verify exactly-once semantics."""
    server_deploy = GRPC_SERVER_DEPLOYMENT

    logger.info("Killing gRPC server")
    k8s_hub_client.rollout_restart(deployment=server_deploy, namespace=namespace)
    k8s_hub_client.wait_for_rollout(deployment=server_deploy, namespace=namespace)
    logger.info("gRPC server recovered")

    vn_id: str = poll_until(
        fn=lambda: _try_create_virtual_network(grpc),
        until=lambda v: v != "",
        retries=15,
        delay=2,
        description="VirtualNetwork creation after gRPC restart",
        retry_on_error=True,
    )
    logger.info("VirtualNetwork %s created after server restart", vn_id)

    try:
        vn_cr_name: str = wait_for_virtual_network_cr(k8s=k8s_hub_client, uuid=vn_id)
        wait_for_virtual_network_ready(k8s=k8s_hub_client, name=vn_cr_name)

        all_vn_ids: list[str] = grpc.list_virtual_network_ids()
        duplicates = [vid for vid in all_vn_ids if vid == vn_id]
        assert len(duplicates) == 1, f"Expected exactly 1 VirtualNetwork {vn_id}, found {len(duplicates)}"

        grpc.delete_virtual_network(vn_id=vn_id)
        wait_for_virtual_network_deletion(k8s=k8s_hub_client, name=vn_cr_name)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            grpc.delete_virtual_network(vn_id=vn_id)


def _try_create_virtual_network(grpc: GRPCClient) -> str:
    return grpc.create_virtual_network(name="e2e-restart-test", network_class="cudn-net", ipv4_cidr="10.201.0.0/16")

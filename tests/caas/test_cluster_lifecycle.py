from __future__ import annotations

import subprocess

from tests.fulfillment_cli import FulfillmentCLI
from tests.grpc_client import GRPCClient
from tests.runner import poll_until


def test_cluster_template_available(grpc: GRPCClient, cluster_template: str) -> None:
    """Verify that the expected cluster template is published and available."""
    template_ids: list[str] = grpc.list_cluster_template_ids()
    assert cluster_template in template_ids, (
        f"Cluster template '{cluster_template}' not found. Available: {template_ids}"
    )


def test_cluster_lifecycle(cli: FulfillmentCLI, grpc: GRPCClient, cluster_template: str) -> None:
    """Test full cluster lifecycle: create -> verify -> wait ready -> delete -> verify deleted."""
    # Create cluster
    uuid: str = cli.create_cluster(template=cluster_template)
    assert uuid in grpc.list_cluster_ids()

    # Wait for cluster to reach READY state
    def get_cluster_state() -> str:
        try:
            cluster = grpc.get_cluster(cluster_id=uuid)
            return cluster.get("status", {}).get("state", "")
        except subprocess.CalledProcessError:
            return ""

    poll_until(
        fn=get_cluster_state,
        until=lambda state: state in ("CLUSTER_STATE_READY", "CLUSTER_STATE_FAILED"),
        retries=120,
        delay=10,
        description=f"cluster {uuid} to reach READY or FAILED",
    )

    cluster = grpc.get_cluster(cluster_id=uuid)
    state = cluster.get("status", {}).get("state", "")
    assert state == "CLUSTER_STATE_READY", f"Cluster ended in state {state}, expected READY"

    # Verify cluster has API URL and console URL when ready
    assert cluster.get("status", {}).get("apiUrl", "") != "", "Expected api_url to be set on READY cluster"
    assert cluster.get("status", {}).get("consoleUrl", "") != "", "Expected console_url to be set on READY cluster"

    # Delete cluster
    cli.delete_cluster(uuid=uuid)

    # Wait for cluster to be fully deleted
    poll_until(
        fn=lambda: uuid not in grpc.list_cluster_ids(),
        until=lambda deleted: deleted is True,
        retries=60,
        delay=10,
        description=f"cluster {uuid} deletion",
    )

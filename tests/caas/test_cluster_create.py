from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from tests.catalog.conftest import unique_name
from tests.core.grpc_client import GRPCClient
from tests.core.helpers import (
    wait_for_cluster_deleting,
    wait_for_cluster_deletion,
    wait_for_cluster_grpc_deleting_or_archived,
    wait_for_cluster_grpc_removal,
    wait_for_cluster_order_cr,
    wait_for_cluster_ready,
)
from tests.core.k8s_client import K8sClient
from tests.core.osac_cli import OsacCLI
from tests.core.runner import poll_until, run

# Real OCP release image used to provision the ClusterVersion(s) created by these
# tests. Fixed (not randomly generated) so repeated runs reuse the same
# ClusterVersion via GRPCClient.ensure_cluster_version instead of accumulating
# duplicates on the shared cluster.
TEST_RELEASE_IMAGE = "quay.io/openshift-release-dev/ocp-release:4.20.0-multi"


def test_cluster_create(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    cluster_template: str,
    pull_secret_path: str,
    ssh_public_key_path: str,
) -> None:
    name = unique_name("e2e-cluster")
    uuid = cli.create_cluster(
        name=name,
        template=cluster_template,
        template_parameter_files={"pull_secret": pull_secret_path},
        template_parameters={"ssh_public_key": Path(ssh_public_key_path).read_text().strip()},
    )

    try:
        co_name = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=uuid)
        assert uuid in grpc.list_cluster_ids()

        wait_for_cluster_ready(k8s=k8s_hub_client, name=co_name)

        cli.delete_cluster(uuid=uuid)

        wait_for_cluster_deleting(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_deleting_or_archived(grpc=grpc, uuid=uuid)

        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_cluster(uuid=uuid)


def test_cluster_create_with_version(
    cli: OsacCLI,
    grpc: GRPCClient,
    private_grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    cluster_template: str,
    pull_secret_path: str,
    ssh_public_key_path: str,
) -> None:
    """Verify that an explicit --version resolves end-to-end: the Cluster API
    resource stores versionName, the ClusterOrder CR's releaseImage is resolved
    from the matching ClusterVersion, and the provisioned HostedCluster uses
    that release image."""
    version = private_grpc.ensure_cluster_version(version="4.20.0-e2e", image=TEST_RELEASE_IMAGE)

    name = unique_name("e2e-cluster-version")
    uuid = cli.create_cluster(
        name=name,
        template=cluster_template,
        version=version["name"],
        template_parameter_files={"pull_secret": pull_secret_path},
        template_parameters={"ssh_public_key": Path(ssh_public_key_path).read_text().strip()},
    )

    try:
        co_name = wait_for_cluster_order_cr(k8s=k8s_hub_client, uuid=uuid)

        cluster = grpc.get_cluster(cluster_id=uuid)
        assert cluster["object"]["spec"]["versionName"] == version["name"]

        release_image = poll_until(
            fn=lambda: k8s_hub_client.get_cluster_order_spec(name=co_name).get("releaseImage", ""),
            until=lambda v: v != "",
            retries=30,
            delay=5,
            description=f"{co_name} ClusterOrder releaseImage resolution",
        )
        assert release_image == TEST_RELEASE_IMAGE

        wait_for_cluster_ready(k8s=k8s_hub_client, name=co_name)

        hosted_cluster_name = k8s_hub_client.get_cluster_order_hosted_cluster_name(name=co_name)
        hosted_cluster_ns = k8s_hub_client.get_cluster_order_namespace(name=co_name)
        hosted_cluster_image = run(
            *k8s_hub_client._base(),
            "get",
            "hostedcluster",
            hosted_cluster_name,
            "-n",
            hosted_cluster_ns,
            "-o",
            "jsonpath={.spec.release.image}",
        )
        assert hosted_cluster_image == TEST_RELEASE_IMAGE

        cli.delete_cluster(uuid=uuid)

        wait_for_cluster_deleting(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_deleting_or_archived(grpc=grpc, uuid=uuid)
        wait_for_cluster_deletion(k8s=k8s_hub_client, name=co_name)
        wait_for_cluster_grpc_removal(grpc=grpc, uuid=uuid)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_cluster(uuid=uuid)


def test_cluster_create_rejected_for_invalid_version(
    grpc: GRPCClient, private_grpc: GRPCClient, cluster_template: str
) -> None:
    """Verify cluster creation is rejected for disabled, obsolete, and
    non-existent versions."""
    disabled = private_grpc.ensure_cluster_version(version="4.20.0-e2e-disabled", image=TEST_RELEASE_IMAGE)
    private_grpc.update_cluster_version(version_id=disabled["id"], enabled=False)

    obsolete = private_grpc.ensure_cluster_version(version="4.20.0-e2e-obsolete", image=TEST_RELEASE_IMAGE)
    private_grpc.update_cluster_version(version_id=obsolete["id"], state="CLUSTER_VERSION_STATE_OBSOLETE")

    def _create_with_version(version_name: str) -> tuple[str, int]:
        return grpc.call_unchecked(
            service="osac.public.v1.Clusters/Create",
            data={"object": {"spec": {"template": {"name": cluster_template}, "version_name": version_name}}},
        )

    output, rc = _create_with_version(disabled["name"])
    assert rc != 0, f"Expected create to reject disabled version, got: {output}"
    assert "disabled" in output.lower(), f"Expected 'disabled' in rejection, got: {output}"

    output, rc = _create_with_version(obsolete["name"])
    assert rc != 0, f"Expected create to reject obsolete version, got: {output}"
    assert "obsolete" in output.lower(), f"Expected 'obsolete' in rejection, got: {output}"

    output, rc = _create_with_version("4-20-0-e2e-does-not-exist")
    assert rc != 0, f"Expected create to reject non-existent version, got: {output}"
    assert "not found" in output.lower(), f"Expected 'not found' in rejection, got: {output}"

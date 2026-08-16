from __future__ import annotations

import contextlib
import logging
import subprocess

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
from tests.core.osac_cli import OsacCLI
from tests.core.runner import poll_until

logger = logging.getLogger(__name__)

OPERATOR_SELECTOR_LABEL = "control-plane=controller-manager"


@pytest.mark.disruptive
def test_operator_mid_provision_restart(
    cli: OsacCLI,
    grpc: GRPCClient,
    k8s_hub_client: K8sClient,
    k8s_virt_client: K8sClient,
    namespace: str,
    vm_template: str,
    default_subnet: str,
) -> None:
    """Kill operator while VM is provisioning, verify it resumes and reaches Running."""
    operator_deploy = k8s_hub_client.get_deployment_name_by_label(label=OPERATOR_SELECTOR_LABEL, namespace=namespace)

    name = unique_name("e2e-ci")
    uuid: str = cli.create_compute_instance(
        name=name, template=vm_template, network_attachments=[{"subnet": default_subnet}]
    )

    try:
        ci_name: str = wait_for_cr(k8s=k8s_hub_client, uuid=uuid)

        poll_until(
            fn=lambda: k8s_hub_client.get_compute_instance_phase(name=ci_name, checked=False),
            until=lambda v: v in ("Starting", "Running"),
            retries=60,
            delay=2,
            description=f"{ci_name} active phase",
        )
        logger.info("VM %s in active phase, killing operator", ci_name)

        k8s_hub_client.rollout_restart(deployment=operator_deploy, namespace=namespace)
        k8s_hub_client.wait_for_rollout(deployment=operator_deploy, namespace=namespace)
        logger.info("Operator pod recovered")

        wait_for_provision(k8s=k8s_hub_client, name=ci_name)
        wait_for_running(k8s=k8s_hub_client, name=ci_name)
        logger.info("VM %s reached Running after operator restart", ci_name)

        orphan_count: int = k8s_virt_client.count_by_label_all_namespaces(
            resource="virtualmachine", label=f"osac.openshift.io/computeinstance={ci_name}"
        )
        assert orphan_count <= 1, f"Found {orphan_count} VMs for {ci_name}, expected at most 1"

        cli.delete_compute_instance(uuid=uuid)
        wait_for_deletion(k8s=k8s_hub_client, name=ci_name)
        wait_for_grpc_removal(grpc=grpc, uuid=uuid)
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            cli.delete_compute_instance(uuid=uuid)

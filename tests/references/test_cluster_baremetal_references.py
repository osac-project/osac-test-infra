from __future__ import annotations

import logging
import subprocess
from typing import Any
from uuid import uuid4

import pytest

from tests.core.grpc_client import PRIVATE_API, PUBLIC_API, GRPCClient
from tests.core.helpers import assert_grpc_field_violation
from tests.core.osac_cli import OsacCLI
from tests.core.runner import env

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def cluster_template(private_grpc: GRPCClient) -> str:
    configured = env("OSAC_CLUSTER_TEMPLATE", "")
    if configured:
        return configured
    response: dict[str, Any] = private_grpc.call(service=f"{PRIVATE_API}.ClusterTemplates/List")
    items = response.get("items", [])
    assert items, "No ClusterTemplates found; set OSAC_CLUSTER_TEMPLATE or deploy a template"
    return items[0]["metadata"]["name"]


@pytest.fixture(scope="module")
def bmi_template(private_grpc: GRPCClient) -> str:
    configured = env("OSAC_BMI_TEMPLATE", "")
    if configured:
        return configured
    response: dict[str, Any] = private_grpc.call(service=f"{PRIVATE_API}.BareMetalInstanceTemplates/List")
    items = response.get("items", [])
    assert items, "No BareMetalInstanceTemplates found; set OSAC_BMI_TEMPLATE or deploy a template"
    return items[0]["metadata"]["name"]


class TestClusterBareMetalReferences:
    """OSAC-3110: Cluster and bare metal resource reference tests."""

    def test_cluster_provisioning_chain_by_name(
        self, private_grpc: GRPCClient, grpc: GRPCClient, cli: OsacCLI, cluster_template: str
    ):
        tag = uuid4().hex[:8]
        cat_name = f"ref-cl-cat-{tag}"

        cat_id = private_grpc.create_cluster_catalog_item(name=cat_name, template=cluster_template)
        cluster_id: str | None = None
        try:
            cat_response = grpc.get_cluster_catalog_item(catalog_item_id=cat_id)
            tmpl_ref = cat_response["object"]["template"]
            assert tmpl_ref.get("name") == cluster_template
            assert tmpl_ref.get("id"), "template.id should be auto-populated in catalog item"

            cluster_id = cli.create_cluster_with_catalog_item(
                catalog_item=cat_name, name=f"ref-cl-{tag}"
            )
            cluster = grpc.get_cluster(cluster_id=cluster_id)
            spec = cluster["object"]["spec"]
            cat_ref = spec.get("catalog_item", spec.get("catalogItem", {}))
            assert cat_ref.get("name") == cat_name
            assert cat_ref.get("id") == cat_id
        finally:
            if cluster_id:
                try:
                    cli.delete_cluster(uuid=cluster_id)
                except subprocess.CalledProcessError:
                    logger.warning("Failed to cleanup cluster %s", cluster_id)
            try:
                private_grpc.delete_cluster_catalog_item(catalog_item_id=cat_id)
            except subprocess.CalledProcessError:
                logger.warning("Failed to cleanup cluster catalog item %s", cat_id)

    def test_baremetal_instance_chain_by_name(self, private_grpc: GRPCClient, grpc: GRPCClient, bmi_template: str):
        tag = uuid4().hex[:8]
        cat_name = f"ref-bmi-cat-{tag}"

        cat_id = private_grpc.create_baremetal_instance_catalog_item(
            name=cat_name, title=cat_name, description="Reference test", template=bmi_template
        )
        bmi_id: str | None = None
        try:
            cat_response: dict[str, Any] = private_grpc.call(
                service=f"{PRIVATE_API}.BareMetalInstanceCatalogItems/Get", data={"id": cat_id}
            )
            tmpl_ref = cat_response["object"]["template"]
            assert tmpl_ref.get("name") == bmi_template
            assert tmpl_ref.get("id"), "template.id should be auto-populated in BMI catalog item"

            bmi_response: dict[str, Any] = grpc.call(
                service=f"{PUBLIC_API}.BareMetalInstances/Create",
                data={"object": {"metadata": {"name": f"ref-bmi-{tag}"}, "spec": {"catalog_item": {"name": cat_name}}}},
            )
            bmi_id = bmi_response["object"]["id"]
            spec = bmi_response["object"]["spec"]
            cat_ref = spec.get("catalog_item", spec.get("catalogItem", {}))
            assert cat_ref.get("name") == cat_name
            assert cat_ref.get("id") == cat_id
        finally:
            if bmi_id:
                try:
                    grpc.delete_baremetal_instance(bmi_id=bmi_id)
                except subprocess.CalledProcessError:
                    logger.warning("Failed to cleanup BMI %s", bmi_id)
            try:
                private_grpc.delete_baremetal_instance_catalog_item(item_id=cat_id)
            except subprocess.CalledProcessError:
                logger.warning("Failed to cleanup BMI catalog item %s", cat_id)

    def test_cross_tenant_cluster_template_reference(
        self, private_grpc: GRPCClient, jwt_grpc_tenant1: GRPCClient, jwt_cli_user: OsacCLI, cluster_template: str
    ):
        tag = uuid4().hex[:8]
        cat_name = f"ref-xt-cl-cat-{tag}"

        cat_id = private_grpc.create_cluster_catalog_item(name=cat_name, template=cluster_template)
        cluster_id: str | None = None
        try:
            cluster_id = jwt_cli_user.create_cluster_with_catalog_item(
                catalog_item=cat_name, name=f"ref-xt-cl-{tag}"
            )
            cluster = jwt_grpc_tenant1.get_cluster(cluster_id=cluster_id)
            spec = cluster["object"]["spec"]
            cat_ref = spec.get("catalog_item", spec.get("catalogItem", {}))
            assert cat_ref.get("name") == cat_name
            assert cat_ref.get("id") == cat_id
        finally:
            if cluster_id:
                try:
                    jwt_cli_user.delete_cluster(uuid=cluster_id)
                except subprocess.CalledProcessError:
                    logger.warning("Failed to cleanup cross-tenant cluster %s", cluster_id)
            try:
                private_grpc.delete_cluster_catalog_item(catalog_item_id=cat_id)
            except subprocess.CalledProcessError:
                logger.warning("Failed to cleanup cross-tenant catalog item %s", cat_id)

    def test_invalid_cluster_template_name_returns_error(self, private_grpc: GRPCClient):
        tag = uuid4().hex[:8]
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            private_grpc.create_cluster_catalog_item(name=f"ref-bad-cat-{tag}", template="nonexistent-template")
        assert_grpc_field_violation(exc_info, field_path="template")

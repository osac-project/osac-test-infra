from __future__ import annotations

import logging
import subprocess
from typing import Any
from uuid import uuid4

import pytest

from tests.core.grpc_client import PUBLIC_API, GRPCClient
from tests.core.helpers import assert_grpc_field_violation

logger = logging.getLogger(__name__)

TENANT_ADMIN_USER = "tenant1_admin"
TENANT_USER = "tenant1_user"
TENANT_ADMIN_ROLE = "tenant-admin"


class TestIAMReferences:
    """OSAC-3114: IAM resource reference tests."""

    def test_role_binding_with_role_and_users_by_name(self, jwt_grpc_tenant1: GRPCClient):
        tag = uuid4().hex[:8]
        rb_name = f"ref-rb-{tag}"

        rb_id = jwt_grpc_tenant1.create_role_binding(
            name=rb_name, role_name=TENANT_ADMIN_ROLE, user_names=[TENANT_ADMIN_USER]
        )
        try:
            response = jwt_grpc_tenant1.get_role_binding(role_binding_id=rb_id)
            spec = response["object"]["spec"]

            role_ref = spec["role"]
            assert role_ref.get("name") == TENANT_ADMIN_ROLE
            assert role_ref.get("id"), "role.id should be auto-populated"

            users = spec["users"]
            assert len(users) >= 1
            user_ref = users[0]
            assert user_ref.get("name") == TENANT_ADMIN_USER
            assert user_ref.get("id"), "user.id should be auto-populated"
        finally:
            try:
                jwt_grpc_tenant1.delete_role_binding(role_binding_id=rb_id)
            except subprocess.CalledProcessError:
                logger.warning("Failed to cleanup role binding %s", rb_id)

    def test_project_membership_by_name(self, jwt_grpc_tenant1: GRPCClient):
        tag = uuid4().hex[:8]
        pm_name = f"ref-pm-{tag}"

        pm_id = jwt_grpc_tenant1.create_project_membership(name=pm_name, user_names=[TENANT_USER])
        try:
            response: dict[str, Any] = jwt_grpc_tenant1.call(
                service=f"{PUBLIC_API}.ProjectMemberships/Get", data={"id": pm_id}
            )
            spec = response["object"]["spec"]

            users = spec["users"]
            assert len(users) >= 1
            user_ref = users[0]
            assert user_ref.get("name") == TENANT_USER
            assert user_ref.get("id"), "user.id should be auto-populated"
        finally:
            try:
                jwt_grpc_tenant1.delete_project_membership(membership_id=pm_id)
            except subprocess.CalledProcessError:
                logger.warning("Failed to cleanup project membership %s", pm_id)

    def test_invalid_role_name_returns_error(self, jwt_grpc_tenant1: GRPCClient):
        tag = uuid4().hex[:8]
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            jwt_grpc_tenant1.create_role_binding(
                name=f"ref-bad-rb-{tag}", role_name="nonexistent-role", user_names=[TENANT_ADMIN_USER]
            )
        assert_grpc_field_violation(exc_info, field_path="role")

    def test_role_binding_with_multiple_users_by_name(self, jwt_grpc_tenant1: GRPCClient):
        tag = uuid4().hex[:8]
        rb_name = f"ref-rb-multi-{tag}"

        rb_id = jwt_grpc_tenant1.create_role_binding(
            name=rb_name, role_name=TENANT_ADMIN_ROLE, user_names=[TENANT_ADMIN_USER, TENANT_USER]
        )
        try:
            response = jwt_grpc_tenant1.get_role_binding(role_binding_id=rb_id)
            users = response["object"]["spec"]["users"]
            resolved_names = {u.get("name") for u in users}
            assert TENANT_ADMIN_USER in resolved_names
            assert TENANT_USER in resolved_names
            for u in users:
                assert u.get("id"), f"user.id should be auto-populated for {u.get('name')}"
        finally:
            try:
                jwt_grpc_tenant1.delete_role_binding(role_binding_id=rb_id)
            except subprocess.CalledProcessError:
                logger.warning("Failed to cleanup role binding %s", rb_id)

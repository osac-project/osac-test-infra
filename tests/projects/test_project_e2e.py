from __future__ import annotations

import subprocess

import pytest

from tests.core.helpers import assert_grpc_rejected
from tests.core.keycloak_admin import check_project_not_in_keycloak, get_admin_token, wait_for_project_in_keycloak


def test_project_full_lifecycle(
    jwt_grpc_tenant1_admin,
    jwt_grpc_tenant1,
    jwt_grpc_tenant2,
    keycloak_url,
    keycloak_admin_password,
    skip_keycloak_sync_checks,
):
    """
    Full E2E test scenario for Projects:

    1. Create a project as tenant1_admin
    2. Observe the project gets created in Keycloak as a group
    3. Create a ProjectMembership to assign tenant1_user as viewer
    4. Get the project as tenant1_user (has access via membership)
    5. Create a sub-project under the parent project
    6. Get both projects as tenant1_user
    7. Attempt to get projects as tenant2_user (different tenant, should fail)
    8. Attempt to delete parent project (should fail due to child project)
    9. Delete the child project, verify removal from gRPC and Keycloak
    10. Delete the parent project, verify removal from gRPC and Keycloak
    """

    # Get admin token and org ID for Keycloak checks (if needed)
    admin_token = None
    org_id = None
    if not skip_keycloak_sync_checks:
        admin_token = get_admin_token(keycloak_url=keycloak_url, username="admin", password=keycloak_admin_password)
        from tests.core.keycloak_admin import wait_for_organization

        org_id = wait_for_organization(keycloak_url=keycloak_url, admin_token=admin_token, org_name="tenant1")

    # 1. Create a project as tenant1_admin
    parent_project_id = jwt_grpc_tenant1_admin.create_project(name="parent-project")
    assert parent_project_id != "", "Parent project ID should not be empty"
    assert parent_project_id in jwt_grpc_tenant1_admin.list_project_ids()

    # 2. Observe the project gets created in Keycloak as a group
    parent_group_id = None
    if not skip_keycloak_sync_checks:
        parent_group_id = wait_for_project_in_keycloak(
            keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name="parent-project"
        )
        assert parent_group_id != "", "Parent project should exist in Keycloak"

    # 3. Create a ProjectMembership to assign tenant1_user as viewer
    # The tenant1_user should now be able to view the project
    membership_id = jwt_grpc_tenant1_admin.create_project_membership(
        name="parent-project-viewer", user_names=["tenant1-user"], role="PROJECT_MEMBERSHIP_ROLE_VIEWER"
    )
    assert membership_id != "", "ProjectMembership ID should not be empty"

    # 4. Get the project as tenant1_user (has access via membership)
    project_response = jwt_grpc_tenant1.get_project(project_id=parent_project_id)
    assert project_response["object"]["id"] == parent_project_id
    assert project_response["object"]["metadata"]["name"] == "parent-project"

    # 5. Create a sub-project under the parent project
    child_project_id = jwt_grpc_tenant1_admin.create_project(name="child-project", parent_project_id=parent_project_id)
    assert child_project_id != "", "Child project ID should not be empty"
    assert child_project_id in jwt_grpc_tenant1_admin.list_project_ids()

    # Observe child project in Keycloak
    child_group_id = None
    if not skip_keycloak_sync_checks:
        child_group_id = wait_for_project_in_keycloak(
            keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name="child-project"
        )
        assert child_group_id != "", "Child project should exist in Keycloak"

    # 6. Get both projects as tenant1_user
    parent_check = jwt_grpc_tenant1.get_project(project_id=parent_project_id)
    assert parent_check["object"]["id"] == parent_project_id

    child_check = jwt_grpc_tenant1.get_project(project_id=child_project_id)
    assert child_check["object"]["id"] == child_project_id
    # Verify parent-child relationship
    assert child_check["object"]["spec"]["parent_project"]["id"] == parent_project_id

    # 7. Attempt to get projects as tenant2_user (different tenant, should fail)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        jwt_grpc_tenant2.get_project(project_id=parent_project_id)
    assert_grpc_rejected(exc_info, "NotFound")

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        jwt_grpc_tenant2.get_project(project_id=child_project_id)
    assert_grpc_rejected(exc_info, "NotFound")

    # 8. Attempt to delete parent project (should fail due to child project)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        jwt_grpc_tenant1_admin.delete_project(project_id=parent_project_id)
    # The error should indicate that the project has child projects
    assert_grpc_rejected(exc_info, "FailedPrecondition")

    # Verify parent still exists
    assert parent_project_id in jwt_grpc_tenant1_admin.list_project_ids()

    # 9. Delete the child project, verify removal from gRPC and Keycloak
    jwt_grpc_tenant1_admin.delete_project(project_id=child_project_id)

    # Verify child removed from gRPC
    assert child_project_id not in jwt_grpc_tenant1_admin.list_project_ids()

    # Verify child removed from Keycloak
    if not skip_keycloak_sync_checks:
        assert check_project_not_in_keycloak(
            keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name="child-project"
        ), "Child project should be removed from Keycloak"

    # 10. Delete the parent project, verify removal from gRPC and Keycloak
    jwt_grpc_tenant1_admin.delete_project(project_id=parent_project_id)

    # Verify parent removed from gRPC
    assert parent_project_id not in jwt_grpc_tenant1_admin.list_project_ids()

    # Verify parent removed from Keycloak
    if not skip_keycloak_sync_checks:
        assert check_project_not_in_keycloak(
            keycloak_url=keycloak_url, admin_token=admin_token, org_id=org_id, project_name="parent-project"
        ), "Parent project should be removed from Keycloak"

    # Clean up the ProjectMembership
    jwt_grpc_tenant1_admin.delete_project_membership(membership_id=membership_id)

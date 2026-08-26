# Projects E2E Tests

This directory contains end-to-end tests for the Projects API.

## Test Coverage

### test_project_e2e.py

Full lifecycle test that validates:

1. **Project Creation**: Tenant admin creates a parent project
2. **Keycloak Synchronization**: Project appears as a group in Keycloak organization
3. **Project Membership**: Assign viewer role to tenant user via ProjectMembership
4. **Access Control**: Verify tenant user can view project, non-member cannot
5. **Sub-projects**: Create child project under parent project
6. **Parent-Child Relationship**: Verify hierarchy is maintained
7. **Tenant Isolation**: Verify users from other tenants cannot access projects
8. **Deletion Constraints**: Parent project with children cannot be deleted
9. **Cascade Cleanup**: Child deletion removes from both gRPC and Keycloak
10. **Complete Cleanup**: Parent deletion after children are removed

## Running Tests

```bash
# Run all projects tests
make test-projects

# Run specific test
TEST=test_project_full_lifecycle make test-projects
```

## Dependencies

- **Keycloak**: Must be accessible for organization/group verification
- **Multiple Tenants**: tenant1 and tenant2 must exist
- **JWT Authentication**: tenant1_admin, tenant1_user, tenant2_user must be configured
- **Environment Variables**:
  - `OSAC_KEYCLOAK_URL`: Keycloak base URL
  - `OSAC_KEYCLOAK_ADMIN_PASSWORD`: Keycloak admin password (default: "admin")
  - `OSAC_JWT_PASSWORD`: Password for JWT test users (default: "foobar")
  - `OSAC_SKIP_KEYCLOAK_SYNC`: Set to "true" to skip Keycloak sync verification (default: "false"). Useful when the project sync feature isn't deployed yet.

## Architecture Notes

Projects are **NOT** Kubernetes custom resources. They:
- Live only in the fulfillment service database
- Are accessed exclusively via the gRPC API
- Sync to Keycloak as organization groups for RBAC
- Support hierarchical parent-child relationships
- Enforce deletion constraints (cannot delete parent with children)

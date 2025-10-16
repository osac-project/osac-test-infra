# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository contains Ansible-based test infrastructure for CloudKit end-to-end testing. It focuses on testing hub creation and VM lifecycle management through the CloudKit fulfillment service using gRPC APIs and the fulfillment-cli tool.

## Architecture

### Directory Structure
- `playbooks/` - Main Ansible playbooks for executing tests
- `roles/` - Reusable Ansible roles for test functionality
  - `test_vm_creation/` - Role for VM creation, verification, and cleanup
  - `fulfillment_cli_base/` - Base role for fulfillment CLI operations (dependency)
- `retry/` - Ansible retry files for failed playbook runs

### Core Components

**Hub Creation Testing**
- Hub creation and registration using fulfillment-cli
- Hub verification through gRPC API calls
- Automated cleanup of hub resources and temporary files

**VM Lifecycle Testing**
- VM creation using fulfillment-cli with CloudKit templates
- gRPC-based verification through the fulfillment service
- Status monitoring and readiness checks
- Automated cleanup and deletion

**Communication Methods**
- `fulfillment-cli` for VM creation operations
- `grpcurl` for direct API communication with fulfillment service
- gRPC authentication using Bearer tokens

## Common Development Tasks

### Running Tests

Execute hub creation test:
```bash
ansible-playbook playbooks/test_hub_creation.yml -e test_hub_id=my-test-hub-001 -e test_namespace=foobar
```

Execute VM creation test:
```bash
ansible-playbook playbooks/test_vm_creation.yml -e test_vm_id=my-test-vm-001 -e test_namespace=foobar
```

### Test Tags

Use tags to run specific parts of tests:
- `info` - Display test information and resource listings
- `test` - Run actual creation tests (hub_creation, vm_creation)
- `validation` - Resource verification and status checks
- `cleanup` - Resource deletion and file cleanup
- `mrclean` - Mass cleanup of test VMs with e2e-test-vm- prefix

Examples:
```bash
# Run only hub creation without cleanup
ansible-playbook playbooks/test_hub_creation.yml --tags test

# Run only VM creation without cleanup
ansible-playbook playbooks/test_vm_creation.yml --tags test

# Run only cleanup operations
ansible-playbook playbooks/test_vm_creation.yml --tags cleanup

# Mass cleanup of all test VMs
ansible-playbook playbooks/test_vm_creation.yml --tags mrclean
```

### Key Variables

Required variables that must be set:
- `test_hub_id` - Unique identifier for the hub being tested (hub tests)
- `test_vm_id` - Unique identifier for the VM being tested (VM tests)
- `test_namespace` - Namespace for the test environment (use: foobar)
- `grpc_token_result.stdout` - Authentication token for gRPC calls
- `fulfillment_address` - gRPC endpoint for fulfillment service
- `fulfillment_cli.binary_path` - Path to fulfillment-cli binary (default: ./fulfillment-cli)
- `testing_workspace` - Directory for temporary test files

## VM Template Configuration

Default VM template: `cloudkit.templates.ocp_virt_vm`
- CPU: 2 cores
- Memory: 4GB
- Disk: 20GB
- Network: default
- Ready timeout: 15 minutes

## gRPC API Operations

The test infrastructure uses these gRPC endpoints:
- `private.v1.VirtualMachines/List` - List all VMs
- `private.v1.VirtualMachines/Get` - Get specific VM details
- `private.v1.VirtualMachines/Delete` - Delete a VM

All gRPC calls use insecure connections and require Bearer token authentication.

## Test Execution Flow

1. **Setup**: Display test information and parameters
2. **Creation**: Create VM using fulfillment-cli with specified template
3. **Verification**: Verify VM registration via gRPC API
4. **Monitoring**: Wait for VM to reach "Running" status
5. **Cleanup**: Delete VM and remove temporary files
6. **Logging**: Record test results to test-execution.log

## Error Handling

- Failed tests trigger automatic cleanup of temporary files
- VM deletion verification ensures proper cleanup
- All operations include proper error logging and failure messages
- Retry functionality available through Ansible retry files

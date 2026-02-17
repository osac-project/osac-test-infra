# ComputeInstance API Fields Test Role

## Overview

This role tests the new explicit API fields introduced in MGMT-23103 for ComputeInstance resources.

## Tests Performed

### 1. runStrategy Mutability Tests
- **Test**: Change runStrategy from `Always` to `Halted`
  - Verifies VirtualMachine stops (`spec.running=false`, `status=Stopped`)
  - Verifies VirtualMachineInstance is terminated

- **Test**: Change runStrategy from `Halted` to `Always`
  - Verifies VirtualMachine starts (`spec.running=true`, `status=Running`)
  - Verifies new VirtualMachineInstance is created and running

### 2. Immutability Validation Tests
- **Test**: Attempt to change `cores` field
  - Verifies patch is rejected with "cores is immutable" error

- **Test**: Attempt to change `memoryGiB` field
  - Verifies patch is rejected with "memoryGiB is immutable" error

- **Test**: Attempt to change `image.sourceRef` field
  - Verifies patch is rejected with "image is immutable" error

## Prerequisites

- ComputeInstance must already exist and be in Running state
- Variable `compute_instance_order_uuid` must be set
- Variable `test_namespace` must be set

## Usage

### As part of a playbook

```yaml
- name: Run API fields test
  ansible.builtin.include_role:
    name: test_compute_instance_api_fields
  vars:
    compute_instance_order_uuid: "{{ uuid }}"
    test_namespace: "{{ namespace }}"
```

### Using the dedicated playbook

```bash
ansible-playbook playbooks/test_compute_instance_api_fields.yml \
  -e compute_instance_order_uuid=<UUID> \
  -e test_namespace=vmaas-dev
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `status_poll_interval` | 10 | Seconds between status checks |
| `vm_state_change_timeout` | 120 | Maximum seconds to wait for VM state changes |

## Expected Outcomes

All tests should PASS:
- ✓ VM stops when runStrategy=Halted
- ✓ VM starts when runStrategy=Always
- ✓ Immutable fields reject patch attempts

## Related

- Issue: https://issues.redhat.com/browse/MGMT-23103
- Enhancement Proposal: https://github.com/osac-project/enhancement-proposals/pull/21
- Pull Request: https://github.com/osac-project/osac-operator/pull/111

# Parallel Test Safety Guide

This guide applies to all tests under `tests/vmaas/`. The CI runs VMaaS tests
in two phases using `make test-vmaas-parallel`:

1. `pytest -n 3 -m "not serial"` — up to 3 tests run simultaneously
2. `pytest -m "serial"` — serial-only tests run alone after phase 1 completes

Every new test must be classified before merging.

---

## Test Classification

### Group A — Parallel, no changes needed

The test already uses unique resource IDs and shares no global state. It can
run alongside any other test immediately.

Examples: `test_compute_instance_creation`, `test_compute_instance_restart`

### Group B — Parallel after a targeted fix

The test is logically independent but uses a hardcoded value (name or CIDR)
that would collide if two workers ran it simultaneously. A small fix makes it
parallel-safe.

Examples: `test_virtual_network_lifecycle`, `test_compute_instance_api_fields`

### Group C — Serial only

The test depends on a timing or mid-state condition that requires the system
to be otherwise idle — for example, catching a provision job in flight before
AAP completes it. Add `@pytest.mark.serial` and it will run alone in phase 2.

Examples: `test_compute_instance_delete_during_provision`

---

## Rules for Writing Parallel-Safe Tests

### 1. Name every resource with a unique ID

Always use `uuid4().hex[:8]` for resource names. Never use `int(time.time())`
alone — two workers can start within the same second and produce identical names.

If you need the timestamp for log correlation, append a short random suffix:

```python
from uuid import uuid4
import time

name = f"my-resource-{int(time.time())}-{uuid4().hex[:4]}"
```

### 2. Never hardcode network CIDRs

Hardcoded CIDRs like `10.100.0.0/16` conflict when two workers request the
same range from the same CNI backend simultaneously. Use the helpers from
`tests/core/parallel.py`:

```python
from tests.core.parallel import virtual_network_cidr, subnet_cidr

# For a standalone virtual network test:
cidr = virtual_network_cidr()   # 10.100.0.0/16, 10.101.0.0/16, 10.102.0.0/16 per worker

# For a subnet test (parent vnet + child subnet):
vnet_cidr, sub_cidr = subnet_cidr()
```

If you need a different range, add a new helper to `parallel.py` following the
same pattern — derive the octet from `xdist_worker_index()`.

### 3. Do not share CLI config paths

`osac login` writes to a config file on disk. Per-worker isolation is handled
automatically by the `isolate_osac_config` autouse fixture in `tests/conftest.py`.
Do not set `XDG_CONFIG_HOME` manually in test files or fixtures.

### 4. Do not assume an idle AAP queue

Tests that provision VMs share the same AAP instance. A test must not assert
on the state of another test's provision job, and must not assume it is the
only job running unless it is marked serial.

### 5. Mark serial tests explicitly

If the test must be the only active workload:

```python
import pytest

@pytest.mark.serial
def test_my_timing_sensitive_test(...) -> None:
    ...
```

Add a one-line comment above the decorator explaining why:

```python
# Requires AAP to be idle — asserts mid-provision state before job completes.
@pytest.mark.serial
def test_compute_instance_delete_during_provision(...) -> None:
    ...
```

### 6. State the group in your PR description

Every PR adding or modifying a test must include one of:

```
Parallel safety: Group A — unique IDs, no shared state
Parallel safety: Group B — fixed by <describe the fix>
Parallel safety: Group C — serial, because <describe the timing dependency>
```

---

## Classification Checklist

Before marking a test as Group A or B, confirm all of the following:

- [ ] All resource names include `uuid4().hex[:8]` or equivalent unique suffix
- [ ] No hardcoded CIDRs — uses `parallel.py` helpers or generates a unique range
- [ ] Does not read or write shared files outside `XDG_CONFIG_HOME`
- [ ] Does not assert on another resource's provisioning state
- [ ] Does not depend on being the only AAP job in the queue

If any box is unchecked → the test is Group C until the issue is resolved.

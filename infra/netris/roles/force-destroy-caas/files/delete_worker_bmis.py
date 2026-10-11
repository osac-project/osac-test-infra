#!/usr/bin/env python3
"""Delete worker BMIs and wait for API and hub cleanup before removing BMHs."""
from __future__ import annotations

import json
import os
import subprocess
import time

OSAC = ["osac", "--tenant", os.environ["OSAC_TENANT"]]
UUID_LABEL = "osac.openshift.io/baremetalinstance-uuid"
WORKER_REFS = {f"{os.environ['BMH_NAMESPACE']}/{name}" for name in json.loads(os.environ["WORKERS"])}


def load(*args: str) -> list[dict]:
    """Accept CLI arrays, Kubernetes lists, and single-object CLI responses."""
    raw = json.loads(subprocess.check_output(args, text=True, timeout=60))
    if isinstance(raw, dict):
        raw = raw["items"] if "items" in raw else ([raw] if raw else [])
    if not isinstance(raw, list):
        raise SystemExit("Unexpected JSON response; stopping before BMH cleanup.")
    return raw


def api_bmis() -> dict[str, dict]:
    return {item["id"]: item for item in load(*OSAC, "get", "baremetalinstances", "-o", "json")}


def hub_bmis() -> list[dict]:
    return load("oc", "get", "baremetalinstances", "-A", "-o", "json")


def bmi_id(item: dict) -> str:
    return ((item.get("metadata") or {}).get("labels") or {}).get(UUID_LABEL, "")


def deleting(item: dict) -> bool:
    meta = item.get("metadata") or {}
    state = ((item.get("status") or {}).get("state") or "").upper()
    return bool(meta.get("deletion_timestamp") or meta.get("deletionTimestamp") or state.endswith("_DELETING"))


# Active BMIs must map to the configured worker hosts; API UUIDs come from hub labels.
targets = set()
for item in hub_bmis():
    if (item.get("spec") or {}).get("externalHostID") in WORKER_REFS:
        if not bmi_id(item):
            raise SystemExit(f"Worker BMI {item['metadata']['name']} lacks its OSAC UUID label.")
        targets.add(bmi_id(item))

api = api_bmis()
pending = {key for key, item in api.items() if deleting(item)}
other_clusters = any(
    (item.get("metadata") or {}).get("name") != os.environ["OSAC_CLUSTER_NAME"]
    for item in load(*OSAC, "get", "clusters", "-o", "json")
)
# Retry orphaned API deletions only in the dedicated test tenant with no other clusters.
# Stop on ambiguous ownership instead of allowing BMH removal to strand more BMIs.
ambiguous = (pending if other_clusters else set(api) - pending) - targets
if ambiguous:
    raise SystemExit("Cannot associate BMI records with this cleanup; BMHs retained: " + ", ".join(sorted(ambiguous)))
if not other_clusters:
    targets.update(pending)
if not targets:
    print("No worker BMIs to delete.")
    raise SystemExit(0)

# Keep all target IDs for verification, including CRs whose API record is already gone.
for key in sorted(targets & api.keys()):
    print(f"Requesting OSAC deletion for BMI {key}", flush=True)
    result = subprocess.run(
        [*OSAC, "delete", "baremetalinstances", key], capture_output=True, text=True, timeout=60
    )
    if result.returncode and key in api_bmis():
        raise SystemExit(f"BMI {key} deletion failed: {(result.stderr or result.stdout).strip()}")

deadline = time.monotonic() + 600
next_report = 0
while True:
    api = api_bmis()
    remaining_api = targets & api.keys()
    remaining_hub = targets & {bmi_id(item) for item in hub_bmis()}
    if not remaining_api and not remaining_hub:
        print("All targeted API records and hub BMI resources are gone.")
        break
    now = time.monotonic()
    if now >= deadline:
        states = {key: (api[key].get("status") or {}).get("state") for key in sorted(remaining_api)}
        raise SystemExit(
            f"BMI cleanup timed out; BMHs and VM disks retained. "
            f"API={states}; hub BMI IDs={sorted(remaining_hub)}"
        )
    if now >= next_report:
        print(f"Waiting for BMI cleanup: API={sorted(remaining_api)}, hub={sorted(remaining_hub)}", flush=True)
        next_report = now + 60
    time.sleep(5)

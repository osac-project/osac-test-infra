#!/usr/bin/env python3
"""Reconcile BareMetalHost resources with a raw inventory file.

One command for the whole BMH lifecycle: applies the namespace, removes
the group's BMHs the inventory no longer lists — deleted with --wait=false
and stripped of their finalizers, since the backing VMs are typically
already gone; an emptied inventory removes them all — then applies one
BMC Secret and BareMetalHost per host (credentials from the inventory),
waits for 'available' and labels.

Ironic must already be active and its webhook ready (activate-ironic.sh).
Needs oc on PATH (KUBECONFIG or the default kubeconfig).

Run directly: python3 .github/scripts/reconcile-bmhs.py --help
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from raw_inventory import Host, InventoryError, RawInventory, is_group_host_name

# 300s: a hung API server must fail, not hang the job. oc's own
# --timeout flags only bound the server-side wait.
OC_TIMEOUT_SECONDS = 300

# registering -> inspecting -> available; inspection takes 3-5 minutes.
AVAILABLE_RETRIES = 120
AVAILABLE_DELAY = 10


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run_oc(
    oc_args: list[str], *, stdin: str | None = None, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["oc", *oc_args],
        input=stdin,
        text=True,
        check=check,
        capture_output=capture,
        timeout=OC_TIMEOUT_SECONDS,
    )


def dump_oc(oc_args: list[str]) -> None:
    """Best-effort diagnostic output on the failure path (stderr, like the
    bash script it replaces)."""
    result = run_oc(oc_args, check=False, capture=True)
    output = (result.stdout or result.stderr or "").strip()
    if output:
        print(output, file=sys.stderr)


def render_namespace_manifest(namespace: str) -> str:
    return f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
"""


def render_bmh_manifest(host: Host, namespace: str) -> str:
    return f"""---
apiVersion: v1
kind: Secret
metadata:
  name: {host.name}-bmc-secret
  namespace: {namespace}
type: Opaque
stringData:
  # json.dumps = quoted YAML scalar: credentials can never be parsed
  # as YAML syntax ("12345", "true", "a: b").
  username: {json.dumps(host.bmc_username)}
  password: {json.dumps(host.bmc_password)}
---
apiVersion: metal3.io/v1alpha1
kind: BareMetalHost
metadata:
  name: {host.name}
  namespace: {namespace}
spec:
  online: true
  bootMACAddress: "{host.boot_mac}"
  bootMode: UEFI
  automatedCleaningMode: metadata
  rootDeviceHints:
    deviceName: /dev/vda
  bmc:
    address: "{host.bmc_address}"
    credentialsName: {host.name}-bmc-secret
"""


def apply_namespace(namespace: str) -> None:
    """Ensure the namespace exists before BMHs are listed or created in it."""
    print(f"==> Ensuring namespace {namespace}...")
    run_oc(["apply", "-f", "-"], stdin=render_namespace_manifest(namespace))


def create_bmhs(hosts: list[Host], namespace: str) -> None:
    print(f"==> Creating BareMetalHost resources in namespace {namespace}...")
    for host in hosts:
        print(f"  {host.name}: MAC={host.boot_mac}, BMC={host.bmc_address}")
        run_oc(["apply", "-f", "-"], stdin=render_bmh_manifest(host, namespace))


def list_managed_bmh_names(namespace: str, name_prefix: str) -> list[str]:
    """Names of the BareMetalHosts this tool owns (inventory naming scheme)."""
    result = run_oc(
        ["get", "bmh", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"],
        capture=True,
    )
    return [name for name in result.stdout.split() if is_group_host_name(name, name_prefix)]


def strip_finalizers(kind: str, name: str, namespace: str) -> None:
    """Remove the finalizers holding a deletion.

    The deletion is issued with --wait=false first: a live object's
    controller re-adds its finalizer, a terminating one does not. Once
    the finalizers are gone, the API server completes the deletion.
    """
    run_oc(
        ["patch", kind, name, "-n", namespace, "--type", "merge", "-p", '{"metadata":{"finalizers":null}}'],
        check=False,
    )


def delete_stale_bmhs(hosts: list[Host], namespace: str, name_prefix: str) -> None:
    """Delete BareMetalHosts of this group that the inventory no longer lists."""
    desired_names = {host.name for host in hosts}
    stale_names = [name for name in list_managed_bmh_names(namespace, name_prefix) if name not in desired_names]
    if not stale_names:
        return
    print("==> Deleting BareMetalHosts no longer in the raw inventory...")
    for name in stale_names:
        print(f"  Deleting {name}...")
        # The backing VM may already be gone and Ironic cannot act on
        # the host: delete without waiting, then strip the finalizers.
        run_oc(["delete", "bmh", name, "-n", namespace, "--ignore-not-found", "--wait=false"])
        strip_finalizers("bmh", name, namespace)
        run_oc(["delete", "secret", f"{name}-bmc-secret", "-n", namespace, "--ignore-not-found", "--wait=false"])
        strip_finalizers("secret", f"{name}-bmc-secret", namespace)


def wait_for_available(hosts: list[Host], namespace: str) -> None:
    print("==> Waiting for BareMetalHosts to reach 'available' state...")
    for host in hosts:
        name = host.name
        print(f"  Waiting for {name}...")
        for attempt in range(1, AVAILABLE_RETRIES + 1):
            result = run_oc(
                ["get", "bmh", name, "-n", namespace, "-o", "jsonpath={.status.provisioning.state}"],
                check=False,
                capture=True,
            )
            state = result.stdout.strip() or "unknown"
            if state == "available":
                print(f"  {name} is available.")
                break
            if attempt == AVAILABLE_RETRIES:
                print(f"ERROR: {name} did not reach 'available' state (current: {state})", file=sys.stderr)
                print("BMH status:", file=sys.stderr)
                dump_oc(["get", "bmh", name, "-n", namespace, "-o", "yaml"])
                sys.exit(1)
            print(f"    attempt {attempt}/{AVAILABLE_RETRIES}: state={state}")
            time.sleep(AVAILABLE_DELAY)


def label_bmhs(hosts: list[Host], namespace: str) -> None:
    print("==> Labeling BareMetalHosts...")
    for host in hosts:
        name = host.name
        run_oc(["label", "bmh", name, "-n", namespace, "osac.openshift.io/host-type=default", "--overwrite"])
        print(f"  Labeled {name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--inventory-file", type=Path, required=True, help="raw inventory JSON to read")
    parser.add_argument(
        "--namespace", default="host-inventory", help="namespace for BMH resources (default: host-inventory)"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    try:
        if not args.inventory_file.exists():
            fail(f"raw inventory file '{args.inventory_file}' not found")
        inventory = RawInventory.load(args.inventory_file)
        hosts = inventory.hosts
        name_prefix = inventory.name_prefix
        apply_namespace(args.namespace)
        delete_stale_bmhs(hosts, args.namespace, name_prefix)
        create_bmhs(hosts, args.namespace)
        wait_for_available(hosts, args.namespace)
        label_bmhs(hosts, args.namespace)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        sys.exit(exc.returncode)
    except subprocess.TimeoutExpired as exc:
        fail(f"command timed out after {exc.timeout} seconds: {' '.join(exc.cmd)}")
    except InventoryError as exc:
        fail(str(exc))
    except FileNotFoundError as exc:
        fail(f"required command not found: {exc}")
    print(f"==> {len(hosts)} BareMetalHosts available.")


if __name__ == "__main__":
    main()

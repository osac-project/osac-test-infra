#!/usr/bin/python3
"""Repair known transient control-plane stalls during an Assisted compact boot.

The parent is the root-owned compact-boot helper. All API calls use the new
clone's kubeconfig, and every mutation is limited to that disposable clone.
"""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def fail(message):
    raise SystemExit(message)


if len(sys.argv) != 4 or os.geteuid() != 0:
    fail("Usage (as root): assisted-compact-watchdog.py CLONE SOURCE_PREFIX BOOT_PID")
clone, source_prefix, boot_pid_text = sys.argv[1:]
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?", clone):
    fail("Invalid clone ID")
if not re.fullmatch(r"test-infra-cluster-[a-f0-9]{8}", source_prefix):
    fail("Invalid Assisted source prefix")
if not boot_pid_text.isdecimal() or int(boot_pid_text) < 2:
    fail("Invalid boot process ID")
boot_pid = int(boot_pid_text)
clone_dir = Path("/var/lib/libvirt/images") / clone
kubeconfig = clone_dir / "clone.kubeconfig"
running = True
restarted_ovn = set()
cleared_mcd = set()
removed_source_controller = set()


def stop(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def boot_alive():
    try:
        os.kill(boot_pid, 0)
        return True
    except ProcessLookupError:
        return False


def oc(*args):
    command = ["/usr/local/bin/oc", "--kubeconfig", str(kubeconfig),
               "--request-timeout=8s", *args]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return None


def get_json(*args):
    result = oc("get", *args, "-o", "json")
    if result is None or result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def age_seconds(timestamp):
    if not timestamp:
        return 0
    try:
        created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds()
    except ValueError:
        return 0


def expected_source_node(name):
    return bool(re.fullmatch(re.escape(source_prefix) + r"-master-0-[012]", name))


def csr_is_expected(csr):
    spec = csr.get("spec") or {}
    if (csr.get("status") or {}).get("conditions"):
        return False
    signer = spec.get("signerName")
    username = spec.get("username")
    try:
        request = base64.b64decode(spec.get("request", ""), validate=True)
        parsed = subprocess.run(
            ["/usr/bin/openssl", "req", "-noout", "-subject", "-nameopt", "RFC2253"],
            input=request, capture_output=True, timeout=5)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False
    if parsed.returncode != 0:
        return False
    subject = parsed.stdout.decode(errors="replace").strip().removeprefix("subject=")
    common_names = re.findall(r"(?:^|,)CN=([^,]+)", subject)
    organizations = re.findall(r"(?:^|,)O=([^,]+)", subject)
    if len(common_names) != 1 or len(organizations) != 1:
        return False
    cn, organization = common_names[0], organizations[0]
    if signer == "kubernetes.io/kube-apiserver-client-kubelet":
        node = cn.removeprefix("system:node:")
        return (cn == f"system:node:{node}" and expected_source_node(node) and
                organization == "system:nodes" and username ==
                "system:serviceaccount:openshift-machine-config-operator:node-bootstrapper")
    if signer == "kubernetes.io/kube-apiserver-client":
        node = cn.removeprefix("system:ovn-node:")
        return (cn == f"system:ovn-node:{node}" and expected_source_node(node) and
                organization == "system:ovn-nodes" and username == f"system:node:{node}")
    return False


def approve_expected_csrs():
    csrs = get_json("csr")
    if csrs is None:
        return
    for csr in csrs.get("items", []):
        name = (csr.get("metadata") or {}).get("name", "")
        if not re.fullmatch(r"csr-[a-z0-9]+", name) or not csr_is_expected(csr):
            continue
        result = oc("adm", "certificate", "approve", name)
        if result is not None and result.returncode == 0:
            print(f"Approved expected compact node CSR {name}", flush=True)


def ready_containers(pod):
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    return bool(statuses) and all(status.get("ready") for status in statuses)


def repair_ovn_and_controller():
    ovn = get_json("pods", "-n", "openshift-ovn-kubernetes")
    mco = get_json("pods", "-n", "openshift-machine-config-operator")
    etcd = get_json("pods", "-n", "openshift-etcd")
    if ovn is None or mco is None:
        return
    ovn_pods = [pod for pod in ovn.get("items", [])
                if pod.get("metadata", {}).get("name", "").startswith("ovnkube-node-")]
    for pod in ovn_pods:
        metadata, spec = pod.get("metadata") or {}, pod.get("spec") or {}
        name = metadata.get("name", "")
        if (expected_source_node(spec.get("nodeName", "")) and
                age_seconds(metadata.get("deletionTimestamp")) > 35):
            result = oc("delete", "pod", name, "-n", "openshift-ovn-kubernetes",
                        "--force", "--grace-period=0", "--wait=false")
            if result is not None and result.returncode == 0:
                print(f"Removed stale source OVN pod {name}", flush=True)

    # Snapshot pods can report Ready from their old status. Count only pods
    # created during this boot when deciding whether a controller can move.
    ready_ovn = sum(1 for pod in ovn_pods if ready_containers(pod) and
                    0 < age_seconds((pod.get("metadata") or {}).get("creationTimestamp")) < 30 * 60)
    for pod in mco.get("items", []):
        metadata, spec = pod.get("metadata") or {}, pod.get("spec") or {}
        name, node = metadata.get("name", ""), spec.get("nodeName", "")
        if (name.startswith("machine-config-controller-") and expected_source_node(node)
                and not ready_containers(pod) and age_seconds(metadata.get("creationTimestamp")) > 90
                and ready_ovn >= 2 and name not in removed_source_controller):
            result = oc("delete", "pod", name, "-n", "openshift-machine-config-operator",
                        "--force", "--grace-period=0", "--wait=false")
            if result is not None and result.returncode == 0:
                removed_source_controller.add(name)
                print(f"Replaced stale source MCO controller {name}", flush=True)
    pending = [(pod, "openshift-machine-config-operator") for pod in mco.get("items", [])
               if pod.get("metadata", {}).get("name", "").startswith(
                   ("machine-config-controller-", "machine-config-operator-"))]
    if etcd is not None:
        pending.extend((pod, "openshift-etcd") for pod in etcd.get("items", [])
                       if pod.get("metadata", {}).get("name", "").startswith("installer-"))
    for pod, namespace in pending:
        metadata, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
        name, node = metadata.get("name", ""), spec.get("nodeName", "")
        if (not re.fullmatch(r"master-0-[012]", node) or ready_containers(pod) or
                age_seconds(metadata.get("creationTimestamp")) < 90 or node in restarted_ovn or
                status.get("phase") != "Pending"):
            continue
        events = get_json("events", "-n", namespace,
                          "--field-selector", f"involvedObject.name={name}")
        if events is None or not any(
                "ovn-cni-server.sock: connect: connection refused" in event.get("message", "") or
                "timed out waiting for OVS port binding" in event.get("message", "")
                for event in events.get("items", [])):
            continue
        for ovn_pod in ovn_pods:
            if ((ovn_pod.get("spec") or {}).get("nodeName") == node and
                    ready_containers(ovn_pod)):
                ovn_name = ovn_pod["metadata"]["name"]
                result = oc("delete", "pod", ovn_name, "-n", "openshift-ovn-kubernetes",
                            "--wait=false")
                if result is not None and result.returncode == 0:
                    restarted_ovn.add(node)
                    print(f"Restarted OVN on {node} after CNI failure", flush=True)
                break


def clear_transient_mcd_errors():
    nodes = get_json("nodes")
    if nodes is None:
        return
    for node in nodes.get("items", []):
        metadata = node.get("metadata") or {}
        name, annotations = metadata.get("name", ""), metadata.get("annotations") or {}
        if not re.fullmatch(r"master-0-[012]", name) or name in cleared_mcd:
            continue
        reason = annotations.get("machineconfiguration.openshift.io/reason", "")
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in (node.get("status") or {}).get("conditions") or [])
        if (ready and age_seconds(metadata.get("creationTimestamp")) > 60 and
                annotations.get("machineconfiguration.openshift.io/state") == "Degraded" and
                annotations.get("machineconfiguration.openshift.io/currentConfig") ==
                annotations.get("machineconfiguration.openshift.io/desiredConfig") and
                reason.startswith("failed to set annotations on node:") and
                ("Timeout" in reason or "connection reset by peer" in reason)):
            result = oc("annotate", "node", name, "machineconfiguration.openshift.io/state=Done",
                        "machineconfiguration.openshift.io/reason-", "--overwrite")
            if result is not None and result.returncode == 0:
                cleared_mcd.add(name)
                print(f"Cleared transient MCD API error on {name}", flush=True)


deadline = time.monotonic() + 50 * 60
while running and boot_alive() and time.monotonic() < deadline:
    if (clone_dir.is_dir() and not clone_dir.is_symlink() and clone_dir.stat().st_uid == 0
            and kubeconfig.is_file() and not kubeconfig.is_symlink()
            and kubeconfig.stat().st_uid == 0):
        approve_expected_csrs()
        repair_ovn_and_controller()
        clear_transient_mcd_errors()
    time.sleep(15)

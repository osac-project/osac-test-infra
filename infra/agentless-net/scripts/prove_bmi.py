#!/usr/bin/env python3
"""Create one virtual network, subnet, and BMI, then wait for its DHCP address.

This is the OSAC-6640 proof: one fabric interface, one subnet, one lease.
It does not exercise isolation, security groups, or a second host.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PUBLIC_API = "osac.public.v1"
PRIVATE_API = "osac.private.v1"
FABRIC_INTERFACE = "eth9"
DEFAULT_DISK_IMAGE = "oci://quay.io/osac-project/fedora-cloud-bmi:44"
DEFAULT_TEMPLATE = "bm-host-provisioning"


def bmi_create_body(
    *,
    name: str,
    tenant: str,
    subnet_id: str,
    catalog_name: str,
    instance_type_name: str,
    disk_image_name: str,
    ssh_public_key: str,
) -> dict[str, Any]:
    return {
        "object": {
            "metadata": {"name": name, "tenant": tenant},
            "spec": {
                "catalog_item": {"name": catalog_name, "shared": True},
                "instance_type": {"name": instance_type_name, "shared": True},
                "disk_image": {"name": disk_image_name, "shared": True},
                "run_strategy": "RUN_STRATEGY_ALWAYS",
                "ssh_public_key": ssh_public_key,
                "network_attachments": [
                    {
                        "interface": FABRIC_INTERFACE,
                        "subnet": {"id": subnet_id},
                        "primary": True,
                    }
                ],
            },
        }
    }


def discovered_address(resource: dict[str, Any]) -> str:
    """Return the first non-empty attachment address in a Get response."""
    status = resource.get("status") or {}
    attachments = (
        status.get("network_attachment_statuses")
        or status.get("networkAttachmentStatuses")
        or status.get("network_attachments")
        or status.get("networkAttachments")
        or []
    )
    if isinstance(attachments, dict):
        attachments = list(attachments.values())
    for item in attachments:
        if not isinstance(item, dict):
            continue
        address = item.get("ip_address") or item.get("ipAddress") or ""
        if address:
            return str(address)
    return ""


def state_of(resource: dict[str, Any]) -> str:
    return str((resource.get("status") or {}).get("state") or "")


def is_ready(resource: dict[str, Any]) -> bool:
    """True when a virtual network or subnet is in a *_READY state."""
    state = state_of(resource)
    if "NOT_READY" in state or "FAILED" in state:
        return False
    return state.endswith("_READY")


def is_bmi_running(resource: dict[str, Any]) -> bool:
    """True only for BARE_METAL_INSTANCE_STATE_RUNNING."""
    state = state_of(resource)
    return state.endswith("_RUNNING") and "FAILED" not in state


def is_failed(resource: dict[str, Any]) -> bool:
    return "FAILED" in state_of(resource)


class CallTimeout(RuntimeError):
    """One gRPC call exceeded its own deadline."""


def grpcurl_command(
    *,
    address: str,
    token: str,
    ca_file: str,
    service: str,
    data: dict[str, Any] | None = None,
) -> list[str]:
    return [
        "grpcurl",
        "-cacert",
        ca_file,
        "-H",
        f"Authorization: Bearer {token}",
        "-d",
        json.dumps(data or {}),
        address,
        service,
    ]


class Api:
    def __init__(self, *, address: str, token: str, ca_file: str, call_timeout: int = 30) -> None:
        self.address = address
        self.token = token
        self.ca_file = ca_file
        self.call_timeout = call_timeout

    def call(self, service: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        command = grpcurl_command(
            address=self.address,
            token=self.token,
            ca_file=self.ca_file,
            service=service,
            data=data,
        )
        timed_out = False
        try:
            completed = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=self.call_timeout
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        if timed_out:
            raise CallTimeout(f"{service} timed out after {self.call_timeout}s")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"{service} failed: {detail}")
        if not completed.stdout.strip():
            return {}
        parsed = json.loads(completed.stdout)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"{service} returned {type(parsed).__name__}, expected an object")
        return parsed


def _object_id(response: dict[str, Any], service: str) -> str:
    try:
        return str(response["object"]["id"])
    except KeyError as exc:
        raise RuntimeError(f"{service} did not return an object id") from exc


def _object_name(response: dict[str, Any]) -> str:
    return str(response["object"]["metadata"]["name"])


def create_proof(
    api: Api,
    *,
    tenant: str,
    disk_image_ref: str,
    template_name: str,
    ssh_public_key: str,
    state_path: Path,
) -> dict[str, str]:
    state: dict[str, str] = {}

    def remember(key: str, value: str) -> None:
        state[key] = value
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))

    network = api.call(
        f"{PUBLIC_API}.VirtualNetworks/Create",
        {"object": {"metadata": {"name": "bmaas-proof", "tenant": tenant}, "spec": {"ipv4_cidr": "10.100.0.0/16"}}},
    )
    network_id = _object_id(network, "VirtualNetworks/Create")
    remember("vnet_id", network_id)
    wait_for_state(
        api,
        f"{PUBLIC_API}.VirtualNetworks/Get",
        network_id,
        timeout_seconds=300,
        interval_seconds=5,
    )
    subnet = api.call(
        f"{PUBLIC_API}.Subnets/Create",
        {
            "object": {
                "metadata": {"name": "bmaas-proof-subnet", "tenant": tenant},
                "spec": {"virtual_network": {"id": network_id}, "ipv4_cidr": "10.100.1.0/24"},
            }
        },
    )
    subnet_id = _object_id(subnet, "Subnets/Create")
    remember("subnet_id", subnet_id)
    wait_for_state(
        api,
        f"{PUBLIC_API}.Subnets/Get",
        subnet_id,
        timeout_seconds=300,
        interval_seconds=5,
    )
    instance_type = api.call(
        f"{PRIVATE_API}.BareMetalInstanceTypes/Create",
        {
            "object": {
                "metadata": {"name": "bmaas-agentless-ci", "tenant": "shared"},
                "spec": {
                    "hardware": {
                        "cpu": {"cores": 4, "architecture": "amd64", "threads_per_core": 2},
                        "memory": {"total_gb": 8},
                        "network_ports": [
                            {"name": FABRIC_INTERFACE, "role": "fabric", "type": "Ethernet", "speed": "10Gbps"}
                        ],
                    },
                    "host_label_selector": {"match_labels": {"osac.openshift.io/host-type": "agentless-ci"}},
                },
            }
        },
    )
    remember("type_id", _object_id(instance_type, "BareMetalInstanceTypes/Create"))
    disk_image = api.call(
        f"{PUBLIC_API}.DiskImages/Create",
        {
            "object": {
                "metadata": {"name": "bmaas-proof-image", "tenant": tenant},
                "spec": {
                    "source_type": "SOURCE_TYPE_REGISTRY",
                    "source_ref": disk_image_ref,
                    "guest_os_family": "GUEST_OS_FAMILY_LINUX",
                    "architecture": ["ARCHITECTURE_AMD64"],
                },
            }
        },
    )
    remember("disk_image_id", _object_id(disk_image, "DiskImages/Create"))
    catalog = api.call(
        f"{PUBLIC_API}.BareMetalInstanceCatalogItems/Create",
        {
            "object": {
                "metadata": {"name": "bmaas-proof", "tenant": tenant},
                "title": "BMaaS Agentless proof",
                "description": "One virtual BMH on the Agentless fabric.",
                "template": {"name": template_name, "shared": True},
                "published": True,
            }
        },
    )
    remember("catalog_id", _object_id(catalog, "BareMetalInstanceCatalogItems/Create"))
    bmi = api.call(
        f"{PUBLIC_API}.BareMetalInstances/Create",
        bmi_create_body(
            name="bmaas-proof",
            tenant=tenant,
            subnet_id=subnet_id,
            catalog_name=_object_name(catalog),
            instance_type_name=_object_name(instance_type),
            disk_image_name=_object_name(disk_image),
            ssh_public_key=ssh_public_key,
        ),
    )
    remember("bmi_id", _object_id(bmi, "BareMetalInstances/Create"))
    return state


def wait_for_state(api: Api, service: str, resource_id: str, *, timeout_seconds: int, interval_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            response = api.call(service, {"id": resource_id})
        except CallTimeout as exc:
            last = str(exc)
            time.sleep(interval_seconds)
            continue
        resource = response.get("object") or response
        last = state_of(resource)
        if is_ready(resource):
            return
        if is_failed(resource):
            raise RuntimeError(f"{service} {resource_id} entered {last}")
        time.sleep(interval_seconds)
    raise TimeoutError(f"{service} {resource_id} did not become ready. Last state: {last}")


def wait_for_address(api: Api, bmi_id: str, *, timeout_seconds: int, interval_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            response = api.call(f"{PUBLIC_API}.BareMetalInstances/Get", {"id": bmi_id})
        except CallTimeout as exc:
            last = str(exc)
            time.sleep(interval_seconds)
            continue
        resource = response.get("object") or response
        last = json.dumps(resource.get("status") or {})
        if is_failed(resource):
            raise RuntimeError(f"BMI {bmi_id} entered {state_of(resource)}. Last status: {last}")
        address = discovered_address(resource)
        if address and is_bmi_running(resource):
            return address
        time.sleep(interval_seconds)
    raise TimeoutError(f"BMI {bmi_id} did not become running with an address. Last status: {last}")


def delete_proof(api: Api, state: dict[str, str]) -> None:
    for service, key in (
        (f"{PUBLIC_API}.BareMetalInstances/Delete", "bmi_id"),
        (f"{PUBLIC_API}.BareMetalInstanceCatalogItems/Delete", "catalog_id"),
        (f"{PUBLIC_API}.DiskImages/Delete", "disk_image_id"),
        (f"{PRIVATE_API}.BareMetalInstanceTypes/Delete", "type_id"),
        (f"{PUBLIC_API}.Subnets/Delete", "subnet_id"),
        (f"{PUBLIC_API}.VirtualNetworks/Delete", "vnet_id"),
    ):
        resource_id = state.get(key, "")
        if not resource_id:
            continue
        try:
            api.call(service, {"id": resource_id})
            print(f"deleted {key} {resource_id}")
        except RuntimeError as exc:
            print(f"warning: {exc}", file=sys.stderr)


def _token(namespace: str) -> str:
    completed = subprocess.run(
        ["oc", "create", "token", "-n", namespace, "admin"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    token = completed.stdout.strip()
    if not token:
        raise RuntimeError("oc create token returned an empty token")
    return token


def ingress_ca_file(namespace: str) -> str:
    """Return a CA file for the fulfillment route, obtained through the kube API.

    The route certificate is not verified by disabling TLS. The CA is the
    route's own caCertificate when set, otherwise the cluster ingress CA.
    """
    route = subprocess.run(
        ["oc", "get", "route", "fulfillment-internal-api", "-n", namespace, "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if route.returncode != 0:
        raise RuntimeError((route.stderr or route.stdout).strip() or "fulfillment-internal-api route is not available")
    tls = (json.loads(route.stdout).get("spec") or {}).get("tls") or {}
    material = str(tls.get("caCertificate") or "").strip()
    if not material:
        secret = subprocess.run(
            [
                "oc",
                "get",
                "secret",
                "router-ca",
                "-n",
                "openshift-ingress-operator",
                "-o",
                r"jsonpath={.data.tls\.crt}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if secret.returncode != 0 or not secret.stdout.strip():
            raise RuntimeError("the ingress CA is not available; refusing an unverified API connection")
        material = base64.b64decode(secret.stdout).decode()
    descriptor, name = tempfile.mkstemp(prefix="osac-ingress-ca-", suffix=".crt")
    os.close(descriptor)
    path = Path(name)
    path.write_text(material if material.endswith("\n") else material + "\n")
    path.chmod(0o600)
    return str(path)


def _api_address(namespace: str) -> str:
    completed = subprocess.run(
        ["oc", "get", "route", "fulfillment-internal-api", "-n", namespace, "-o", "jsonpath={.spec.host}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("fulfillment-internal-api route is not available")
    return completed.stdout.strip() + ":443"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=os.environ.get("OSAC_NAMESPACE", "osac-e2e-ci"))
    parser.add_argument("--tenant", default=os.environ.get("OSAC_TENANT", "osac-e2e-ci"))
    parser.add_argument("--disk-image-ref", default=os.environ.get("BMI_DISK_IMAGE_REF", DEFAULT_DISK_IMAGE))
    parser.add_argument("--template", default=os.environ.get("OSAC_BMI_TEMPLATE", DEFAULT_TEMPLATE))
    parser.add_argument("--ssh-public-key-file", default=os.environ.get("BMI_SSH_PUBLIC_KEY_FILE", ""))
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=2700)
    parser.add_argument("--interval-seconds", type=int, default=20)
    args = parser.parse_args(argv)

    state_path = Path(args.state_file)
    api = Api(
        address=_api_address(args.namespace),
        token=_token(args.namespace),
        ca_file=ingress_ca_file(args.namespace),
    )
    if args.delete:
        if not state_path.is_file():
            print(f"no proof state at {state_path}; nothing to delete")
            return 0
        delete_proof(api, json.loads(state_path.read_text()))
        return 0

    key_path = Path(args.ssh_public_key_file) if args.ssh_public_key_file else None
    if key_path is None or not key_path.is_file():
        raise RuntimeError("BMI_SSH_PUBLIC_KEY_FILE must point at the public key registered on the guest")
    state = create_proof(
        api,
        tenant=args.tenant,
        disk_image_ref=args.disk_image_ref,
        template_name=args.template,
        ssh_public_key=key_path.read_text().strip(),
        state_path=state_path,
    )
    address = wait_for_address(
        api,
        state["bmi_id"],
        timeout_seconds=args.timeout_seconds,
        interval_seconds=args.interval_seconds,
    )
    state["address"] = address
    state_path.write_text(json.dumps(state))
    print(f"BMI {state['bmi_id']} is Ready at {address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

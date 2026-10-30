#!/usr/bin/env python3
"""Reconcile NetBox devices with a raw inventory file.

One command for the whole device lifecycle, the NetBox-mode counterpart
of reconcile-bmhs.py: ensures the NetBox schema (site, manufacturer,
device type, role and the OSAC custom fields), removes the group's
devices the inventory no longer lists — their Secret API BMC Secrets go
too; an emptied inventory removes them all — then creates one staged
device per host and one BMC Secret per host in the OSAC Secret API,
labeled osac.openshift.io/netbox-device-<device-id> for the operator's
credential lookup.

The NetBox connection comes from --url/--token (the OSAC_NETBOX_* env
vars work too); plain HTTP requires --insecure. The Secret API is
reached through the osac CLI, which must be on PATH and logged in.
pynetbox and requests are imported lazily, only when a session actually
needs configuring, so the unit tests run without them.

Run directly: python3 .github/scripts/reconcile-netbox-devices.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from raw_inventory import Host, InventoryError, RawInventory, is_group_host_name

if TYPE_CHECKING:
    # Imported for annotations only: the test environment has no pynetbox.
    from pynetbox.core.api import Api
    from pynetbox.core.endpoint import Endpoint
    from pynetbox.core.response import Record

# 300s: a hung CLI must fail, not hang the job.
OSAC_TIMEOUT_SECONDS = 300

T = TypeVar("T")

# The device custom fields the operator's free-host query and credential
# lookup depend on: (name, NetBox field type).
CUSTOM_FIELDS = (
    ("osac_managed", "boolean"),
    ("osac_instance_id", "text"),
    ("osac_bmc_address", "text"),
    ("osac_boot_mac", "text"),
    ("osac_interface_macs", "json"),
    ("host_type", "text"),
)

# NetBox requires a color on device role creation; grey stays neutral.
ROLE_COLOR = "9e9e9e"

# The Secret API label that binds a BMC Secret to its NetBox device:
# the operator lists Secrets by this label when it claims a device.
DEVICE_LABEL_PREFIX = "osac.openshift.io/netbox-device-"


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def run_osac(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Output is captured so CLI failures never spill Secret data into logs.
    return subprocess.run(
        ["osac", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=OSAC_TIMEOUT_SECONDS,
    )


def build_netbox_api(url: str, token: str, ca_file: str | None, insecure: bool) -> Api:
    """Construct the pynetbox API; third-party imports stay lazy.

    A dedicated session is only needed to customize TLS verification;
    the default session verifies against the system roots.
    """
    import pynetbox

    api = pynetbox.api(url, token=token)
    if ca_file is not None or insecure:
        import requests

        session = requests.Session()
        session.verify = ca_file if ca_file is not None else False
        api.http_session = session
    return api


def run_netbox(operation: str, call: Callable[[], T]) -> T:
    """Run one NetBox API call, re-raising request failures sanitized.

    Response bodies and payloads are dropped: they carry device data,
    and the sanitized form (method, endpoint, status) is all a caller
    can act on anyway. Failures without a request attached (programming
    errors) re-raise unchanged.
    """
    try:
        return call()
    except Exception as exc:
        req = getattr(exc, "req", None)
        if req is None:
            raise
        status = getattr(req, "status_code", "?")
        url = getattr(req, "url", None) or getattr(exc, "base", "<unknown endpoint>")
        method = getattr(getattr(req, "request", None), "method", "?")
        raise RuntimeError(f"NetBox {operation} failed: {method} {url} -> HTTP {status}") from exc


def _ensure_slug_record(
    endpoint: Endpoint, *, slug: str, kind: str, create_kwargs: dict[str, Any] | None = None
) -> int:
    """Resolve a slug record to its id, creating it when missing."""
    record = run_netbox(f"{kind} lookup", lambda: endpoint.get(slug=slug))
    if record is None:
        print(f"  Creating {kind} {slug}...")
        record = run_netbox(
            f"{kind} create", lambda: endpoint.create(slug=slug, name=slug, **(create_kwargs or {}))
        )
    return int(record.id)


def ensure_schema(api: Api, args: argparse.Namespace) -> dict[str, int]:
    """Ensure the objects and custom fields devices need; return their ids.

    The provider owns the NetBox schema: the operator only ever reads
    it, so a fresh NetBox must be bootstrapped before any device.
    """
    print("==> Ensuring the NetBox schema...")
    site_id = _ensure_slug_record(api.dcim.sites, slug=args.site, kind="site")
    manufacturer_id = _ensure_slug_record(api.dcim.manufacturers, slug=args.manufacturer, kind="manufacturer")
    role_id = _ensure_slug_record(
        api.dcim.device_roles, slug=args.role, kind="device role", create_kwargs={"color": ROLE_COLOR}
    )
    # Device types have no writable slug in recent NetBox releases: it is
    # derived from the model, so both take the same value.
    device_type = run_netbox("device type lookup", lambda: api.dcim.device_types.get(slug=args.device_type))
    if device_type is None:
        print(f"  Creating device type {args.device_type}...")
        device_type = run_netbox(
            "device type create",
            lambda: api.dcim.device_types.create(manufacturer=manufacturer_id, model=args.device_type),
        )
    _ensure_custom_fields(api)
    return {"site": site_id, "role": role_id, "device_type": int(device_type.id)}


def _ensure_custom_fields(api: Api) -> None:
    existing = {cf.name for cf in run_netbox("custom field list", lambda: list(api.extras.custom_fields.all()))}
    for name, field_type in CUSTOM_FIELDS:
        if name in existing:
            continue
        print(f"  Creating custom field {name}...")
        run_netbox(
            f"custom field create {name}",
            lambda name=name, field_type=field_type: api.extras.custom_fields.create(
                name=name, type=field_type, object_types=["dcim.device"]
            ),
        )


def device_custom_fields(host: Host, host_type: str) -> dict[str, Any]:
    """The operator's free-host contract: managed, reachable, selectable."""
    return {
        "osac_managed": True,
        "osac_bmc_address": host.bmc_address,
        "osac_boot_mac": host.boot_mac,
        "osac_interface_macs": {"data-0": host.boot_mac},
        "host_type": host_type,
    }


def _status_value(status: object) -> str:
    """Normalize a NetBox device status.

    NetBox may return a plain string (brief responses) or a choice
    object (``{"value": ..., "label": ...}`` / pynetbox Record with
    ``.value``).
    """
    if isinstance(status, str):
        return status
    if isinstance(status, dict):
        value = status.get("value", "")
        return value if isinstance(value, str) else ""
    value = getattr(status, "value", None)
    return value if isinstance(value, str) else ""


def list_group_devices(api: Api, name_prefix: str) -> dict[str, Record]:
    """The group's existing devices, by name.

    Only osac_managed devices are considered, and only names following
    the group's naming scheme: other groups' and unmanaged devices are
    never ours to touch.
    """
    devices = run_netbox("device list", lambda: list(api.dcim.devices.filter(cf_osac_managed=True)))
    return {device.name: device for device in devices if is_group_host_name(device.name, name_prefix)}


def delete_device(api: Api, device: Record) -> None:
    instance_id = (getattr(device, "custom_fields", None) or {}).get("osac_instance_id")
    if _status_value(getattr(device, "status", "")) == "active" or instance_id:
        print(f"  WARNING: {device.name} is claimed; deleting anyway (the inventory rules).")
    run_netbox("device delete", lambda: api.dcim.devices.delete([device]))


def create_device(api: Api, host: Host, schema: dict[str, int], host_type: str) -> Record:
    return run_netbox(
        "device create",
        lambda: api.dcim.devices.create(
            name=host.name,
            role=schema["role"],
            site=schema["site"],
            device_type=schema["device_type"],
            status="staged",
            custom_fields=device_custom_fields(host, host_type),
        ),
    )


def reconcile_devices(
    api: Api, inventory: RawInventory, schema: dict[str, int], args: argparse.Namespace
) -> dict[str, str]:
    """Delete stale, create missing; return host name -> device id."""
    print("==> Reconciling NetBox devices with the raw inventory...")
    existing = list_group_devices(api, inventory.name_prefix)
    desired_names = {host.name for host in inventory.hosts}
    for name, device in sorted(existing.items()):
        if name in desired_names:
            continue
        print(f"  Deleting {name} (no longer in the inventory)...")
        delete_device(api, device)
        delete_bmc_secret(args, name)
    device_ids = {name: str(device.id) for name, device in existing.items() if name in desired_names}
    for host in inventory.hosts:
        if host.name in device_ids:
            continue
        print(f"  Creating {host.name}: MAC={host.boot_mac}, BMC={host.bmc_address}")
        device_ids[host.name] = str(create_device(api, host, schema, args.host_type).id)
    return device_ids


def device_label_key(device_id: str) -> str:
    return f"{DEVICE_LABEL_PREFIX}{device_id}"


def bmc_secret_name(host_name: str) -> str:
    return f"{host_name}-bmc"


def list_secret_labels(args: argparse.Namespace) -> dict[str, dict[str, str]]:
    """Map Secret name -> labels for the tenant's OSAC Secrets.

    One list call instead of one get per host. Secret data is omitted
    from list responses, so nothing sensitive is read or printed.
    """
    result = run_osac(["--tenant", args.secret_tenant, "get", "secrets", "-o", "json"])
    payload = json.loads(result.stdout)
    # A single result renders as a bare object, not a one-element array.
    secrets = [payload] if isinstance(payload, dict) else payload
    labels: dict[str, dict[str, str]] = {}
    for secret in secrets:
        metadata = secret.get("metadata") or {}
        name = metadata.get("name")
        if name:
            labels[name] = metadata.get("labels") or {}
    return labels


def ensure_bmc_secrets(hosts: list[Host], device_ids: dict[str, str], args: argparse.Namespace) -> None:
    """One BMC Secret per host, labeled for its device.

    The Secret's data cannot be read back, so an existing Secret with
    the right device label is kept as-is; one with a stale label (the
    device was recreated with a new id) is deleted and recreated from
    the inventory — the source of truth for credentials.
    """
    print("==> Ensuring BMC Secrets in the OSAC Secret API...")
    existing = list_secret_labels(args)
    for host in hosts:
        name = bmc_secret_name(host.name)
        label_key = device_label_key(device_ids[host.name])
        if existing.get(name, {}).get(label_key) == "true":
            continue
        if name in existing:
            print(f"  Recreating Secret {name} (device label changed)...")
            run_osac(["--tenant", args.secret_tenant, "delete", "secret", name])
        else:
            print(f"  Creating Secret {name}...")
        # Credentials travel in temp files, never on the command line.
        with tempfile.TemporaryDirectory(prefix="bmc-secret-") as cred_dir:
            username_file = Path(cred_dir) / "username"
            password_file = Path(cred_dir) / "password"
            username_file.write_text(host.bmc_username)
            password_file.write_text(host.bmc_password)
            run_osac(
                [
                    "--tenant", args.secret_tenant,
                    "create", "secret",
                    "--name", name,
                    "--type", "opaque",
                    "--from-file", f"username={username_file}",
                    "--from-file", f"password={password_file}",
                    "--label", f"{label_key}=true",
                ]
            )


def delete_bmc_secret(args: argparse.Namespace, host_name: str) -> None:
    """Delete a host's BMC Secret; a missing Secret is not an error."""
    name = bmc_secret_name(host_name)
    result = run_osac(["--tenant", args.secret_tenant, "delete", "secret", name], check=False)
    if result.returncode != 0 and "not found" not in (result.stderr + result.stdout).lower():
        fail(f"osac delete secret {name} failed: {(result.stderr or result.stdout).strip()}")


def resolve_connection(args: argparse.Namespace) -> tuple[str, str, str | None, bool]:
    """Combine flags with the OSAC_NETBOX_* env fallbacks; validate."""
    url = args.url or os.environ.get("OSAC_NETBOX_URL", "").strip()
    token = args.token or ""
    if not token and args.token_file is not None:
        try:
            token = args.token_file.read_text().strip()
        except OSError as exc:
            fail(f"cannot read token file '{args.token_file}': {exc}")
    if not token:
        token = os.environ.get("OSAC_NETBOX_TOKEN", "").strip()
    ca_file = str(args.ca_file) if args.ca_file else os.environ.get("OSAC_NETBOX_CA_FILE", "").strip() or None
    insecure = args.insecure or os.environ.get("OSAC_NETBOX_INSECURE", "").strip().lower() in ("1", "true", "yes")
    if not url:
        fail("NetBox URL is empty: pass --url or set OSAC_NETBOX_URL")
    if not token:
        fail("NetBox token is empty: pass --token/--token-file or set OSAC_NETBOX_TOKEN")
    if url.startswith("http://") and not insecure:
        fail(f"NetBox URL {url!r} uses http://: pass --insecure (or set OSAC_NETBOX_INSECURE) to allow it")
    return url, token, ca_file, insecure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--inventory-file", type=Path, required=True, help="raw inventory JSON to read")
    parser.add_argument("--url", help="NetBox base URL (default: env OSAC_NETBOX_URL)")
    parser.add_argument("--token", help="NetBox API token (default: env OSAC_NETBOX_TOKEN)")
    parser.add_argument("--token-file", type=Path, help="file holding the NetBox API token")
    parser.add_argument(
        "--ca-file", type=Path, help="CA bundle PEM for NetBox TLS (default: env OSAC_NETBOX_CA_FILE)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="allow plain-HTTP NetBox and skip TLS verification (default: env OSAC_NETBOX_INSECURE)",
    )
    parser.add_argument("--site", default="osac-lab", help="NetBox site slug for devices (default: osac-lab)")
    parser.add_argument("--role", default="server", help="NetBox device role slug (default: server)")
    parser.add_argument("--manufacturer", default="osac", help="NetBox manufacturer slug (default: osac)")
    parser.add_argument(
        "--device-type", default="virtual-host", help="NetBox device type slug (default: virtual-host)"
    )
    parser.add_argument(
        "--host-type", default="default", help="host_type capability custom field value (default: default)"
    )
    parser.add_argument("--secret-tenant", default="system", help="OSAC tenant for the BMC Secrets (default: system)")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    try:
        if not args.inventory_file.exists():
            fail(f"raw inventory file '{args.inventory_file}' not found")
        inventory = RawInventory.load(args.inventory_file)
        url, token, ca_file, insecure = resolve_connection(args)
        api = build_netbox_api(url, token, ca_file, insecure)
        schema = ensure_schema(api, args)
        device_ids = reconcile_devices(api, inventory, schema, args)
        ensure_bmc_secrets(inventory.hosts, device_ids, args)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}", file=sys.stderr)
        sys.exit(exc.returncode)
    except subprocess.TimeoutExpired as exc:
        fail(f"command timed out after {exc.timeout} seconds: {' '.join(exc.cmd)}")
    except InventoryError as exc:
        fail(str(exc))
    except FileNotFoundError as exc:
        fail(f"required command not found: {exc}")
    print(f"==> {len(inventory.hosts)} NetBox devices staged.")


if __name__ == "__main__":
    main()

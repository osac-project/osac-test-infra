#!/usr/bin/env python3
"""Tests for reconcile-netbox-devices.py.

Unit tests cover the device custom-field rendering and the Secret API
name and label derivation; the inventory library itself is covered by
test_raw_inventory.py. The end-to-end tests run the script as a
subprocess with a stub pynetbox package on PYTHONPATH (the script
imports it lazily, so these tests themselves run without it) and a
fake osac CLI on PATH, verifying the schema bootstrap, the staged
devices created per inventory host, the per-device BMC Secrets in the
Secret API, the deletion of devices the inventory no longer lists, and
the emptied-inventory cleanup.

Run directly: python3 -I .github/scripts/test_reconcile_netbox_devices.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

# Import the sibling modules first: the script under test imports them
# too, and the isolated-mode test run resolves them through sys.modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import raw_inventory
import test_fakes

_SPEC = importlib.util.spec_from_file_location(
    "reconcile_netbox_devices", Path(__file__).resolve().parent / "reconcile-netbox-devices.py"
)
reconcile_netbox_devices = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = reconcile_netbox_devices
_SPEC.loader.exec_module(reconcile_netbox_devices)

SCRIPT = Path(__file__).resolve().parent / "reconcile-netbox-devices.py"

INVENTORY = {
    "name_prefix": "virtual-bmh-test",
    "hosts": [
        {
            "name": "virtual-bmh-test-01",
            "uuid": "11111111-1111-1111-1111-111111111111",
            "boot_mac": "52:54:00:bb:cc:01",
            "bmc_address": "redfish-virtualmedia+http://192.0.2.1:8000/redfish/v1/Systems/11111111-1111-1111-1111-111111111111",
            "bmc_username": "ci-admin",
            "bmc_password": "ci-pass",
            "cpu": 4,
            "memory_mib": 8192,
        },
        {
            "name": "virtual-bmh-test-02",
            "uuid": "22222222-2222-2222-2222-222222222222",
            "boot_mac": "52:54:00:bb:cc:02",
            "bmc_address": "redfish-virtualmedia+http://192.0.2.1:8000/redfish/v1/Systems/22222222-2222-2222-2222-222222222222",
            "bmc_username": "ci-admin",
            "bmc_password": "ci-pass",
            "cpu": 4,
            "memory_mib": 8192,
        },
    ],
}

# A stub pynetbox package: records every call as one JSON line in
# NETBOX_CALLS_FILE, persists objects in NETBOX_STATE_FILE, and models
# the schema pieces the script touches. Device types derive their slug
# from the model, like the real API.
STUB_PYNETBOX = textwrap.dedent(
    '''\
    import json
    import os
    import sys
    from pathlib import Path

    STATE_FILE = Path(os.environ["NETBOX_STATE_FILE"])
    CALLS_FILE = Path(os.environ["NETBOX_CALLS_FILE"])

    EMPTY_STATE = {
        "next_id": 100,
        "sites": [],
        "manufacturers": [],
        "device_roles": [],
        "device_types": [],
        "custom_fields": [],
        "devices": [],
    }

    def _state():
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return json.loads(json.dumps(EMPTY_STATE))

    def _save(state):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))

    def _next_id(state):
        state["next_id"] += 1
        return state["next_id"]

    def _log(call):
        with CALLS_FILE.open("a") as handle:
            handle.write(json.dumps(call) + "\\n")

    class Record:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SlugEndpoint:
        def __init__(self, key):
            self._key = key

        def get(self, slug):
            _log({"endpoint": self._key, "op": "get", "slug": slug})
            for entry in _state()[self._key]:
                if entry.get("slug") == slug:
                    return Record(**entry)
            return None

        def create(self, **kwargs):
            kwargs.setdefault("slug", kwargs.get("model"))
            state = _state()
            entry = {"id": _next_id(state), **kwargs}
            state[self._key].append(entry)
            _save(state)
            _log({"endpoint": self._key, "op": "create", "kwargs": kwargs})
            return Record(**entry)

    class DevicesEndpoint:
        def filter(self, **kwargs):
            if kwargs != {"cf_osac_managed": True}:
                print(f"unexpected device filter: {kwargs}", file=sys.stderr)
                sys.exit(89)
            _log({"endpoint": "devices", "op": "filter", "kwargs": kwargs})
            devices = [Record(**entry) for entry in _state()["devices"]]
            return [d for d in devices if (d.custom_fields or {}).get("osac_managed") is True]

        def create(self, **kwargs):
            state = _state()
            entry = {"id": _next_id(state), **kwargs}
            state["devices"].append(entry)
            _save(state)
            _log({"endpoint": "devices", "op": "create", "kwargs": kwargs})
            return Record(**entry)

        def delete(self, records):
            ids = sorted(record.id for record in records)
            state = _state()
            state["devices"] = [e for e in state["devices"] if e["id"] not in ids]
            _save(state)
            _log({"endpoint": "devices", "op": "delete", "ids": ids})

    class CustomFieldsEndpoint:
        def all(self):
            _log({"endpoint": "custom_fields", "op": "all"})
            return [Record(**entry) for entry in _state()["custom_fields"]]

        def create(self, **kwargs):
            state = _state()
            entry = {"id": _next_id(state), **kwargs}
            state["custom_fields"].append(entry)
            _save(state)
            _log({"endpoint": "custom_fields", "op": "create", "kwargs": kwargs})
            return Record(**entry)

    class Dcim:
        sites = SlugEndpoint("sites")
        manufacturers = SlugEndpoint("manufacturers")
        device_roles = SlugEndpoint("device_roles")
        device_types = SlugEndpoint("device_types")
        devices = DevicesEndpoint()

    class Extras:
        custom_fields = CustomFieldsEndpoint()

    class Api:
        def __init__(self, url, token=None):
            self.url = url
            self.token = token
            self.http_session = None
            self.dcim = Dcim()
            self.extras = Extras()

    def api(url, token=None):
        _log({"op": "api", "url": url, "token": token})
        return Api(url, token)
    '''
)

# A stub requests package: the script only builds a Session to override
# TLS verification, so the setter records what the script configured.
STUB_REQUESTS = textwrap.dedent(
    '''\
    import json
    import os
    from pathlib import Path

    class Session:
        def __init__(self):
            self._verify = True

        @property
        def verify(self):
            return self._verify

        @verify.setter
        def verify(self, value):
            self._verify = value
            with Path(os.environ["NETBOX_CALLS_FILE"]).open("a") as handle:
                handle.write(json.dumps({"op": "session", "verify": value}) + "\\n")
    '''
)


def _device_entry(
    name: str,
    device_id: int,
    *,
    status: str = "staged",
    managed: bool = True,
    osac_instance_id: str | None = None,
) -> dict[str, Any]:
    custom_fields: dict[str, Any] = {"osac_managed": managed}
    if osac_instance_id is not None:
        custom_fields["osac_instance_id"] = osac_instance_id
    return {
        "id": device_id,
        "name": name,
        "status": {"value": status, "label": status.title()},
        "custom_fields": custom_fields,
    }


class RenderingTests(unittest.TestCase):
    def test_device_custom_fields_come_from_the_inventory_entry(self) -> None:
        host = raw_inventory.Host(**INVENTORY["hosts"][0])
        fields = reconcile_netbox_devices.device_custom_fields(host, "default")
        self.assertEqual(fields["osac_managed"], True)
        self.assertEqual(fields["osac_bmc_address"], host.bmc_address)
        self.assertEqual(fields["osac_boot_mac"], host.boot_mac)
        self.assertEqual(fields["osac_interface_macs"], {"data-0": host.boot_mac})
        self.assertEqual(fields["host_type"], "default")
        # The claim fields belong to the operator, never to this tool.
        self.assertNotIn("osac_instance_id", fields)

    def test_bmc_secret_name_and_device_label_derive_from_the_host(self) -> None:
        host = raw_inventory.Host(**INVENTORY["hosts"][0])
        self.assertEqual(reconcile_netbox_devices.bmc_secret_name(host.name), f"{host.name}-bmc")
        self.assertEqual(
            reconcile_netbox_devices.device_label_key("46"), "osac.openshift.io/netbox-device-46"
        )


class EndToEndTests(unittest.TestCase):
    """Run the script against the stub pynetbox and the fake osac."""

    DEFAULT_FLAGS = ("--url", "https://netbox.example.test", "--token", "nb-token")

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp = Path(self._temp.name)

        self.fakes = test_fakes.FakeBins(self.temp, ["osac"])
        self.calls_file = self.fakes.calls_file
        self.capture_dir = self.fakes.capture_dir
        self.secrets_dir = self.temp / "secrets"
        self.secrets_dir.mkdir()

        self.inventory_file = self.temp / "inventory.json"
        self.inventory_file.write_text(json.dumps(INVENTORY))

        stub_dir = self.temp / "stubs"
        (stub_dir / "pynetbox").mkdir(parents=True)
        (stub_dir / "pynetbox" / "__init__.py").write_text(STUB_PYNETBOX)
        (stub_dir / "requests").mkdir(parents=True)
        (stub_dir / "requests" / "__init__.py").write_text(STUB_REQUESTS)

        self.state_file = self.temp / "netbox-state.json"
        self.netbox_calls_file = self.temp / "netbox-calls.jsonl"

        self.env = self.fakes.env(
            SECRETS_DIR=str(self.secrets_dir),
            PYTHONPATH=str(stub_dir),
            NETBOX_STATE_FILE=str(self.state_file),
            NETBOX_CALLS_FILE=str(self.netbox_calls_file),
        )

    def _run(
        self,
        inventory: dict[str, Any] | None = None,
        extra_args: tuple[str, ...] = (),
        env_extra: dict[str, str] | None = None,
        flags: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if inventory is not None:
            self.inventory_file.write_text(json.dumps(inventory))
        env = dict(self.env)
        if env_extra is not None:
            env.update(env_extra)
        if flags is None:
            flags = list(self.DEFAULT_FLAGS)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--inventory-file", str(self.inventory_file), *flags, *extra_args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _seed_state(self, devices: list[dict[str, Any]]) -> None:
        self.state_file.write_text(
            json.dumps(
                {
                    "next_id": 100,
                    "sites": [],
                    "manufacturers": [],
                    "device_roles": [],
                    "device_types": [],
                    "custom_fields": [],
                    "devices": devices,
                }
            )
        )

    def _state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text())

    def _netbox_calls(self) -> list[dict[str, Any]]:
        if not self.netbox_calls_file.exists():
            return []
        return [json.loads(line) for line in self.netbox_calls_file.read_text().splitlines()]

    def _seed_secret(self, name: str, labels: dict[str, str]) -> None:
        (self.secrets_dir / f"{name}.json").write_text(
            json.dumps({"metadata": {"name": name, "tenant": "system", "labels": labels}})
        )

    def _secret_labels(self, name: str) -> dict[str, str] | None:
        state = self.secrets_dir / f"{name}.json"
        if not state.exists():
            return None
        return json.loads(state.read_text())["metadata"]["labels"]

    def test_missing_inventory_file_fails(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--inventory-file", str(self.temp / "missing.json"), *self.DEFAULT_FLAGS],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not found", result.stderr)

    def test_full_flow_creates_devices_and_secrets(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self._state()

        # The schema is bootstrapped: one of each object, six custom
        # fields bound to devices.
        self.assertEqual([entry["slug"] for entry in state["sites"]], ["osac-lab"])
        self.assertEqual([entry["slug"] for entry in state["manufacturers"]], ["osac"])
        self.assertEqual([entry["slug"] for entry in state["device_roles"]], ["server"])
        self.assertEqual([entry["model"] for entry in state["device_types"]], ["virtual-host"])
        self.assertEqual(
            {(entry["name"], entry["type"]) for entry in state["custom_fields"]},
            {
                ("osac_managed", "boolean"),
                ("osac_instance_id", "text"),
                ("osac_bmc_address", "text"),
                ("osac_boot_mac", "text"),
                ("osac_interface_macs", "json"),
                ("host_type", "text"),
            },
        )
        for call in self._netbox_calls():
            if call.get("endpoint") == "custom_fields" and call["op"] == "create":
                self.assertEqual(call["kwargs"]["object_types"], ["dcim.device"])

        # One staged device per host, carrying the OSAC custom fields
        # and the schema ids.
        devices = {device["name"]: device for device in state["devices"]}
        self.assertEqual(set(devices), {host["name"] for host in INVENTORY["hosts"]})
        for host in INVENTORY["hosts"]:
            device = devices[host["name"]]
            self.assertEqual(device["status"], "staged")
            self.assertEqual(device["role"], state["device_roles"][0]["id"])
            self.assertEqual(device["site"], state["sites"][0]["id"])
            self.assertEqual(device["device_type"], state["device_types"][0]["id"])
            self.assertEqual(device["custom_fields"]["osac_managed"], True)
            self.assertEqual(device["custom_fields"]["osac_bmc_address"], host["bmc_address"])
            self.assertEqual(device["custom_fields"]["osac_boot_mac"], host["boot_mac"])
            self.assertEqual(device["custom_fields"]["osac_interface_macs"], {"data-0": host["boot_mac"]})
            self.assertEqual(device["custom_fields"]["host_type"], "default")

        # One BMC Secret per host, labeled for its device, with the
        # credentials from the inventory.
        for index, host in enumerate(INVENTORY["hosts"]):
            device_id = devices[host["name"]]["id"]
            name = f"{host['name']}-bmc"
            self.assertEqual(
                self._secret_labels(name), {f"osac.openshift.io/netbox-device-{device_id}": "true"}
            )
            self.assertEqual(self.capture_dir.joinpath(f"secret-{index}-username").read_text(), host["bmc_username"])
            self.assertEqual(self.capture_dir.joinpath(f"secret-{index}-password").read_text(), host["bmc_password"])

        calls = self.calls_file.read_text()
        self.assertIn("tenant=system", calls)
        self.assertNotIn("delete secret", calls)
        self.assertIn("==> 2 NetBox devices staged.", result.stdout)

    def test_rerun_with_steady_state_changes_nothing(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        creates_after_first = [call for call in self._netbox_calls() if call.get("op") == "create"]
        device_count = len(self._state()["devices"])
        calls_after_first = self.calls_file.read_text()

        second = self._run()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(len(self._state()["devices"]), device_count)
        # No new NetBox objects, no Secret churn beyond the list call.
        self.assertEqual([call for call in self._netbox_calls() if call.get("op") == "create"], creates_after_first)
        second_calls = self.calls_file.read_text().removeprefix(calls_after_first)
        self.assertNotIn("create secret", second_calls)
        self.assertNotIn("delete secret", second_calls)
        self.assertIn("get secrets", second_calls)

    def test_deletes_devices_absent_from_the_inventory(self) -> None:
        # virtual-bmh-test-ff belongs to the group's naming scheme but
        # is no longer in the inventory; virtual-bmh-test-01-7f belongs
        # to an overlapping group; virtual-bmh-test-ee is not an OSAC
        # device at all.
        self._seed_state(
            [
                _device_entry("virtual-bmh-test-01", 46),
                _device_entry("virtual-bmh-test-02", 47),
                _device_entry("virtual-bmh-test-ff", 48),
                _device_entry("virtual-bmh-test-01-7f", 49),
                _device_entry("virtual-bmh-test-ee", 50, managed=False),
            ]
        )
        self._seed_secret("virtual-bmh-test-ff-bmc", {"osac.openshift.io/netbox-device-48": "true"})

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        names = {device["name"] for device in self._state()["devices"]}
        self.assertEqual(
            names, {"virtual-bmh-test-01", "virtual-bmh-test-02", "virtual-bmh-test-01-7f", "virtual-bmh-test-ee"}
        )
        self.assertIn("delete secret virtual-bmh-test-ff-bmc tenant=system", self.calls_file.read_text())
        self.assertIsNone(self._secret_labels("virtual-bmh-test-ff-bmc"))
        # Hosts still in the inventory keep their devices: the missing
        # Secrets are created, not their devices recreated.
        self.assertEqual(self._secret_labels("virtual-bmh-test-01-bmc"), {"osac.openshift.io/netbox-device-46": "true"})

    def test_warns_and_deletes_a_claimed_device(self) -> None:
        self._seed_state(
            [
                _device_entry("virtual-bmh-test-01", 46),
                _device_entry("virtual-bmh-test-ff", 48, status="active", osac_instance_id="bmi-123"),
            ]
        )

        result = self._run(inventory={"name_prefix": "virtual-bmh-test", "hosts": [INVENTORY["hosts"][0]]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.assertIn("WARNING: virtual-bmh-test-ff is claimed", result.stdout)
        self.assertNotIn("virtual-bmh-test-ff", {device["name"] for device in self._state()["devices"]})

    def test_emptied_inventory_removes_all_group_devices_and_secrets(self) -> None:
        self._seed_state(
            [
                _device_entry("virtual-bmh-test-01", 46),
                _device_entry("virtual-bmh-test-02", 47),
                _device_entry("virtual-bmh-test-01-7f", 49),
                _device_entry("virtual-bmh-test-ee", 50, managed=False),
            ]
        )
        for name in ("virtual-bmh-test-01-bmc", "virtual-bmh-test-02-bmc"):
            self._seed_secret(name, {})

        result = self._run(inventory={"name_prefix": "virtual-bmh-test", "hosts": []})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        names = {device["name"] for device in self._state()["devices"]}
        self.assertEqual(names, {"virtual-bmh-test-01-7f", "virtual-bmh-test-ee"})
        for name in ("virtual-bmh-test-01-bmc", "virtual-bmh-test-02-bmc"):
            self.assertIsNone(self._secret_labels(name))
        self.assertIn("==> 0 NetBox devices staged.", result.stdout)

    def test_recreates_secret_with_stale_device_label(self) -> None:
        self._seed_state([_device_entry("virtual-bmh-test-01", 46)])
        # The device was recreated with a new id: the existing Secret
        # still points at the old one.
        self._seed_secret("virtual-bmh-test-01-bmc", {"osac.openshift.io/netbox-device-999": "true"})

        result = self._run(inventory={"name_prefix": "virtual-bmh-test", "hosts": [INVENTORY["hosts"][0]]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        calls = self.calls_file.read_text()
        self.assertIn("delete secret virtual-bmh-test-01-bmc tenant=system", calls)
        self.assertIn("create secret virtual-bmh-test-01-bmc tenant=system", calls)
        self.assertEqual(self._secret_labels("virtual-bmh-test-01-bmc"), {"osac.openshift.io/netbox-device-46": "true"})

    def test_plain_http_url_requires_opt_in(self) -> None:
        result = self._run(extra_args=("--url", "http://netbox.example.test"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("--insecure", result.stderr)
        # The refusal happens before any connection attempt.
        self.assertFalse(self.netbox_calls_file.exists())

        result = self._run(extra_args=("--url", "http://netbox.example.test", "--insecure"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            [call for call in self._netbox_calls() if call["op"] == "session"], [{"op": "session", "verify": False}]
        )

    def test_env_vars_configure_the_connection(self) -> None:
        result = self._run(
            flags=[],
            env_extra={"OSAC_NETBOX_URL": "https://env.example.test", "OSAC_NETBOX_TOKEN": "env-token"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            [call for call in self._netbox_calls() if call["op"] == "api"],
            [{"op": "api", "url": "https://env.example.test", "token": "env-token"}],
        )


if __name__ == "__main__":
    unittest.main()

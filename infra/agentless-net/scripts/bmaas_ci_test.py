#!/usr/bin/env python3
"""Contract tests for the virtual-BMH AgentlessNet CI environment."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bmaas_inventory  # noqa: E402
import prove_bmi  # noqa: E402
import sanitize_diagnostics  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "infra" / "agentless-net"
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-bmaas-agentless-net-full-install.yml"
CALLER = ROOT / ".github" / "workflows" / "e2e-bmaas-agentless-net-full-install-caller.yml"
PR_SUITE = ROOT / ".github" / "workflows" / "e2e-pr-bmaas-agentless-net.yml"
CLEANUP = ROOT / ".github" / "workflows" / "e2e-pr-cancel.yml"


class InventoryTest(unittest.TestCase):
    def test_access_port_is_not_a_trunk_and_no_secrets_are_embedded(self) -> None:
        document = bmaas_inventory.inventory_document(mgmt_prefix="192.168.160", host_name="virtual-bmh-1")
        rendered = bmaas_inventory.render_inventory(mgmt_prefix="192.168.160", host_name="virtual-bmh-1")

        self.assertNotIn("password", rendered)
        self.assertNotIn("private_key", rendered)
        host_ports = document["all"]["vars"]["agentless_net_host_ports"]
        self.assertEqual(host_ports["virtual-bmh-1"]["eth9"], {"switch": "leaf-1", "port": "swp2"})
        second = bmaas_inventory.inventory_document(
            mgmt_prefix="192.168.160", host_name="virtual-bmh-1", switch="leaf-2", port="swp2"
        )
        self.assertEqual(
            second["all"]["vars"]["agentless_net_host_ports"]["virtual-bmh-1"]["eth9"],
            {"switch": "leaf-2", "port": "swp2"},
        )
        with self.assertRaises(ValueError):
            bmaas_inventory.inventory_document(mgmt_prefix="192.168.160", host_name="virtual-bmh-1", switch="leaf-9", port="swp2")
        switches = document["all"]["children"]["switches"]["hosts"]
        for switch in switches.values():
            self.assertNotIn("swp2", switch["trunk_ports"])
        net_nodes = document["all"]["children"]["net_nodes"]["hosts"]
        self.assertEqual(list(net_nodes), ["network-node"])
        self.assertEqual(net_nodes["network-node"]["ansible_host"], "192.168.160.30")
        self.assertEqual(document["all"]["vars"]["agentless_net_provisioning_network"]["vlan_id"], 2)

    def test_prefix_trailing_dot_is_not_doubled(self) -> None:
        document = bmaas_inventory.inventory_document(mgmt_prefix="10.0.0.", host_name="host-a")
        self.assertEqual(
            document["all"]["children"]["switches"]["hosts"]["leaf-1"]["ansible_host"],
            "10.0.0.11",
        )


class ProofPayloadTest(unittest.TestCase):
    def test_attachment_is_the_single_fabric_port(self) -> None:
        body = prove_bmi.bmi_create_body(
            name="bmaas-proof",
            tenant="osac-e2e-ci",
            subnet_id="subnet-1",
            catalog_name="bmaas-proof",
            instance_type_name="bmaas-agentless-ci",
            disk_image_name="bmaas-proof-image",
        )
        self.assertNotIn("ssh_public_key", body["object"]["spec"])
        self.assertNotIn("ssh_key", body["object"]["spec"])
        spec = body["object"]["spec"]
        self.assertFalse(spec["catalog_item"]["shared"])
        self.assertTrue(spec["instance_type"]["shared"])
        self.assertFalse(spec["disk_image"]["shared"])
        attachments = body["object"]["spec"]["network_attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["interface"], "eth9")
        self.assertTrue(attachments[0]["primary"])
        self.assertEqual(attachments[0]["subnet"], {"id": "subnet-1"})
        self.assertNotIn("security_groups", attachments[0])

    def test_address_and_ready_state(self) -> None:
        ready = {"status": {"state": "BARE_METAL_INSTANCE_STATE_READY", "network_attachments": [{"ip_address": "10.100.1.193"}]}}
        camel = {"status": {"state": "STATE_READY", "networkAttachmentStatuses": [{"ipAddress": "10.100.1.50"}]}}
        self.assertEqual(prove_bmi.discovered_address(ready), "10.100.1.193")
        self.assertEqual(prove_bmi.discovered_address(camel), "10.100.1.50")
        self.assertTrue(prove_bmi.is_ready(ready))
        running = {"status": {"state": "BARE_METAL_INSTANCE_STATE_RUNNING", "network_attachments": [{"ip_address": "10.100.1.193"}]}}
        self.assertTrue(prove_bmi.is_bmi_running(running))
        self.assertFalse(prove_bmi.is_bmi_running(ready))
        self.assertFalse(prove_bmi.is_ready(running))
        self.assertEqual(prove_bmi.discovered_address(running), "10.100.1.193")
        proto = {"status": {"network_attachment_statuses": [{"ip_address": "10.100.1.194"}]}}
        self.assertEqual(prove_bmi.discovered_address(proto), "10.100.1.194")
        command = prove_bmi.grpcurl_command(
            address="api.example:443", token="token", ca_file="/tmp/ca.crt", service="osac.public.v1.BareMetalInstances/Get"
        )
        self.assertNotIn("-insecure", command)
        self.assertIn("-cacert", command)
        rendered = " ".join(command)
        self.assertIn("-expand-headers", command)
        self.assertIn("${OSAC_GRPC_TOKEN}", rendered)
        self.assertNotIn("Bearer token", rendered)
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_NOT_READY"}}))
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_PROVISIONING"}}))
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_FAILED"}}))
        self.assertEqual(prove_bmi.discovered_address({"status": {}}), "")

    def test_timeout_does_not_include_the_request(self) -> None:
        test_token = "super-secret-token"
        api = prove_bmi.Api(address="api.example:443", token=test_token, ca_file="/tmp/ca.crt", call_timeout=1)

        def expire(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

        with mock.patch("prove_bmi.subprocess.run", side_effect=expire):
            with self.assertRaises(prove_bmi.CallTimeout) as caught:
                api.call("osac.public.v1.BareMetalInstances/Get")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(test_token, str(caught.exception))

    def test_unavailable_is_retried_and_other_status_codes_are_not(self) -> None:
        class Flaky:
            def __init__(self, code: str) -> None:
                self.code = code
                self.calls = 0

            def call(self, service: str, data: dict | None = None) -> dict:
                self.calls += 1
                if self.code == "Unavailable" and self.calls >= 3:
                    return {"object": {"status": {"state": "VIRTUAL_NETWORK_STATE_READY"}}}
                raise prove_bmi.GrpcError(service, f"code = {self.code}", self.code)

        ready = Flaky("Unavailable")
        with mock.patch("prove_bmi.time.sleep"):
            prove_bmi.wait_for_state(ready, "svc", "id-1", timeout_seconds=30, interval_seconds=1)
        self.assertEqual(ready.calls, 3)

        missing = Flaky("NotFound")
        with mock.patch("prove_bmi.time.sleep"):
            with self.assertRaises(prove_bmi.GrpcError):
                prove_bmi.wait_for_state(missing, "svc", "id-1", timeout_seconds=30, interval_seconds=1)
        self.assertEqual(missing.calls, 1)

    def test_call_records_the_grpc_status_code(self) -> None:
        test_token = "super-secret-token"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"rpc error: code = Unavailable desc = unreachable {test_token}",
        )
        api = prove_bmi.Api(address="api.example:443", token=test_token, ca_file="/tmp/ca.crt")
        with mock.patch("prove_bmi.subprocess.run", return_value=completed):
            with self.assertRaises(prove_bmi.GrpcError) as caught:
                api.call("osac.public.v1.VirtualNetworks/Get")
        self.assertEqual(caught.exception.code, "Unavailable")
        self.assertNotIn(test_token, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_successful_delete_removes_the_state_file(self) -> None:
        test_token = "not-used"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"vnet_id": "vnet-1"}')
            with (
                mock.patch("prove_bmi._api_address", return_value="api.example:443"),
                mock.patch("prove_bmi._token", return_value=test_token),
                mock.patch("prove_bmi.ingress_ca_file", return_value="/tmp/ca.crt"),
                mock.patch.object(prove_bmi.Api, "call", return_value={}),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = prove_bmi.main(["--delete", "--state-file", str(state_path)])
            self.assertEqual(code, 0)
            self.assertFalse(state_path.exists())

    def test_failed_delete_keeps_the_state_file(self) -> None:
        test_token = "not-used"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"vnet_id": "vnet-1"}')
            with (
                mock.patch("prove_bmi._api_address", return_value="api.example:443"),
                mock.patch("prove_bmi._token", return_value=test_token),
                mock.patch("prove_bmi.ingress_ca_file", return_value="/tmp/ca.crt"),
                mock.patch.object(prove_bmi.Api, "call", side_effect=RuntimeError("api rejected the delete")),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = prove_bmi.main(["--delete", "--state-file", str(state_path)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(state_path.read_text())["vnet_id"], "vnet-1")

    def test_interrupted_checkpoint_keeps_the_previous_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"vnet_id": "kept"}')
            with mock.patch("prove_bmi.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    prove_bmi._save_state(path, {"vnet_id": "kept", "subnet_id": "new"})
            self.assertEqual(json.loads(path.read_text()), {"vnet_id": "kept"})
            self.assertEqual(list(Path(tmp).glob(".state.json.*.tmp")), [])

    def test_retry_reuses_a_recorded_network(self) -> None:
        class RecordingApi:
            def __init__(self) -> None:
                self.services: list[str] = []

            def call(self, service: str, data: dict | None = None) -> dict:
                self.services.append(service)
                if service.endswith("/Get"):
                    return {"object": {"metadata": {"name": "kept"}, "status": {"state": "VIRTUAL_NETWORK_STATE_READY"}}}
                return {"object": {"id": "created-id", "metadata": {"name": "created"}}}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"vnet_id": "vnet-kept"}')
            api = RecordingApi()
            result = prove_bmi.create_proof(
                api,
                tenant="osac-e2e-ci",
                disk_image_ref="oci://example/image:1",
                template_name="bm-host-provisioning",
                state_path=state_path,
            )
        self.assertEqual(result["vnet_id"], "vnet-kept")
        self.assertNotIn(f"{prove_bmi.PUBLIC_API}.VirtualNetworks/Create", api.services)
        self.assertIn(f"{prove_bmi.PUBLIC_API}.Subnets/Create", api.services)

    def test_delete_reports_a_failed_resource(self) -> None:
        class FailingApi:
            def call(self, service: str, data: dict | None = None) -> dict:
                raise RuntimeError("api rejected the delete")

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(prove_bmi.delete_proof(FailingApi(), {"bmi_id": "bmi-1", "vnet_id": "vnet-1"}))

    def test_partial_delete_forgets_only_removed_ids(self) -> None:
        class Selective:
            def call(self, service: str, data: dict | None = None) -> dict:
                if "Catalog" in service:
                    raise RuntimeError("catalog delete failed")
                return {}

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"catalog_id": "cat-1", "vnet_id": "vnet-1", "address": "10.100.1.9"}
            state_path.write_text(json.dumps(state))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(prove_bmi.delete_proof(Selective(), state, state_path))
            remaining = json.loads(state_path.read_text())
        self.assertEqual(remaining["catalog_id"], "cat-1")
        self.assertNotIn("vnet_id", remaining)
        self.assertEqual(remaining["address"], "10.100.1.9")

    def test_not_found_delete_drops_the_recorded_id(self) -> None:
        class Missing:
            def call(self, service: str, data: dict | None = None) -> dict:
                raise prove_bmi.GrpcError(service, "Code: NotFound", "NotFound")

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text('{"bmi_id": "bmi-1"}')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(prove_bmi.delete_proof(Missing(), {"bmi_id": "bmi-1"}, state_path))
            self.assertEqual(json.loads(state_path.read_text()), {})

    def test_unavailable_delete_keeps_the_recorded_id(self) -> None:
        class Unavailable:
            def call(self, service: str, data: dict | None = None) -> dict:
                raise prove_bmi.GrpcError(service, "Code: Unavailable", "Unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = {"bmi_id": "bmi-1"}
            state_path.write_text(json.dumps(state))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(prove_bmi.delete_proof(Unavailable(), state, state_path))
            self.assertEqual(json.loads(state_path.read_text())["bmi_id"], "bmi-1")


class SanitizeTest(unittest.TestCase):
    def test_escaped_json_password_key_is_redacted(self) -> None:
        cleaned = sanitize_diagnostics.redact(b'{"pass\\u0077ord":"hunter2"}\n')
        self.assertNotIn(b"hunter2", cleaned)
        self.assertIn(b"[REDACTED]", cleaned)
        with self.assertRaises(RuntimeError) as caught:
            sanitize_diagnostics.redact(b'{"pass\\u12":"hunter2"}\n')
        self.assertIn("unsupported escaped secret key", str(caught.exception))
        structured = sanitize_diagnostics.redact(b'{"Authorization": "super-secret"}\n')
        self.assertNotIn(b"super-secret", structured)
        self.assertIn(b"[REDACTED]", structured)
        split_value = sanitize_diagnostics.redact(b'{"password":\n  "hunter2"\n}\n')
        self.assertNotIn(b"hunter2", split_value)
        self.assertIn(b"[REDACTED]", split_value)
        nested = sanitize_diagnostics.redact(b'{"password":\n  {\n"credential": "hunter2"\n  }\n}\n')
        self.assertNotIn(b"hunter2", nested)
        self.assertIn(b"[REDACTED]", nested)
        shared_line = sanitize_diagnostics.redact(b'{"password":\n{"a": 1}, "token": "hunter2", "host": "edge35"}\n')
        self.assertNotIn(b"hunter2", shared_line)
        self.assertIn(b"edge35", shared_line)
        self.assertIn(b"[REDACTED]", shared_line)
        later_value = sanitize_diagnostics.redact(b'{"password":\n{"a": 1}, "token":\n"hunter2"}\n')
        self.assertNotIn(b"hunter2", later_value)
        flow = sanitize_diagnostics.redact(b"password:\n  ['rack ] west',\n  'second-secret']\n")
        self.assertNotIn(b"second-secret", flow)
        self.assertNotIn(b"rack ] west", flow)
        mapping = sanitize_diagnostics.redact(b'password:\n  "username": service\n  credential: sample\n')
        self.assertNotIn(b"service", mapping)
        self.assertNotIn(b"sample", mapping)
        plain_flow = sanitize_diagnostics.redact(b"password:\n  [operator's note, backup]\n")
        self.assertNotIn(b"operator's note", plain_flow)
        self.assertNotIn(b"backup", plain_flow)
        self.assertIn(b"[REDACTED]", plain_flow)
        numbered = sanitize_diagnostics.redact(b'{"password":\n{"a": 1}, "token": 123, "host": "edge35"}\n')
        self.assertNotIn(b"123", numbered)
        self.assertIn(b"edge35", numbered)
        split_after = sanitize_diagnostics.redact(
            b'{"password":\n{"a": 1}, "token": "kept-out", "secret":\n"next-secret", "host": "edge35"}\n'
        )
        self.assertNotIn(b"kept-out", split_after)
        self.assertNotIn(b"next-secret", split_after)
        self.assertIn(b"edge35", split_after)
        nested_object = sanitize_diagnostics.redact(b'{"password":{"a":"x","b":"y"},"host":"edge"}\n')
        self.assertNotIn(b'"y"', nested_object)
        self.assertIn(b"edge", nested_object)
        commented = sanitize_diagnostics.redact(b"password:\n  [\n  # note ]\n  'second-secret']\n")
        self.assertNotIn(b"second-secret", commented)
        with self.assertRaises(RuntimeError) as open_value:
            sanitize_diagnostics.redact(b'{"password":\n  {\n"credential": "hunter2"\n')
        self.assertIn("unsupported json secret value", str(open_value.exception))

    def test_private_keys_passwords_and_bearers_are_removed(self) -> None:
        begin = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        end = "-----END " + "OPENSSH PRIVATE KEY-----"
        raw = (
            f"{begin}\n"
            "c2VjcmV0a2V5ZGF0YQ==\n"
            f"{end}\n"
            "password: hunter2\n"
            'PASSWORD="my secret"\n'
            '"password": "hunter2"\n'
            "DB_PASSWORD=hunter2\n"
            "AWS_SECRET_ACCESS_KEY: abc123\n"
            '"aws_secret_access_key": "abc123"\n'
            "Authorization: Bearer abc.def.ghi\n"
            "Authorization: Basic dXNlcjpwYXNz\n"
            "client-key-data: bXlrZXk=\n"
            "bridge sysctl is 0\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            self.assertEqual(sanitize_diagnostics.sanitize_tree(source, destination), 1)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn("c2VjcmV0a2V5ZGF0YQ==", cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("my secret", cleaned)
        self.assertNotIn("abc123", cleaned)
        self.assertNotIn("abc.def.ghi", cleaned)
        self.assertNotIn("dXNlcjpwYXNz", cleaned)
        self.assertNotIn("bXlrZXk=", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)
        self.assertIn("[REDACTED PRIVATE KEY]", cleaned)

    def test_secret_block_scalars_are_removed(self) -> None:
        test_literal = "hunter2"
        test_folded = "folded-secret"
        raw = (
            "password: |\n"
            f"  {test_literal}\n"
            "bridge sysctl is 0\n"
            "token: >\n"
            f"  {test_folded}\n"
            "done: yes\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            self.assertEqual(sanitize_diagnostics.sanitize_tree(source, destination), 1)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_literal, cleaned)
        self.assertNotIn(test_folded, cleaned)
        self.assertIn("password: [REDACTED]", cleaned)
        self.assertIn("token: [REDACTED]", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)
        self.assertIn("done: yes", cleaned)

    def test_unquoted_secret_values_keep_no_trailing_words(self) -> None:
        test_secret = "hunter2"
        raw = f"password: {test_secret} still secret\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_secret, cleaned)
        self.assertNotIn("still secret", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_plain_secret_continuation_is_not_published(self) -> None:
        test_secret = "second-half"
        raw = f"password: first-half\n  {test_secret}\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn("first-half", cleaned)
        self.assertNotIn(test_secret, cleaned)
        self.assertIn("password: [REDACTED]", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_blank_line_does_not_publish_a_secret_continuation(self) -> None:
        test_secret = "second-half"
        raw = f"password: first-half\n\n  {test_secret}\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn("first-half", cleaned)
        self.assertNotIn(test_secret, cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_doubled_single_quotes_stay_inside_the_secret(self) -> None:
        test_suffix = "credential-suffix"
        raw = f"password: 'prefix''{test_suffix}'\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn("prefix", cleaned)
        self.assertNotIn(test_suffix, cleaned)
        self.assertIn("password: '[REDACTED]'", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_later_multiline_assignment_on_the_same_line_is_rejected(self) -> None:
        test_secret = "hunter2"
        raw = '{"password": "closed", "token": {\n  "value": "' + test_secret + '"\n}}\n'
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            source.mkdir()
            (source / "job.log").write_text(raw)
            with self.assertRaises(RuntimeError) as caught:
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")
            self.assertIn("multiline", str(caught.exception))
            destination = Path(tmp) / "clean"
            if destination.exists():
                for path in destination.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(test_secret, path.read_text())

    def test_explicit_block_indent_is_redacted_and_unknown_headers_fail(self) -> None:
        test_literal = "indented-secret"
        test_folded = "folded-secret"
        raw = f"password: |2\n  {test_literal}\nbridge sysctl is 0\ntoken: >2-\n  {test_folded}\ndone: yes\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_literal, cleaned)
        self.assertNotIn(test_folded, cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)
        self.assertIn("done: yes", cleaned)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            source.mkdir()
            (source / "job.log").write_text("password: |10\n  leftover\n")
            with self.assertRaises(RuntimeError):
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")

    def test_single_quoted_secret_keys_are_redacted(self) -> None:
        test_secret = "quoted-secret"
        raw = f"'password': '{test_secret}'\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_secret, cleaned)
        self.assertIn("'password': '[REDACTED]'", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_diagnostic_symlinks_are_rejected(self) -> None:
        test_outside_secret = "outside-secret"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "logs"
            source.mkdir()
            outside = root / "outside.txt"
            outside.write_text(test_outside_secret)
            (source / "linked.log").symlink_to(outside)
            with self.assertRaises(RuntimeError) as caught:
                sanitize_diagnostics.sanitize_tree(source, root / "clean")
            self.assertIn("symlink", str(caught.exception))
            published = (root / "clean").rglob("*") if (root / "clean").exists() else []
            for path in published:
                if path.is_file() and not path.is_symlink():
                    self.assertNotIn(test_outside_secret, path.read_text())
            with self.assertRaises(RuntimeError) as caught:
                sanitize_diagnostics._diagnostic_bytes(source.resolve(), outside)
            self.assertIn("outside", str(caught.exception))

    def test_multiline_secret_values_are_rejected(self) -> None:
        second = "second-half"
        samples = (
            f'password: "first-half\n  {second}"\n',
            f"password: 'first-half\n  {second}'\n",
            "password: {\n  value: " + second + "\n}\n",
        )
        for raw in samples:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "logs"
                source.mkdir()
                (source / "job.log").write_text(raw)
                with self.assertRaises(RuntimeError) as caught:
                    sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")
                self.assertIn("multiline", str(caught.exception))
                destination = Path(tmp) / "clean"
                if destination.exists():
                    for path in destination.rglob("*"):
                        if path.is_file():
                            self.assertNotIn(second, path.read_text())
        closed = sanitize_diagnostics.redact(b'password: "closed-secret"\npassword: {inline: closed-secret}\n')
        self.assertNotIn(b"closed-secret", closed)

    def test_long_identifier_line_does_not_stall_redaction(self) -> None:
        payload = b"a" * 8192 + b"\n"
        started = time.monotonic()
        self.assertEqual(sanitize_diagnostics.redact(payload), payload)
        self.assertLess(time.monotonic() - started, 1)
        prefixed = (b"a" * 64) + b"password: hunter2\n"
        self.assertNotIn(b"hunter2", sanitize_diagnostics.redact(prefixed))

    def test_registry_credentials_are_redacted(self) -> None:
        test_credential = "dXNlcjpwYXNz"
        raw = (
            'DOCKER_AUTH_CONFIG={"auths":{"registry.example":{"auth":"'
            + test_credential
            + '"}}}\nbridge sysctl is 0\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_credential, cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)
        quoted = sanitize_diagnostics.redact(b'"auth": "dXNlcjpwYXNz"\n')
        self.assertNotIn(b"dXNlcjpwYXNz", quoted)
        self.assertIn(b'"auth": "[REDACTED]"', quoted)

    def test_anchored_secret_blocks_are_rejected(self) -> None:
        test_secret = "hunter2"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            source.mkdir()
            (source / "job.log").write_text(f"password: &credential |\n  {test_secret}\n")
            with self.assertRaises(RuntimeError) as caught:
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")
            self.assertIn("anchor", str(caught.exception))
            destination = Path(tmp) / "clean"
            if destination.exists():
                for path in destination.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(test_secret, path.read_text())

    def test_private_key_header_without_an_end_marker_fails(self) -> None:
        begin = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            source.mkdir()
            (source / "job.log").write_text(f"{begin}\npartial\n")
            with self.assertRaises(RuntimeError):
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")

    def test_pgp_private_key_is_redacted_and_an_open_header_fails(self) -> None:
        begin = "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----"
        end = "-----END " + "PGP PRIVATE KEY BLOCK-----"
        material = "pgp-key-material"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(f"{begin}\n{material}\n{end}\nbridge sysctl is 0\n")
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
            self.assertNotIn(material, cleaned)
            self.assertNotIn(begin, cleaned)
            self.assertIn("bridge sysctl is 0", cleaned)
            (source / "open.log").write_text(f"{begin}\npartial\n")
            with self.assertRaises(RuntimeError):
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean-open")

    def test_nested_secret_mappings_are_redacted(self) -> None:
        test_secret = "nested-secret"
        raw = f"password:\n  value: {test_secret}\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_secret, cleaned)
        self.assertNotIn("value:", cleaned)
        self.assertIn("password: [REDACTED]", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_sequence_secret_entries_are_redacted(self) -> None:
        test_secret = "hunter2"
        raw = f"- password:\n    value: {test_secret}\nbridge sysctl is 0\n"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(raw)
            sanitize_diagnostics.sanitize_tree(source, destination)
            cleaned = (destination / "job.log").read_text()
        self.assertNotIn(test_secret, cleaned)
        self.assertNotIn("value:", cleaned)
        self.assertIn("- password: [REDACTED]", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)

    def test_secret_key_without_a_value_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            source.mkdir()
            (source / "job.log").write_text("password:\nbridge sysctl is 0\n")
            with self.assertRaises(RuntimeError) as caught:
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")
        self.assertIn("secret key has no value", str(caught.exception))

    def test_a_flow_secret_without_a_sibling_does_not_fail_the_upload(self) -> None:
        cleaned = sanitize_diagnostics.redact(b'{"password":{"a":"closed-secret"}}\n')
        self.assertNotIn(b"closed-secret", cleaned)
        self.assertIn(b'"[REDACTED]"', cleaned)

    def test_a_bracket_phrase_inside_a_string_is_prose(self) -> None:
        raw = b'{"message":"unable to read password: [permission denied"}\n'
        cleaned = sanitize_diagnostics.redact(raw)
        self.assertIn(b"permission denied", cleaned)
        assigned = sanitize_diagnostics.redact(b'command: "make install PASSWORD=[hunter2"\n')
        self.assertNotIn(b"hunter2", assigned)
        self.assertIn(b"make install", assigned)
        self.assertIn(b"[REDACTED]", assigned)
        bracketed = sanitize_diagnostics.redact(b'msg: "password: [hunter2]"\n')
        self.assertNotIn(b"hunter2", bracketed)
        self.assertIn(b"[REDACTED]", bracketed)
        extended = sanitize_diagnostics.redact(b'msg: "password: [permission denied hunter2"\n')
        self.assertNotIn(b"hunter2", extended)
        self.assertIn(b"[REDACTED]", extended)

    def test_a_secret_token_inside_a_quoted_string_is_redacted(self) -> None:
        samples = (
            b'msg: "login failed password: hunter2"\n',
            b'command: "make install PASSWORD=hunter2"\n',
            b'{"message":"login failed password: hunter2"}\n',
        )
        for raw in samples:
            cleaned = sanitize_diagnostics.redact(raw)
            self.assertNotIn(b"hunter2", cleaned, raw)
            self.assertIn(b"[REDACTED]", cleaned, raw)
            self.assertIn(b"login failed" if b"login failed" in raw else b"make install", cleaned)

    def test_a_quoted_command_redacts_each_assignment_value(self) -> None:
        hash_value = sanitize_diagnostics.redact(b'command: "make install PASSWORD=#hunter2"\n')
        self.assertNotIn(b"hunter2", hash_value)
        self.assertNotIn(b"#hunter2", hash_value)
        self.assertIn(b"make install", hash_value)
        quoted = sanitize_diagnostics.redact(b"command: 'make install PASSWORD=\"my secret\"'\n")
        self.assertNotIn(b"my secret", quoted)
        self.assertIn(b"make install", quoted)
        paired = sanitize_diagnostics.redact(b'command: "make install PASSWORD=first TOKEN=second"\n')
        self.assertNotIn(b"first", paired)
        self.assertNotIn(b"second", paired)
        self.assertIn(b"make install", paired)
        comma = sanitize_diagnostics.redact(b'command: "make install PASSWORD=alpha,beta TOKEN=sample"\n')
        self.assertNotIn(b"alpha", comma)
        self.assertNotIn(b"beta", comma)
        self.assertNotIn(b"sample", comma)
        self.assertIn(b"make install", comma)

    def test_a_comment_valued_secret_drops_a_same_indent_sequence(self) -> None:
        raw = b"password: # recovery codes\n- first-secret\n- second-secret\nbridge: ok\n"
        cleaned = sanitize_diagnostics.redact(raw)
        self.assertNotIn(b"first-secret", cleaned)
        self.assertNotIn(b"second-secret", cleaned)
        self.assertIn(b"bridge: ok", cleaned)

    def test_a_comment_between_secret_sequence_entries_is_not_a_sibling(self) -> None:
        raw = b"password: # recovery codes\n- first-secret\n# still a code\n- second-secret\nbridge: ok\n"
        cleaned = sanitize_diagnostics.redact(raw)
        self.assertNotIn(b"first-secret", cleaned)
        self.assertNotIn(b"second-secret", cleaned)
        self.assertIn(b"bridge: ok", cleaned)

    def test_an_apostrophe_inside_a_plain_value_does_not_hide_a_secret(self) -> None:
        raw = b'message: operator\'s note, "password": "closed-secret"\nbridge: ok\n'
        cleaned = sanitize_diagnostics.redact(raw)
        self.assertNotIn(b"closed-secret", cleaned)
        self.assertIn(b"operator's note", cleaned)
        self.assertIn(b"bridge: ok", cleaned)

    def test_a_truncated_key_after_an_assignment_is_rejected(self) -> None:
        begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            destination = Path(tmp) / "clean"
            source.mkdir()
            (source / "job.log").write_text(f"PASSWORD=placeholder\n{begin}\nline-one-of-key\n")
            with self.assertRaises(RuntimeError):
                sanitize_diagnostics.sanitize_tree(source, destination)
            published = destination / "job.log"
            if published.exists():
                self.assertNotIn("line-one-of-key", published.read_text())


class WorkflowContractTest(unittest.TestCase):
    def test_manual_run_is_isolated_and_proves_one_bmi(self) -> None:
        caller = yaml.safe_load(CALLER.read_text())
        caller_trigger = caller[True]
        self.assertIn("workflow_dispatch", caller_trigger)
        self.assertEqual(caller_trigger["schedule"][0]["cron"], "15 1 * * *")
        self.assertIn("inputs.pr-sha || github.sha", CALLER.read_text())
        self.assertNotIn("pull_request_target", caller_trigger)
        self.assertNotIn("pull_request", caller_trigger)
        self.assertNotIn("github.event.pull_request", CALLER.read_text())
        self.assertIn("pr-number", caller["concurrency"]["group"])

        suite = yaml.safe_load(PR_SUITE.read_text())
        cleanup = yaml.safe_load(CLEANUP.read_text())
        cleanup_job = cleanup["jobs"]["cancel-stale-run"]
        self.assertTrue(suite["concurrency"]["group"].startswith("e2e-bmaas-agentless-net-full-install-"))
        self.assertTrue(cleanup_job["concurrency"]["group"].startswith("e2e-bmaas-agentless-net-full-install-"))
        self.assertIn("inputs.repo", suite["concurrency"]["group"])
        self.assertIn("inputs.pr-number", suite["concurrency"]["group"])
        self.assertIn("github.event.pull_request.head.repo.full_name", cleanup_job["concurrency"]["group"])
        self.assertIn("github.event.pull_request.number", cleanup_job["concurrency"]["group"])
        self.assertTrue(suite["concurrency"]["cancel-in-progress"])
        self.assertTrue(cleanup_job["concurrency"]["cancel-in-progress"])
        self.assertTrue(all(name.startswith("cancel-") for name in cleanup["jobs"]))
        self.assertNotIn("remove-label", CLEANUP.read_text())
        self.assertIn("cancel-stale-run", cleanup["jobs"])
        for name in (
            "e2e-pr-bmaas.yml",
            "e2e-pr-bmaas-netris.yml",
            "e2e-pr-caas.yml",
            "e2e-pr-vmaas.yml",
            "e2e-pr-vmaas-netris.yml",
            "e2e-pr-stretched-netris.yml",
        ):
            other = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
            self.assertIn("inputs.repo", other["concurrency"]["group"])
            self.assertTrue(other["concurrency"]["cancel-in-progress"])
            self.assertIn(other["concurrency"]["group"].split("-${{", 1)[0], CLEANUP.read_text())

        document = yaml.safe_load(WORKFLOW.read_text())
        self.assertIn("workflow_call", document[True])
        self.assertNotIn("workflow_dispatch", document[True])
        self.assertNotIn("pull_request", document[True])
        self.assertEqual(
            set(document["jobs"]),
            {"authorize", "provision", "push-secrets", "e2e", "teardown", "notify"},
        )
        self.assertFalse(document[True]["workflow_call"]["inputs"]["notify-on-failure"]["default"])
        notify = document["jobs"]["notify"]
        self.assertIn("inputs.notify-on-failure", notify["if"])
        self.assertIn("contains(needs.*.result, 'failure')", notify["if"])
        self.assertEqual(notify["runs-on"], "${{ inputs.orchestrator-runner-label }}")
        self.assertEqual(
            caller["jobs"]["e2e-bmaas-agentless-net"]["with"]["notify-on-failure"],
            "${{ github.event_name == 'schedule' }}",
        )
        self.assertIn("suite-inputs.yml@", caller["jobs"]["verify"]["uses"])
        self.assertEqual(caller["jobs"]["e2e-bmaas-agentless-net"]["needs"], "verify")
        self.assertIn(
            "needs.verify.outputs.fork-association",
            caller["jobs"]["e2e-bmaas-agentless-net"]["with"]["fork-pr-author-association"],
        )
        self.assertNotIn("fork-pr-author", caller_trigger["workflow_dispatch"]["inputs"])
        workflow_text = WORKFLOW.read_text()
        self.assertIn(
            "inputs.pr-repository != '' && (inputs.pr-number == '' || inputs.pr-sha == '')",
            workflow_text,
        )
        self.assertIn("pr-repository requires both", workflow_text)
        self.assertNotIn("id-token", CALLER.read_text())
        self.assertNotIn("attestations", CALLER.read_text())
        self.assertNotIn("id-token", PR_SUITE.read_text())
        self.assertNotIn("attestations", workflow_text)
        self.assertEqual(suite["jobs"]["bmaas-agentless-net"]["with"]["deploy-suite"], "bmaas")
        self.assertIn("needs.provision.outputs.runner-label", workflow_text)
        self.assertIn("./.github/workflows/ec2-runner-provision.yml", workflow_text)
        self.assertIn("./.github/workflows/ec2-runner-teardown.yml", workflow_text)
        self.assertNotIn("test-infra-sha", document["jobs"]["provision"].get("with", {}))
        self.assertNotIn("test-infra-sha", document["jobs"]["teardown"].get("with", {}))
        self.assertNotIn("run-bmaas-agentless.sh", workflow_text)
        self.assertNotIn("destroy-suite", workflow_text)
        self.assertNotIn("sanitize_diagnostics.py", workflow_text)
        self.assertNotIn("attest-build-provenance", workflow_text)

        e2e = document["jobs"]["e2e"]
        self.assertEqual(e2e["needs"], ["provision", "push-secrets"])
        self.assertEqual(e2e["runs-on"], "${{ needs.provision.outputs.runner-label }}")
        self.assertEqual(e2e["timeout-minutes"], 360)
        self.assertEqual(e2e["permissions"], {"contents": "read"})
        steps = e2e["steps"]
        joined = "\n".join(step.get("run", "") + str(step.get("uses", "")) for step in steps)
        self.assertIn("push-agentless-net-secrets.sh", document["jobs"]["push-secrets"]["steps"][-1]["run"])
        for target in (
            "setup-infra",
            "deploy-ocp",
            "deploy-osac",
            "deploy-suite",
            "gather-infra",
            "gather-suite",
        ):
            self.assertIn(f"make {target} INFRA=agentless-net DEPLOY_SUITE=bmaas", joined)
        self.assertIn("build-osac-images.sh", joined)
        self.assertIn("./.github/actions/gather-artifacts", joined)
        self.assertIn("./.github/actions/gather-ec2-runner-artifacts", joined)
        build = (INFRA / "scripts" / "build-osac-images.sh").read_text()
        self.assertIn(
            'export KUBECONFIG="${KUBECONFIG:-${HOME}/.kube/agentless-lab-mgmt.kubeconfig}"',
            build,
        )
        stage = next(step for step in steps if step.get("name") == "Stage Agentless diagnostics for redaction")
        gather = next(step for step in steps if step.get("name") == "Gather and redact artifacts")
        self.assertIn("always()", stage["if"])
        self.assertIn("infra/agentless-net/logs/bmaas", stage["run"])
        self.assertIn("${RUNNER_TEMP}/osac-artifacts/agentless-net", stage["run"])
        self.assertNotIn("osac-ec2-runner-artifacts", stage["run"])
        self.assertLess(steps.index(stage), steps.index(gather))
        host_action = (ROOT / ".github" / "actions" / "gather-ec2-runner-artifacts" / "action.yml").read_text()
        self.assertLess(host_action.index("release_gate.py"), host_action.index("actions/upload-artifact"))
        self.assertIn("withholding them", host_action)
        self.assertNotIn("setup-caas", joined)
        setup = (INFRA / "scripts" / "setup-bmaas.sh").read_text()
        self.assertIn('sh -s -- "$home" "$PUB_KEY"', setup)
        self.assertIn("--patch-file=/dev/stdin", setup)
        self.assertNotIn("AGENTLESS_NET_SSH_PRIVATE_KEY\":\"${KEY_B64}\"}}\"", setup)
        self.assertIn("--switch", setup)
        self.assertIn("br-host2", setup)
        self.assertIn("detach-interface", setup)
        self.assertLess(setup.index('"$nic_source" != "$FABRIC_BRIDGE"'), setup.index("Publishing the AgentlessNet inventory"))
        self.assertLess(setup.index('domain_state" == "running"'), setup.index("detach-interface"))
        self.assertIn('"${live_flags[@]}" --config', setup)
        self.assertIn("<<'EOF'", setup)
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("Deploy OCP from snapshot"), names.index("Build and load component images"))
        self.assertLess(names.index("Build and load component images"), names.index("Deploy OSAC"))
        self.assertLess(names.index("Deploy OSAC"), names.index("Prove one BMI"))
        self.assertLess(names.index("Prove one BMI"), names.index("Gather infra diagnostics"))
        self.assertLess(names.index("Gather infra diagnostics"), names.index("Gather BMaaS diagnostics"))
        self.assertLess(names.index("Gather BMaaS diagnostics"), names.index("Gather and redact artifacts"))
        for step in steps:
            if "timeout-minutes" in step:
                self.assertLessEqual(step["timeout-minutes"], 150)
        deploy_osac = next(step for step in steps if step.get("name") == "Deploy OSAC")
        self.assertIn("FULFILLMENT_SERVICE_IMAGE", deploy_osac["env"])
        self.assertIn(
            "steps.components.outputs.fulfillment_service_image",
            deploy_osac["env"]["FULFILLMENT_SERVICE_IMAGE"],
        )
        self.assertEqual(deploy_osac["env"]["OSAC_BRANCH"], "${{ steps.components.outputs.osac_sha }}")
        self.assertEqual(document["jobs"]["teardown"]["if"], "always()")
        trusted = next(
            step for step in document["jobs"]["push-secrets"]["steps"] if step.get("name") == "Checkout trusted scripts"
        )
        self.assertEqual(trusted["with"]["repository"], "${{ job.workflow_repository }}")
        self.assertEqual(trusted["with"]["ref"], "${{ job.workflow_sha }}")
        self.assertFalse(trusted["with"]["persist-credentials"])
        under_test = next(step for step in steps if step.get("name") == "Checkout the revision under test")
        self.assertNotIn("path", under_test["with"])
        self.assertFalse(under_test["with"]["persist-credentials"])
        self.assertIn("inputs.test-infra-sha", under_test["with"]["ref"])
        self.assertIn("inputs.test-infra-repository", under_test["with"]["repository"])
        privileged = next(
            step for step in document["jobs"]["authorize"]["steps"] if str(step.get("uses", "")).startswith("actions/checkout")
        )
        self.assertEqual(privileged["with"]["repository"], "${{ job.workflow_repository }}")
        self.assertEqual(privileged["with"]["ref"], "${{ job.workflow_sha }}")
        self.assertNotIn("inputs.test-infra", privileged["with"]["repository"])
        self.assertNotIn("inputs.test-infra", privileged["with"]["ref"])
        self.assertFalse(privileged["with"]["persist-credentials"])
        destroy = (INFRA / "scripts" / "destroy-bmaas.sh").read_text()
        self.assertNotIn("--names ", destroy)
        self.assertIn('"${INVENTORY_NAMES[@]}"', destroy)
        self.assertIn("cleanup_failed", destroy)
        self.assertIn("proof resource deletion failed", destroy)
        self.assertIn("failed to read virtual BMH inventory", destroy)
        self.assertIn("failed to delete BareMetalHost", destroy)
        self.assertNotIn("|| true", destroy)
        self.assertLess(destroy.index("failed to read virtual BMH inventory"), destroy.index('if [[ -n "${BMH_NAME:-}" ]]'))
        self.assertEqual(destroy.count("exit 1"), 1)
        self.assertIn('{"metadata":{"finalizers":null}}', destroy)
        self.assertIn("NotFound", destroy)
        delete_bmh = destroy.split("delete_bmh()", 1)[1].split("cleanup_failed=0", 1)[0]
        self.assertLess(delete_bmh.index("wait --for=delete"), delete_bmh.index('strip_bmh_finalizers "$name"'))
        self.assertLess(destroy.index('delete_bmh "$name"'), destroy.index("manage-virtual-bmh-vms.py"))
        bmh_fallback = destroy.split('if [[ -n "${BMH_NAME:-}" ]]', 1)[1]
        self.assertIn('delete_bmh "$BMH_NAME"', bmh_fallback)
        leak_script = (ROOT / ".github" / "scripts" / "comment-pr-leak-findings.sh").read_text()
        self.assertIn("display_title", leak_script)
        self.assertIn("workflow_dispatch", leak_script)
        self.assertIn("SUITE_RUN_VALIDATED", leak_script)
        self.assertLess(leak_script.index('capture("^(?:E2E suite'), leak_script.index("OPEN_PR_COUNT="))
        pattern = re.search(r'capture\("([^"]+)"\)', leak_script)
        self.assertIsNotNone(pattern)
        program = '$title | capture("' + pattern.group(1) + '")'
        for title, expected in (
            ("E2E suite · bmaas-agentless-net · #795 · " + ("a" * 40), {"suite": "bmaas-agentless-net", "pr": "795", "sha": "a" * 40}),
            ("E2E BMaaS Agentless-Net Full Install / PR #795 @ " + ("b" * 40), {"pr": "795", "sha": "b" * 40}),
            ("E2E BMaaS Netris Full Install / PR #12 @ " + ("c" * 40), {"pr": "12", "sha": "c" * 40}),
        ):
            parsed = subprocess.check_output(["jq", "-Rn", "--arg", "title", title, program], text=True)
            identity = json.loads(parsed)
            for key, value in expected.items():
                self.assertEqual(identity[key], value)
        rerun = (ROOT / ".github" / "workflows" / "e2e-pr-rerun.yml").read_text()
        self.assertIn('"E2E BMaaS Agentless-Net PR suite"', rerun)
        self.assertIn("728d1b064cdbfa429c68ce5bc5a35b47b6c127d7", PR_SUITE.read_text())

    def test_host_bridge_sysctl_and_bmaas_overlay_are_separate_from_caas(self) -> None:
        bridges = (INFRA / "scripts" / "prepare-host-bridges.sh").read_text()
        self.assertIn("net.bridge.bridge-nf-call-iptables=0", bridges)
        self.assertIn("net.bridge.bridge-nf-call-ip6tables=0", bridges)
        self.assertIn("br-host1", bridges)
        self.assertIn("leaf1-swp2", bridges)
        self.assertIn("sysctl -n net.bridge.bridge-nf-call-iptables", bridges)
        self.assertIn("sysctl -n net.bridge.bridge-nf-call-ip6tables", bridges)
        self.assertIn("/var/lib/osac-host-bridges/sysctl.prior", bridges)
        self.assertIn('sudo test -e "$STATE_FILE"', bridges)
        self.assertIn('sudo test -L "$STATE_FILE"', bridges)
        self.assertNotIn('[[ -e "$STATE_FILE" ]]', bridges)
        self.assertNotIn('[[ -f "$STATE_FILE" ]]', bridges)
        self.assertLess(bridges.index("not a root-owned private"), bridges.index("net.bridge.bridge-nf-call-iptables=0"))
        self.assertLess(bridges.index("sysctl -n net.bridge.bridge-nf-call-iptables"), bridges.index("net.bridge.bridge-nf-call-iptables=0"))
        self.assertIn("restore_host_bridge_sysctls", bridges)
        restore = bridges.split("restore_host_bridge_sysctls()", 1)[1].split("prepare_failed()", 1)[0]
        removal = restore.index('sudo rm -f "$SYSCTL_FILE" "$STATE_FILE"')
        self.assertLess(restore.index('net.bridge.bridge-nf-call-iptables=${ipt}'), removal)
        self.assertLess(restore.index('net.bridge.bridge-nf-call-ip6tables=${ip6}'), removal)
        self.assertLess(restore.rindex("return 1"), removal)
        corrupt = restore.split("has no saved bridge sysctl values", 1)[1]
        self.assertLess(corrupt.index("return 1"), corrupt.index("sysctl -w"))
        self.assertIn("restore_host_bridge_sysctls || exit", bridges)
        self.assertLess(bridges.index("changed_sysctls=1"), bridges.index("net.bridge.bridge-nf-call-iptables=0"))
        failed = bridges.split("prepare_failed()", 1)[1]
        removal = failed.index('sudo rm -f "$STATE_FILE"')
        self.assertLess(failed.index("ipt_restored"), removal)
        self.assertLess(failed.index("ip6_restored"), removal)
        self.assertIn('"$ipt_restored" -eq 1 && "$ip6_restored" -eq 1 && "$checkpoint_existed" -ne 1', failed)
        self.assertIn("sudo -n true", bridges)
        fabric_teardown = (INFRA / "scripts" / "destroy-fabric.sh").read_text()
        self.assertIn('prepare-host-bridges.sh" restore', fabric_teardown)

        overlay = yaml.safe_load((INFRA / "values-bmaas-overlay.yaml").read_text())
        self.assertEqual(overlay["global"]["networking"]["fabricManager"], "agentless_net")
        self.assertTrue(overlay["global"]["networking"]["provisioningEnabled"])
        self.assertTrue(overlay["aap"]["instanceGroups"]["networkFulfillment"]["enabled"])

        caas_overlay = (INFRA / "values-overlay.yaml").read_text()
        self.assertNotIn("fabricManager", caas_overlay)
        deploy = (INFRA / "scripts" / "deploy-osac.sh").read_text()
        self.assertIn('PROFILE="$OSAC_PROFILE"', deploy)
        self.assertIn("VALUES_FILE=values/agentless-net-ci/values.yaml", deploy)
        self.assertLess(deploy.index('if [[ "${DEPLOY_SUITE:-}" != "bmaas" ]];'), deploy.index("Merging values overlay"))
        self.assertLess(deploy.index('if [[ "${DEPLOY_SUITE:-}" == "bmaas" ]];'), deploy.index("values-bmaas-overlay.yaml"))
        fabric = (INFRA / "scripts" / "deploy-fabric.sh").read_text()
        self.assertLess(fabric.index('"${DEPLOY_SUITE:-}" == "bmaas"'), fabric.index("prepare-host-bridges.sh"))
        self.assertLess(fabric.index("use_kubeconfig"), fabric.index("require_recorded_cluster"))
        self.assertLess(fabric.index("require_recorded_cluster"), fabric.index("deploy -t"))
        self.assertIn("caas-l2-advertisement", fabric)
        self.assertIn("--ignore-not-found", fabric)
        self.assertLess(fabric.index("l2advertisements.metallb.io"), fabric.index("oc get l2advertisement"))
        self.assertLess(fabric.index("caas-l2-advertisement is required"), fabric.index("skipping the MetalLB patch"))
        self.assertIn('"${DEPLOY_SUITE:-caas}" != "bmaas"', fabric)
        self.assertNotIn("osac-bmaas-install-stamp", deploy)
        self.assertNotIn("already running", deploy)
        self.assertLess(deploy.index('sha256sum "$overlay_dst"'), deploy.index("make install"))
        self.assertLess(deploy.index("installed_revision="), deploy.index("make install"))
        self.assertIn("FULFILLMENT_SERVICE_IMAGE", deploy)
        self.assertIn("service.images.service.repository", deploy)
        self.assertIn("aap.bootstrap.image", deploy)
        self.assertIn("--set-string ${tag_key}=${tag}", deploy)
        self.assertIn('fetch origin "$branch"', deploy)
        self.assertIn("fetch --prune --no-tags origin", deploy)
        self.assertNotIn("fetch --prune --tags origin", deploy)
        self.assertNotIn("git clone --branch", deploy)
        self.assertNotIn('rev-parse --verify --quiet "${OSAC_BRANCH}', deploy)
        self.assertIn(".effective-kubeconfig", deploy)
        self.assertIn("use_kubeconfig", deploy)
        helper = (INFRA / "scripts" / "use-kubeconfig.sh").read_text()
        self.assertLess(helper.index("OSAC_KUBECONFIG_FROM_CALLER"), helper.index("unset KUBECONFIG"))
        self.assertLess(helper.index("unset KUBECONFIG"), helper.index('source "$recorded"'))
        self.assertIn("IFS=':'", helper)
        self.assertIn('KUBECONFIG="${KUBECONFIG:-$HOME/.kube/${MGMT_CLONE_NAME:-agentless-lab-mgmt}.kubeconfig}"', helper)
        self.assertNotIn(".effective-kubeconfig", helper)
        self.assertIn("require_recorded_cluster", helper)
        ocp = (INFRA / "scripts" / "deploy-ocp.sh").read_text()
        self.assertIn("KUBECONFIG=%q", ocp)
        self.assertIn("use_kubeconfig", ocp)
        self.assertLess(ocp.index("use_kubeconfig"), ocp.index('MGMT_VM_NAME="test-infra-cluster-'))
        self.assertIn("require_cluster_tool_kubeconfig", ocp)
        self.assertIn("require_cluster_tool_kubeconfig", deploy)
        self.assertIn("require_recorded_cluster", (INFRA / "scripts" / "destroy-caas.sh").read_text())
        self.assertIn("require_recorded_cluster", (INFRA / "scripts" / "destroy-fabric.sh").read_text())
        self.assertIn('wait --for=delete', (INFRA / "scripts" / "destroy-bmaas.sh").read_text())
        self.assertNotIn("The proof already copied diagnostics", WORKFLOW.read_text())
        backend = (INFRA / "Makefile").read_text()
        self.assertIn("prepare-suite && $(MAKE) deploy-suite-workload", backend)
        self.assertIn('if [ "$(DEPLOY_SUITE)" = bmaas ]; then $(MAKE) prove-bmi', backend)
        root = (ROOT / "Makefile").read_text()
        root_suite = root.split("deploy-suite: _validate-backend", 1)[1].split("\nrun-tests:", 1)[0]
        self.assertLess(root_suite.index("prepare-suite"), root_suite.index("_validate-suite-contract"))
        self.assertLess(root_suite.index("_validate-suite-contract"), root_suite.index("stage=workload"))
        dispatch = root_suite.split("_validate-suite-contract", 1)[1]
        self.assertIn("exit 0; \\\n", root_suite)
        self.assertLess(dispatch.index("exit 0"), dispatch.index('SUITE_STAGE="$$stage"'))
        self.assertLess(root_suite.index("all|setup"), root_suite.index("prepare-suite"))
        self.assertNotIn('INFRA)" = agentless-net', root_suite)
        run_tests = root.split("run-tests: _validate-backend", 1)[1].split("\ndestroy-ocp:", 1)[0]
        self.assertLess(run_tests.index("does not run the Netris bmaas suite"), run_tests.index("_validate-suite-contract"))
        self.assertNotIn("{.data.profile}", deploy)
        self.assertNotIn('--from-literal=profile="$OSAC_PROFILE"', deploy)
        setup = (INFRA / "scripts" / "setup-bmaas.sh").read_text()
        self.assertLess(setup.index("Reusing the virtual BMH"), setup.index("reconcile-bmhs.py"))
        self.assertLess(setup.index("reconcile-bmhs.py"), setup.index("annotate baremetalhost"))
        self.assertNotIn("apk add --no-cache dnsmasq supervisor", setup)
        setup_infra = (INFRA / "scripts" / "setup-infra.sh").read_text()
        self.assertLess(
            setup_infra.index('"${DEPLOY_SUITE:-}" == "bmaas"'),
            setup_infra.index("Installing grpcurl"),
        )
        self.assertIn("apk add --no-cache dnsmasq supervisor", fabric)
        self.assertIn("/etc/agentless-net/supervisord.conf", fabric)
        self.assertIn("supervisorctl -c /etc/agentless-net/supervisord.conf pid", fabric)
        gather = (INFRA / "scripts" / "gather-bmaas.sh").read_text()
        self.assertIn("supervisorctl -c /etc/agentless-net/supervisord.conf status", gather)
        self.assertIn(".gather-bmaas-complete", gather)
        self.assertLess(gather.index("capture_failed"), gather.index(".gather-bmaas-complete"))
        self.assertLess(gather.index("OSAC_GATHER_RETRY"), gather.index("get baremetalhosts"))
        self.assertLess(gather.index('rm -f "${LOG_DIR}/.gather-bmaas-complete"'), gather.index("get baremetalhosts"))
        runner = (ROOT / "scripts" / "ec2-runner" / "run-bmaas-agentless.sh").read_text()
        self.assertIn("OSAC_GATHER_RETRY=yes", runner)
        self.assertIn(".gather-infra-complete", (INFRA / "scripts" / "gather-infra.sh").read_text())
        self.assertIn("br-host1", gather)
        self.assertIn("br-host2", gather)
        setup = (INFRA / "scripts" / "setup-bmaas.sh").read_text()
        self.assertIn("OSAC_BMI_NETWORK_INTERFACE=eth9", setup)
        self.assertIn("OSAC_BMI_INSTANCE_TYPE=bmaas-agentless-ci", setup)
        self.assertLess(setup.index("Publishing the AgentlessNet inventory"), setup.index("OSAC_BMI_NETWORK_INTERFACE=eth9"))
        makefile = (INFRA / "Makefile").read_text()
        capabilities = (INFRA / "capabilities").read_text()
        self.assertIn('SUPPORTED_DEPLOY_SUITES="caas bmaas"', capabilities)
        self.assertIn("OSAC_PROFILE is derived from DEPLOY_SUITE", makefile)
        self.assertLess(makefile.index("origin OSAC_KUBECONFIG_FROM_CALLER"), makefile.index("origin KUBECONFIG"))
        self.assertIn("deploy-setup: setup-infra", makefile)
        self.assertIn("prepare-suite:", makefile)
        self.assertIn("setup-$(DEPLOY_SUITE)", makefile)
        contract = (INFRA / "contracts" / "bmaas").read_text()
        self.assertIn("OSAC_BMI_INSTANCE_TYPE", contract)
        self.assertIn("OSAC_BMI_NETWORK_INTERFACE", contract)


class CheckoutRefTest(unittest.TestCase):
    def test_a_conflicting_local_tag_does_not_block_the_branch(self) -> None:
        script = INFRA / "scripts" / "deploy-osac.sh"
        git = ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            work = root / "work"
            clone = root / "clone"
            subprocess.check_call(["git", "init", "--bare", "-b", "main", str(remote)])
            subprocess.check_call(["git", "-C", str(remote), "config", "uploadpack.allowReachableSHA1InWant", "true"])
            subprocess.check_call(["git", "init", "-b", "main", str(work)])
            (work / "f").write_text("a\n")
            subprocess.check_call(git + ["-C", str(work), "add", "f"])
            subprocess.check_call(git + ["-C", str(work), "commit", "-m", "a"])
            old = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
            subprocess.check_call(["git", "-C", str(work), "tag", "v1"])
            (work / "f").write_text("b\n")
            subprocess.check_call(git + ["-C", str(work), "commit", "-am", "b"])
            main = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
            subprocess.check_call(["git", "-C", str(work), "remote", "add", "origin", str(remote)])
            subprocess.check_call(["git", "-C", str(work), "push", "origin", "HEAD:refs/heads/main", "refs/tags/v1"])
            subprocess.check_call(["git", "clone", str(remote), str(clone)])
            subprocess.check_call(["git", "-C", str(work), "tag", "-f", "v1"])
            subprocess.check_call(["git", "-C", str(work), "push", "--force", "origin", "refs/tags/v1"])
            conflict = subprocess.run(
                ["git", "-C", str(clone), "fetch", "--tags", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(conflict.returncode, 0, conflict.stderr)

            def checkout(branch: str) -> str:
                env = dict(os.environ)
                env.update(OSAC_REPO=str(remote), OSAC_BRANCH=branch, OSAC_DIR=str(clone))
                completed = subprocess.run(
                    ["bash", str(script), "--checkout-only"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip().splitlines()[-1]

            self.assertEqual(checkout("main"), main)
            self.assertEqual(checkout("v1"), main)
            self.assertEqual(checkout(old), old)


if __name__ == "__main__":
    unittest.main()

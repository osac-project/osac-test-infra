#!/usr/bin/env python3
"""Contract tests for the virtual-BMH AgentlessNet CI environment."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
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


class InventoryTest(unittest.TestCase):
    def test_access_port_is_not_a_trunk_and_no_secrets_are_embedded(self) -> None:
        document = bmaas_inventory.inventory_document(mgmt_prefix="192.168.160", host_name="virtual-bmh-1")
        rendered = bmaas_inventory.render_inventory(mgmt_prefix="192.168.160", host_name="virtual-bmh-1")

        self.assertNotIn("password", rendered)
        self.assertNotIn("private_key", rendered)
        host_ports = document["all"]["vars"]["agentless_net_host_ports"]
        self.assertEqual(host_ports["virtual-bmh-1"]["eth9"], {"switch": "leaf-1", "port": "swp2"})
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
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_NOT_READY"}}))
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_PROVISIONING"}}))
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_FAILED"}}))
        self.assertEqual(prove_bmi.discovered_address({"status": {}}), "")

    def test_timeout_does_not_include_the_request(self) -> None:
        api = prove_bmi.Api(address="api.example:443", token="super-secret-token", ca_file="/tmp/ca.crt", call_timeout=1)

        def expire(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

        with mock.patch("prove_bmi.subprocess.run", side_effect=expire):
            with self.assertRaises(prove_bmi.CallTimeout) as caught:
                api.call("osac.public.v1.BareMetalInstances/Get")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("super-secret-token", str(caught.exception))

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


class SanitizeTest(unittest.TestCase):
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

    def test_private_key_header_without_an_end_marker_fails(self) -> None:
        begin = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "logs"
            source.mkdir()
            (source / "job.log").write_text(f"{begin}\npartial\n")
            with self.assertRaises(RuntimeError):
                sanitize_diagnostics.sanitize_tree(source, Path(tmp) / "clean")


class WorkflowContractTest(unittest.TestCase):
    def test_manual_run_is_isolated_and_proves_one_bmi(self) -> None:
        caller = yaml.safe_load(CALLER.read_text())
        caller_trigger = caller[True]
        self.assertIn("workflow_dispatch", caller_trigger)
        self.assertNotIn("schedule", caller_trigger)
        self.assertIn("labeled", caller_trigger["pull_request_target"]["types"])
        self.assertNotIn("pull_request", caller_trigger)
        self.assertIn("pr-number", caller["concurrency"]["group"])
        self.assertIn("pull_request_target", caller["concurrency"]["cancel-in-progress"])

        document = yaml.safe_load(WORKFLOW.read_text())
        self.assertIn("workflow_call", document[True])
        self.assertNotIn("workflow_dispatch", document[True])
        self.assertNotIn("pull_request", document[True])

        steps = [step["run"] for step in document["jobs"]["e2e"]["steps"] if "run" in step]
        joined = "\n".join(steps)
        self.assertIn("setup-bmaas", joined)
        self.assertIn("prove-bmi", joined)
        self.assertIn("destroy-bmaas", joined)
        self.assertIn("sanitize_diagnostics.py", joined)
        self.assertNotIn("setup-caas", joined)
        self.assertEqual(document["jobs"]["e2e"]["runs-on"], "${{ needs.provision.outputs.runner-label }}")
        self.assertEqual(document["jobs"]["teardown"]["if"], "always()")

        upload = next(step for step in document["jobs"]["e2e"]["steps"] if step.get("name") == "Upload sanitized diagnostics")
        self.assertIn("steps.sanitize.outcome == 'success'", upload["if"])
        checkout = next(step for step in document["jobs"]["e2e"]["steps"] if step.get("name") == "Checkout repository")
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertIn("inputs.test-infra-sha", checkout["with"]["ref"])
        self.assertIn("inputs.test-infra-repository", checkout["with"]["repository"])
        for job_name in ("authorize", "push-secrets"):
            privileged = next(
                step for step in document["jobs"][job_name]["steps"] if str(step.get("uses", "")).startswith("actions/checkout")
            )
            self.assertNotIn("repository", privileged["with"])
            self.assertNotIn("ref", privileged["with"])
            self.assertFalse(privileged["with"]["persist-credentials"])
        self.assertNotIn("test-infra-sha", document["jobs"]["provision"]["with"])
        self.assertNotIn("test-infra-sha", document["jobs"]["teardown"]["with"])
        destroy = (INFRA / "scripts" / "destroy-bmaas.sh").read_text()
        self.assertNotIn("--names ", destroy)
        self.assertIn('"${INVENTORY_NAMES[@]}"', destroy)
        self.assertIn("cleanup_failed", destroy)
        self.assertIn("proof resource deletion failed", destroy)

    def test_host_bridge_sysctl_and_bmaas_overlay_are_separate_from_caas(self) -> None:
        bridges = (INFRA / "scripts" / "prepare-host-bridges.sh").read_text()
        self.assertIn("net.bridge.bridge-nf-call-iptables=0", bridges)
        self.assertIn("net.bridge.bridge-nf-call-ip6tables=0", bridges)
        self.assertIn("br-host1", bridges)
        self.assertIn("leaf1-swp2", bridges)

        overlay = yaml.safe_load((INFRA / "values-bmaas-overlay.yaml").read_text())
        self.assertEqual(overlay["global"]["networking"]["fabricManager"], "agentless_net")
        self.assertTrue(overlay["global"]["networking"]["provisioningEnabled"])
        self.assertTrue(overlay["aap"]["instanceGroups"]["networkFulfillment"]["enabled"])

        caas_overlay = (INFRA / "values-overlay.yaml").read_text()
        self.assertNotIn("fabricManager", caas_overlay)
        deploy = (INFRA / "scripts" / "deploy-osac.sh").read_text()
        self.assertIn('PROFILE="$OSAC_PROFILE"', deploy)
        self.assertIn("VALUES_FILE=values/agentless-net-ci/values.yaml", deploy)
        self.assertLess(deploy.index('if [[ -z "${OSAC_PROFILE:-}" ]];'), deploy.index("Merging values overlay"))
        fabric = (INFRA / "scripts" / "deploy-fabric.sh").read_text()
        self.assertIn("prepare-host-bridges.sh", fabric)
        self.assertIn("caas-l2-advertisement", fabric)
        self.assertIn("--ignore-not-found", fabric)
        self.assertIn("osac-bmaas-install-stamp", deploy)
        self.assertIn("{.data.profile}", deploy)
        self.assertIn('--from-literal=profile="$OSAC_PROFILE"', deploy)
        setup = (INFRA / "scripts" / "setup-bmaas.sh").read_text()
        self.assertLess(setup.index("Reusing the virtual BMH"), setup.index("reconcile-bmhs.py"))
        self.assertLess(setup.index("reconcile-bmhs.py"), setup.index("annotate baremetalhost"))
        self.assertIn("apk add --no-cache dnsmasq supervisor", setup)
        self.assertIn("/etc/agentless-net/supervisord.conf", setup)
        self.assertIn("supervisorctl -c /etc/agentless-net/supervisord.conf pid", setup)
        gather = (INFRA / "scripts" / "gather-bmaas.sh").read_text()
        self.assertIn("supervisorctl -c /etc/agentless-net/supervisord.conf status", gather)


if __name__ == "__main__":
    unittest.main()

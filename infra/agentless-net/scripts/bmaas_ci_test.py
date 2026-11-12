#!/usr/bin/env python3
"""Contract tests for the virtual-BMH AgentlessNet CI environment."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bmaas_inventory  # noqa: E402
import prove_bmi  # noqa: E402
import sanitize_diagnostics  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "infra" / "agentless-net"
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-bmaas-agentless-net-full-install.yml"


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
            ssh_public_key="ssh-ed25519 AAAA proof",
        )
        attachments = body["object"]["spec"]["network_attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["interface"], "eth9")
        self.assertTrue(attachments[0]["primary"])
        self.assertEqual(attachments[0]["subnet"], {"id": "subnet-1"})
        self.assertNotIn("security_groups", attachments[0])

    def test_address_and_ready_state(self) -> None:
        ready = {"status": {"state": "BARE_METAL_INSTANCE_STATE_READY", "network_attachments": [{"ip_address": "10.100.1.193"}]}}
        camel = {"status": {"state": "STATE_READY", "networkAttachments": [{"ipAddress": "10.100.1.50"}]}}
        self.assertEqual(prove_bmi.discovered_address(ready), "10.100.1.193")
        self.assertEqual(prove_bmi.discovered_address(camel), "10.100.1.50")
        self.assertTrue(prove_bmi.is_ready(ready))
        running = {"status": {"state": "BARE_METAL_INSTANCE_STATE_RUNNING", "network_attachments": [{"ip_address": "10.100.1.193"}]}}
        self.assertTrue(prove_bmi.is_bmi_running(running))
        self.assertFalse(prove_bmi.is_bmi_running(ready))
        self.assertFalse(prove_bmi.is_ready(running))
        self.assertEqual(prove_bmi.discovered_address(running), "10.100.1.193")
        command = prove_bmi.grpcurl_command(
            address="api.example:443", token="token", ca_file="/tmp/ca.crt", service="osac.public.v1.BareMetalInstances/Get"
        )
        self.assertNotIn("-insecure", command)
        self.assertIn("-cacert", command)
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_NOT_READY"}}))
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_PROVISIONING"}}))
        self.assertFalse(prove_bmi.is_ready({"status": {"state": "BARE_METAL_INSTANCE_STATE_FAILED"}}))
        self.assertEqual(prove_bmi.discovered_address({"status": {}}), "")


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
            "Authorization: Bearer abc.def.ghi\n"
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
        self.assertNotIn("abc.def.ghi", cleaned)
        self.assertNotIn("bXlrZXk=", cleaned)
        self.assertIn("bridge sysctl is 0", cleaned)
        self.assertIn("[REDACTED PRIVATE KEY]", cleaned)


class WorkflowContractTest(unittest.TestCase):
    def test_manual_run_is_isolated_and_proves_one_bmi(self) -> None:
        document = yaml.safe_load(WORKFLOW.read_text())
        trigger = document[True]
        self.assertIn("workflow_dispatch", trigger)
        self.assertNotIn("schedule", trigger)
        self.assertNotIn("pull_request", trigger)
        self.assertIn("github.run_id", document["concurrency"]["group"])
        self.assertFalse(document["concurrency"]["cancel-in-progress"])

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


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for guest_ingress_vip_range.py, the CI guest ingress VIP picker.

Run directly: python3 -I .github/scripts/guest_ingress_vip_range_test.py
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Import sibling module without requiring package install.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guest_ingress_vip_range as vip

# The shape cluster-tool writes for a clone network (allocate_subnet()): the
# gateway at .1, the management-cluster VM pinned at .10, DHCP at .128-.254.
CLONE_XML = """<network>
  <name>test-infra-net-caas1234567-caas</name>
  <forward mode='nat'/>
  <bridge name='br-caas1234'/>
  <ip family='ipv4' address='192.168.163.1' prefix='24'>
    <dhcp>
      <range start='192.168.163.128' end='192.168.163.254'/>
      <host mac='52:54:00:aa:bb:01' name='test-infra-cluster-caas1234-master-0' ip='192.168.163.10'/>
    </dhcp>
  </ip>
</network>
"""


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = vip.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(directory: str, name: str, text: str) -> str:
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return str(path)


class ParseNetworkTests(unittest.TestCase):
    def test_reads_the_cluster_tool_layout(self) -> None:
        net = vip.parse_network(CLONE_XML)

        self.assertEqual(vip._ip(net.network), "192.168.163.0")
        self.assertEqual(net.prefix, 24)
        self.assertEqual(vip._ip(net.gateway), "192.168.163.1")
        self.assertEqual(vip._ip(net.dhcp_first), "192.168.163.128")
        self.assertEqual(vip._ip(net.dhcp_last), "192.168.163.254")
        self.assertIn(vip._parse_ipv4("192.168.163.10"), net.reserved)

    def test_rejects_netmask_definitions(self) -> None:
        for netmask in ("255.255.255.0", "255.255.255.128"):
            xml = f"<network><ip family='ipv4' address='192.168.163.1' netmask='{netmask}'/></network>"
            with self.subTest(netmask=netmask), self.assertRaisesRegex(vip.RangeError, "netmask is unsupported"):
                vip.parse_network(xml)

    def test_rejects_multiple_dhcp_ranges_in_either_order(self) -> None:
        ranges = [
            "<range start='192.168.163.128' end='192.168.163.254'/>",
            "<range start='192.168.163.120' end='192.168.163.127'/>",
        ]
        for reverse in (False, True):
            ordered = list(reversed(ranges)) if reverse else ranges
            xml = (
                "<network><ip family='ipv4' address='192.168.163.1' prefix='24'><dhcp>"
                + "".join(ordered)
                + "</dhcp></ip></network>"
            )
            with self.subTest(reverse=reverse), self.assertRaisesRegex(vip.RangeError, "multiple DHCP ranges"):
                vip.parse_network(xml)

    def test_ignores_ipv6_element_and_survives_no_dhcp(self) -> None:
        xml = (
            "<network><ip family='ipv6' address='fd00::1' prefix='64'/>"
            "<ip family='ipv4' address='192.168.170.1' prefix='24'/></network>"
        )
        net = vip.parse_network(xml)

        self.assertEqual(net.dhcp_first, None)
        self.assertEqual(vip._ip(net.gateway), "192.168.170.1")

    def test_only_ipv4_ip_hosts_are_reserved(self) -> None:
        xml = (
            "<network>"
            "<ip family='ipv6' address='fd00::1' prefix='64'>"
            "<dhcp><host id='guest-v6' ip='fd00::10'/></dhcp></ip>"
            "<dns><host ip='fd00::11'><hostname>other.example</hostname></host></dns>"
            "<ip family='ipv4' address='192.168.170.1' prefix='24'>"
            "<dhcp><host mac='52:54:00:aa:bb:01' ip='192.168.170.10'/></dhcp></ip>"
            "</network>"
        )

        net = vip.parse_network(xml)

        self.assertEqual(net.reserved, {vip._parse_ipv4("192.168.170.10")})

    def test_rejects_malformed_input(self) -> None:
        for xml in ("not xml", "<network/>", "<network><ip family='ipv4'/></network>"):
            with self.subTest(xml=xml), self.assertRaises(vip.RangeError):
                vip.parse_network(xml)

    def test_rejects_inverted_dhcp_range(self) -> None:
        xml = (
            "<network><ip family='ipv4' address='192.168.170.1' prefix='24'>"
            "<dhcp><range start='192.168.170.200' end='192.168.170.150'/></dhcp></ip></network>"
        )
        with self.assertRaises(vip.RangeError):
            vip.parse_network(xml)


class SelectBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.net = vip.parse_network(CLONE_XML)

    def test_block_sits_directly_below_the_dhcp_pool(self) -> None:
        first, last = vip.select_block(self.net, 4)

        self.assertEqual((vip._ip(first), vip._ip(last)), ("192.168.163.124", "192.168.163.127"))

    def test_block_size_is_honoured(self) -> None:
        for size in (1, 2, 3, 8):
            with self.subTest(size=size):
                first, last = vip.select_block(self.net, size)
                self.assertEqual(last - first + 1, size)

    def test_single_address_block_is_valid_input(self) -> None:
        first, last = vip.select_block(self.net, 1)

        self.assertEqual(vip._ip(first), "192.168.163.127")
        self.assertEqual(first, last)

    def test_reserved_addresses_are_skipped(self) -> None:
        # A pin at the top of the static window pushes the block down.
        xml = CLONE_XML.replace("ip='192.168.163.10'", "ip='192.168.163.127'")
        net = vip.parse_network(xml)

        first, last = vip.select_block(net, 4)

        self.assertEqual((vip._ip(first), vip._ip(last)), ("192.168.163.123", "192.168.163.126"))

    def test_leases_are_skipped(self) -> None:
        leased = {vip._parse_ipv4("192.168.163.126"), vip._parse_ipv4("192.168.163.125")}

        first, last = vip.select_block(self.net, 2, leased)

        self.assertEqual((vip._ip(first), vip._ip(last)), ("192.168.163.123", "192.168.163.124"))

    def test_gateway_is_never_selected(self) -> None:
        xml = (
            "<network><ip family='ipv4' address='192.168.171.1' prefix='24'>"
            "<dhcp><range start='192.168.171.4' end='192.168.171.20'/></dhcp></ip></network>"
        )
        net = vip.parse_network(xml)

        _first, last = vip.select_block(net, 1)

        self.assertEqual(vip._ip(last), "192.168.171.3")

    def test_no_room_below_the_pool_fails_clearly(self) -> None:
        # A pool that starts at .2 leaves nothing between the gateway and it.
        xml = (
            "<network><ip family='ipv4' address='192.168.172.1' prefix='24'>"
            "<dhcp><range start='192.168.172.2' end='192.168.172.20'/></dhcp></ip></network>"
        )
        net = vip.parse_network(xml)

        with self.assertRaises(vip.RangeError) as caught:
            vip.select_block(net, 4)

        self.assertIn("no static space", str(caught.exception))

    def test_exhausted_window_reports_the_block_size(self) -> None:
        xml = (
            "<network><ip family='ipv4' address='192.168.173.1' prefix='24'>"
            "<dhcp><range start='192.168.173.5' end='192.168.173.20'/></dhcp></ip></network>"
        )
        net = vip.parse_network(xml)

        with self.assertRaises(vip.RangeError) as caught:
            vip.select_block(net, 4)

        self.assertIn("block of 4 does not fit", str(caught.exception))


class ParseLeasesTests(unittest.TestCase):
    def test_reads_virsh_table_rows(self) -> None:
        table = (
            " Expiry Time           MAC address         Protocol   IP address         "
            "Hostname   Client ID or DUID\n"
            "-------------------------------------------------------------------------------\n"
            " 2026-05-04 10:00:00   52:54:00:11:22:33   ipv4       192.168.163.129/24  -          -\n"
            " 2026-05-04 10:05:00   52:54:00:11:22:44   ipv4       192.168.163.130/24  -          -\n"
        )

        self.assertEqual(
            sorted(vip._ip(address) for address in vip.parse_leases(table)), ["192.168.163.129", "192.168.163.130"]
        )

    def test_empty_output_is_empty(self) -> None:
        self.assertEqual(vip.parse_leases(""), set())


class CommandLineTests(unittest.TestCase):
    def test_prints_only_the_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = _write(directory, "net.xml", CLONE_XML)

            code, out, err = _run(["--network-xml", xml_path])

        self.assertEqual(code, 0, err)
        self.assertEqual(out, "192.168.163.124-192.168.163.127\n")

    def test_explain_writes_to_stderr_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = _write(directory, "net.xml", CLONE_XML)

            code, out, err = _run(["--network-xml", xml_path, "--explain"])

        self.assertEqual(code, 0)
        self.assertEqual(out, "192.168.163.124-192.168.163.127\n")
        self.assertIn("192.168.163.128-192.168.163.254", err)
        self.assertIn("selected 192.168.163.124-192.168.163.127", err)

    def test_leases_file_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            xml_path = _write(directory, "net.xml", CLONE_XML)
            leases = _write(
                directory,
                "leases.txt",
                " 2026-05-04 10:00:00   52:54:00:11:22:33   ipv4       192.168.163.127/24  -  -\n",
            )

            code, out, _ = _run(["--network-xml", xml_path, "--net-leases", leases, "--block-size", "2"])

        self.assertEqual(code, 0)
        # The topmost free run, so the lease at .127 pushes the block to .125-.126.
        self.assertEqual(out, "192.168.163.125-192.168.163.126\n")

    def test_requires_a_clone_name_or_a_network_file(self) -> None:
        previous = vip.os.environ.pop("CLONE_NAME", None)
        try:
            code, out, err = _run([])
        finally:
            if previous is not None:
                vip.os.environ["CLONE_NAME"] = previous

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("CLONE_NAME or --network-xml is required", err)

    def test_missing_network_file_exits_one(self) -> None:
        code, out, err = _run(["--network-xml", "/nonexistent/net.xml"])

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("error:", err)

    def test_unsupported_network_layout_exits_one_without_a_range(self) -> None:
        cases = (
            (
                "<network><ip family='ipv4' address='192.168.163.1' netmask='255.255.255.128'/></network>",
                "netmask is unsupported",
            ),
            (
                "<network><ip family='ipv4' address='192.168.163.1' prefix='24'><dhcp>"
                "<range start='192.168.163.128' end='192.168.163.254'/>"
                "<range start='192.168.163.120' end='192.168.163.127'/></dhcp></ip></network>",
                "multiple DHCP ranges",
            ),
        )
        for xml, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                xml_path = _write(directory, "net.xml", xml)
                code, out, err = _run(["--network-xml", xml_path])

                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn(message, err)

    def test_unavailable_block_exits_one(self) -> None:
        xml = (
            "<network><ip family='ipv4' address='192.168.174.1' prefix='24'>"
            "<dhcp><range start='192.168.174.4' end='192.168.174.20'/></dhcp></ip></network>"
        )
        with tempfile.TemporaryDirectory() as directory:
            xml_path = _write(directory, "net.xml", xml)

            code, out, err = _run(["--network-xml", xml_path])

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("does not fit", err)

    def test_virsh_is_used_when_no_network_file_is_given(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stub = _write(
                directory,
                "virsh",
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *net-dumpxml*)\n"
                "    cat <<'XML'\n"
                f"{CLONE_XML}"
                "XML\n"
                "    ;;\n"
                "  *net-dhcp-leases*)\n"
                "    echo ' 2026-05-04 10:00:00   52:54:00:11:22:33   ipv4       192.168.163.129/24  -  -'\n"
                "    ;;\n"
                "esac\n",
            )
            Path(stub).chmod(0o755)

            code, out, err = _run(["--virsh", stub, "--network", "test-infra-net-caas1234-caas", "--explain"])

        self.assertEqual(code, 0, err)
        self.assertEqual(out, "192.168.163.124-192.168.163.127\n")
        self.assertIn("leased addresses: 192.168.163.129", err)

    def test_virsh_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stub = _write(
                directory, "virsh", "#!/usr/bin/env bash\necho 'failed to connect to the hypervisor' >&2\nexit 1\n"
            )
            Path(stub).chmod(0o755)

            code, out, err = _run(["--virsh", stub, "--network", "test-infra-net-missing"])

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("failed to connect to the hypervisor", err)

    def test_clone_name_derives_the_network_name(self) -> None:
        # The derivation is the whole point of the CI wiring: no clone name, no
        # network name, so --network must come from CLONE_NAME.
        previous = vip.os.environ.get("CLONE_NAME")
        vip.os.environ["CLONE_NAME"] = "caas1234-caas"
        try:
            self.assertEqual(vip.NETWORK_PREFIX + "caas1234-caas", "test-infra-net-caas1234-caas")
        finally:
            if previous is None:
                vip.os.environ.pop("CLONE_NAME", None)
            else:
                vip.os.environ["CLONE_NAME"] = previous


if __name__ == "__main__":
    unittest.main()

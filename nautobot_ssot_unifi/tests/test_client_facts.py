"""Evidence rules for client network/attachment facts (synthetic documentation values)."""

from unittest import TestCase

from nautobot_ssot_unifi.ssot._clients import _ClientHold, _client_facts

VIDS = {"net-iot": 20, "net-default": 1, "net-unk": None}
DEVICES = {
    "sw1": {"mac": "02:00:00:00:00:01"},
    "ap1": {"mac": "02:00:00:00:00:02"},
}


def wired(**over):
    """One synthetic wired client row."""
    return {
        "mac": "02:00:00:00:10:01",
        "is_wired": True,
        "sw_mac": "02:00:00:00:00:01",
        "sw_port": 5,
        "network_id": "net-iot",
        "vlan": 20,
        "last_seen": 1760000000,
        **over,
    }


def wireless(**over):
    """One synthetic wireless client row."""
    return {
        "mac": "02:00:00:00:10:02",
        "is_wired": False,
        "ap_mac": "02:00:00:00:00:02",
        "essid": "ExampleNet",
        "network_id": "net-iot",
        "last_seen": 1760000000,
        **over,
    }


class ClientFactTests(TestCase):
    """Only explicit per-client evidence yields facts."""

    def reason(self, row, devices=None):
        """The hold reason raised for the row."""
        devices = DEVICES if devices is None else devices
        with self.assertRaises(_ClientHold) as caught:
            _client_facts(row, VIDS, devices)
        return caught.exception.reason

    def test_wired_and_wireless_facts(self):
        self.assertEqual(
            _client_facts(wired(), VIDS, DEVICES),
            {
                "mac": "02:00:00:00:10:01",
                "last_seen": 1760000000,
                "network_id": "net-iot",
                "kind": "wired",
                "device": "sw1",
                "port_idx": 5,
            },
        )
        facts = _client_facts(wireless(), VIDS, DEVICES)
        self.assertEqual((facts["kind"], facts["device"], facts["essid"]), ("wireless", "ap1", "ExampleNet"))

    def test_network_comes_from_the_client_report_only(self):
        # An SSID default or an allowed trunk VLAN is not a client network fact.
        self.assertEqual(self.reason(wireless(network_id=None)), "incomplete-membership")
        self.assertEqual(self.reason(wired(network_id="net-missing", vlan=None)), "incomplete-membership")
        self.assertEqual(self.reason(wired(network_id="net-unk")), "invalid-vlan")

    def test_reported_vlan_must_agree_with_network(self):
        self.assertEqual(self.reason(wired(vlan=30)), "incomplete-membership")
        self.assertEqual(self.reason(wired(vlan="20")), "incomplete-membership")
        self.assertEqual(
            _client_facts(wired(network_id="net-default", vlan=None), VIDS, DEVICES)["network_id"], "net-default"
        )

    def test_unresolved_or_ambiguous_attachment_is_held(self):
        self.assertEqual(self.reason(wired(sw_mac="02:00:00:00:00:99")), "unresolved-endpoint")
        self.assertEqual(self.reason(wired(sw_mac="")), "unresolved-endpoint")
        self.assertEqual(self.reason(wired(sw_port=None)), "incomplete-membership")
        duplicate = {**DEVICES, "sw2": {"mac": "02:00:00:00:00:01"}}
        self.assertEqual(self.reason(wired(), duplicate), "unresolved-endpoint")
        self.assertEqual(self.reason(wireless(essid="")), "incomplete-membership")
        self.assertEqual(self.reason(wired(is_wired="yes")), "incomplete-membership")

    def test_stale_or_malformed_report_is_held(self):
        self.assertEqual(self.reason(wired(last_seen=0)), "incomplete-membership")
        self.assertEqual(self.reason(wired(mac="not-a-mac")), "unresolved-endpoint")

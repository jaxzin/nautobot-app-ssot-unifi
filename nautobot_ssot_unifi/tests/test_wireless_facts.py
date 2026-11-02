"""Configured SSID projection: explicit evidence only, held otherwise."""

# White-box regressions intentionally exercise private connector boundaries.
# pylint: disable=protected-access

from unittest import TestCase
from uuid import uuid4

from nautobot_ssot_unifi.ssot._wireless import _wireless_rows

VLAN = uuid4()
NETWORKS = {"net-a": VLAN, "net-held": None}


def wlan(_id="wlan-a", name="Synthetic SSID", **over):
    """One synthetic WPA2/WPA3 transition SSID broadcast by every AP."""
    return {
        "_id": _id,
        "name": name,
        "enabled": True,
        "hide_ssid": False,
        "security": "wpapsk",
        "wpa_mode": "wpa2",
        "wpa3_support": True,
        "wpa3_transition": True,
        "wpa3_enhanced_192": False,
        "networkconf_id": "net-a",
        "ap_group_mode": "all",
        **over,
    }


def project(rows, single_site=True):
    """Project one site's rows."""
    return _wireless_rows(rows, "controller", "site", NETWORKS, single_site)


class WirelessFactTests(TestCase):
    """Verify SSID facts, security mapping, and hold classification."""

    def test_security_mapping_keeps_raw_facts_only_for_transition_modes(self):
        cases = [
            ({}, "WPA3 Personal", True),
            ({"security": "wpaeap"}, "WPA3 Enterprise", True),
            ({"wpa3_support": False, "wpa3_transition": False}, "WPA2 Personal", False),
            ({"security": "wpaeap", "wpa3_support": False, "wpa3_transition": False}, "WPA2 Enterprise", False),
            ({"security": "open", "wpa_mode": "wpa2", "wpa3_support": False, "wpa3_transition": False}, "Open", False),
        ]
        for over, choice, mapped in cases:
            with self.subTest(over=over):
                rows, held = project([wlan(**over)])
                self.assertEqual(held, [])
                self.assertEqual(rows[0]["authentication"], choice)
                raw = rows[0]["source_authentication"]
                if mapped:
                    self.assertEqual(raw["wpa3_transition"], True)
                    self.assertEqual(raw["security"], wlan(**over)["security"])
                else:
                    self.assertIsNone(raw)

    def test_unrecognized_or_incomplete_security_is_held_not_defaulted(self):
        for over in (
            {"wpa3_transition": False},  # pure WPA3: no observed source shape yet
            {"wpa_mode": "auto"},
            {"wpa3_enhanced_192": True},
            {"security": "wep"},
            {"wpa3_transition": None},
        ):
            with self.subTest(over=over):
                rows, held = project([wlan(**over)])
                self.assertEqual(rows, [])
                self.assertEqual(held, [("wlan-a", "wireless-network", "unsupported-security")])

    def test_ssid_facts_and_generated_name(self):
        rows, _held = project([wlan(enabled=False, hide_ssid=True)])
        self.assertEqual(
            {key: rows[0][key] for key in ("name", "ssid", "enabled", "hidden", "vlan_id")},
            {
                "name": "controller/site/Synthetic SSID",
                "ssid": "Synthetic SSID",
                "enabled": False,
                "hidden": True,
                "vlan_id": VLAN,
            },
        )

    def test_assignment_holds_do_not_hold_the_network(self):
        for over, single_site, reason in (
            ({"ap_group_mode": "groups"}, True, "unsupported-scope"),
            ({}, False, "unsupported-scope"),
            ({"networkconf_id": "net-held"}, True, "unresolved-network"),
            ({"networkconf_id": "net-unknown"}, True, "unresolved-network"),
            ({"networkconf_id": None}, True, "unresolved-network"),
        ):
            with self.subTest(over=over, single_site=single_site):
                rows, held = project([wlan(**over)], single_site)
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["vlan_id"])
                self.assertEqual(held, [("wlan-a", "wireless-assignment", reason)])

    def test_invalid_and_duplicate_ssids_are_held(self):
        rows, held = project([wlan("a", "Same"), wlan("b", "Same"), wlan("c", hide_ssid="no"), wlan("d", "x" * 300)])
        self.assertEqual(rows, [])
        self.assertEqual(
            held,
            [
                ("a", "wireless-network", "duplicate-wireless"),
                ("b", "wireless-network", "duplicate-wireless"),
                ("c", "wireless-network", "invalid-wireless"),
                ("d", "wireless-network", "invalid-wireless"),
            ],
        )

    def test_unidentifiable_or_repeated_rows_fail_the_channel(self):
        for rows in ([wlan(_id="")], [wlan(), wlan()], [wlan(), "not-a-row"]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                project(rows)

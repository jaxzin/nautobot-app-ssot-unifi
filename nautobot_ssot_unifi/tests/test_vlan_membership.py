"""Presence-aware VLAN mapping cannot turn omission into removal."""

from unittest import TestCase
from uuid import UUID
from nautobot_ssot_unifi.ssot._vlans import _VlanHold, _networks, _membership


class MembershipTests(TestCase):
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")

    def test_access_all_tagged_selected_and_explicit_removal(self):
        networks = {"a": self.a, "b": self.b}
        self.assertEqual(
            _membership({"native_networkconf_id": "a", "tagged_vlan_mgmt": "block_all"}, {}, [], networks),
            ("access", self.a, []),
        )
        self.assertEqual(
            _membership(
                {"native_networkconf_id": "a", "tagged_vlan_mgmt": "auto", "excluded_networkconf_ids": []},
                {},
                [],
                networks,
            ),
            ("tagged-all", self.a, []),
        )
        self.assertEqual(
            _membership(
                {"native_networkconf_id": "a", "forward": "customize", "tagged_networkconf_ids": ["b"]},
                {},
                [],
                networks,
            ),
            ("tagged", self.a, [self.b]),
        )
        self.assertEqual(
            _membership({"native_networkconf_id": "", "tagged_vlan_mgmt": "block_all"}, {}, [], networks),
            ("access", None, []),
        )

    def test_profile_then_explicit_override_and_missing_profile(self):
        profiles = {"p": {"native_networkconf_id": "a", "tagged_vlan_mgmt": "auto", "excluded_networkconf_ids": []}}
        self.assertEqual(
            _membership(
                {"port_idx": 1, "portconf_id": "p"},
                profiles,
                [{"port_idx": 1, "tagged_vlan_mgmt": "block_all"}],
                {"a": self.a},
            ),
            ("access", self.a, []),
        )
        with self.assertRaises(_VlanHold) as caught:
            _membership({"portconf_id": "absent"}, profiles, [], {})
        self.assertEqual(caught.exception.reason, "missing-profile")

    def test_omission_unresolved_and_duplicate_override_are_not_empty(self):
        for port, overrides in [
            ({}, []),
            ({"tagged_vlan_mgmt": "block_all"}, []),
            ({"native_networkconf_id": "unknown", "tagged_vlan_mgmt": "block_all"}, []),
            ({"native_networkconf_id": "", "tagged_vlan_mgmt": "auto"}, []),
            ({"port_idx": 1}, [{"port_idx": 1}, {"port_idx": 1}]),
        ]:
            with self.subTest(port=port), self.assertRaises(_VlanHold):
                _membership(port, {}, overrides, {})

    def test_explicit_vlan_only_and_duplicate_names_or_vids_held(self):
        rows, held = _networks(
            [
                {"_id": "a", "name": "LAN", "vlan": 10},
                {"_id": "b", "name": "LAN", "vlan": 20},
                {"_id": "c", "name": "untagged"},
            ]
        )
        self.assertEqual(rows, [])
        self.assertEqual({reason for _, reason in held}, {"invalid-vlan", "duplicate-vlan"})
        self.assertEqual(_networks([{"_id": "a", "name": "LAN", "vlan": 10}])[0][0]["vid"], 10)

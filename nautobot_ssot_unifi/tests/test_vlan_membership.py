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

    def test_non_vlan_networks_do_not_generate_invalid_vlan_holds(self):
        rows, held = _networks(
            [
                {"_id": "wan", "name": "Internet", "purpose": "wan"},
                {"_id": "wan2", "name": "Backup", "purpose": "wan2"},
                {"_id": "native", "name": "Untagged", "purpose": "corporate", "vlan_enabled": False},
                {"_id": "tagged", "name": "Tagged", "purpose": "corporate", "vlan_enabled": True, "vlan": 20},
            ]
        )
        self.assertEqual(rows, [{"source_id": "tagged", "vid": 20, "name": "Tagged"}])
        self.assertEqual(held, [])

    def test_non_vlan_names_do_not_poison_tagged_vlan_uniqueness(self):
        rows, held = _networks(
            [
                {"_id": "wan", "name": "Shared", "purpose": "wan"},
                {"_id": "native", "name": "Shared", "vlan_enabled": False},
                {"_id": "tagged", "name": "Shared", "vlan": 20},
            ]
        )
        self.assertEqual(rows, [{"source_id": "tagged", "vid": 20, "name": "Shared"}])
        self.assertEqual(held, [])

    def test_missing_or_malformed_vlan_evidence_still_held(self):
        rows, held = _networks(
            [
                {"_id": "unknown", "name": "Unknown", "purpose": "corporate"},
                {"_id": "disabled-with-tag", "name": "Conflict", "vlan_enabled": False, "vlan": 20},
                {"_id": "invalid", "name": "Invalid", "purpose": "wan", "vlan": "not-a-vid"},
            ]
        )
        self.assertEqual(rows, [])
        self.assertEqual(
            held, [("unknown", "invalid-vlan"), ("disabled-with-tag", "invalid-vlan"), ("invalid", "invalid-vlan")]
        )

    def test_malformed_purpose_does_not_break_vlan_classification(self):
        self.assertEqual(
            _networks([{"_id": "a", "name": "A", "vlan": 20, "purpose": {}}]),
            ([{"source_id": "a", "vid": 20, "name": "A"}], []),
        )

    def test_malformed_name_is_held_without_breaking_other_vlans(self):
        self.assertEqual(
            _networks([{"_id": "a", "name": [], "vlan": 10}, {"_id": "b", "name": "B", "vlan": 20}]),
            ([{"source_id": "b", "vid": 20, "name": "B"}], [("a", "invalid-vlan")]),
        )

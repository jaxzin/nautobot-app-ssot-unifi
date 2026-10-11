"""Exact site/group declarations validate before source collection."""

from unittest import TestCase
from uuid import UUID
from django.core.exceptions import ValidationError
from nautobot_ssot_unifi.ssot._vlans import _group_bindings, _included_sites


class GroupBindingTests(TestCase):
    group = UUID("00000000-0000-0000-0000-000000000001")

    def test_exact_entries_and_disabled_validation(self):
        entries = [{"site_id": "site-a", "vlan_group_id": str(self.group)}]
        self.assertEqual(_group_bindings(entries, existing={self.group}), {"site-a": self.group})
        _included_sites({"site-a": self.group}, ["site-a"])

    def test_invalid_shape_ids_duplicates_and_reused_groups(self):
        entry = {"site_id": "site-a", "vlan_group_id": str(self.group)}
        cases = [
            None,
            {},
            [1],
            [{**entry, "extra": True}],
            [{**entry, "vlan_group_id": "bad"}],
            [entry, entry],
            [entry, {**entry, "site_id": "site-b"}],
            [{**entry, "site_id": ""}],
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError) as caught:
                _group_bindings(value, existing={self.group})
            self.assertIn("vlan_groups", caught.exception.message_dict)
        with self.assertRaises(ValidationError):
            _group_bindings([entry], existing=set())

    def test_unknown_and_new_unbound_site_fail_before_import(self):
        for sites in (["site-b"], ["site-a", "site-b"]):
            with self.assertRaises(ValidationError) as caught:
                _included_sites({"site-a": self.group}, sites)
            self.assertIn("vlan_groups", caught.exception.message_dict)

"""Exact site/group declarations validate before source collection."""

from unittest import TestCase
from uuid import UUID

from django.core.exceptions import ValidationError
from aiounifi.models.site import Site

from nautobot_ssot_unifi.ssot._vlans import _group_bindings, _included_sites, _site_scopes


class GroupBindingTests(TestCase):
    """Verify group binding behavior."""

    group = UUID("00000000-0000-0000-0000-000000000001")
    old_id = "0123456789abcdef01234567"
    current_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"

    def test_network_11_uses_reported_legacy_id_only_for_an_existing_scope(self):
        site = Site({"_id": self.current_id, "legacy_id": self.old_id, "name": "default"})
        self.assertEqual(_site_scopes([site], {self.old_id}), {self.current_id: self.old_id})
        self.assertEqual(_site_scopes([site], {self.current_id}), {self.current_id: self.current_id})
        self.assertEqual(_site_scopes([site], set()), {self.current_id: self.current_id})
        self.assertEqual(site.site_id, self.current_id)
        self.assertEqual(site.raw["legacy_id"], self.old_id)

    def test_site_name_and_non_migration_id_shapes_do_not_establish_continuity(self):
        for row in (
            {"_id": self.current_id, "name": "default"},
            {"_id": self.current_id, "legacy_id": self.second_id, "name": "default"},
        ):
            with self.subTest(row=row):
                scopes = _site_scopes([Site(row)], {self.old_id, self.second_id})
                self.assertEqual(scopes, {self.current_id: self.current_id})

    def test_conflicting_site_id_evidence_is_rejected(self):
        site = Site({"_id": self.current_id, "legacy_id": self.old_id})
        same_legacy = Site({"_id": self.second_id, "legacy_id": self.old_id})
        old_still_live = Site({"_id": self.old_id})
        for sites, recorded in (
            ([site], {self.old_id, self.current_id}),
            ([site, same_legacy], {self.old_id}),
            ([site, old_still_live], {self.old_id}),
            ([site, site], set()),
        ):
            with self.subTest(sites=len(sites)), self.assertRaises(ValidationError):
                _site_scopes(sites, recorded)

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

"""Exact source identities must resolve native UUIDs, never editable names."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

from unittest import TestCase
from uuid import UUID

from nautobot_ssot_unifi.ssot import _bindings
from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _BindingIndex, _interface_id

INTEGRATION = "00000000-0000-0000-0000-000000000001"
OTHER_INTEGRATION = "00000000-0000-0000-0000-000000000002"
TARGET = "00000000-0000-0000-0000-000000000010"
OTHER_TARGET = "00000000-0000-0000-0000-000000000011"


def record(target=TARGET, kind="device", site="site-a", source="device-a", integration=INTEGRATION):
    """Build a synthetic ownership record with exact source identity."""
    return {
        "assigned_object_type__app_label": "dcim",
        "assigned_object_type__model": kind,
        "assigned_object_id": UUID(target),
        "scoped_fields": [],
        "value": {
            "schema_version": 1,
            "integration_id": integration,
            "site_id": site,
            "source_object_id": source,
            "field_paths": [],
        },
    }


class InterfaceIdentityTests(TestCase):
    """Verify interface identity behavior."""

    def test_logical_management_binding_cannot_alias_a_real_nic(self):
        management = _bindings._management_id("device-a")
        self.assertEqual(management, '["device-a","management","config_network"]')
        index = _BindingIndex([record(kind="interface", source=management)])
        self.assertEqual(index.resolve("interface", INTEGRATION, "site-a", management), UUID(TARGET))
        self.assertFalse(_bindings._physical_interface_id(management))
        self.assertTrue(_bindings._physical_interface_id('["device-a","ifname","mgmt"]'))
        with self.assertRaises(_BindingHold):
            index.resolve("interface", INTEGRATION, "site-a", '["device-a","ifname","mgmt"]')

    def test_management_identifier_rejects_invalid_parent_and_arbitrary_channel(self):
        with self.assertRaises(ValueError):
            _bindings._management_id("test-only-sensitive\n")
        for source in ['["device-a","management","other"]', '["device-a", "management", "config_network"]']:
            with self.assertRaises(ValueError):
                _BindingIndex([record(kind="interface", source=source)])

    def test_port_index_wins_over_editable_display_information(self):
        self.assertEqual(_interface_id("device-a", port_idx=3, ifname="eth0"), '["device-a","port_idx",3]')
        self.assertEqual(_interface_id("device-a", port_idx=0), '["device-a","port_idx",0]')

    def test_os_identifier_is_exact_and_collision_free(self):
        self.assertEqual(_interface_id("device-a", ifname="eth0"), '["device-a","ifname","eth0"]')
        self.assertEqual(_interface_id("device/α", ifname='eth"1'), '["device/α","ifname","eth\\"1"]')
        self.assertNotEqual(_interface_id("a/b", ifname="c"), _interface_id("a", ifname="b/c"))

    def test_invalid_identifiers_are_rejected_without_echoing_input(self):
        cases = [
            ("device-a", {"port_idx": True}),
            ("device-a", {"port_idx": -1}),
            ("device-a", {"port_idx": "3"}),
            ("device-a", {}),
            ("device-a", {"ifname": " "}),
            ("device-a", {"ifname": "test-only-sensitive\n"}),
            ("test-only-sensitive\x00", {"port_idx": 3}),
        ]
        for device_id, kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError) as error:
                    _interface_id(device_id, **kwargs)
                self.assertNotIn("test-only-sensitive", str(error.exception))


class BindingIndexTests(TestCase):
    """Verify binding index behavior."""

    def test_site_and_integration_and_model_are_part_of_identity(self):
        records = [record(), record(OTHER_TARGET, site="site-b")]
        index = _BindingIndex(records)
        self.assertEqual(index.resolve("device", INTEGRATION, "site-a", "device-a"), UUID(TARGET))
        self.assertEqual(index.resolve("device", INTEGRATION, "site-b", "device-a"), UUID(OTHER_TARGET))
        for kind, integration, site in [
            ("interface", INTEGRATION, "site-a"),
            ("device", OTHER_INTEGRATION, "site-a"),
            ("device", INTEGRATION, "site-c"),
        ]:
            with self.assertRaises(_BindingHold) as error:
                source_id = '["device-a","port_idx",3]' if kind == "interface" else "device-a"
                index.resolve(kind, integration, site, source_id)
            self.assertEqual(error.exception.reason, "missing-binding")

    def test_duplicate_source_binding_is_held_instead_of_first_match(self):
        index = _BindingIndex([record(), record(OTHER_TARGET)])
        with self.assertRaises(_BindingHold) as error:
            index.resolve("device", INTEGRATION, "site-a", "device-a")
        self.assertEqual(error.exception.reason, "ambiguous-binding")

    def test_duplicate_target_or_foreign_owner_is_invalid_prerequisite(self):
        for second in [record(source="device-b"), record(integration=OTHER_INTEGRATION)]:
            with self.subTest(second=second):
                with self.assertRaises(ValueError):
                    _BindingIndex([record(), second])

    def test_empty_field_paths_do_not_claim_inventory_fields(self):
        index = _BindingIndex([record()])
        self.assertEqual(index.claimed_paths("device", TARGET), frozenset())

    def test_invalid_binding_documents_fail_closed_without_values_in_errors(self):
        mutations = [
            ("schema_version", True),
            ("schema_version", 2),
            ("integration_id", "test-only-sensitive"),
            ("site_id", ""),
            ("source_object_id", "test-only-sensitive\n"),
            ("field_paths", ["software_version", "software_version"]),
            ("field_paths", ["name"]),
            ("field_paths", "software_version"),
        ]
        for field, value in mutations:
            row = record()
            row["value"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as error:
                    _BindingIndex([row])
                self.assertNotIn("test-only-sensitive", str(error.exception))

    def test_native_partial_scope_is_not_a_whole_object_binding(self):
        row = record()
        row["scoped_fields"] = ["software_version"]
        with self.assertRaises(ValueError):
            _BindingIndex([row])

    def test_target_recheck_rejects_foreign_owner_and_retargeting(self):
        index = _BindingIndex([record()])
        cases = [
            (OTHER_INTEGRATION, "site-a", "device-a", "foreign-owner"),
            (INTEGRATION, "site-b", "device-a", "foreign-owner"),
            (INTEGRATION, "site-a", "device-b", "identity-conflict"),
        ]
        for integration, site, source, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(_BindingHold) as error:
                    index.assert_target("device", TARGET, integration, site, source)
                self.assertEqual(error.exception.reason, expected)

    def test_target_recheck_accepts_only_the_unique_exact_binding(self):
        index = _BindingIndex([record()])
        self.assertEqual(index.assert_target("device", TARGET, INTEGRATION, "site-a", "device-a"), UUID(TARGET))
        with self.assertRaises(_BindingHold) as error:
            index.assert_target("device", OTHER_TARGET, INTEGRATION, "site-a", "device-a")
        self.assertEqual(error.exception.reason, "missing-binding")

    def test_interface_bindings_reject_noncanonical_or_label_only_identity(self):
        for source_id in [
            "eth0",
            '["device-a", "port_idx", 3]',
            '["device-a","port_idx",true]',
            '["device-a","label","Port 3"]',
        ]:
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    _BindingIndex([record(kind="interface", source=source_id)])

"""VLAN and membership mutations use native validation and atomic field claims."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotUpdated
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import Interface
from nautobot.extras.models import MetadataType, ObjectMetadata
from nautobot.ipam.models import VLAN, VLANGroup

from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures


class NativeVlanTests(TestCase):
    """Verify VLAN membership writes and preservation of held evidence."""

    # Initialized by the borrowed NativeNamespaceTests.setUp fixture builder.
    job: Any
    integration: Any
    status: Any
    bind = fixtures.NativeNamespaceTests.bind
    address = fixtures.NativeNamespaceTests.address

    def setUp(self):
        fixtures.NativeNamespaceTests.setUp(self)
        self.device, self.interface = fixtures.NativeNamespaceTests.fixture(self)
        self.group = VLANGroup.objects.create(name="site-a")
        self.other = VLANGroup.objects.create(name="site-b")
        MetadataType.objects.get(name="UniFi inventory ownership").content_types.add(
            ContentType.objects.get_for_model(VLAN)
        )
        self.job.sync_vlans = True
        self.job.vlan_groups = [{"site_id": "site-a", "vlan_group_id": str(self.group.pk)}]
        self.job._unifi_vlan_groups = {"site-a": self.group.pk}

    def source(self, rows, port):
        """Build a source adapter from synthetic inventory evidence."""
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        session = self.job._unifi_bindings
        session.plan("device", "site-a", "device-a", {"name": self.device.name})
        interface_id = session.plan(
            "interface", "site-a", '["device-a","ifname","eth0"]', {"device_id": self.device.pk, "name": "eth0"}
        )
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        source.top_level = ("vlan", "interface")
        networks = async_to_sync(source._load_vlans)("site-a", rows)
        interface = source.interface(id=interface_id, label="eth0", device__id=self.device.pk, type="other")
        async_to_sync(source._load_membership)(
            interface, "site-a", '["device-a","ifname","eth0"]', port, {}, [], networks
        )
        source.add(interface)
        return source

    def sync(self, source):
        """Apply the source adapter to the native target and refresh fixtures."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        self.interface.refresh_from_db()
        return target

    def test_native_vlan_membership_preview_apply_repeat_and_explicit_clear(self):
        rows = [{"_id": "net-a", "name": "A", "vlan": 10}, {"_id": "net-b", "name": "B", "vlan": 20}]
        port = {"native_networkconf_id": "net-a", "forward": "customize", "tagged_networkconf_ids": ["net-b"]}
        source = self.source(rows, port)
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.diff_to(target)
        self.assertFalse(VLAN.objects.exists())
        self.sync(source)
        self.assertEqual(self.interface.mode, "tagged")
        self.assertEqual(self.interface.untagged_vlan.vid, 10)
        self.assertEqual(list(self.interface.tagged_vlans.values_list("vid", flat=True)), [20])
        claim = ObjectMetadata.objects.get(
            assigned_object_id=self.interface.pk, metadata_type__name="UniFi inventory ownership"
        )
        self.assertEqual(set(claim.value["field_paths"]), {"mode", "untagged_vlan", "tagged_vlans"})
        self.assertEqual(self.sync(self.source(rows, port)).diff_from(self.source(rows, port)).summary()["update"], 0)
        self.sync(self.source(rows, {"native_networkconf_id": "", "tagged_vlan_mgmt": "block_all"}))
        self.assertEqual(self.interface.mode, "access")
        self.assertIsNone(self.interface.untagged_vlan_id)
        self.assertFalse(self.interface.tagged_vlans.exists())

    def test_verified_native_update_preserves_omitted_tagged_policy(self):
        rows = [
            {"_id": "a", "name": "A", "vlan": 10},
            {"_id": "b", "name": "B", "vlan": 20},
            {"_id": "c", "name": "C", "vlan": 30},
        ]
        self.sync(
            self.source(rows, {"native_networkconf_id": "a", "forward": "customize", "tagged_networkconf_ids": ["b"]})
        )
        source = self.source(rows, {"native_networkconf_id": "c"})
        self.sync(source)
        self.assertEqual(self.interface.untagged_vlan.vid, 30)
        self.assertEqual(self.interface.mode, "tagged")
        self.assertEqual(list(self.interface.tagged_vlans.values_list("vid", flat=True)), [20])
        self.assertTrue(any(row["reason"] == "incomplete-membership" for row in source._held))

    def test_verified_allow_all_preserves_omitted_native_assignment(self):
        rows = [{"_id": "a", "name": "A", "vlan": 10}]
        self.sync(self.source(rows, {"native_networkconf_id": "a", "forward": "native"}))
        self.sync(self.source(rows, {"forward": "all"}))
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertEqual(self.interface.untagged_vlan.vid, 10)
        self.assertFalse(self.interface.tagged_vlans.exists())

    def test_partial_native_update_preserves_foreign_owned_tagged_policy(self):
        rows = [
            {"_id": "a", "name": "A", "vlan": 10},
            {"_id": "b", "name": "B", "vlan": 20},
            {"_id": "c", "name": "C", "vlan": 30},
        ]
        self.sync(self.source(rows, {}))
        self.interface.mode = "tagged"
        self.interface.untagged_vlan = VLAN.objects.get(vid=10)
        self.interface.validated_save()
        self.interface.tagged_vlans.add(VLAN.objects.get(vid=20))
        foreign = MetadataType.objects.create(name="Other source field ownership", data_type="json")
        foreign.content_types.add(ContentType.objects.get_for_model(Interface))
        claim = ObjectMetadata(metadata_type=foreign, assigned_object=self.interface, scoped_fields=[])
        claim.value = {
            "schema_version": 1,
            "integration_id": "00000000-0000-0000-0000-000000000002",
            "field_paths": ["mode", "tagged_vlans"],
        }
        claim.validated_save()
        original = claim.value, claim.last_updated
        self.sync(self.source(rows, {"native_networkconf_id": "c"}))
        claim.refresh_from_db()
        self.assertEqual(self.interface.untagged_vlan.vid, 30)
        self.assertEqual(self.interface.mode, "tagged")
        self.assertEqual(list(self.interface.tagged_vlans.values_list("vid", flat=True)), [20])
        self.assertEqual((claim.value, claim.last_updated), original)

    def test_explicit_default_reference_resolves_to_vlan_one(self):
        rows = [
            {"_id": "native", "name": "Default", "purpose": "corporate", "vlan_enabled": False, "attr_no_delete": True}
        ]
        self.sync(self.source(rows, {"native_networkconf_id": "native", "forward": "all"}))
        self.assertEqual(self.interface.untagged_vlan.vid, 1)
        self.assertEqual(self.interface.mode, "tagged-all")

    def test_source_rename_vid_change_preserves_uuid_and_missing_source_vlan(self):
        self.sync(self.source([{"_id": "net-a", "name": "A", "vlan": 10}], {}))
        original = VLAN.objects.get(name="A").pk
        self.sync(self.source([{"_id": "net-a", "name": "Renamed", "vlan": 11}], {}))
        self.assertEqual(VLAN.objects.get(name="Renamed").pk, original)
        self.assertEqual(VLAN.objects.get(pk=original).vid, 11)
        self.sync(self.source([], {}))
        self.assertTrue(VLAN.objects.filter(pk=original).exists())

    def test_agreeing_allow_all_applies_then_omission_retains_and_explicit_removal_clears(self):
        rows = [{"_id": "net-a", "name": "A", "vlan": 10}, {"_id": "net-b", "name": "B", "vlan": 20}]
        port = {"native_networkconf_id": "net-a", "forward": "all", "tagged_vlan_mgmt": "auto"}
        original_id = self.interface.pk
        source = self.source(rows, port)
        self.assertFalse(source._held)
        self.sync(source)
        self.assertEqual(self.interface.pk, original_id)
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertEqual(self.interface.untagged_vlan.vid, 10)
        self.assertFalse(self.interface.tagged_vlans.exists())
        claim = ObjectMetadata.objects.get(
            assigned_object_id=original_id, metadata_type__name="UniFi inventory ownership"
        )
        self.assertEqual(set(claim.value["field_paths"]), {"mode", "untagged_vlan", "tagged_vlans"})

        omitted = self.source(rows, {"forward": "all", "tagged_vlan_mgmt": "auto"})
        self.assertTrue(any(row["reason"] == "incomplete-membership" for row in omitted._held))
        self.sync(omitted)
        self.assertEqual(self.interface.pk, original_id)
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertEqual(self.interface.untagged_vlan.vid, 10)

        self.sync(self.source(rows, {"native_networkconf_id": "", "tagged_vlan_mgmt": "block_all"}))
        self.assertEqual(self.interface.pk, original_id)
        self.assertEqual(self.interface.mode, "access")
        self.assertIsNone(self.interface.untagged_vlan_id)
        self.assertFalse(self.interface.tagged_vlans.exists())

    def test_wan_config_does_not_poison_complete_tagged_membership(self):
        rows = [
            {"_id": "net-a", "name": "A", "vlan": 10},
            {"_id": "net-b", "name": "B", "vlan": 20},
            {"_id": "wan", "name": "Internet", "purpose": "wan"},
        ]
        port = {"native_networkconf_id": "net-a", "tagged_vlan_mgmt": "custom", "excluded_networkconf_ids": []}
        source = self.source(rows, port)
        self.assertFalse(source._held)
        self.sync(source)
        self.assertEqual(self.interface.mode, "tagged")
        self.assertEqual(self.interface.untagged_vlan.vid, 10)
        self.assertEqual(list(self.interface.tagged_vlans.values_list("vid", flat=True)), [20])
        self.assertEqual(VLAN.objects.count(), 2)

    def test_unidentified_untagged_network_retains_native_but_observes_allow_all(self):
        rows = [{"_id": "native", "name": "Untagged", "purpose": "corporate", "vlan_enabled": False}]
        source = self.source(rows, {"native_networkconf_id": "native", "forward": "all"})
        self.assertEqual(
            [(r["kind"], r["reason"]) for r in source._held], [("vlan-membership", "incomplete-membership")]
        )
        self.sync(source)
        self.assertFalse(VLAN.objects.exists())
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertIsNone(self.interface.untagged_vlan_id)

    def test_foreign_native_vlan_key_is_held_not_adopted(self):
        foreign = VLAN.objects.create(name="Curated", vid=10, vlan_group=self.group, status=self.status)
        source = self.source([{"_id": "net-a", "name": "A", "vlan": 10}], {})
        self.sync(source)
        self.assertEqual(VLAN.objects.count(), 1)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Curated")
        self.assertTrue(any(row["kind"] == "vlan" and row["reason"] == "identity-conflict" for row in source._held))

    def test_incomplete_membership_and_claim_failure_preserve_native_state(self):
        rows = [{"_id": "net-a", "name": "A", "vlan": 10}]
        self.sync(
            self.source(
                rows, {"native_networkconf_id": "net-a", "tagged_vlan_mgmt": "auto", "excluded_networkconf_ids": []}
            )
        )
        source = self.source(rows, {})
        self.assertTrue(any(row["reason"] == "incomplete-membership" for row in source._held))
        self.sync(source)
        self.assertEqual(self.interface.mode, "tagged-all")
        source = self.source(rows, {"native_networkconf_id": "", "tagged_vlan_mgmt": "block_all"})
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        desired = source.get_all("interface")[0]
        with patch.object(ObjectMetadata, "validated_save", side_effect=ValidationError("test-only failure")):
            with self.assertRaises(ObjectNotUpdated):
                target.get("interface", str(self.interface.pk)).update(desired.get_attrs())
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mode, "tagged-all")
        self.assertEqual(self.interface.untagged_vlan.vid, 10)

    def test_static_argument_error_precedes_source_client_construction(self):
        job = UnifiDataSource()
        with patch(
            "nautobot_ssot_unifi.ssot.adapters.Client", side_effect=AssertionError("No source request permitted")
        ):
            with self.assertRaises(ValidationError) as caught:
                job.run(
                    dryrun=True,
                    debug=False,
                    controller=SimpleNamespace(),
                    default_location=None,
                    location_type=None,
                    sync_vlans=True,
                    vlan_groups=[],
                )
        self.assertIn("vlan_groups", caught.exception.message_dict)

    def test_discovered_unbound_site_rejects_collection_before_inventory_changes(self):
        source = self.source([], {})
        source.client = SimpleNamespace(
            get_sites=AsyncMock(return_value=[SimpleNamespace(site_id="site-a"), SimpleNamespace(site_id="new-site")])
        )
        self.job.controller = SimpleNamespace(name="contract-controller")
        before = ObjectMetadata.objects.count()
        with self.assertRaises(ValidationError):
            async_to_sync(source._load_sites)()
        self.assertFalse(VLAN.objects.exists())
        self.assertEqual(ObjectMetadata.objects.count(), before)

    def test_equal_vids_in_different_groups_and_disabled_membership_are_independent(self):
        VLAN.objects.create(name="Other site", vid=10, vlan_group=self.other, status=self.status)
        self.sync(
            self.source(
                [{"_id": "net-a", "name": "A", "vlan": 10}],
                {"native_networkconf_id": "net-a", "tagged_vlan_mgmt": "block_all"},
            )
        )
        self.assertEqual(VLAN.objects.filter(vid=10).count(), 2)
        self.job.sync_vlans = False
        target = UnifiNautobotAdapter(job=self.job)
        self.assertNotIn("vlan", target.top_level)
        self.assertNotIn("mode", target.interface._attributes)
        self.assertEqual(self.interface.untagged_vlan.vlan_group_id, self.group.pk)

    def test_held_source_vlan_cannot_disappear_from_enumerated_membership(self):
        rows = [
            {"_id": "net-a", "name": "A", "vlan": 10},
            {"_id": "net-b", "name": "B", "vlan": 20},
            {"_id": "net-c", "name": "C", "vlan": 30},
        ]
        port = {"native_networkconf_id": "net-a", "tagged_vlan_mgmt": "custom", "excluded_networkconf_ids": ["net-b"]}
        self.sync(self.source(rows, port))
        retained = list(self.interface.tagged_vlans.values_list("pk", flat=True))
        foreign_type = MetadataType.objects.create(name="Other VLAN owner", data_type="json")
        foreign_type.content_types.add(ContentType.objects.get_for_model(VLAN))
        foreign = ObjectMetadata(
            metadata_type=foreign_type, assigned_object=VLAN.objects.get(name="C"), scoped_fields=[]
        )
        foreign.value = {
            "schema_version": 1,
            "integration_id": "00000000-0000-4000-8000-000000000002",
            "field_paths": ["name"],
        }
        foreign.validated_save()
        source = self.source(rows, port)
        self.assertTrue(
            any(row["kind"] == "vlan-membership" and row["reason"] == "incomplete-membership" for row in source._held)
        )
        self.sync(source)
        self.assertEqual(list(self.interface.tagged_vlans.values_list("pk", flat=True)), retained)

    def test_foreign_interface_claim_holds_only_membership(self):

        foreign_type = MetadataType.objects.create(name="Other Interface owner", data_type="json")
        foreign_type.content_types.add(ContentType.objects.get_for_model(Interface))
        foreign = ObjectMetadata(metadata_type=foreign_type, assigned_object=self.interface, scoped_fields=[])
        foreign.value = {
            "schema_version": 1,
            "integration_id": "00000000-0000-4000-8000-000000000002",
            "field_paths": ["mode"],
        }
        foreign.validated_save()
        source = self.source(
            [{"_id": "net-a", "name": "A", "vlan": 10}],
            {"native_networkconf_id": "net-a", "tagged_vlan_mgmt": "block_all"},
        )
        self.assertEqual(source._held[-1]["reason"], "foreign-owner")
        self.assertEqual(source._held[-1]["kind"], "vlan-membership")
        self.sync(source)
        self.assertTrue(VLAN.objects.filter(name="A").exists())
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.mode, "")

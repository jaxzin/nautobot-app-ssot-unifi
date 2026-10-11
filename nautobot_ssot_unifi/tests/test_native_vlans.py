"""VLAN and membership mutations use native validation and atomic field claims."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotUpdated
from nautobot.ipam.models import VLAN, VLANGroup
from nautobot.extras.models import MetadataType, ObjectMetadata
from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures
from nautobot_ssot_unifi.jobs import UnifiDataSource


class NativeVlanTests(TestCase):
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

    def test_source_rename_vid_change_preserves_uuid_and_missing_source_vlan(self):
        self.sync(self.source([{"_id": "net-a", "name": "A", "vlan": 10}], {}))
        original = VLAN.objects.get(name="A").pk
        self.sync(self.source([{"_id": "net-a", "name": "Renamed", "vlan": 11}], {}))
        self.assertEqual(VLAN.objects.get(name="Renamed").pk, original)
        self.assertEqual(VLAN.objects.get(pk=original).vid, 11)
        self.sync(self.source([], {}))
        self.assertTrue(VLAN.objects.filter(pk=original).exists())

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

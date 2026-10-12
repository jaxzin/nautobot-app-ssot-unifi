"""Native per-reporter neighbor transactions preserve physical inventory."""

from datetime import datetime, timedelta, timezone
from django.test import TestCase
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from unittest.mock import patch
from types import SimpleNamespace
from asgiref.sync import async_to_sync
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotCreated
from nautobot.dcim.models import Device, Interface, Cable
from nautobot.extras.models import MetadataType, ObjectMetadata, Relationship, RelationshipAssociation
from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot._neighbors import _observation, _read_observation
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures


class NativeNeighborTests(TestCase):
    bind = fixtures.NativeNamespaceTests.bind
    address = fixtures.NativeNamespaceTests.address
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def setUp(self):
        fixtures.NativeNamespaceTests.setUp(self)
        self.device, self.interface = fixtures.NativeNamespaceTests.fixture(self)
        self.interface.type = "1000base-t"
        self.interface.validated_save()
        peer_device = Device.objects.create(
            name="peer-device",
            device_type=self.device.device_type,
            role=self.device.role,
            location=self.device.location,
            status=self.status,
            platform=self.device.platform,
        )
        peer_device.tags.add(self.tag)
        self.peer = Interface.objects.create(device=peer_device, name="eth1", type="1000base-t", status=self.status)
        self.peer.tags.add(self.tag)
        self.bind(peer_device, "device-b")
        self.bind(self.peer, '["device-b","ifname","eth1"]')
        content = ContentType.objects.get_for_model(Interface)
        metadata_type = MetadataType.objects.create(name="UniFi neighbor observations", data_type="json")
        metadata_type.content_types.add(content)
        self.relationship = Relationship.objects.create(
            key="unifi_observed_neighbor",
            label="Last-known UniFi neighbors",
            type="symmetric-many-to-many",
            source_type=content,
            destination_type=content,
            source_label="Last-known UniFi neighbors",
            destination_label="Last-known UniFi neighbors",
        )
        self.job.sync_neighbors = True
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        for device, source_id in ((self.device, "device-a"), (peer_device, "device-b")):
            self.job._unifi_bindings.plan("device", "site-a", source_id, {"name": device.name})
        for interface, source_id in (
            (self.interface, '["device-a","ifname","eth0"]'),
            (self.peer, '["device-b","ifname","eth1"]'),
        ):
            self.job._unifi_bindings.plan(
                "interface", "site-a", source_id, {"device_id": interface.device_id, "name": interface.name}
            )

    def source(self, reporters, now=None):
        now = now or self.now
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        source.top_level = ("neighbor",)
        self.job._unifi_neighbor_proofs = {}
        for reporter, peers in reporters:
            previous = _read_observation(reporter.pk, self.integration.pk, "site-a")
            document = _observation(previous, self.integration.pk, "site-a", {peer.pk: {"lldp"} for peer in peers}, now)
            self.job._unifi_neighbor_proofs[reporter.pk] = {
                "device": {"state": 1, "last_seen": now.timestamp() - 1, "next_heartbeat_at": now.timestamp() + 10},
                "now": now,
                "site_id": "site-a",
            }
            source.add(source.neighbor(id=reporter.pk, neighbor_observations=document))
        return source

    def sync(self, source):
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        return target

    def test_preview_reciprocal_deduplication_omission_reappearance_and_cable_preservation(self):
        inventory_changed_at = self.interface.last_updated
        source = self.source([(self.interface, [self.peer]), (self.peer, [self.interface])])
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.diff_to(target)
        self.assertFalse(RelationshipAssociation.objects.exists())
        self.assertFalse(ObjectMetadata.objects.filter(metadata_type__name="UniFi neighbor observations").exists())
        self.sync(source)
        self.assertEqual(RelationshipAssociation.objects.count(), 1)
        self.assertFalse(Cable.objects.exists())
        other = _read_observation(self.peer.pk, self.integration.pk, "site-a")
        self.sync(self.source([(self.interface, [])], self.now + timedelta(seconds=30)))
        missing = _read_observation(self.interface.pk, self.integration.pk, "site-a")
        self.assertEqual(missing["peers"][0]["presence"], "not-observed")
        self.assertEqual(_read_observation(self.peer.pk, self.integration.pk, "site-a"), other)
        self.sync(self.source([(self.interface, [self.peer])], self.now + timedelta(seconds=60)))
        self.assertEqual(RelationshipAssociation.objects.count(), 1)
        self.assertEqual(len(_read_observation(self.interface.pk, self.integration.pk, "site-a")["peers"]), 1)
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.status_id, self.status.pk)
        self.assertEqual(self.interface.last_updated, inventory_changed_at)

    def test_metadata_failure_rolls_back_new_edges_and_preserves_other_reporters(self):
        source = self.source([(self.interface, [self.peer]), (self.peer, [self.interface])])
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        desired = source.get("neighbor", str(self.interface.pk))
        with patch.object(ObjectMetadata, "validated_save", side_effect=ValidationError("test-only failure")):
            with self.assertRaises(ObjectNotCreated):
                target.neighbor.create(target, desired.get_identifiers(), desired.get_attrs())
        self.assertFalse(RelationshipAssociation.objects.exists())
        desired = source.get("neighbor", str(self.peer.pk))
        target.neighbor.create(target, desired.get_identifiers(), desired.get_attrs())
        self.assertEqual(RelationshipAssociation.objects.count(), 1)
        self.assertIsNone(_read_observation(self.interface.pk, self.integration.pk, "site-a"))
        self.assertIsNotNone(_read_observation(self.peer.pk, self.integration.pk, "site-a"))

    def test_later_sample_cannot_overwrite_a_changed_observation_baseline(self):
        from diffsync.exceptions import ObjectNotUpdated

        stale_source = self.source([(self.interface, [])], self.now + timedelta(seconds=60))
        stale_proofs = dict(self.job._unifi_neighbor_proofs)
        stale_target = UnifiNautobotAdapter(job=self.job)
        stale_target.top_level = stale_source.top_level
        stale_target.load()
        # Another sync commits a peer after this run's read-only snapshot.
        self.sync(self.source([(self.interface, [self.peer])], self.now + timedelta(seconds=30)))
        current = _read_observation(self.interface.pk, self.integration.pk, "site-a")
        self.job._unifi_neighbor_proofs = stale_proofs
        desired = stale_source.get("neighbor", str(self.interface.pk))
        existing = stale_target.get_all("neighbor")
        with self.assertRaises((ObjectNotCreated, ObjectNotUpdated)):
            if existing:
                existing[0].update(desired.get_attrs())
            else:
                stale_target.neighbor.create(stale_target, desired.get_identifiers(), desired.get_attrs())
        self.assertEqual(_read_observation(self.interface.pk, self.integration.pk, "site-a"), current)
        self.assertEqual(RelationshipAssociation.objects.count(), 1)

    def test_foreign_observation_owner_and_changed_native_parent_block_writes(self):
        source = self.source([(self.interface, [self.peer])])
        metadata = ObjectMetadata(
            metadata_type=MetadataType.objects.get(name="UniFi neighbor observations"),
            assigned_object=self.interface,
            scoped_fields=[],
        )
        foreign = _observation(None, "00000000-0000-0000-0000-000000000002", "site-a", {}, self.now)
        metadata.value = foreign
        metadata.validated_save()
        target = UnifiNautobotAdapter(job=self.job)
        desired = source.get("neighbor", str(self.interface.pk))
        with self.assertRaises(ObjectNotCreated):
            target.neighbor.create(target, desired.get_identifiers(), desired.get_attrs())
        self.assertFalse(RelationshipAssociation.objects.exists())

        metadata.delete()
        self.peer.device = self.device
        self.peer.validated_save()
        with self.assertRaises(ObjectNotCreated):
            target.neighbor.create(target, desired.get_identifiers(), desired.get_attrs())
        self.assertFalse(RelationshipAssociation.objects.exists())

    def test_invalid_relationship_definition_fails_before_source_inventory_changes(self):
        source = self.source([])
        self.job.controller = SimpleNamespace(external_integration=self.integration)
        self.relationship.type = "many-to-many"
        self.relationship.validated_save()
        with self.assertRaises(ValueError):
            async_to_sync(source._prepare_bindings)()
        self.assertFalse(RelationshipAssociation.objects.exists())

    def test_logical_management_binding_cannot_create_neighbors_even_with_ethernet_type(self):
        metadata = ObjectMetadata.objects.get(
            assigned_object_id=self.peer.pk, metadata_type__name="UniFi inventory ownership"
        )
        metadata.value = {**metadata.value, "source_object_id": '["device-b","management","config_network"]'}
        metadata.validated_save()
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        self.job._unifi_bindings.plan("device", "site-a", "device-a", {"name": self.device.name})
        self.job._unifi_bindings.plan("device", "site-a", "device-b", {"name": self.peer.device.name})
        self.job._unifi_bindings.plan(
            "interface",
            "site-a",
            '["device-a","ifname","eth0"]',
            {"device_id": self.device.pk, "name": self.interface.name},
        )
        self.job._unifi_bindings.plan(
            "interface",
            "site-a",
            '["device-b","management","config_network"]',
            {"device_id": self.peer.device_id, "name": self.peer.name},
        )
        source = self.source([(self.interface, [self.peer])])
        target = UnifiNautobotAdapter(job=self.job)
        desired = source.get("neighbor", str(self.interface.pk))
        with self.assertRaises(ObjectNotCreated):
            target.neighbor.create(target, desired.get_identifiers(), desired.get_attrs())
        self.assertFalse(RelationshipAssociation.objects.exists())
        self.assertIsNone(_read_observation(self.interface.pk, self.integration.pk, "site-a"))

    def test_contradictory_device_and_port_lldp_projections_hold_the_reporter(self):
        source = self.source([])
        first = '["device-a","ifname","eth0"]'
        source._neighbor_devices = {
            "site-a": {
                "device-a": {
                    "state": 1,
                    "last_seen": self.now.timestamp() - 1,
                    "next_heartbeat_at": self.now.timestamp() + 10,
                    "uplink": {"type": "wireless"},
                    "lldp_table": [
                        {"local_port_name": "eth0", "is_wired": True, "chassis_id": "unknown", "port_id": "eth1"}
                    ],
                    "port_table": [{"ifname": "eth0", "lldp_table": []}],
                }
            }
        }
        source._neighbor_interfaces = {
            ("site-a", "device-a", first): (
                source.interface(id=self.interface.pk, label="eth0", device__id=self.device.pk, type="1000base-t"),
                {"port_idx": None, "ifname": "eth0"},
            )
        }
        source._collection_completed_at = self.now
        async_to_sync(source._load_neighbors)()
        self.assertEqual(source.get_all("neighbor"), [])
        self.assertTrue(any(row["reason"] == "incomplete-neighbor-evidence" for row in source._held))

    def test_conflicting_local_lldp_index_and_alias_hold_the_reporter(self):
        source = self.source([])
        identity = '["device-a","ifname","eth0"]'
        source._neighbor_devices = {
            "site-a": {
                "device-a": {
                    "state": 1,
                    "last_seen": self.now.timestamp() - 1,
                    "next_heartbeat_at": self.now.timestamp() + 10,
                    "uplink": {"type": "wireless"},
                    "lldp_table": [
                        {
                            "local_port_idx": 2,
                            "local_port_name": "eth0",
                            "is_wired": True,
                            "chassis_id": "unknown",
                            "port_id": "eth1",
                        }
                    ],
                    "port_table": [{"port_idx": 1}],
                }
            }
        }
        source._neighbor_interfaces = {
            ("site-a", "device-a", identity): (
                source.interface(id=self.interface.pk, label="eth0", device__id=self.device.pk, type="1000base-t"),
                {"port_idx": 1, "ifname": "eth0"},
            )
        }
        source._collection_completed_at = self.now
        async_to_sync(source._load_neighbors)()
        self.assertEqual(source.get_all("neighbor"), [])
        self.assertTrue(any(row["reason"] == "incomplete-neighbor-evidence" for row in source._held))

    def test_source_resolves_vendor_mac_only_through_exact_bound_device_and_os_alias(self):
        source = self.source([])
        first, second = '["device-a","ifname","eth0"]', '["device-b","ifname","eth1"]'
        source._neighbor_devices = {
            "site-a": {
                "device-a": {
                    "mac": "00:00:00:00:00:01",
                    "state": 1,
                    "last_seen": self.now.timestamp() - 1,
                    "next_heartbeat_at": self.now.timestamp() + 10,
                    "uplink": {"type": "wireless"},
                    "port_table": [
                        {
                            "ifname": "eth0",
                            "lldp_table": [{"lldp_chassis_id": "00:00:00:00:00:02", "lldp_port_id": "eth1"}],
                        }
                    ],
                },
                "device-b": {"mac": "00:00:00:00:00:02"},
            }
        }
        source._neighbor_interfaces = {
            ("site-a", "device-a", first): (
                source.interface(id=self.interface.pk, label="eth0", device__id=self.device.pk, type="1000base-t"),
                {"port_idx": None, "ifname": "eth0"},
            ),
            ("site-a", "device-b", second): (
                source.interface(id=self.peer.pk, label="eth1", device__id=self.peer.device_id, type="1000base-t"),
                {"port_idx": None, "ifname": "eth1"},
            ),
        }
        source._collection_completed_at = self.now
        async_to_sync(source._load_neighbors)()
        desired = source.get("neighbor", str(self.interface.pk))
        self.assertEqual(desired.neighbor_observations["peers"][0]["peer_interface_id"], str(self.peer.pk))
        source._neighbor_devices["site-a"]["device-a"]["port_table"][0]["lldp_table"][0]["lldp_port_id"] = "Port 1"
        source._held.clear()
        source.remove(desired)
        async_to_sync(source._load_neighbors)()
        self.assertTrue(
            any(row["source_id"] == first and row["reason"] == "unresolved-endpoint" for row in source._held)
        )

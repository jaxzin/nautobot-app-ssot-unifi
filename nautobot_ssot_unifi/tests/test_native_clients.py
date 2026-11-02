"""Native client observations: guarded matching, retained pending records, no inventory creation."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code,too-many-public-methods

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from diffsync.enum import DiffSyncFlags
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from nautobot.dcim.models import Device, Interface
from nautobot.extras.models import (
    ExternalIntegration,
    MetadataType,
    ObjectMetadata,
    Relationship,
    RelationshipAssociation,
)
from nautobot.virtualization.models import Cluster, ClusterType, VirtualMachine, VMInterface

from nautobot_ssot_unifi.ssot import models
from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot._clients import (
    _ClientHold,
    _client_facts,
    _observation,
    _read_observation,
    _read_unresolved,
    _resolve,
)
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures

SITE = "site-a"
MAC = "02:00:00:00:10:01"
NETWORKS = {"net-a": uuid4()}
NETWORK_ROWS = [{"_id": "net-a", "name": "IoT", "vlan": 20, "vlan_enabled": True, "purpose": "corporate"}]
DEVICES = {"src-sw": {"mac": "02:00:00:00:00:01"}}


def wired(mac=MAC, port=1, seen=1760000000, **over):
    """One synthetic wired client row."""
    return {
        "mac": mac,
        "is_wired": True,
        "sw_mac": "02:00:00:00:00:01",
        "sw_port": port,
        "network_id": "net-a",
        "vlan": 20,
        "last_seen": seen,
        **over,
    }


class NativeClientTests(TestCase):
    """Observed client facts attach to existing interfaces and never create or reconfigure inventory."""

    job: Any
    integration: Any
    status: Any
    tag: Any
    bind = fixtures.NativeNamespaceTests.bind
    address = fixtures.NativeNamespaceTests.address

    def setUp(self):
        fixtures.NativeNamespaceTests.setUp(self)
        self.device, self.port = fixtures.NativeNamespaceTests.fixture(self)
        self.port.type = "1000base-t"
        self.port.validated_save()
        interface, vm_interface = (ContentType.objects.get_for_model(model) for model in (Interface, VMInterface))
        integration = ContentType.objects.get_for_model(ExternalIntegration)
        observations = MetadataType.objects.create(name="UniFi client observations", data_type="json")
        observations.content_types.add(interface, vm_interface)
        MetadataType.objects.create(name="UniFi unresolved client observations", data_type="json").content_types.add(
            integration
        )
        for key, destination in (
            ("unifi_observed_client_attachment", interface),
            ("unifi_observed_vm_client_attachment", vm_interface),
        ):
            Relationship.objects.create(
                key=key,
                label=key,
                type="one-to-many",
                source_type=interface,
                destination_type=destination,
                source_label="Attached clients",
                destination_label="Attachment point",
            )
        self.job.sync_clients = True
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        self.job._unifi_bindings.plan("device", SITE, "device-a", {"name": self.device.name})
        self.job._unifi_bindings.plan(
            "interface", SITE, '["device-a","ifname","eth0"]', {"device_id": self.device.pk, "name": self.port.name}
        )
        other = Device.objects.create(
            name="client-device",
            device_type=self.device.device_type,
            role=self.device.role,
            location=self.device.location,
            status=self.status,
        )
        self.client_nic = Interface.objects.create(
            device=other, name="eth0", type="1000base-t", status=self.status, mac_address=MAC
        )
        self.attach = {"src-sw": {"ports": {1: self.port.pk, 2: self.port.pk}, "radios": {}}}

    def vm_interface(self, mac):
        """A virtual machine interface carrying the given MAC."""
        cluster_type = ClusterType.objects.create(name="contract-cluster-type")
        cluster = Cluster.objects.create(name="contract-cluster", cluster_type=cluster_type)
        machine = VirtualMachine.objects.create(name="client-vm", cluster=cluster, status=self.status)
        return VMInterface.objects.create(virtual_machine=machine, name="eth0", status=self.status, mac_address=mac)

    def source(self, rows):
        """Project one complete synthetic site report."""
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        source.top_level = ("client_observation", "vm_client_observation", "unresolved_clients")
        async_to_sync(source._load_clients)(SITE, rows, NETWORKS, NETWORK_ROWS, DEVICES, self.attach)
        async_to_sync(source._finalize_clients)()
        return source

    def sync(self, source):
        """Apply the projected report to the native target."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        return target

    def pending(self):
        """Unresolved entries keyed by site and MAC."""
        return {(row["site_id"], row["mac"]): row for row in _read_unresolved(self.integration.pk)["clients"]}

    def test_unique_match_records_observation_and_attachment_without_touching_nic_configuration(self):
        before = Interface.objects.get(pk=self.client_nic.pk)
        self.sync(self.source([wired()]))
        document = _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)
        self.assertEqual(
            document["network"], {"source_network_id": "net-a", "vlan": 20, "vlan_id": str(NETWORKS["net-a"])}
        )
        self.assertEqual(document["attachment"]["interface_id"], str(self.port.pk))
        association = RelationshipAssociation.objects.get()
        self.assertEqual((association.source_id, association.destination_id), (self.port.pk, self.client_nic.pk))
        after = Interface.objects.get(pk=self.client_nic.pk)
        self.assertEqual(
            (after.mode, after.untagged_vlan_id, after.last_updated), (before.mode, None, before.last_updated)
        )
        self.assertEqual(self.pending(), {})
        again = self.source([wired()])
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = again.top_level
        target.load()
        self.assertEqual(again.diff_to(target).summary()["update"], 0)

    def test_virtual_machine_interface_match_uses_its_own_relationship(self):
        self.client_nic.mac_address = ""
        self.client_nic.validated_save()
        vm = self.vm_interface(MAC)
        self.sync(self.source([wired()]))
        document = _read_observation(VMInterface, vm.pk, self.integration.pk, SITE)
        self.assertEqual(document["mac"], MAC)
        association = RelationshipAssociation.objects.get()
        self.assertEqual(association.relationship.key, "unifi_observed_vm_client_attachment")
        self.assertEqual(association.destination_id, vm.pk)

    def test_unmatched_client_is_retained_for_review_and_creates_no_inventory(self):
        devices = Device.objects.count()
        self.sync(self.source([wired(mac="02:00:00:00:10:99")]))
        entry = self.pending()[(SITE, "02:00:00:00:10:99")]
        self.assertEqual(entry["reason"], "no-match")
        self.assertEqual(entry["attachment"]["interface_id"], str(self.port.pk))
        self.assertEqual(Device.objects.count(), devices)
        self.assertFalse(RelationshipAssociation.objects.exists())

    def test_ambiguity_across_device_and_vm_interfaces_is_retained_not_merged(self):
        vm = self.vm_interface(MAC)
        self.sync(self.source([wired()]))
        entry = self.pending()[(SITE, MAC)]
        self.assertEqual(entry["reason"], "ambiguous-binding")
        self.assertEqual({c["id"] for c in entry["candidates"]}, {str(self.client_nic.pk), str(vm.pk)})
        self.assertFalse(ObjectMetadata.objects.filter(metadata_type__name="UniFi client observations").exists())

    def test_incomplete_report_is_retained_with_only_explicit_facts(self):
        row = wired(network_id=None, vlan=None)
        self.sync(self.source([row]))
        entry = self.pending()[(SITE, MAC)]
        self.assertEqual(entry["reason"], "incomplete-membership")
        self.assertIsNone(entry["network"]["source_network_id"])
        self.assertEqual(entry["attachment"]["port_idx"], 1)
        self.assertFalse(ObjectMetadata.objects.filter(metadata_type__name="UniFi client observations").exists())

    def test_pending_entry_retires_after_the_matched_write_succeeds(self):
        self.client_nic.mac_address = ""
        self.client_nic.validated_save()
        self.sync(self.source([wired()]))
        self.assertEqual(self.pending()[(SITE, MAC)]["reason"], "no-match")
        self.client_nic.mac_address = MAC
        self.client_nic.validated_save()
        self.sync(self.source([wired()]))
        self.assertEqual(self.pending(), {})
        self.assertIsNotNone(_read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE))

    def test_failed_matched_write_keeps_the_pending_entry(self):
        self.client_nic.mac_address = ""
        self.client_nic.validated_save()
        self.sync(self.source([wired()]))
        self.client_nic.mac_address = MAC
        self.client_nic.validated_save()
        with patch.object(models.ClientObservationModel, "update", side_effect=RuntimeError("test-only failure")):
            try:
                self.sync(self.source([wired()]))
            except RuntimeError:
                pass
        self.assertEqual(self.pending()[(SITE, MAC)]["reason"], "no-match")
        self.assertIsNone(_read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE))

    def test_failed_write_with_equal_last_seen_but_changed_attachment_keeps_the_pending_entry(self):
        # An older committed observation shares last_seen but not the attachment this run intends.
        facts = _client_facts(wired(port=1), {"net-a": 20}, DEVICES)
        old = _observation(None, self.integration.pk, SITE, facts, self.port.pk, NETWORKS["net-a"], 20)
        metadata = ObjectMetadata(
            metadata_type=MetadataType.objects.get(name="UniFi client observations"),
            assigned_object=self.client_nic,
            scoped_fields=[],
        )
        metadata.value = old
        metadata.validated_save()
        self.client_nic.mac_address = ""
        self.client_nic.validated_save()
        pending = self.source([wired(mac=MAC, network_id=None)])
        self.sync(pending)
        self.client_nic.mac_address = MAC
        self.client_nic.validated_save()
        changed = {**self.attach["src-sw"]["ports"], 2: uuid4()}
        self.attach["src-sw"]["ports"] = changed
        with patch.object(models.ClientObservationModel, "update", side_effect=RuntimeError("test-only failure")):
            try:
                self.sync(self.source([wired(port=2)]))
            except RuntimeError:
                pass
        self.assertIn((SITE, MAC), self.pending())
        self.assertEqual(_read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE), old)

    def test_omitted_client_flips_presence_and_keeps_last_known_facts(self):
        self.sync(self.source([wired()]))
        observed = _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)
        self.sync(self.source([]))
        missing = _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)
        self.assertEqual(missing, {**observed, "presence": "not-observed"})
        self.assertEqual(RelationshipAssociation.objects.count(), 1)

    def test_roam_moves_the_single_attachment(self):
        second = Interface.objects.create(device=self.device, name="eth1", type="1000base-t", status=self.status)
        second.tags.add(self.tag)
        self.bind(second, '["device-a","ifname","eth1"]')
        session = self.job._unifi_bindings = _BindingSession(self.integration.pk)
        session.plan("device", SITE, "device-a", {"name": self.device.name})
        for source_id, name in (
            ('["device-a","ifname","eth0"]', self.port.name),
            ('["device-a","ifname","eth1"]', "eth1"),
        ):
            session.plan("interface", SITE, source_id, {"device_id": self.device.pk, "name": name})
        self.attach["src-sw"]["ports"][2] = second.pk
        self.sync(self.source([wired(port=1)]))
        self.sync(self.source([wired(port=2, seen=1760000100)]))
        self.assertEqual(RelationshipAssociation.objects.get().source_id, second.pk)

    def test_stale_report_does_not_overwrite_newer_observation(self):
        self.sync(self.source([wired(seen=1760000100)]))
        self.sync(self.source([wired(seen=1760000000)]))
        document = _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)
        self.assertEqual(document["last_seen_at"], "2025-10-09T08:55:00Z")
        self.assertEqual(self.pending()[(SITE, MAC)]["reason"], "incomplete-membership")

    def test_binding_survives_a_blank_native_mac_on_device_interfaces(self):
        self.sync(self.source([wired()]))
        Interface.objects.filter(pk=self.client_nic.pk).update(mac_address="")
        self.assertEqual(_resolve(MAC, self.integration.pk, SITE), ("interface", self.client_nic.pk))

    def test_binding_survives_a_blank_native_mac_on_vm_interfaces(self):
        self.client_nic.mac_address = ""
        self.client_nic.validated_save()
        vm = self.vm_interface(MAC)
        self.sync(self.source([wired()]))
        VMInterface.objects.filter(pk=vm.pk).update(mac_address="")
        self.assertEqual(_resolve(MAC, self.integration.pk, SITE), ("vminterface", vm.pk))

    def test_older_conflicting_or_incomplete_reports_keep_last_known_pending_facts(self):
        mac = "02:00:00:00:10:99"
        self.sync(self.source([wired(mac=mac, port=1, seen=1760000100)]))
        rich = self.pending()[(SITE, mac)]
        self.sync(self.source([wired(mac=mac, port=2, seen=1760000000)]))
        self.assertEqual(self.pending()[(SITE, mac)], rich)
        self.sync(self.source([wired(mac=mac), wired(mac=mac, port=2)]))
        conflicting = self.pending()[(SITE, mac)]
        self.assertEqual(conflicting["reason"], "ambiguous-binding")
        for fact in ("network", "attachment", "last_seen_at"):
            self.assertEqual(conflicting[fact], rich[fact])
        self.sync(self.source([wired(mac=mac, network_id=None, vlan=None, seen=1760000200)]))
        incomplete = self.pending()[(SITE, mac)]
        self.assertEqual(incomplete["reason"], "incomplete-membership")
        self.assertEqual(incomplete["network"], rich["network"])
        self.assertEqual(incomplete["last_seen_at"], "2025-10-09T08:56:40Z")

    def test_missing_port_keeps_the_pending_attachment_until_a_complete_roam_replaces_it(self):
        mac = "02:00:00:00:10:99"
        self.sync(self.source([wired(mac=mac, port=1, seen=1760000100)]))
        rich = self.pending()[(SITE, mac)]
        row = wired(mac=mac, seen=1760000200)
        del row["sw_port"]
        self.sync(self.source([row]))
        held = self.pending()[(SITE, mac)]
        self.assertEqual(held["reason"], "incomplete-membership")
        self.assertEqual(held["attachment"], rich["attachment"])
        self.sync(self.source([wired(mac=mac, port=2, seen=1760000300)]))
        roamed = self.pending()[(SITE, mac)]
        self.assertEqual(roamed["attachment"]["port_idx"], 2)
        self.assertEqual(roamed["attachment"]["kind"], "wired")

    def test_contradictory_vlan_keeps_the_pending_network_group(self):
        mac = "02:00:00:00:10:99"
        self.sync(self.source([wired(mac=mac, seen=1760000100)]))
        rich = self.pending()[(SITE, mac)]
        self.sync(self.source([wired(mac=mac, vlan=30, seen=1760000200)]))
        held = self.pending()[(SITE, mac)]
        self.assertEqual(held["reason"], "incomplete-membership")
        self.assertEqual(held["network"], rich["network"])

    def test_explicit_site_mismatch_fails_the_collection_and_preserves_pending_and_matched_facts(self):
        self.sync(self.source([wired(), wired(mac="02:00:00:00:10:99")]))
        pending, matched = self.pending(), _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)
        with self.assertRaises(ValueError):
            self.source([wired(site_id="other-site"), wired(mac="02:00:00:00:10:99", site_id="other-site")])
        self.assertEqual(self.pending(), pending)
        self.assertEqual(_read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE), matched)
        self.sync(self.source([wired(site_id=SITE), wired(mac="02:00:00:00:10:99")]))
        self.assertEqual(
            _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)["presence"], "observed"
        )

    def test_unidentifiable_row_fails_the_collection_before_absence_inference(self):
        self.sync(self.source([wired()]))
        before = _read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE)
        with self.assertRaises(ValueError):
            self.source([wired(), {"is_wired": True}])
        self.assertEqual(_read_observation(Interface, self.client_nic.pk, self.integration.pk, SITE), before)

    def test_established_binding_wins_over_a_later_second_mac_match(self):
        self.sync(self.source([wired()]))
        extra = Interface.objects.create(
            device=self.client_nic.device, name="eth9", type="1000base-t", status=self.status, mac_address=MAC
        )
        self.assertEqual(_resolve(MAC, self.integration.pk, SITE), ("interface", self.client_nic.pk))
        self.assertNotEqual(extra.pk, self.client_nic.pk)

    def test_binding_conflicts_with_changed_interface_mac_and_foreign_owner_hold(self):
        self.sync(self.source([wired()]))
        self.client_nic.mac_address = "02:00:00:00:10:77"
        self.client_nic.validated_save()
        with self.assertRaises(_ClientHold) as caught:
            _resolve(MAC, self.integration.pk, SITE)
        self.assertEqual(caught.exception.reason, "identity-conflict")
        self.client_nic.mac_address = MAC
        self.client_nic.validated_save()
        with self.assertRaises(_ClientHold) as caught:
            _resolve(MAC, "00000000-0000-0000-0000-000000000009", SITE)
        self.assertIn(caught.exception.reason, {"foreign-owner", "ambiguous-binding", "identity-conflict"})

    def test_missing_schema_fails_before_collection(self):
        Relationship.objects.filter(key="unifi_observed_vm_client_attachment").delete()
        self.job.controller = SimpleNamespace(external_integration=self.integration)
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        with self.assertRaises(ValueError):
            async_to_sync(source._prepare_bindings)()

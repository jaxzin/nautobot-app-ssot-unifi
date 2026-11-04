"""Native client addresses converge without claiming client NICs or pre-existing links."""

from typing import Any
from unittest.mock import patch

# These tests exercise projection and diff before the job contacts a source.
# pylint: disable=protected-access

from asgiref.sync import async_to_sync
from diffsync.exceptions import ObjectNotUpdated
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import Interface
from nautobot.extras.models import MetadataType, ObjectMetadata, Status
from nautobot.ipam.models import IPAddress, IPAddressToInterface, Namespace, Prefix
from nautobot.virtualization.models import VMInterface

from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_clients as clients

OWNERSHIP = "UniFi client IP assignments"
OLD = "192.0.2.10"
NEW = "192.0.2.11"


class NativeClientIPTests(TestCase):
    """Exercise the normal client projection, native diff, and apply on disposable data."""

    bind = clients.NativeClientTests.bind
    address = clients.NativeClientTests.address
    sync = clients.NativeClientTests.sync
    vm_interface = clients.NativeClientTests.vm_interface
    a: Namespace
    status: Status
    port: Interface
    client_nic: Interface
    job: Any
    attach: dict

    def setUp(self):
        clients.NativeClientTests.setUp(self)
        metadata = MetadataType.objects.create(name=OWNERSHIP, data_type="json")
        metadata.content_types.add(*[ContentType.objects.get_for_model(model) for model in (Interface, VMInterface)])
        self.prefix = Prefix.objects.create(namespace=self.a, prefix="192.0.2.0/24", type="network", status=self.status)
        self.job.sync_client_ips = True
        self.network_rows = [{**clients.NETWORK_ROWS[0], "ip_subnet": "192.0.2.1/24"}]

    def source(self, rows):
        """Project synthetic client evidence against the fixture's configured network."""
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
            namespace_name=self.a.name,
        )
        source.top_level = tuple(
            name for name in source.top_level if name.startswith(("client_", "vm_client_", "unresolved_clients"))
        )
        async_to_sync(source._load_clients)(
            clients.SITE, rows, clients.NETWORKS, self.network_rows, clients.DEVICES, self.attach
        )
        async_to_sync(source._finalize_clients)()
        return source

    def diff(self, source):
        """Compare without applying any database changes."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        return source.diff_to(target).summary()

    def test_preview_apply_then_replace_owned_link_without_deleting_address(self):
        source = self.source([clients.wired(ip=OLD)])
        self.diff(source)
        self.assertFalse(IPAddress.objects.filter(host=OLD, parent__namespace=self.a).exists())
        self.sync(source)
        old = IPAddress.objects.get(host=OLD, parent__namespace=self.a)
        link = old.interface_assignments.get(interface=self.client_nic)
        self.assertEqual((old.parent_id, old.mask_length), (self.prefix.pk, 24))
        claim = ObjectMetadata.objects.get(metadata_type__name=OWNERSHIP)
        self.assertEqual(claim.value["assignments"], [{"ip_address_id": str(old.pk), "assignment_id": str(link.pk)}])
        self.assertEqual(self.diff(self.source([clients.wired(ip=OLD)]))["update"], 0)

        self.sync(self.source([clients.wired(ip=NEW, seen=1760000001)]))
        self.assertTrue(IPAddress.objects.filter(pk=old.pk).exists())
        self.assertFalse(IPAddressToInterface.objects.filter(pk=link.pk).exists())
        self.assertTrue(IPAddressToInterface.objects.filter(ip_address__host=NEW, interface=self.client_nic).exists())

    def test_reuses_existing_address_and_link_without_claiming_or_removing_link(self):
        address = IPAddress(address=f"{OLD}/24", parent=self.prefix, status=self.status)
        address.validated_save()
        original = IPAddressToInterface.objects.create(ip_address=address, interface=self.client_nic)
        self.sync(self.source([clients.wired(ip=OLD)]))
        self.assertFalse(ObjectMetadata.objects.filter(metadata_type__name=OWNERSHIP).exists())

        self.sync(self.source([clients.wired(ip=NEW, seen=1760000001)]))
        self.assertTrue(IPAddressToInterface.objects.filter(pk=original.pk).exists())
        self.assertEqual(IPAddress.objects.filter(parent__namespace=self.a, host__in=(OLD, NEW)).count(), 2)

    def test_vm_assignment_reuses_unassigned_address_in_correct_namespace(self):
        self.client_nic.mac_address = ""
        self.client_nic.validated_save()
        vm = self.vm_interface(clients.MAC)
        IPAddress(address=f"{OLD}/24", parent=self.prefix, status=self.status).validated_save()
        self.sync(self.source([clients.wired(ip=OLD)]))
        link = IPAddressToInterface.objects.get(vm_interface=vm)
        self.assertEqual(link.ip_address.parent_id, self.prefix.pk)
        self.assertEqual(IPAddress.objects.filter(host=OLD).count(), 2)

    def test_missing_or_out_of_network_address_retains_last_known_link(self):
        self.sync(self.source([clients.wired(ip=OLD)]))
        original = IPAddressToInterface.objects.get(ip_address__host=OLD, ip_address__parent__namespace=self.a)
        for rows in ([], [clients.wired()], [clients.wired(ip="198.51.100.4")]):
            self.sync(self.source(rows))
            self.assertEqual(
                IPAddressToInterface.objects.get(ip_address__host=OLD, ip_address__parent__namespace=self.a).pk,
                original.pk,
            )
            self.assertEqual(IPAddress.objects.filter(parent__namespace=self.a, host__in=(OLD, NEW)).count(), 1)

    def test_failed_new_assignment_rolls_back_and_retains_old_link(self):
        self.sync(self.source([clients.wired(ip=OLD)]))
        original = IPAddressToInterface.objects.get(ip_address__host=OLD, ip_address__parent__namespace=self.a)
        source = self.source([clients.wired(ip=NEW, seen=1760000001)])
        with patch.object(IPAddressToInterface, "validated_save", side_effect=ValidationError("synthetic failure")):
            with self.assertRaises(ObjectNotUpdated):
                self.sync(source)
        self.assertEqual(
            IPAddressToInterface.objects.get(ip_address__host=OLD, ip_address__parent__namespace=self.a).pk, original.pk
        )
        self.assertFalse(IPAddress.objects.filter(host=NEW).exists())

    def test_replacement_link_created_by_another_writer_is_preserved(self):
        self.sync(self.source([clients.wired(ip=OLD)]))
        old = IPAddress.objects.get(host=OLD, parent__namespace=self.a)
        old.interface_assignments.get(interface=self.client_nic).delete()
        replacement = IPAddressToInterface.objects.create(ip_address=old, interface=self.client_nic)
        self.sync(self.source([clients.wired(ip=NEW, seen=1760000001)]))
        self.assertTrue(IPAddressToInterface.objects.filter(pk=replacement.pk).exists())
        self.assertTrue(self.client_nic.ip_address_assignments.filter(ip_address__host=NEW).exists())

    def test_duplicate_address_evidence_does_not_pick_a_client(self):
        second_mac = "02:00:00:00:10:02"
        Interface.objects.create(
            device=self.client_nic.device, name="eth1", type="1000base-t", status=self.status, mac_address=second_mac
        )
        source = self.source([clients.wired(ip=OLD), clients.wired(mac=second_mac, ip=OLD)])
        self.sync(source)
        self.assertFalse(IPAddress.objects.filter(host=OLD, parent__namespace=self.a).exists())
        self.assertEqual([item["reason"] for item in source._held], ["identity-conflict"] * 2)

    def test_role_on_owned_link_blocks_automatic_replacement(self):
        self.sync(self.source([clients.wired(ip=OLD)]))
        link = self.client_nic.ip_address_assignments.get()
        link.is_preferred = True
        link.validated_save()
        source = self.source([clients.wired(ip=NEW, seen=1760000001)])
        self.sync(source)
        self.assertTrue(IPAddressToInterface.objects.filter(pk=link.pk, is_preferred=True).exists())
        self.assertFalse(IPAddress.objects.filter(host=NEW, parent__namespace=self.a).exists())
        self.assertIn("foreign-owner", [row["reason"] for row in source._held])

    def test_other_endpoint_assignment_is_a_hold(self):
        address = IPAddress(address=f"{OLD}/24", parent=self.prefix, status=self.status)
        address.validated_save()
        link = IPAddressToInterface.objects.create(ip_address=address, interface=self.port)
        source = self.source([clients.wired(ip=OLD)])
        self.sync(source)
        self.assertEqual(
            IPAddressToInterface.objects.get(ip_address__host=OLD, ip_address__parent__namespace=self.a).pk, link.pk
        )
        self.assertIn("foreign-owner", [row["reason"] for row in source._held])

    def test_disabled_option_does_not_write_addresses(self):
        self.job.sync_client_ips = False
        self.sync(self.source([clients.wired(ip=OLD)]))
        self.assertFalse(IPAddress.objects.filter(host=OLD, parent__namespace=self.a).exists())
        self.assertTrue(ObjectMetadata.objects.filter(metadata_type__name="UniFi client observations").exists())

"""Configured network subnets and DHCP spans use native owned IPAM records."""

# White-box integration tests exercise private connector extension boundaries.
# pylint: disable=protected-access,duplicate-code

from typing import Any
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotCreated
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from nautobot.extras.models import MetadataType, ObjectMetadata
from nautobot.ipam.models import VLAN, VLANGroup, Prefix, IPAddressRange

from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.ssot.address_scopes import AddressScopeResolver
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures


class NativeNetworkGraphTests(TestCase):
    """Verify the actual native object graph, not a mocked model writer."""

    bind = fixtures.NativeNamespaceTests.bind
    job: Any
    integration: Any
    a: Any
    b: Any
    tag: Any
    status: Any

    def setUp(self):
        fixtures.NativeNamespaceTests.setUp(self)
        self.group = VLANGroup.objects.create(name="site-a")
        metadata = MetadataType.objects.get(name="UniFi inventory ownership")
        metadata.content_types.add(
            *(ContentType.objects.get_for_model(model) for model in (VLAN, Prefix, IPAddressRange))
        )
        self.job.sync_vlans = True
        self.job._unifi_vlan_groups = {"site-a": self.group.pk}

    @staticmethod
    def network(**changes):
        """The gateway address identifies a configured subnet, not a host lease."""
        return {
            "_id": "lan",
            "name": "Example",
            "purpose": "corporate",
            "vlan": 20,
            "ip_subnet": "192.0.2.1/24",
            "dhcpd_enabled": True,
            "dhcpd_start": "192.0.2.10",
            "dhcpd_stop": "192.0.2.100",
            **changes,
        }

    def source(self, rows, resolver=None):
        """Build an opt-in snapshot from synthetic network configuration."""
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
            namespace_name=self.a.name,
            address_scope_resolver=resolver,
        )
        loader = getattr(source, "_load_network_graph", None)
        self.assertTrue(callable(loader), "Configured subnet/DHCP graph loader is missing")
        source.top_level = ("vlan", "network_prefix", "ip_address_range")
        networks = async_to_sync(source._load_vlans)("site-a", rows)
        async_to_sync(loader)("site-a", rows, networks)
        return source

    def sync(self, source):
        """Apply through native SSoT and return the target adapter."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        return target

    def test_configured_subnet_and_pool_are_native_and_repeat_without_changes(self):
        rows = [self.network()]
        self.sync(self.source(rows))
        prefix = Prefix.objects.get(namespace=self.a, network="192.0.2.0", prefix_length=24)
        pool = IPAddressRange.objects.get(parent=prefix)
        self.assertEqual(prefix.vlan.vid, 20)
        self.assertEqual((str(pool.start_host), str(pool.end_host)), ("192.0.2.10", "192.0.2.100"))
        self.assertFalse(pool.count_as_utilized)
        self.assertFalse(pool.is_exclusive)
        original = prefix.pk, pool.pk
        source = self.source(rows)
        target = self.sync(source)
        self.assertEqual(source.diff_to(target).summary()["update"], 0)
        self.assertEqual(
            (Prefix.objects.get(pk=original[0]).pk, IPAddressRange.objects.get(pk=original[1]).pk), original
        )
        self.assertEqual(ObjectMetadata.objects.filter(metadata_type__name="UniFi inventory ownership").count(), 3)

    def test_existing_unbound_prefix_is_held_instead_of_implicitly_adopted(self):
        existing = Prefix.objects.create(namespace=self.a, prefix="192.0.2.0/24", type="network", status=self.status)
        source = self.source([self.network()])
        self.sync(source)
        existing.refresh_from_db()
        self.assertIsNone(existing.vlan_id)
        self.assertFalse(IPAddressRange.objects.exists())
        self.assertTrue(
            any(row["kind"] == "network_prefix" and row["reason"] == "identity-conflict" for row in source._held)
        )

    def test_reviewed_existing_prefix_binding_preserves_its_uuid(self):
        existing = Prefix.objects.create(namespace=self.a, prefix="192.0.2.0/24", type="network", status=self.status)
        self.bind(existing, f"lan:ipv4:{self.a.pk}:192.0.2.0/24")
        self.sync(self.source([self.network()]))
        existing.refresh_from_db()
        self.assertEqual(Prefix.objects.filter(namespace=self.a).count(), 1)
        self.assertEqual(existing.vlan.vid, 20)
        self.assertEqual(IPAddressRange.objects.get().parent_id, existing.pk)

    def test_configured_prefix_does_not_duplicate_address_derived_prefix_and_namespace_isolated(self):
        Prefix.objects.create(namespace=self.b, prefix="192.0.2.0/24", type="network", status=self.status)
        self.sync(self.source([self.network()]))
        source = self.source([self.network()])
        interface = source.interface(id=uuid4(), label="eth0", device__id=uuid4(), type="other")
        async_to_sync(source._assign_ip)("192.0.2.10", "255.255.255.0", interface)
        self.assertEqual(source.get_all("prefix"), [])
        target = UnifiNautobotAdapter(job=self.job)
        target.load()
        self.assertEqual(target.get_all("prefix"), [])
        self.assertEqual(Prefix.objects.filter(namespace=self.b).count(), 1)
        self.assertIsNone(Prefix.objects.get(namespace=self.b).vlan_id)

    def test_dhcp_endpoint_and_label_update_preserves_range_identity(self):
        self.sync(self.source([self.network()]))
        pool = IPAddressRange.objects.get()
        self.sync(self.source([self.network(name="Renamed", dhcpd_stop="192.0.2.150")]))
        pool.refresh_from_db()
        self.assertEqual(IPAddressRange.objects.count(), 1)
        self.assertEqual(str(pool.end_host), "192.0.2.150")
        self.assertEqual(pool.name, "Renamed DHCP")

    def test_omitted_subnet_or_pool_and_invalid_pool_preserve_last_known_records(self):
        self.sync(self.source([self.network()]))
        prefix = Prefix.objects.get(namespace=self.a, network="192.0.2.0", prefix_length=24)
        pool = IPAddressRange.objects.get(parent=prefix)
        for changes in ({"ip_subnet": None}, {"dhcpd_start": None}, {"dhcpd_stop": "198.51.100.10"}):
            with self.subTest(changes=changes):
                self.sync(self.source([self.network(**changes)]))
                pool.refresh_from_db()
                self.assertEqual(str(pool.end_host), "192.0.2.100")
                self.assertEqual(pool.parent_id, prefix.pk)

    def test_protected_wan_is_not_a_lan_prefix_or_dhcp_pool(self):
        rows = [self.network(purpose="wan", vlan=20, attr_no_delete=True)]
        self.sync(self.source(rows))
        self.assertFalse(Prefix.objects.filter(namespace=self.a).exists())
        self.assertFalse(IPAddressRange.objects.exists())

    def test_changed_cidr_cannot_reparent_a_retained_pool(self):
        self.sync(self.source([self.network()]))
        pool = IPAddressRange.objects.get()
        original_parent = pool.parent_id
        self.sync(self.source([self.network(ip_subnet="192.0.2.1/25")]))
        pool.refresh_from_db()
        self.assertEqual(pool.parent_id, original_parent)
        self.assertFalse(Prefix.objects.filter(namespace=self.a, prefix_length=25).exists())

    def test_new_prefix_cannot_reparent_a_foreign_range(self):
        parent = Prefix.objects.create(namespace=self.a, prefix="192.0.0.0/16", type="network", status=self.status)
        pool = IPAddressRange.objects.create(
            parent=parent, start_host="192.0.2.10", end_host="192.0.2.100", status=self.status
        )
        source = self.source([self.network()])
        self.sync(source)
        pool.refresh_from_db()
        self.assertEqual(pool.parent_id, parent.pk)
        self.assertFalse(Prefix.objects.filter(namespace=self.a, prefix_length=24).exists())
        self.assertTrue(any(row["reason"] == "identity-conflict" for row in source._held))

    def test_write_rechecks_ranges_created_after_preview(self):
        source = self.source([self.network()])
        parent = Prefix.objects.create(namespace=self.a, prefix="192.0.0.0/16", type="network", status=self.status)
        pool = IPAddressRange.objects.create(
            parent=parent, start_host="192.0.2.10", end_host="192.0.2.100", status=self.status
        )
        with self.assertRaises(ObjectNotCreated):
            self.sync(source)
        pool.refresh_from_db()
        self.assertEqual(pool.parent_id, parent.pk)
        self.assertFalse(Prefix.objects.filter(namespace=self.a, prefix_length=24).exists())

    def test_native_range_reparenting_is_rolled_back_if_precheck_is_raced(self):
        source = self.source([self.network()])
        parent = Prefix.objects.create(namespace=self.a, prefix="192.0.0.0/16", type="network", status=self.status)
        pool = IPAddressRange.objects.create(
            parent=parent, start_host="192.0.2.10", end_host="192.0.2.100", status=self.status
        )
        with patch("nautobot_ssot_unifi.ssot._ipam._ranges_reparented_by", return_value=IPAddressRange.objects.none()):
            with self.assertRaises(ObjectNotCreated):
                self.sync(source)
        pool.refresh_from_db()
        self.assertEqual(pool.parent_id, parent.pk)
        self.assertFalse(Prefix.objects.filter(namespace=self.a, prefix_length=24).exists())

    def test_subnet_requires_complete_unique_namespace_coverage_including_its_interior(self):
        cases = [
            [(self.a, ["192.0.2.0/26", "192.0.2.192/26"]), (self.b, ["192.0.2.64/26", "192.0.2.128/26"])],
            [(self.a, ["192.0.2.0/26", "192.0.2.192/26"])],
            [(self.a, ["192.0.2.0/24"]), (self.b, ["192.0.2.64/26"])],
        ]
        for scopes in cases:
            with self.subTest(scopes=scopes):
                resolver = AddressScopeResolver(
                    [{"namespace_id": str(ns.pk), "prefixes": prefixes} for ns, prefixes in scopes], self.a.name
                )
                source = self.source([self.network(dhcpd_start="192.0.2.80")], resolver)
                self.assertEqual(source.get_all("network_prefix"), [])
                self.assertEqual(source.get_all("ip_address_range"), [])
                self.assertTrue(any(row["reason"] == "invalid-network" for row in source._held))

    def test_one_namespace_may_cover_a_subnet_with_multiple_adjacent_rules(self):
        resolver = AddressScopeResolver(
            [{"namespace_id": str(self.a.pk), "prefixes": ["192.0.2.0/25", "192.0.2.128/25"]}], self.a.name
        )
        self.sync(self.source([self.network()], resolver))
        self.assertEqual(IPAddressRange.objects.get().parent.namespace_id, self.a.pk)

    def test_reviewed_range_binding_does_not_depend_on_a_tag(self):
        self.sync(self.source([self.network()]))
        pool = IPAddressRange.objects.get()
        pool.tags.remove(self.tag)
        self.sync(self.source([self.network(dhcpd_stop="192.0.2.150")]))
        pool.refresh_from_db()
        self.assertEqual(str(pool.end_host), "192.0.2.150")

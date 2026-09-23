"""Duplicate address text must remain isolated by the selected namespace."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from diffsync import Adapter
from nautobot.ipam.models import Namespace
from nautobot_ssot.contrib import NautobotModel
from nautobot_ssot.jobs.base import DataSource
from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.ssot.models import IPAddressModel

from nautobot_ssot_unifi.tests import test_persistent_interfaces


class NamespaceTests(TestCase):
    def address(self, **changes):
        return IPAddressModel(
            host="192.0.2.1",
            parent__namespace__name="routing-a",
            parent__network="192.0.2.0",
            **{"mask_length": 24, "parent__prefix_length": 24, **changes},
        )

    def test_source_mask_change_updates_existing_scoped_address(self):
        class Snapshot(Adapter):
            ip_address = IPAddressModel
            top_level = ("ip_address",)

        source, target = Snapshot(), Snapshot()
        target.add(self.address())
        source.add(self.address(mask_length=25, parent__prefix_length=25))
        summary = source.diff_to(target).summary()
        self.assertEqual((summary["create"], summary["update"], summary["delete"]), (0, 1, 0))

    def test_parent_update_supplies_complete_namespace_scoped_lookup(self):
        address = self.address()
        with patch.object(NautobotModel, "update", return_value=address) as update:
            address.update({"parent__prefix_length": 25})
        # The connector must supply all natural-key components to native SSoT.
        self.assertEqual(
            update.call_args.args[0],
            {
                "parent__namespace__name": "routing-a",
                "parent__network": "192.0.2.0",
                "parent__prefix_length": 25,
            },
        )

    def source(self, namespace):
        return test_persistent_interfaces.PersistentInterfaceTests().load(
            namespace=namespace,
            ethernet_table=[{"name": "eth0", "ip": "192.0.2.1", "netmask": "255.255.255.0"}],
        )

    def test_source_prefix_address_and_assignment_identities_include_namespace(self):
        left, right = self.source("routing-a"), self.source("routing-b")
        fields = {
            "prefix": "namespace__name",
            "ip_address": "parent__namespace__name",
            "ip_address_to_interface": "ip_address__parent__namespace__name",
        }
        for kind, field in fields.items():
            with self.subTest(kind=kind):
                first, second = left.get_all(kind)[0], right.get_all(kind)[0]
                self.assertEqual(first.get_identifiers().get(field), "routing-a")
                self.assertEqual(second.get_identifiers().get(field), "routing-b")
                self.assertNotEqual(first.get_unique_id(), second.get_unique_id())

    def test_unspecified_namespace_keeps_global(self):
        adapter = self.source(None)
        self.assertEqual(adapter.get_all("prefix")[0].get_identifiers().get("namespace__name"), "Global")

    def test_job_passes_selected_native_namespace_to_the_source(self):
        job = UnifiDataSource()
        selected = Namespace(name="routing-a")
        location = SimpleNamespace(location_type=SimpleNamespace(name="Site"))
        controller = SimpleNamespace(location=location)
        with patch.object(DataSource, "run", return_value=None):
            job.run(
                dryrun=True,
                debug=False,
                controller=controller,
                default_location=None,
                location_type=None,
                namespace=selected,
            )
        self.assertIs(getattr(job, "namespace", None), selected)

    def test_job_default_namespace_is_existing_global(self):
        job = UnifiDataSource()
        selected = Namespace(name="Global")
        location = SimpleNamespace(location_type=SimpleNamespace(name="Site"))
        controller = SimpleNamespace(location=location)
        with patch.object(DataSource, "run", return_value=None), patch.object(
            Namespace.objects, "get", return_value=selected
        ):
            job.run(dryrun=True, debug=False, controller=controller, default_location=None, location_type=None)
        self.assertIs(getattr(job, "namespace", None), selected)

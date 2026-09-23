"""Address rules reject unsafe mappings before the native sync can write."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

from nautobot.ipam.models import Namespace
from nautobot_ssot.jobs.base import DataSource

from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.tests import test_persistent_interfaces


A = "00000000-0000-4000-8000-000000000004"
B = "00000000-0000-4000-8000-000000000005"


class AddressScopeTests(TestCase):
    def test_native_job_json_field_preserves_empty_rules_for_api_and_form(self):
        field = UnifiDataSource._get_vars()["address_scopes"].as_field()
        for value in ([], "[]"):
            with self.subTest(value=value):
                self.assertEqual(field.clean(value), [])
        for value in ({}, "{}"):
            with self.subTest(value=value):
                self.assertEqual(field.clean(value), {})  # Resolver must reject this, never use a fallback.
        for value in (None, "", "null"):
            self.assertIsNone(field.clean(value))

    def prepare(self, rules):
        namespaces = {UUID(A): Namespace(id=A, name="routing-a"), UUID(B): Namespace(id=B, name="routing-b")}

        def lookup(**fields):
            if fields.get("name") == "Global":
                return Namespace(name="Global")
            try:
                return namespaces[UUID(str(fields["pk"]))]
            except KeyError:
                raise Namespace.DoesNotExist from None

        job = UnifiDataSource()
        location = SimpleNamespace(location_type=SimpleNamespace(name="Site"))
        with patch.object(Namespace.objects, "get", side_effect=lookup), patch.object(
            DataSource, "run", return_value=None
        ) as run:
            try:
                job.run(
                    dryrun=True,
                    debug=False,
                    controller=SimpleNamespace(location=location),
                    default_location=None,
                    location_type=None,
                    address_scopes=rules,
                )
            except ValueError:
                run.assert_not_called()  # Collection/target writes have not started.
                raise
        return job

    def resolver(self, rules):
        job = self.prepare(rules)
        self.assertIsNotNone(
            getattr(job, "address_scope_resolver", None), "Job must validate and prepare address rules"
        )
        return job.address_scope_resolver

    def test_invalid_rules_fail_before_source_collection(self):
        invalid = [
            {},
            "[]",
            False,
            [None],
            [{}],
            [{"prefixes": [], "namespace_id": A}],
            [{"prefixes": "192.0.2.0/24", "namespace_id": A}],
            [{"prefixes": ["192.0.2.1/24"], "namespace_id": A}],
            [{"prefixes": ["192.0.2.0"], "namespace_id": A}],
            [{"prefixes": ["192.0.2.0/255.255.255.0"], "namespace_id": A}],
            [{"prefixes": ["2001:0db8::/32"], "namespace_id": A}],
            [{"prefixes": ["fe80::%eth0/64"], "namespace_id": A}],
            [{"prefixes": [42], "namespace_id": A}],
            [{"prefixes": ["192.0.2.0/24"], "namespace_id": "https://user:secret@invalid.example"}],
            [{"prefixes": ["192.0.2.0/24"], "namespace_id": "00000000-0000-4000-8000-000000000099"}],
            [{"prefixes": ["192.0.2.0/24"], "namespace_id": A, "fallback": True}],
        ]
        for rules in invalid:
            with self.subTest(rules=rules):
                with self.assertRaises(ValueError) as error:
                    self.prepare(rules)
                self.assertNotIn("secret", str(error.exception))
                self.assertNotIn("invalid.example", str(error.exception))

    def test_null_rules_use_selected_namespace_and_empty_rules_admit_nothing(self):
        self.assertEqual(self.resolver(None).resolve("203.0.113.1"), "Global")
        with self.assertRaisesRegex(ValueError, "match"):
            self.resolver([]).resolve("192.0.2.1")

    def test_same_namespace_overlaps_are_allowed_but_conflicting_matches_fail(self):
        rules = [{"prefixes": ["192.0.2.0/24", "192.0.2.0/25", "2001:db8::/32"], "namespace_id": A}]
        resolver = self.resolver(rules)
        self.assertEqual(resolver.resolve("192.0.2.1"), "routing-a")
        self.assertEqual(resolver.resolve("2001:db8::1"), "routing-a")
        with self.assertRaisesRegex(ValueError, "match"):
            resolver.resolve("203.0.113.1")
        rules.append({"prefixes": ["192.0.2.0/25"], "namespace_id": B})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.resolver(rules).resolve("192.0.2.1")

    def test_rules_preserve_first_assignment_and_empty_rules_allow_addressless_inventory(self):
        resolver = self.resolver([{"prefixes": ["192.0.2.0/24"], "namespace_id": A}])
        source = test_persistent_interfaces.PersistentInterfaceTests().load(
            address_scope_resolver=resolver,
            ethernet_table=[
                {"name": "eth0", "ip": "192.0.2.1", "netmask": "24"},
                {"name": "eth1", "ip": "192.0.2.1", "netmask": "25"},
            ],
        )
        self.assertEqual(len(source.get_all("ip_address")), 1)
        self.assertEqual(source.get_all("ip_address")[0].mask_length, 24)
        self.assertEqual([link.interface__label for link in source.get_all("ip_address_to_interface")], ["eth0"])
        source = test_persistent_interfaces.PersistentInterfaceTests().load(
            address_scope_resolver=self.resolver([]), ethernet_table=[{"name": "eth0"}]
        )
        self.assertEqual(len(source.get_all("interface")), 1)
        self.assertEqual(source.get_all("ip_address"), [])

    def test_source_routes_prefix_address_link_and_primary_to_matching_namespace(self):
        resolver = self.resolver(
            [
                {"prefixes": ["192.0.2.0/24"], "namespace_id": A},
                {"prefixes": ["198.51.100.0/24", "2001:db8::/32"], "namespace_id": B},
            ]
        )
        source = test_persistent_interfaces.PersistentInterfaceTests().load(
            address_scope_resolver=resolver,
            ethernet_table=[{"name": "eth0", "ip": "192.0.2.1", "netmask": "24"}],
            config_network={"type": "static", "ip": "198.51.100.1", "netmask": "24"},
        )
        for model, field in (
            ("prefix", "namespace__name"),
            ("ip_address", "parent__namespace__name"),
            ("ip_address_to_interface", "ip_address__parent__namespace__name"),
        ):
            self.assertEqual([getattr(row, field) for row in source.get_all(model)], ["routing-a", "routing-b"])
        device = source.get_all("device")[0]
        self.assertEqual(device.primary_ip4__parent__namespace__name, "routing-b")
        source6 = test_persistent_interfaces.PersistentInterfaceTests().load(
            address_scope_resolver=resolver,
            config_network={"type": "static", "ip": "2001:db8::1", "netmask": "64"},
        )
        self.assertEqual(source6.get_all("device")[0].primary_ip6__parent__namespace__name, "routing-b")
        with self.assertRaisesRegex(ValueError, "match"):
            test_persistent_interfaces.PersistentInterfaceTests().load(
                address_scope_resolver=resolver,
                ethernet_table=[{"name": "eth0", "ip": "203.0.113.1", "netmask": "24"}],
            )

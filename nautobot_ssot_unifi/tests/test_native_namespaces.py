"""Real native DiffSync/ORM namespace and primary-ordering regressions."""

import logging
from types import SimpleNamespace
from unittest.mock import patch

from aiounifi.models.device import Device as UnifiDevice
from aiounifi.models.site import Site
from diffsync import Adapter
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotCreated
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import (
    Controller,
    ControllerManagedDeviceGroup,
    Device,
    DeviceType,
    Interface,
    Location,
    LocationType,
    Manufacturer,
    Platform,
)
from nautobot.extras.models import JobResult, Role, Status, Tag
from nautobot.ipam.models import IPAddress, IPAddressToInterface, Namespace, Prefix

from nautobot_ssot_unifi.const import UNIFI_SSOT_TAG
from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapterMixin, UnifiNautobotAdapter
from nautobot_ssot_unifi.ssot.models import IPAddressModel, IPAddressToInterfaceModel, PrefixModel
from nautobot_ssot_unifi.unifi.client import Client
import structlog


class Snapshot(UnifiAdapterMixin, Adapter):
    top_level = ("prefix", "ip_address", "device", "interface", "ip_address_to_interface")


class NativeNamespaceTests(TestCase):
    def setUp(self):
        self.tag, _ = Tag.objects.get_or_create(name=UNIFI_SSOT_TAG)
        self.status = Status.objects.get(name="Active")
        self.a = Namespace.objects.create(name="contract-a")
        self.b = Namespace.objects.create(name="contract-b")
        self.job = SimpleNamespace(namespace=self.a, logger=logging.getLogger(__name__))

    def test_scoped_parent_update_preserves_address_identity(self):
        addresses = []
        for namespace in (self.a, self.b):
            adapter = UnifiNautobotAdapter(job=self.job)
            PrefixModel.create(
                adapter, {"namespace__name": namespace.name, "network": "192.0.2.0", "prefix_length": 24}, {}
            )
            model = IPAddressModel.create(
                adapter,
                {"parent__namespace__name": namespace.name, "host": "192.0.2.10"},
                {"mask_length": 24, "parent__network": "192.0.2.0", "parent__prefix_length": 24},
            )
            obj = IPAddress.objects.get(parent__namespace=namespace, host="192.0.2.10")
            model.pk = obj.pk  # Native target loading supplies this on later runs.
            addresses.append(obj.pk)
            Prefix.objects.create(namespace=namespace, prefix="192.0.2.0/25", type="network", status=self.status)
            model.update({"mask_length": 25, "parent__prefix_length": 25})
            obj.refresh_from_db()
            self.assertEqual(
                (obj.mask_length, obj.parent.prefix_length, obj.parent.namespace_id), (25, 25, namespace.pk)
            )
        self.assertNotEqual(*addresses)

    def fixture(self):
        role = Role.objects.create(name="contract-role")
        role.content_types.add(ContentType.objects.get_for_model(Device))
        maker = Manufacturer.objects.create(name="contract-maker")
        dtype = DeviceType.objects.create(manufacturer=maker, model="contract-model")
        platform = Platform.objects.create(name="contract-platform")
        ltype = LocationType.objects.create(name="contract-site")
        ltype.content_types.add(ContentType.objects.get_for_model(Device))
        location = Location.objects.create(name="contract-site", location_type=ltype, status=self.status)
        controller = Controller.objects.create(name="contract-controller", location=location, status=self.status)
        group = ControllerManagedDeviceGroup.objects.create(name="default", controller=controller)
        device = Device.objects.create(
            name="contract-device",
            device_type=dtype,
            role=role,
            location=location,
            status=self.status,
            platform=platform,
            controller_managed_device_group=group,
        )
        device.tags.add(self.tag)
        interface = Interface.objects.create(device=device, name="eth0", label="eth0", type="other", status=self.status)
        interface.tags.add(self.tag)
        old = self.address(self.a, "203.0.113.0/24", "203.0.113.1/24")
        IPAddressToInterface.objects.create(ip_address=old, interface=interface)
        device.primary_ip4 = old
        device.validated_save()
        # Same desired hosts already exist elsewhere, but are not assigned.
        self.address(self.b, "192.0.2.0/24", "192.0.2.10/24")
        self.address(self.b, "2001:db8::/64", "2001:db8::10/64")
        return device, interface

    def address(self, namespace, prefix, address):
        parent = Prefix.objects.create(namespace=namespace, prefix=prefix, type="network", status=self.status)
        parent.tags.add(self.tag)
        obj = IPAddress.objects.create(parent=parent, address=address, status=self.status)
        obj.tags.add(self.tag)
        return obj

    def source(self, namespace=None, device_name="contract-device"):
        namespace = namespace or self.a
        source = Snapshot()
        device = source.device(
            name=device_name,
            controller_managed_device_group__name="default",
            controller_managed_device_group__controller__name="contract-controller",
            device_type__model="contract-model",
            role__name="contract-role",
            location__name="contract-site",
            serial="",
            platform__name="contract-platform",
            primary_ip4__host="192.0.2.10",
            primary_ip4__parent__namespace__name=namespace.name,
            primary_ip6__host="2001:db8::10",
            primary_ip6__parent__namespace__name=namespace.name,
        )
        source.add(device)
        interface = source.interface(
            label="eth0", type="other", **{f"device__{key}": value for key, value in device.get_identifiers().items()}
        )
        source.add(interface)
        for host, network, mask in (("192.0.2.10", "192.0.2.0", 24), ("2001:db8::10", "2001:db8::", 64)):
            source.add(source.prefix(namespace__name=namespace.name, network=network, prefix_length=mask))
            source.add(
                source.ip_address(
                    host=host,
                    parent__namespace__name=namespace.name,
                    mask_length=mask,
                    parent__network=network,
                    parent__prefix_length=mask,
                )
            )
            source.add(
                source.ip_address_to_interface(
                    ip_address__host=host,
                    ip_address__parent__namespace__name=namespace.name,
                    **{f"interface__{key}": value for key, value in interface.get_identifiers().items()},
                )
            )
        return source

    def sync(self, source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST):
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = Snapshot.top_level
        target.load()
        source.sync_to(target, flags=flags)

    def test_existing_device_primary_waits_for_assignments_and_repeat_is_stable(self):
        device, interface = self.fixture()
        source = self.source()
        self.sync(source)
        device.refresh_from_db()
        for ip in (device.primary_ip4, device.primary_ip6):
            self.assertEqual(ip.parent.namespace_id, self.a.pk)
            self.assertTrue(IPAddressToInterface.objects.filter(ip_address=ip, interface=interface).exists())
        before = (
            device.primary_ip4_id,
            device.primary_ip6_id,
            set(IPAddressToInterface.objects.values_list("pk", flat=True)),
        )
        self.sync(source)
        device.refresh_from_db()
        self.assertEqual(
            before,
            (
                device.primary_ip4_id,
                device.primary_ip6_id,
                set(IPAddressToInterface.objects.values_list("pk", flat=True)),
            ),
        )
        # Host text does not change: only the namespace changes.
        self.sync(self.source(self.b))
        device.refresh_from_db()
        self.assertEqual(device.primary_ip4.parent.namespace_id, self.b.pk)
        self.assertEqual(device.primary_ip6.parent.namespace_id, self.b.pk)

    def test_clear_families_independently(self):
        device, _ = self.fixture()
        source = self.source()
        self.sync(source)
        desired = source.get_all("device")[0]
        desired.primary_ip4__host = None
        desired.primary_ip4__parent__namespace__name = None
        self.sync(source)
        device.refresh_from_db()
        self.assertIsNone(device.primary_ip4_id)
        self.assertIsNotNone(device.primary_ip6_id)
        desired.primary_ip4__host = "192.0.2.10"
        desired.primary_ip4__parent__namespace__name = self.a.name
        desired.primary_ip6__host = None
        desired.primary_ip6__parent__namespace__name = None
        self.sync(source)
        device.refresh_from_db()
        self.assertIsNotNone(device.primary_ip4_id)
        self.assertIsNone(device.primary_ip6_id)

    def test_assignment_failure_keeps_old_primary_and_surfaces_native_validation(self):
        device, _ = self.fixture()
        old = device.primary_ip4_id
        with patch.object(
            IPAddressToInterfaceModel, "create", side_effect=ObjectNotCreated("synthetic assignment failure")
        ):
            with self.assertRaises(ValidationError):
                self.sync(self.source(), DiffSyncFlags.SKIP_UNMATCHED_DST | DiffSyncFlags.CONTINUE_ON_FAILURE)
        device.refresh_from_db()
        self.assertEqual(device.primary_ip4_id, old)
        self.assertIsNone(device.primary_ip6_id)

    def test_new_device_receives_primaries_after_native_assignment_creation(self):
        self.fixture()
        self.sync(self.source(device_name="contract-new-device"))
        device = Device.objects.get(name="contract-new-device")
        for ip in (device.primary_ip4, device.primary_ip6):
            self.assertEqual(ip.parent.namespace_id, self.a.pk)
            self.assertTrue(IPAddressToInterface.objects.filter(ip_address=ip, interface__device=device).exists())

    def test_native_job_mapping_failures_and_dryrun_leave_inventory_unchanged(self):
        device, _ = self.fixture()
        user = get_user_model().objects.create_user(username="contract-user")
        controller = SimpleNamespace(
            name="contract-controller",
            location=device.location,
            external_integration=SimpleNamespace(
                remote_url="https://controller.invalid",
                verify_ssl=True,
                timeout=1,
                secrets_group=SimpleNamespace(get_secret_value=lambda **kwargs: "synthetic-test-only"),
            ),
        )

        async def sites(_client):
            return [Site({"name": "default"})]

        async def devices(_client):
            return [
                UnifiDevice(
                    {
                        "name": "contract-device",
                        "model": "TEST",
                        "type": "uap",
                        "serial": "synthetic",
                        "config_network": {"type": "static", "ip": "192.0.2.10", "netmask": "24"},
                    }
                )
            ]

        models = (Device, Interface, Prefix, IPAddress, IPAddressToInterface)
        before = [list(model.objects.order_by("pk").values()) for model in models]
        unmatched = [{"prefixes": ["198.51.100.0/24"], "namespace_id": str(self.a.pk)}]
        valid = [{"prefixes": ["192.0.2.0/24"], "namespace_id": str(self.a.pk)}]
        ambiguous = valid + [{"prefixes": ["192.0.2.0/25"], "namespace_id": str(self.b.pk)}]
        config = structlog.get_config()
        try:
            for rules in ([], unmatched, ambiguous, valid):
                with self.subTest(rules=rules):
                    job = UnifiDataSource()
                    job.job_result = JobResult.objects.create(name="contract-unifi", user=user)
                    with patch.object(Client, "get_sites", sites), patch.object(Client, "get_devices", devices):
                        kwargs = dict(
                            dryrun=rules == valid,
                            debug=False,
                            controller=controller,
                            default_location=None,
                            location_type=None,
                            namespace=self.a,
                            address_scopes=rules,
                        )
                        if rules == valid:
                            job.run(**kwargs)
                        else:
                            with self.assertRaises(ValueError):
                                job.run(**kwargs)
                    self.assertEqual(before, [list(model.objects.order_by("pk").values()) for model in models])
        finally:
            structlog.configure(**config)

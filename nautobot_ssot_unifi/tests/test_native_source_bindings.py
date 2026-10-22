"""Binding resolution against real native metadata, independent of display names."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code
# Scenario-specific imports isolate the dependencies of each native regression.
# pylint: disable=import-outside-toplevel

from typing import Any
from unittest.mock import patch

from diffsync import Adapter
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotCreated, ObjectNotUpdated
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import Device, Interface
from nautobot.extras.models import ExternalIntegration, MetadataType, ObjectMetadata

from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _BindingIndex
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapterMixin, UnifiNautobotAdapter
from nautobot_ssot_unifi.ssot.models import DeviceModel, InterfaceModel
from nautobot_ssot_unifi.tests import test_native_namespaces as native_namespaces

INTEGRATION = "00000000-0000-0000-0000-000000000001"


class NativeBindingTests(TestCase):
    """Verify exact bindings and atomic ownership claims."""

    # Initialized by the borrowed NativeNamespaceTests.setUp fixture builder.
    a: Any
    job: Any
    integration: Any
    status: Any
    tag: Any
    # Reuse existing native fixture builders, without inheriting their tests.
    address = native_namespaces.NativeNamespaceTests.address

    def setUp(self):
        native_namespaces.NativeNamespaceTests.setUp(self)
        self.device, self.interface = native_namespaces.NativeNamespaceTests.fixture(self, bound=False)
        self.metadata_type = MetadataType.objects.get(name="UniFi inventory ownership")
        self.metadata_type.content_types.set(
            [ContentType.objects.get_for_model(Device), ContentType.objects.get_for_model(Interface)]
        )

    def bind(self, target, source_id, site_id="site-a"):
        """Attach an exact source identity to a native fixture object."""
        metadata = ObjectMetadata(
            metadata_type=self.metadata_type,
            assigned_object=target,
            scoped_fields=[],
        )
        metadata.value = {
            "schema_version": 1,
            "integration_id": INTEGRATION,
            "site_id": site_id,
            "source_object_id": source_id,
            "field_paths": [],
        }
        metadata.validated_save()
        return metadata

    def test_native_renames_preserve_exact_source_resolution(self):
        self.bind(self.device, "device-a")
        self.bind(self.interface, '["device-a","port_idx",3]')
        self.device.name = "renamed-device"
        self.device.validated_save()
        self.interface.label = "renamed-port"
        self.interface.validated_save()
        index = _BindingIndex.from_orm()
        self.assertEqual(index.resolve("device", INTEGRATION, "site-a", "device-a"), self.device.pk)
        self.assertEqual(
            index.resolve("interface", INTEGRATION, "site-a", '["device-a","port_idx",3]'), self.interface.pk
        )

    def test_different_site_does_not_adopt_same_native_device_name(self):
        self.bind(self.device, "device-a")
        with self.assertRaises(_BindingHold) as error:
            _BindingIndex.from_orm().resolve("device", INTEGRATION, "site-b", "device-a")
        self.assertEqual(error.exception.reason, "missing-binding")

    def test_existing_unbound_target_is_not_adopted_by_name_or_label(self):
        with self.assertRaises(_BindingHold) as error:
            _BindingIndex.from_orm().resolve("device", INTEGRATION, "site-a", "device-a")
        self.assertEqual(error.exception.reason, "missing-binding")
        self.assertFalse(ObjectMetadata.objects.exists())
        self.device.refresh_from_db()
        self.assertEqual(self.device.name, "contract-device")

    def test_os_identifier_change_requires_explicit_rebind(self):
        self.bind(self.interface, '["device-a","ifname","eth0"]')
        with self.assertRaises(_BindingHold) as error:
            _BindingIndex.from_orm().resolve("interface", INTEGRATION, "site-a", '["device-a","ifname","eth1"]')
        self.assertEqual(error.exception.reason, "missing-binding")

    def test_changed_os_identifier_is_not_admitted_as_a_second_interface(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        self.bind(self.interface, '["device-a","ifname","eth0"]')
        session = _BindingSession(self.integration.pk)
        session.observe_interfaces("site-a", "device-a", {'["device-a","ifname","eth1"]'})
        with self.assertRaises(_BindingHold) as error:
            session.plan(
                "interface", "site-a", '["device-a","ifname","eth1"]', {"device_id": self.device.pk, "name": "eth1"}
            )
        self.assertEqual(error.exception.reason, "identity-conflict")
        self.assertEqual(Interface.objects.filter(device=self.device).count(), 1)

    def test_duplicate_source_mapping_is_held_even_when_native_metadata_is_valid(self):
        other = Interface.objects.create(device=self.device, name="eth1", type="other", status=self.status)
        self.bind(self.interface, '["device-a","port_idx",3]')
        self.bind(other, '["device-a","port_idx",3]')
        with self.assertRaises(_BindingHold) as error:
            _BindingIndex.from_orm().resolve("interface", INTEGRATION, "site-a", '["device-a","port_idx",3]')
        self.assertEqual(error.exception.reason, "ambiguous-binding")

    def test_native_document_overlap_cannot_be_bypassed_by_json_owner(self):
        first = self.bind(self.device, "device-a")
        second = ObjectMetadata(metadata_type=self.metadata_type, assigned_object=self.device, scoped_fields=[])
        with self.assertRaises(ValidationError):
            second.value = {**first.value, "integration_id": "00000000-0000-0000-0000-000000000002"}
            second.validated_save()
        self.assertEqual(ObjectMetadata.objects.filter(assigned_object_id=self.device.pk).count(), 1)

    def test_bound_model_rename_updates_same_uuid_and_preserves_management_ip(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        integration = ExternalIntegration.objects.get(pk=INTEGRATION)
        self.bind(self.device, "device-a")
        self.interface.name = self.interface.label = "mgmt"
        self.interface.validated_save()
        self.bind(self.interface, '["device-a","management","config_network"]')
        before = self.device.primary_ip4_id
        session = _BindingSession(integration.pk)
        device_id = session.plan("device", "site-a", "device-a", {"name": "renamed-device"})
        interface_id = session.plan(
            "interface",
            "site-a",
            '["device-a","management","config_network"]',
            {"device_id": device_id, "name": "mgmt"},
        )

        class Snapshot(UnifiAdapterMixin, Adapter):
            """Expose only the model graph required by the fixture."""

            top_level = ("device", "interface")

        source = Snapshot()
        source.add(
            source.device(
                id=device_id,
                name="renamed-device",
                device_type__model=self.device.device_type.model,
                role__name=self.device.role.name,
                location__name=self.device.location.name,
                serial="",
                platform__name=self.device.platform.name,
                controller_managed_device_group__name="default",
                controller_managed_device_group__controller__name="contract-controller",
                primary_ip4__host=self.device.primary_ip4.host,
                primary_ip4__parent__namespace__name=self.a.name,
            )
        )
        source.add(source.interface(id=interface_id, device__id=device_id, label="renamed-holder", type="other"))
        self.job._unifi_bindings = session
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = Snapshot.top_level
        target.load()
        self.assertEqual(source.device._identifiers, ("id",))
        self.assertEqual(source.interface._identifiers, ("id",))
        diff = source.diff_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        self.assertEqual(diff.summary()["create"], 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.name, "contract-device", "Preview must not write")
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        self.device.refresh_from_db()
        self.interface.refresh_from_db()
        self.assertEqual(self.device.name, "renamed-device")
        self.assertEqual(self.interface.label, "renamed-holder")
        self.assertEqual(self.device.primary_ip4_id, before)
        self.assertEqual(Interface.objects.filter(device=self.device).count(), 1)
        self.assertEqual(source.diff_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST).summary()["update"], 0)

    def device_attrs(self, name="brand-new-device"):
        """Return source attributes for the fixture device."""
        return {
            "name": name,
            "device_type__model": self.device.device_type.model,
            "role__name": self.device.role.name,
            "location__name": self.device.location.name,
            "serial": "",
            "platform__name": self.device.platform.name,
            "controller_managed_device_group__name": "default",
            "controller_managed_device_group__controller__name": "contract-controller",
        }

    def test_new_record_and_binding_roll_back_together_on_metadata_failure(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        session = _BindingSession(self.integration.pk)
        target_id = session.plan("device", "site-a", "brand-new", {"name": "brand-new-device"})
        self.job._unifi_bindings = session
        adapter = UnifiNautobotAdapter(job=self.job)
        # The actual native Device save happens; fail only the binding persistence.
        with patch.object(ObjectMetadata, "validated_save", side_effect=ValidationError("test-only failure")):
            with self.assertRaises(ValidationError):
                DeviceModel.create(adapter, {"id": target_id}, self.device_attrs())
        self.assertFalse(Device.objects.filter(pk=target_id).exists())
        self.assertFalse(ObjectMetadata.objects.filter(assigned_object_id=target_id).exists())
        result = DeviceModel.create(adapter, {"id": target_id}, self.device_attrs())
        self.assertEqual(result.pk, target_id)
        self.assertEqual(_BindingIndex.from_orm().resolve("device", INTEGRATION, "site-a", "brand-new"), target_id)

    def test_changed_owner_after_preview_blocks_native_update(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        binding = self.bind(self.device, "device-a")
        session = _BindingSession(self.integration.pk)
        target_id = session.plan("device", "site-a", "device-a", {"name": self.device.name})
        self.job._unifi_bindings = session
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = ("device",)
        target.load()
        model = target.get("device", str(target_id))
        binding.value = {**binding.value, "integration_id": "00000000-0000-0000-0000-000000000002"}
        binding.validated_save()
        with self.assertRaises(ObjectNotUpdated):
            model.update({"name": "must-not-be-written"})
        self.device.refresh_from_db()
        self.assertEqual(self.device.name, "contract-device")

    def test_native_interface_parent_movement_after_preview_blocks_update_and_assignment(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession
        from nautobot_ssot_unifi.ssot.models import IPAddressToInterfaceModel

        self.bind(self.device, "device-a")
        self.bind(self.interface, '["device-a","ifname","eth0"]')
        session = _BindingSession(self.integration.pk)
        session.plan("device", "site-a", "device-a", {"name": self.device.name})
        session.plan(
            "interface", "site-a", '["device-a","ifname","eth0"]', {"device_id": self.device.pk, "name": "eth0"}
        )
        self.job._unifi_bindings = session
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = ("interface",)
        target.load()
        model = target.get("interface", str(self.interface.pk))
        other = Device.objects.create(
            name="other-parent",
            device_type=self.device.device_type,
            role=self.device.role,
            location=self.device.location,
            status=self.status,
        )
        self.device.primary_ip4 = None
        self.device.validated_save()
        self.interface.device = other
        self.interface.validated_save()
        with self.assertRaises(ObjectNotUpdated):
            model.update({"label": "must-not-be-written"})
        self.interface.refresh_from_db()
        self.assertEqual(self.interface.label, "eth0")
        address = self.address(self.a, "192.0.2.0/24", "192.0.2.10/24")
        with self.assertRaises(ObjectNotCreated):
            IPAddressToInterfaceModel.create(
                target,
                {
                    "interface__id": self.interface.pk,
                    "ip_address__host": address.host,
                    "ip_address__parent__namespace__name": self.a.name,
                },
                {},
            )

    def test_new_interface_cannot_bind_to_a_different_source_parent(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        self.bind(self.device, "device-a")
        session = _BindingSession(self.integration.pk)
        session.plan("device", "site-a", "device-a", {"name": self.device.name})
        target_id = session.plan(
            "interface", "site-a", '["device-b","port_idx",3]', {"device_id": self.device.pk, "name": "new-port"}
        )
        self.job._unifi_bindings = session
        with self.assertRaises(ObjectNotCreated):
            InterfaceModel.create(
                UnifiNautobotAdapter(job=self.job),
                {"id": target_id},
                {"device__id": self.device.pk, "label": "new-port", "type": "other"},
            )
        self.assertFalse(Interface.objects.filter(pk=target_id).exists())

    def test_name_collision_appearing_after_preview_is_not_adopted_or_duplicated(self):
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        session = _BindingSession(self.integration.pk)
        target_id = session.plan("device", "site-a", "brand-new", {"name": "brand-new-device"})
        self.job._unifi_bindings = session
        attrs = self.device_attrs()
        other = Device.objects.create(
            name=attrs["name"],
            device_type=self.device.device_type,
            role=self.device.role,
            location=self.device.location,
            status=self.status,
        )
        with self.assertRaises(ObjectNotCreated):
            DeviceModel.create(UnifiNautobotAdapter(job=self.job), {"id": target_id}, attrs)
        self.assertFalse(Device.objects.filter(pk=target_id).exists())
        self.assertFalse(ObjectMetadata.objects.filter(assigned_object_id=other.pk).exists())

    def test_job_reports_failure_after_an_independent_update_commits(self):  # pylint: disable=too-many-locals
        from types import SimpleNamespace

        import structlog
        from django.contrib.auth import get_user_model
        from nautobot.extras.models import JobResult

        from nautobot_ssot_unifi.jobs import UnifiDataSource
        from nautobot_ssot_unifi.ssot._bindings import _BindingSession

        self.bind(self.device, "device-a")
        other = Device.objects.create(
            name="independent-device",
            device_type=self.device.device_type,
            role=self.device.role,
            location=self.device.location,
            platform=self.device.platform,
            status=self.status,
            controller_managed_device_group=self.device.controller_managed_device_group,
        )
        other.tags.add(self.tag)
        self.bind(other, "device-b")
        session = _BindingSession(self.integration.pk)

        class Snapshot(UnifiAdapterMixin, Adapter):
            """Expose only the model graph required by the fixture."""

            top_level = ("device",)

        source = Snapshot()
        source._held = []  # pylint: disable=attribute-defined-outside-init
        for _obj, source_id, name in [
            (self.device, "device-a", "failed-change"),
            (other, "device-b", "committed-change"),
        ]:
            target_id = session.plan("device", "site-a", source_id, {"name": name})
            source.add(source.device(id=target_id, **self.device_attrs(name)))
        job = UnifiDataSource()
        user = get_user_model().objects.create_user(username="contract-partial-user")
        job.job_result = JobResult.objects.create(name="contract-partial", user=user)

        def load_source():
            job._unifi_bindings = session
            job.source_adapter = source

        def load_target():
            job.target_adapter = UnifiNautobotAdapter(job=job, sync=job.sync)
            job.target_adapter.top_level = ("device",)
            job.target_adapter.load()

        update = DeviceModel.update

        def partial_update(model, attrs):
            if model.id == self.device.pk:
                raise ObjectNotUpdated("test-only native failure")
            return update(model, attrs)

        config = structlog.get_config()
        try:
            with patch.object(job, "load_source_adapter", load_source), patch.object(
                job, "load_target_adapter", load_target
            ), patch.object(DeviceModel, "update", partial_update):
                job.run(
                    dryrun=False,
                    debug=False,
                    controller=SimpleNamespace(location=self.device.location),
                    default_location=None,
                    location_type=None,
                    namespace=self.a,
                )
            self.assertTrue(job._failed, "Native Job failure flag must drive the runner's failure status")
            self.assertTrue(job.sync.logs.filter(status__in=["failure", "error"]).exists())
            self.assertTrue(job.job_result.files.filter(name="unifi-held.json").exists())
            self.device.refresh_from_db()
            other.refresh_from_db()
            self.assertEqual(self.device.name, "contract-device")
            self.assertEqual(other.name, "committed-change")
        finally:
            structlog.configure(**config)

    def test_unavailable_management_evidence_does_not_clear_native_primaries(self):
        import logging
        from types import SimpleNamespace

        from aiounifi.models.device import Device as UnifiDevice
        from aiounifi.models.site import Site

        from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter
        from nautobot_ssot_unifi.unifi.client import Client

        self.bind(self.device, "device-a")
        self.job.controller = SimpleNamespace(name="contract-controller", external_integration=self.integration)
        self.job.hardware_models = {}
        self.job.logger = logging.getLogger(__name__)
        before = self.device.primary_ip4_id

        async def sites(_client):
            return [Site({"_id": "site-a", "name": "default"})]

        for evidence in (None, {}):

            async def devices(_client, network_evidence=evidence):
                return [
                    UnifiDevice(
                        {
                            "_id": "device-a",
                            "name": "contract-device",
                            "model": "TEST",
                            "type": "uap",
                            "serial": "",
                            "config_network": network_evidence,
                        }
                    )
                ]

            source = UnifiAdapter(
                job=self.job,
                controller_name="contract-controller",
                default_location_type="contract-site",
                default_location_name="contract-site",
            )
            with patch.object(Client, "get_sites", sites), patch.object(Client, "get_devices", devices):
                source.load(
                    host="controller.invalid",
                    port=443,
                    username="test-reader",
                    password="test-only",
                    verify_cert=True,
                    timeout=1,
                )
            desired = source.get_all("device")[0]
            desired.device_type__model = self.device.device_type.model
            desired.role__name = self.device.role.name
            desired.platform__name = self.device.platform.name
            desired.name = "independent-rename"
            source.top_level = ("device",)
            target = UnifiNautobotAdapter(job=self.job)
            target.top_level = ("device",)
            target.load()
            source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
            self.device.refresh_from_db()
            self.assertEqual(self.device.primary_ip4_id, before)
            self.assertEqual(self.device.name, "independent-rename")

    def test_unavailable_management_evidence_cannot_overwrite_newer_primary(self):
        import logging
        from types import SimpleNamespace

        from aiounifi.models.device import Device as UnifiDevice
        from aiounifi.models.site import Site
        from nautobot.ipam.models import IPAddressToInterface

        from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter
        from nautobot_ssot_unifi.unifi.client import Client

        self.bind(self.device, "device-a")
        self.device.primary_ip4 = None
        self.device.validated_save()
        self.job.controller = SimpleNamespace(name="contract-controller", external_integration=self.integration)
        self.job.hardware_models = {}
        self.job.logger = logging.getLogger(__name__)

        async def sites(_client):
            return [Site({"_id": "site-a", "name": "default"})]

        async def devices(_client):
            return [
                UnifiDevice(
                    {
                        "_id": "device-a",
                        "name": "contract-device",
                        "model": "TEST",
                        "type": "uap",
                        "serial": "",
                        "config_network": None,
                    }
                )
            ]

        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        with patch.object(Client, "get_sites", sites), patch.object(Client, "get_devices", devices):
            source.load(
                host="controller.invalid",
                port=443,
                username="test-reader",
                password="test-only",
                verify_cert=True,
                timeout=1,
            )
        desired = source.get_all("device")[0]
        desired.device_type__model = self.device.device_type.model
        desired.role__name = self.device.role.name
        desired.platform__name = self.device.platform.name
        desired.name = "independent-rename"
        source.top_level = ("device",)
        newer = self.address(self.a, "198.51.100.0/24", "198.51.100.1/24")
        IPAddressToInterface.objects.create(interface=self.interface, ip_address=newer)
        self.device.primary_ip4 = newer
        self.device.validated_save()
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = ("device",)
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        self.device.refresh_from_db()
        self.assertEqual(self.device.primary_ip4_id, newer.pk)
        self.assertEqual(self.device.name, "independent-rename")

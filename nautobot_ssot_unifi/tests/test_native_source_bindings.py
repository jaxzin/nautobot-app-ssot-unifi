"""Binding resolution against real native metadata, independent of display names."""

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import Device, Interface
from nautobot.extras.models import MetadataType, ObjectMetadata

from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _BindingIndex
from nautobot_ssot_unifi.tests.test_native_namespaces import NativeNamespaceTests

INTEGRATION = "00000000-0000-0000-0000-000000000001"


class NativeBindingTests(TestCase):
    # Reuse existing native fixture builders, without inheriting their tests.
    address = NativeNamespaceTests.address

    def setUp(self):
        NativeNamespaceTests.setUp(self)
        self.device, self.interface = NativeNamespaceTests.fixture(self)
        self.metadata_type = MetadataType.objects.create(name="UniFi inventory ownership", data_type="json")
        self.metadata_type.content_types.set(
            [ContentType.objects.get_for_model(Device), ContentType.objects.get_for_model(Interface)]
        )

    def bind(self, target, source_id, site_id="site-a"):
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

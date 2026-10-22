"""Firmware uses native versions and claims without owning shared catalog facts."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

from typing import Any
from unittest.mock import patch

from asgiref.sync import async_to_sync
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotUpdated
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import SoftwareImageFile, SoftwareVersion
from nautobot.extras.models import MetadataType, ObjectMetadata

from nautobot_ssot_unifi.ssot import models
from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures


class NativeFirmwareTests(TestCase):
    """Verify native software-version writes and ownership boundaries."""

    # Initialized by the borrowed NativeNamespaceTests.setUp fixture builder.
    a: Any
    job: Any
    integration: Any
    status: Any
    bind = fixtures.NativeNamespaceTests.bind
    address = fixtures.NativeNamespaceTests.address

    def setUp(self):
        fixtures.NativeNamespaceTests.setUp(self)
        self.device, self.interface = fixtures.NativeNamespaceTests.fixture(self)
        self.job.sync_firmware = True

    def source(self, version):
        """Build a source adapter from synthetic inventory evidence."""
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        session = self.job._unifi_bindings
        target_id = session.plan("device", "site-a", "device-a", {"name": self.device.name})
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        source.top_level = ("software_version", "device")
        device = source.device(
            id=target_id,
            name=self.device.name,
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
        async_to_sync(source._load_firmware)(device, "site-a", "device-a", version)
        source.add(device)
        return source

    def sync(self, source):
        """Apply the source adapter to the native target and refresh fixtures."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        self.device.refresh_from_db()
        return target

    def test_curated_shared_version_is_reused_without_images_or_catalog_changes(self):
        shared = SoftwareVersion.objects.create(
            platform=self.device.platform,
            version="7.2.34",
            alias="Reviewed release",
            long_term_support=True,
            status=self.status,
        )
        source = self.source(" 7.2.34 ")
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.diff_to(target)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.software_version_id, "Preview must not assign firmware")
        self.sync(source)
        self.assertEqual(self.device.software_version_id, shared.pk)
        shared.refresh_from_db()
        self.assertEqual((shared.alias, shared.long_term_support), ("Reviewed release", True))
        self.assertEqual(SoftwareVersion.objects.count(), 1)
        self.assertFalse(SoftwareImageFile.objects.exists())
        ownership = ObjectMetadata.objects.get(
            assigned_object_id=self.device.pk, metadata_type__name="UniFi inventory ownership"
        )
        self.assertEqual(ownership.value["field_paths"], ["software_version"])
        self.assertEqual(self.sync(self.source("7.2.34")).diff_from(self.source("7.2.34")).summary()["update"], 0)

    def test_missing_version_and_disabled_extension_preserve_firmware(self):
        shared = SoftwareVersion.objects.create(platform=self.device.platform, version="7.2.34", status=self.status)
        self.device.software_version = shared
        self.device.validated_save()
        source = self.source(None)
        self.assertEqual(source._held[0]["reason"], "missing-version")
        self.sync(source)
        self.assertEqual(self.device.software_version_id, shared.pk)
        self.job.sync_firmware = False
        target = UnifiNautobotAdapter(job=self.job)
        self.assertNotIn("software_version", target.top_level)
        self.assertNotIn("software_version__version", target.device._attributes)
        self.assertNotIn("unifi_ownership", target.device._attributes)
        baseline = fixtures.NativeNamespaceTests.source(self)
        self.sync(baseline)
        self.assertEqual(self.device.software_version_id, shared.pk)

    def test_foreign_claim_holds_firmware_and_does_not_create_a_version(self):
        metadata_type = MetadataType.objects.create(name="node_exporter host facts ownership", data_type="json")
        metadata_type.content_types.set(
            ObjectMetadata.objects.get(assigned_object_id=self.device.pk).metadata_type.content_types.all()
        )
        metadata = ObjectMetadata(metadata_type=metadata_type, assigned_object=self.device, scoped_fields=[])
        metadata.value = {
            "schema_version": 1,
            "integration_id": "00000000-0000-0000-0000-000000000002",
            "field_paths": ["software_version"],
        }
        metadata.validated_save()
        source = self.source("7.2.34")
        self.assertEqual(source._held[0]["reason"], "foreign-owner")
        self.sync(source)
        self.assertIsNone(self.device.software_version_id)
        self.assertFalse(SoftwareVersion.objects.exists())
        self.assertEqual(metadata.value["field_paths"], ["software_version"])

    def test_new_version_is_active_and_device_claim_rolls_back_on_metadata_failure(self):
        source = self.source("7.2.35")
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        desired_version = source.get_all("software_version")[0]
        version_model = models.SoftwareVersionModel.create(target, desired_version.get_identifiers(), {})
        shared = SoftwareVersion.objects.get(pk=version_model.pk)
        self.assertEqual(shared.status.name, "Active")
        desired = source.get_all("device")[0]
        existing = target.get("device", str(self.device.pk))
        with patch.object(ObjectMetadata, "validated_save", side_effect=ValidationError("test-only claim failure")):
            with self.assertRaises(ObjectNotUpdated):
                existing.update(
                    {
                        "software_version__platform__name": desired.software_version__platform__name,
                        "software_version__version": desired.software_version__version,
                        "unifi_ownership": desired.unifi_ownership,
                    }
                )
        self.device.refresh_from_db()
        self.assertIsNone(self.device.software_version_id)
        ownership = ObjectMetadata.objects.get(
            assigned_object_id=self.device.pk, metadata_type__name="UniFi inventory ownership"
        )
        self.assertEqual(ownership.value["field_paths"], [])
        self.assertFalse(SoftwareImageFile.objects.exists())

    def test_missing_platform_is_held_without_creating_a_software_dependency(self):
        source = self.source(None)
        source._held.clear()
        device = source.get_all("device")[0]
        device.platform__name = "not-an-existing-platform"
        async_to_sync(source._load_firmware)(device, "site-a", "device-a", "7.2.35")
        self.assertEqual(source._held[0]["reason"], "missing-platform")
        self.assertEqual(source.get_all("software_version"), [])
        self.assertFalse(SoftwareVersion.objects.exists())

    def test_missing_firmware_evidence_preserves_a_newer_native_assignment(self):
        source = self.source(None)
        shared = SoftwareVersion.objects.create(platform=self.device.platform, version="7.3.0", status=self.status)
        self.device.software_version = shared
        self.device.validated_save()
        source.get_all("device")[0].name = "independent-rename"
        self.sync(source)
        self.assertEqual(self.device.software_version_id, shared.pk)
        self.assertEqual(self.device.name, "independent-rename")

    def test_new_device_and_firmware_claim_are_admitted_together(self):
        source = self.source("7.3.0")
        desired = source.get_all("device")[0]
        desired.id = self.job._unifi_bindings.plan("device", "site-a", "new-device", {"name": "new-device"})
        desired.name = "new-device"
        desired.primary_ip4__host = None
        desired.primary_ip4__parent__namespace__name = None
        async_to_sync(source._load_firmware)(desired, "site-a", "new-device", "7.3.0")
        target = UnifiNautobotAdapter(job=self.job)
        models.SoftwareVersionModel.create(target, source.get_all("software_version")[0].get_identifiers(), {})
        result = target.device.create(target, desired.get_identifiers(), desired.get_attrs())
        native = result.get_from_db()
        self.assertEqual(native.software_version.version, "7.3.0")
        claim = ObjectMetadata.objects.get(
            assigned_object_id=native.pk, metadata_type__name="UniFi inventory ownership"
        )
        self.assertEqual(claim.value["source_object_id"], "new-device")
        self.assertEqual(claim.value["field_paths"], ["software_version"])

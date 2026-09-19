"""Source-owned synchronization refuses implicit adoption of unrelated inventory."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from diffsync.exceptions import ObjectNotCreated
from django.contrib.contenttypes.models import ContentType

from nautobot_ssot_unifi.const import UNIFI_SSOT_TAG
from nautobot_ssot_unifi.ssot.models import DeviceTypeModel, IPAddressToInterfaceModel, Tag


class InventoryOwnershipTests(TestCase):
    def test_address_assignments_require_owned_interface_and_address(self):
        # Build the real ORM query without fetching rows. Native associations
        # have no tags of their own: both endpoints must carry ownership.
        with patch.object(ContentType.objects, "get_for_model", return_value=ContentType(pk=1)):
            sql, params = IPAddressToInterfaceModel.get_queryset().query.sql_with_params()
        self.assertEqual(params.count(UNIFI_SSOT_TAG), 2)
        self.assertIn('"dcim_interface"', sql)
        self.assertIn('"ipam_ipaddress"', sql)

    def test_untagged_inventory_is_not_adopted(self):
        existing = SimpleNamespace(pk=UUID("00000000-0000-0000-0000-000000000001"), tags=Mock())
        existing.tags.filter.return_value.exists.return_value = False
        with (
            patch.object(DeviceTypeModel._model.objects, "get", return_value=existing),
            patch.object(Tag.objects, "get", return_value=object()),
        ):
            with self.assertRaises(ObjectNotCreated):
                DeviceTypeModel.create(None, {"manufacturer__name": "Ubiquiti Networks", "model": "USW-Test"}, {})
        existing.tags.add.assert_not_called()

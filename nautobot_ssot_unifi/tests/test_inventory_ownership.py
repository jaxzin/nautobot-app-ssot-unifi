"""Source-owned synchronization refuses implicit adoption of unrelated inventory."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from diffsync.exceptions import ObjectNotCreated

from nautobot_ssot_unifi.ssot.models import DeviceTypeModel, Tag


class InventoryOwnershipTests(TestCase):
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

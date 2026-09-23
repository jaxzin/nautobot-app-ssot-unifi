"""Primary address family values survive deferred device creation."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from diffsync.exceptions import ObjectNotUpdated
from nautobot_ssot.contrib import NautobotModel
from nautobot_ssot_unifi.ssot.adapters import UnifiNautobotAdapter

from nautobot_ssot_unifi.ssot.models import ActiveStatusMixin, DeviceModel


class PrimaryIPTests(TestCase):
    def device(self):
        adapter = UnifiNautobotAdapter(job=SimpleNamespace())
        return DeviceModel(
            adapter=adapter,
            name="Switch",
            device_type__model="test",
            role__name="test",
            location__name="test",
            serial="synthetic",
            platform__name="test",
        )

    def test_update_defers_only_changed_family_and_preserves_explicit_clear(self):
        device = self.device()
        for family, host in (("primary_ip4", "192.0.2.1"), ("primary_ip6", "2001:db8::1")):
            with self.subTest(family=family), patch.object(NautobotModel, "update", return_value=device) as update:
                device.update({family + "__host": host, family + "__parent__namespace__name": "routing-b"})
                self.assertEqual(update.call_args.args[0], {})
                self.assertEqual(
                    device.adapter._primary_ips.pop(),
                    {
                        "device": device.get_identifiers(),
                        family: {"host": host, "parent__namespace__name": "routing-b"},
                    },
                )
                device.update({family + "__host": None})
                self.assertEqual(device.adapter._primary_ips.pop(), {"device": device.get_identifiers(), family: None})

    def test_failed_native_device_update_does_not_queue_a_primary_change(self):
        device = self.device()
        with patch.object(NautobotModel, "update", side_effect=ObjectNotUpdated("synthetic validation failure")):
            with self.assertRaises(ObjectNotUpdated):
                device.update({"primary_ip4__host": "192.0.2.1", "primary_ip4__parent__namespace__name": "routing-a"})
        self.assertEqual(device.adapter._primary_ips, [])

    def test_create_defers_both_families_with_their_own_namespaces(self):
        device = self.device()
        attrs = {
            "primary_ip4__host": "192.0.2.1",
            "primary_ip4__parent__namespace__name": "routing-a",
            "primary_ip6__host": "2001:db8::1",
            "primary_ip6__parent__namespace__name": "routing-b",
        }
        with patch.object(ActiveStatusMixin, "create", return_value=device) as create:
            DeviceModel.create(device.adapter, device.get_identifiers(), attrs)
        self.assertEqual(create.call_args.args[2], {})
        self.assertEqual(
            device.adapter._primary_ips,
            [
                {
                    "device": device.get_identifiers(),
                    "primary_ip4": {"host": "192.0.2.1", "parent__namespace__name": "routing-a"},
                    "primary_ip6": {"host": "2001:db8::1", "parent__namespace__name": "routing-b"},
                }
            ],
        )

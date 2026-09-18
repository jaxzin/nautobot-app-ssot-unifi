"""Primary address family values survive deferred device creation."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from nautobot_ssot_unifi.ssot.models import ActiveStatusMixin, DeviceModel


class PrimaryIPTests(TestCase):
    def test_ipv6_is_deferred_separately_from_ipv4(self):
        adapter = SimpleNamespace(_primary_ips=[])
        attrs = {"primary_ip4__host": "192.0.2.1", "primary_ip6__host": "2001:db8::1"}
        with patch.object(ActiveStatusMixin, "create", return_value=None):
            DeviceModel.create(adapter, {"name": "Switch"}, attrs)
        self.assertEqual(
            adapter._primary_ips,
            [{"device": {"name": "Switch"}, "primary_ip4": "192.0.2.1", "primary_ip6": "2001:db8::1"}],
        )
        self.assertNotIn("primary_ip4__host", attrs)
        self.assertNotIn("primary_ip6__host", attrs)

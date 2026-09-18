"""Certificate verification at the aiounifi configuration boundary."""

import ssl
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from nautobot_ssot_unifi.unifi.client import Client


class ClientTransportTests(IsolatedAsyncioTestCase):
    async def test_verification_is_enforced_by_aiounifi(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        try:
            self.assertIsInstance(client.config.ssl_context, ssl.SSLContext)
            self.assertEqual(client.config.ssl_context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(client.config.ssl_context.check_hostname)
        finally:
            await client.logout()

    async def test_explicit_disabled_verification_is_preserved(self):
        client = Client(host="controller.example", username="reader", password="test-only", verify_cert=False)
        try:
            self.assertIs(client.config.ssl_context, False)
        finally:
            await client.logout()

    async def test_device_results_do_not_leak_between_sites(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True

        async def response(_request):
            return {"data": [{"mac": "00:00:00:00:00:01" if client.current_site == "default" else "00:00:00:00:00:02"}]}

        try:
            with patch.object(client.api, "request", response):
                first = list(await client.get_devices())
                client.current_site = "workshop"
                second = list(await client.get_devices())
            self.assertEqual([device.mac for device in first], ["00:00:00:00:00:01"])
            self.assertEqual([device.mac for device in second], ["00:00:00:00:00:02"])
        finally:
            await client.logout()

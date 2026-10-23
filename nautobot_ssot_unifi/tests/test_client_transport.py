"""Certificate verification at the aiounifi configuration boundary."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

import ssl
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from nautobot_ssot_unifi.unifi.client import Client


class ClientTransportTests(IsolatedAsyncioTestCase):
    """Verify client transport behavior."""

    async def test_vlan_channel_is_get_only_and_discards_credentials(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True
        requests = []

        async def response(request):
            requests.append((request.method, request.path, request.data))
            return {"data": [{"_id": "safe-id", "name": "LAN", "vlan": 10, "x_secret": "synthetic-secret"}]}

        try:
            with patch.object(client.api, "request", response):
                networks, profiles = await client._get_vlan_configuration()
            self.assertEqual(requests, [("get", "/rest/networkconf", None), ("get", "/rest/portconf", None)])
            self.assertNotIn("synthetic-secret", repr((networks, profiles)))
        finally:
            await client.logout()

    async def test_network_inventory_fields_survive_without_unrelated_configuration(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True
        network = {
            "_id": "native",
            "name": "Default",
            "purpose": "corporate",
            "vlan_enabled": False,
            "attr_no_delete": True,
            "ip_subnet": "192.0.2.1/24",
            "dhcpd_enabled": True,
            "dhcpd_start": "192.0.2.10",
            "dhcpd_stop": "192.0.2.100",
        }

        async def response(request):
            return {
                "data": (
                    [{**network, "x_secret": "synthetic-secret", "unrelated": "discard"}]
                    if request.path == "/rest/networkconf"
                    else []
                )
            }

        try:
            with patch.object(client.api, "request", response):
                networks, _profiles = await client._get_vlan_configuration()
            self.assertEqual(networks, [network])
        finally:
            await client.logout()

    async def test_network_purpose_survives_the_credential_safe_projection(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True

        async def response(request):
            return {"data": [{"_id": "wan", "name": "Internet", "purpose": "wan", "x_secret": "test-only"}]}

        try:
            with patch.object(client.api, "request", response):
                networks, _profiles = await client._get_vlan_configuration()
            self.assertEqual(networks, [{"_id": "wan", "name": "Internet", "purpose": "wan"}])
        finally:
            await client.logout()

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

    async def test_login_does_not_collect_household_clients(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        requests = []

        async def login():
            return None

        async def response(request):
            requests.append(request)
            if len(requests) != 1 or request.path != "/stat/device":
                raise AssertionError("Unexpected source collection channel.")
            return {"data": []}

        try:
            with patch.object(client.api, "login", login), patch.object(client.api, "request", response):
                self.assertEqual(list(await client.get_devices()), [])
                self.assertTrue(client.logged_in)
                self.assertEqual(len(requests), 1)
        finally:
            await client.logout()

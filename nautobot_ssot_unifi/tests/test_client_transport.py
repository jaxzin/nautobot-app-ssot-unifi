"""Certificate verification at the aiounifi configuration boundary."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

import json
import logging
import ssl
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from aiounifi.errors import AiounifiException

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

    async def test_client_channel_is_get_only_and_projects_association_evidence(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True
        requests = []

        async def response(request):
            requests.append((request.method, request.path, request.data))
            return {
                "data": [
                    {
                        "_id": "c1",
                        "mac": "02:00:00:00:10:01",
                        "network_id": "net-iot",
                        "x_passphrase": "synthetic-secret",
                        "hostname": "discard",
                        "ip": "192.0.2.50",
                    }
                ]
            }

        try:
            with patch.object(client.api, "request", response):
                clients = await client._get_client_configuration()
            self.assertEqual(requests, [("get", "/stat/sta", None)])
            self.assertEqual(clients, [{"_id": "c1", "mac": "02:00:00:00:10:01", "network_id": "net-iot"}])
            self.assertNotIn("synthetic-secret", repr(clients))
            self.assertNotIn("192.0.2.50", repr(clients))
        finally:
            await client.logout()

    async def test_wireless_channel_is_get_only_and_drops_passphrases_on_receipt(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True
        requests = []
        facts = {
            "_id": "wlan-a",
            "name": "Synthetic SSID",
            "enabled": True,
            "hide_ssid": False,
            "security": "wpapsk",
            "wpa_mode": "wpa2",
            "wpa3_support": True,
            "wpa3_transition": True,
            "wpa3_enhanced_192": False,
            "networkconf_id": "net-a",
            "ap_group_mode": "all",
        }

        async def response(request):
            requests.append((request.method, request.path, request.data))
            secrets = {
                "x_passphrase": "synthetic-secret",
                "x_iapp_key": "synthetic-secret",
                "private_preshared_keys": [{"password": "synthetic-secret"}],
                "radius_profile_id": "discard",
            }
            return {"data": [{**facts, **secrets}]}

        try:
            with patch.object(client.api, "request", response):
                rows = await client._get_wireless_configuration()
            self.assertEqual(requests, [("get", "/rest/wlanconf", None)])
            self.assertEqual(rows, [facts])
            self.assertNotIn("synthetic-secret", repr(rows))
        finally:
            await client.logout()

    async def test_debug_transport_logging_never_records_credentials_or_raw_bodies(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True
        body = json.dumps(
            {"meta": {"rc": "ok"}, "data": [{"_id": "wlan-a", "name": "SSID", "x_passphrase": "synthetic-secret"}]}
        ).encode()

        class Response:  # pylint: disable=too-few-public-methods
            """Minimal aiohttp response for the real aiounifi Connectivity path."""

            status = 200
            content_type = "application/json"

            async def read(self):
                """Return the synthetic body."""
                return body

            def __repr__(self):
                """Mimic a response repr that exposes a session cookie."""
                return "<Response Set-Cookie: TOKEN=synthetic-secret>"

        class Exchange:
            """Async context manager returned by session.request."""

            async def __aenter__(self):
                """Open the synthetic response."""
                return Response()

            async def __aexit__(self, *exc):
                """Close without suppressing errors."""
                return False

        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = lambda record: records.append(record.getMessage())
        aiounifi = logging.getLogger("aiounifi")
        previous = aiounifi.level
        aiounifi.addHandler(handler)
        aiounifi.setLevel(logging.DEBUG)
        try:
            with patch.object(client.config.session, "request", lambda *args, **kwargs: Exchange()):
                await client.api.connectivity._request(
                    "post", "https://controller.example/api/login", {"password": "synthetic-secret"}
                )
                rows = await client._get_wireless_configuration()
            self.assertEqual(rows, [{"_id": "wlan-a", "name": "SSID"}])
            self.assertFalse([message for message in records if "synthetic-secret" in message])
        finally:
            aiounifi.removeHandler(handler)
            aiounifi.setLevel(previous)
            await client.logout()

    async def test_error_status_report_fails_through_the_sdk_instead_of_looking_empty(self):
        client = Client(host="controller.example", username="reader", password="test-only")
        client.logged_in = True
        body = b'{"meta": {"rc": "error", "msg": "api.err.Invalid"}, "data": []}'
        reply = AsyncMock(return_value=(SimpleNamespace(content_type="application/json"), body))
        try:
            with patch.object(client.api.connectivity, "_request", reply):
                with self.assertRaises(AiounifiException):
                    await client._get_client_configuration()
        finally:
            await client.logout()

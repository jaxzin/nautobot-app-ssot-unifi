"""Source selection and connection lifetime without a live controller."""

import asyncio
import logging
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from aiounifi.models.site import Site

from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter
from nautobot_ssot_unifi.unifi.client import Client


class SourceLifecycleTests(TestCase):
    def setUp(self):
        self.adapter = UnifiAdapter(
            job=SimpleNamespace(controller=SimpleNamespace(name="Controller"), logger=logging.getLogger(__name__)),
            controller_name="Controller",
            default_location_type="Site",
            default_location_name="Home",
        )

    def tearDown(self):
        if hasattr(self.adapter, "client") and not self.adapter.client.session.closed:
            asyncio.run(self.adapter.client.logout())

    def load(self):
        self.adapter.load(
            host="controller.example", port=443, username="reader", password="test-only", verify_cert=True, timeout=1
        )

    def test_each_site_selects_its_own_device_endpoint(self):
        selected_sites = []

        async def sites(_client):
            return [Site({"name": "default"}), Site({"name": "workshop"})]

        async def devices(client):
            selected_sites.append(client.current_site)
            return []

        with patch.object(Client, "get_sites", sites), patch.object(Client, "get_devices", devices):
            self.load()

        self.assertEqual(selected_sites, ["default", "workshop"])
        self.assertEqual(
            {(site.name, site.location_type__name) for site in self.adapter.get_all("site")},
            {("Home", "Site"), ("workshop", "Site")},
        )
        self.assertTrue(self.adapter.client.session.closed)

    def test_connection_is_closed_when_source_read_fails(self):
        async def sites(_client):
            raise RuntimeError("source unavailable")

        with patch.object(Client, "get_sites", sites):
            with self.assertRaisesRegex(RuntimeError, "source unavailable"):
                self.load()

        self.assertTrue(self.adapter.client.session.closed)

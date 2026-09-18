"""Source selection and connection lifetime without a live controller."""

import asyncio
import csv
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from aiounifi.models.site import Site
from aiounifi.models.device import Device

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

    def test_observed_models_load_without_inventing_catalog_skus(self):
        observed = [
            ("U7HD", "uap"),
            ("U7NHD", "uap"),
            ("U7NHD", "uap"),
            ("U7NHD", "uap"),
            ("UAL6", "uap"),
            ("UAPA6A4", "uap"),
            ("UCGMAX", "udm"),
            ("UMBBE631", "umbb"),
            ("USF5P", "usw"),
            ("USL16LP", "usw"),
            ("USL8LP", "usw"),
            ("USMINI", "usw"),
            ("USPM24P", "usw"),
            ("USWED76", "usw"),
        ]
        catalog = Path(__file__).resolve().parents[1] / "hardware_models.csv"
        with catalog.open(encoding="utf-8") as records:
            self.adapter.job.hardware_models = {row["model"]: row for row in csv.DictReader(records)}

        async def sites(_client):
            return [Site({"name": "default"})]

        async def devices(_client):
            records = []
            for index, (model, category) in enumerate(observed):
                raw = {
                    "model": model,
                    "type": category,
                    "name": f"device-{index}",
                    "serial": f"synthetic-{index}",
                    "port_table": [],
                    "config_network": {"type": "dhcp"},
                }
                if model == "UMBBE631":
                    del raw["port_table"]
                elif model == "UCGMAX":
                    raw["port_table"] = [{"name": f"port-{port}", "port_idx": port} for port in range(5)]
                    raw["port_table"][0].update(ip="192.0.2.1", media=None)
                    raw["port_table"][1].update(ip="", netmask="255.255.255.0", media=2500)
                    raw["port_table"][2].update(ip="198.51.100.1", netmask="255.255.255.0", media="")
                    raw["port_table"][3].update(ip="203.0.113.1", netmask="")
                elif model in ("USPM24P", "USWED76"):
                    media = ["GE", "2P5GE"] if model == "USPM24P" else ["10GE"]
                    raw["port_table"] = [
                        {"name": f"port-{port}", "port_idx": port, "media": value} for port, value in enumerate(media)
                    ]
                records.append(Device(raw))
            return records

        with patch.object(Client, "get_sites", sites), patch.object(Client, "get_devices", devices):
            self.load()

        loaded = self.adapter.get_all("device")
        self.assertEqual(len(loaded), 14)
        roles = {device.device_type__model: device.role__name for device in loaded}
        self.assertEqual(roles["UCGMAX"], "Firewall")
        self.assertEqual(roles["UMBBE631"], "Modem")
        self.assertEqual(roles["UAPA6A4"], "Access-Point")
        self.assertEqual(roles["USWED76"], "Switch")
        parts = {device_type.model: device_type.part_number for device_type in self.adapter.get_all("device_type")}
        self.assertEqual(len(parts), 12)
        self.assertEqual(parts["U7HD"], "UAP-AC-HD")
        for model in ("UAPA6A4", "UCGMAX", "UMBBE631", "USPM24P", "USWED76"):
            self.assertEqual(parts[model], "")
        interfaces = self.adapter.get_all("interface")
        self.assertEqual(len(interfaces), 8)
        self.assertEqual(
            sorted(interface.type for interface in interfaces),
            sorted(
                [
                    "other",
                    "other",
                    "other",
                    "other",
                    "other",
                    "1000base-t",
                    "2.5gbase-t",
                    "10gbase-t",
                ]
            ),
        )
        self.assertEqual(
            [(ip.host, ip.mask_length) for ip in self.adapter.get_all("ip_address")],
            [("198.51.100.1", 24)],
        )
        self.assertEqual(
            [(prefix.network, prefix.prefix_length) for prefix in self.adapter.get_all("prefix")],
            [("198.51.100.0", 24)],
        )
        self.assertTrue(self.adapter.client.session.closed)

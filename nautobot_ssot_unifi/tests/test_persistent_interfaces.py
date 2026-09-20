"""Persistent source interfaces survive projection without invented identities."""

import logging
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from aiounifi.models.device import Device
from aiounifi.models.site import Site

from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter
from nautobot_ssot_unifi.unifi.client import Client


class PersistentInterfaceTests(TestCase):
    def load(self, **tables):
        raw = {
            "name": "test-device",
            "model": "TEST",
            "type": "uap",
            "serial": "synthetic",
            "config_network": {"type": "dhcp"},
            **tables,
        }
        adapter = UnifiAdapter(
            job=SimpleNamespace(
                controller=SimpleNamespace(name="Controller"), hardware_models={}, logger=logging.getLogger(__name__)
            ),
            controller_name="Controller",
            default_location_type="Site",
            default_location_name="Home",
        )

        async def sites(_client):
            return [Site({"name": "default"})]

        async def devices(_client):
            return [Device(raw)]

        with patch.object(Client, "get_sites", sites), patch.object(Client, "get_devices", devices):
            adapter.load(
                host="controller.example",
                port=443,
                username="reader",
                password="test-only",
                verify_cert=True,
                timeout=1,
            )
        self.assertTrue(adapter.client.session.closed)
        return adapter

    def test_ap_ethernet_and_radios_are_not_lost_when_port_table_is_empty(self):
        adapter = self.load(
            port_table=[],
            ethernet_table=[{"name": "eth0"}, {"name": "eth1"}],
            radio_table=[{"name": "wifi0", "radio": "na"}, {"name": "wifi1", "radio": "6e"}],
        )
        self.assertEqual(
            [(i.label, i.type, i.unifi_port_id) for i in adapter.get_all("interface")],
            [
                ("eth0", "other", None),
                ("eth1", "other", None),
                ("wifi0", "other-wireless", None),
                ("wifi1", "other-wireless", None),
            ],
        )
        self.assertEqual(adapter.get_all("ip_address"), [])

    def test_cellular_gateway_ethernet_is_imported_without_port_or_radio_tables(self):
        adapter = self.load(type="umbb", ethernet_table=[{"name": "eth0"}])
        self.assertEqual([i.label for i in adapter.get_all("interface")], ["eth0"])

    def test_gateway_ethernet_aliases_preserve_existing_port_identities(self):
        adapter = self.load(
            port_table=[
                {"name": "Port 1", "port_idx": 1, "media": "2.5GE"},
                {"name": "Port 2", "port_idx": 2, "ifname": "eth1", "media": "GE"},
            ],
            ethernet_table=[{"name": "eth0", "port_idx": 1}, {"name": "eth1"}],
        )
        self.assertEqual(
            [(i.label, i.type, i.unifi_port_id) for i in adapter.get_all("interface")],
            [("Port 1", "2.5gbase-t", 1), ("Port 2", "1000base-t", 2)],
        )

    def test_media_capability_not_negotiated_speed_determines_port_type(self):
        adapter = self.load(
            port_table=[
                {"name": "Port 1", "port_idx": 1, "media": "2.5GE", "speed": 100},
                {"name": "Port 2", "port_idx": 2, "media": "2P5GE", "speed": 0},
                {"name": "SFP+ 1", "port_idx": 3, "media": "SFP+", "speed": 1000},
                {"name": "Unknown", "port_idx": 4, "speed": 10000},
            ]
        )
        self.assertEqual(
            [i.type for i in adapter.get_all("interface")], ["2.5gbase-t", "2.5gbase-t", "10gbase-x-sfpp", "other"]
        )

    def test_switch_internal_interfaces_are_not_guessed_to_be_front_panel_ports(self):
        adapter = self.load(
            port_table=[{"name": "Port 1", "port_idx": 1, "media": "GE"}],
            ethernet_table=[{"name": "eth0", "num_port": 1}, {"name": "srv0"}, {"name": "rt0"}],
        )
        self.assertEqual([i.label for i in adapter.get_all("interface")], ["Port 1", "eth0", "srv0", "rt0"])

    def test_duplicate_source_rows_do_not_create_duplicate_interfaces(self):
        adapter = self.load(
            ethernet_table=[{"name": "eth0"}, {"name": "eth0"}], radio_table=[{"name": "wifi0"}, {"name": "wifi0"}]
        )
        self.assertEqual([i.label for i in adapter.get_all("interface")], ["eth0", "wifi0"])

    def test_conflicting_alias_evidence_fails_instead_of_silently_dropping_an_interface(self):
        with self.assertRaises(ValueError):
            self.load(
                port_table=[
                    {"name": "Port 1", "port_idx": 1, "ifname": "eth0"},
                    {"name": "Port 2", "port_idx": 2, "ifname": "eth1"},
                ],
                ethernet_table=[{"name": "eth0", "port_idx": 2}],
            )

    def test_malformed_interface_table_fails_collection(self):
        for table in ({"ethernet_table": "not-a-table"}, {"radio_table": [{"name": ""}]}):
            with self.subTest(table=next(iter(table))):
                with self.assertRaises(ValueError):
                    self.load(**table)

    def test_distinct_ethernet_names_cannot_share_a_port_identifier(self):
        with self.assertRaises(ValueError):
            self.load(ethernet_table=[{"name": "eth0", "port_idx": 1}, {"name": "eth1", "port_idx": 1}])

    def test_verified_alias_enriches_the_existing_port_without_renaming_it(self):
        adapter = self.load(
            port_table=[{"name": "Port 1", "port_idx": 1}],
            ethernet_table=[
                {"name": "eth0", "port_idx": 1, "media": "2.5GE", "ip": "192.0.2.1", "netmask": "255.255.255.0"}
            ],
        )
        self.assertEqual([(i.label, i.type) for i in adapter.get_all("interface")], [("Port 1", "2.5gbase-t")])
        self.assertEqual([(ip.host, ip.mask_length) for ip in adapter.get_all("ip_address")], [("192.0.2.1", 24)])
        self.assertEqual(len(adapter.get_all("ip_address_to_interface")), 1)

    def test_conflicting_alias_attributes_fail_without_selecting_a_winner(self):
        for conflict in ({"media": "GE"}, {"ip": "192.0.2.2"}, {"netmask": "255.255.0.0"}):
            with self.subTest(conflict=conflict):
                with self.assertRaises(ValueError):
                    self.load(
                        port_table=[
                            {
                                "name": "Port 1",
                                "port_idx": 1,
                                "media": "2.5GE",
                                "ip": "192.0.2.1",
                                "netmask": "255.255.255.0",
                            }
                        ],
                        ethernet_table=[{"name": "eth0", "port_idx": 1, **conflict}],
                    )

    def test_reported_mgmt_does_not_implicitly_adopt_synthetic_management_address(self):
        with self.assertRaisesRegex(ValueError, "management interface"):
            self.load(
                ethernet_table=[{"name": "mgmt"}],
                config_network={"type": "static", "ip": "192.0.2.1", "netmask": "255.255.255.0"},
            )

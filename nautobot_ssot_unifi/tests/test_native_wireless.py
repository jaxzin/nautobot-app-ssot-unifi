"""Native wireless networks: exact SSID bindings, AP-only assignments, retained last-known facts."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import structlog
from aiounifi.models.device import Device as UnifiDevice
from aiounifi.models.site import Site
from asgiref.sync import async_to_sync
from diffsync.enum import DiffSyncFlags
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from nautobot.dcim.models import ControllerManagedDeviceGroup
from nautobot.extras.models import JobResult, MetadataType, ObjectMetadata
from nautobot.ipam.models import VLAN, IPAddressRange, Prefix, VLANGroup
from nautobot.wireless.models import ControllerManagedDeviceGroupWirelessNetworkAssignment as Assignment
from nautobot.wireless.models import WirelessNetwork

from nautobot_ssot_unifi.const import UNIFI_AP_GROUP
from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.ssot._bindings import _BindingSession
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter, UnifiNautobotAdapter
from nautobot_ssot_unifi.tests import test_native_namespaces as fixtures
from nautobot_ssot_unifi.unifi.client import Client

SITE = "site-a"


def wlan(_id="wlan-a", name="Synthetic SSID", **over):
    """One synthetic WPA2/WPA3 transition SSID broadcast by every AP."""
    return {
        "_id": _id,
        "name": name,
        "enabled": True,
        "hide_ssid": False,
        "security": "wpapsk",
        "wpa_mode": "wpa2",
        "wpa3_support": True,
        "wpa3_transition": True,
        "wpa3_enhanced_192": False,
        "networkconf_id": "net-a",
        "ap_group_mode": "all",
        **over,
    }


class NativeWirelessTests(TestCase):
    """Verify SSID writes, holds, retention, and access-point placement against native models."""

    job: Any
    a: Any
    integration: Any
    status: Any
    tag: Any
    bind = fixtures.NativeNamespaceTests.bind
    address = fixtures.NativeNamespaceTests.address

    def setUp(self):
        fixtures.NativeNamespaceTests.setUp(self)
        self.device, self.interface = fixtures.NativeNamespaceTests.fixture(self)
        ownership = MetadataType.objects.get(name="UniFi inventory ownership")
        ownership.content_types.add(
            *(ContentType.objects.get_for_model(model) for model in (VLAN, Prefix, IPAddressRange, WirelessNetwork))
        )
        MetadataType.objects.create(name="UniFi source authentication", data_type="json").content_types.add(
            ContentType.objects.get_for_model(WirelessNetwork)
        )
        self.vlan_group = VLANGroup.objects.create(name="site-a")
        self.vlan = VLAN.objects.create(name="Wireless", vid=20, vlan_group=self.vlan_group, status=self.status)
        self.job.sync_wireless = True

    def source(self, rows, site_name="contract-site", single_site=True):
        """Project one complete synthetic site report."""
        self.job._unifi_bindings = _BindingSession(self.integration.pk)
        source = UnifiAdapter(
            job=self.job,
            controller_name="contract-controller",
            default_location_type="contract-site",
            default_location_name="contract-site",
        )
        source.top_level = ("wireless_device_group", "wireless_network", "wireless_assignment")
        source.add(
            source.wireless_device_group(
                name=UNIFI_AP_GROUP, controller__name="contract-controller", parent__name="default"
            )
        )
        networks = {"net-a": self.vlan.pk, "net-held": None}
        async_to_sync(source._load_wireless)(SITE, site_name, rows, networks, single_site)
        return source

    def sync(self, source):
        """Apply the source to the native target."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        source.sync_to(target, flags=DiffSyncFlags.SKIP_UNMATCHED_DST)
        return target

    def diff(self, source):
        """Return the summary of a fresh comparison."""
        target = UnifiNautobotAdapter(job=self.job)
        target.top_level = source.top_level
        target.load()
        return target.diff_from(source, flags=DiffSyncFlags.SKIP_UNMATCHED_DST).summary()

    @staticmethod
    def scoped(network):
        """The connector's field-scoped raw security record, if any."""
        return ObjectMetadata.objects.filter(
            metadata_type__name="UniFi source authentication", assigned_object_id=network.pk
        ).first()

    def test_preview_apply_repeat_creates_owned_ssids_in_the_access_point_group(self):
        rows = [wlan(), wlan("wlan-b", "Plain", wpa3_support=False, wpa3_transition=False, hide_ssid=True)]
        self.assertEqual(self.diff(self.source(rows))["create"], 5)
        self.assertFalse(WirelessNetwork.objects.exists())
        self.sync(self.source(rows))

        group = ControllerManagedDeviceGroup.objects.get(name=UNIFI_AP_GROUP)
        self.assertEqual((group.parent.name, group.capabilities), ("default", ["wireless"]))
        transition = WirelessNetwork.objects.get(ssid="Synthetic SSID")
        plain = WirelessNetwork.objects.get(ssid="Plain")
        self.assertEqual(
            (transition.name, transition.mode, transition.authentication, transition.hidden),
            ("contract-controller/contract-site/Synthetic SSID", "Central", "WPA3 Personal", False),
        )
        self.assertEqual((plain.authentication, plain.hidden), ("WPA2 Personal", True))
        record = self.scoped(transition)
        self.assertEqual(record.scoped_fields, ["authentication"])
        self.assertEqual(record.value["wpa3_transition"], True)
        self.assertIsNone(self.scoped(plain))
        for network in (transition, plain):
            assignment = Assignment.objects.get(wireless_network=network)
            self.assertEqual((assignment.controller_managed_device_group, assignment.vlan), (group, self.vlan))
            self.assertTrue(network.tags.filter(pk=self.tag.pk).exists())
        self.assertEqual(
            self.diff(self.source(rows)), {"create": 0, "update": 0, "delete": 0, "no-change": 5, "skip": 0}
        )

    def test_renamed_site_and_changed_security_update_the_same_record(self):
        self.sync(self.source([wlan()]))
        original = WirelessNetwork.objects.get()
        self.sync(self.source([wlan(wpa3_support=False, wpa3_transition=False)], site_name="renamed-site"))
        network = WirelessNetwork.objects.get()
        self.assertEqual(network.pk, original.pk)
        self.assertEqual(
            (network.name, network.authentication), ("contract-controller/renamed-site/Synthetic SSID", "WPA2 Personal")
        )
        self.assertIsNone(self.scoped(network))

    def test_holds_and_omission_preserve_existing_inventory(self):
        self.sync(self.source([wlan()]))
        network = WirelessNetwork.objects.get()
        before = (network.name, network.authentication, Assignment.objects.get().vlan_id)
        for rows, single_site, kind, reason in (
            ([wlan(security="wep")], True, "wireless-network", "unsupported-security"),
            ([wlan(networkconf_id="net-held")], True, "wireless-assignment", "unresolved-network"),
            ([wlan(ap_group_mode="groups")], True, "wireless-assignment", "unsupported-scope"),
            ([wlan()], False, "wireless-assignment", "unsupported-scope"),
            ([], True, None, None),
        ):
            with self.subTest(rows=rows, single_site=single_site):
                source = self.source(rows, single_site=single_site)
                if kind:
                    self.assertIn((kind, reason), [(row["kind"], row["reason"]) for row in source._held])
                self.sync(source)
                network.refresh_from_db()
                self.assertEqual((network.name, network.authentication, Assignment.objects.get().vlan_id), before)

    def test_unowned_name_collision_is_held_without_adoption(self):
        existing = WirelessNetwork.objects.create(
            name="contract-controller/contract-site/Synthetic SSID", ssid="other", mode="Central", authentication="Open"
        )
        source = self.source([wlan()])
        self.assertEqual([row["reason"] for row in source._held], ["identity-conflict"])
        self.sync(source)
        existing.refresh_from_db()
        self.assertEqual((existing.ssid, existing.authentication), ("other", "Open"))
        self.assertFalse(Assignment.objects.exists())
        self.assertFalse(ObjectMetadata.objects.filter(assigned_object_id=existing.pk).exists())

    def test_missing_schema_rejects_collection_before_writes(self):
        MetadataType.objects.get(name="UniFi source authentication").delete()
        source = self.source([])
        self.job.controller = SimpleNamespace(external_integration=SimpleNamespace(pk=self.integration.pk))
        with self.assertRaises(ValueError):
            async_to_sync(source._prepare_bindings)()

    def test_wireless_requires_vlan_sync(self):
        with patch("nautobot_ssot_unifi.ssot.adapters.Client", side_effect=AssertionError("No source request")):
            with self.assertRaises(ValidationError) as caught:
                UnifiDataSource().run(
                    dryrun=True,
                    debug=False,
                    controller=SimpleNamespace(),
                    default_location=None,
                    location_type=None,
                    sync_wireless=True,
                )
        self.assertIn("sync_wireless", caught.exception.message_dict)

    def test_job_moves_access_points_and_keeps_them_when_wireless_is_disabled(self):
        user = get_user_model().objects.create_user(username="contract-user")
        controller = SimpleNamespace(
            name="contract-controller",
            location=self.device.location,
            external_integration=SimpleNamespace(
                pk=self.integration.pk,
                remote_url="https://controller.invalid",
                verify_ssl=True,
                timeout=1,
                secrets_group=SimpleNamespace(get_secret_value=lambda **kwargs: "synthetic-test-only"),
            ),
        )
        device = {"_id": "device-a", "name": "contract-device", "model": "TEST", "type": "uap", "serial": "synthetic"}
        networks = [{"_id": "net-a", "name": "Wireless", "vlan": 20, "vlan_enabled": True, "purpose": "corporate"}]
        self.vlan.delete()  # The VLAN channel creates its own owned record.
        config = structlog.get_config()
        try:
            for sync_wireless in (True, False):
                job = UnifiDataSource()
                job.job_result = JobResult.objects.create(name="contract-unifi", user=user)
                with patch.object(
                    Client, "get_sites", AsyncMock(return_value=[Site({"name": "default", "_id": SITE})])
                ), patch.object(Client, "get_devices", AsyncMock(return_value=[UnifiDevice(device)])), patch.object(
                    Client, "_get_vlan_configuration", AsyncMock(return_value=(networks, []))
                ), patch.object(
                    Client, "_get_wireless_configuration", AsyncMock(return_value=[wlan()])
                ):
                    job.run(
                        dryrun=False,
                        debug=False,
                        controller=controller,
                        default_location=None,
                        location_type=None,
                        namespace=self.a,
                        sync_vlans=True,
                        vlan_groups=[{"site_id": SITE, "vlan_group_id": str(self.vlan_group.pk)}],
                        sync_wireless=sync_wireless,
                    )
                self.device.refresh_from_db()
                with self.subTest(sync_wireless=sync_wireless):
                    self.assertEqual(self.device.controller_managed_device_group.name, UNIFI_AP_GROUP)
                    self.assertEqual(Assignment.objects.get().vlan.vid, 20)
        finally:
            structlog.configure(**config)

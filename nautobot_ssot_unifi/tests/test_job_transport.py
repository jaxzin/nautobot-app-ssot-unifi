"""The controller hostname must reach TLS without losing its certificate identity."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from diffsync.enum import DiffSyncFlags

from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter


class JobTransportTests(TestCase):
    def test_missing_source_objects_are_not_deleted(self):
        job = UnifiDataSource()
        self.assertTrue(job.diffsync_flags & DiffSyncFlags.SKIP_UNMATCHED_DST)
        self.assertFalse(job.diffsync_flags & DiffSyncFlags.SKIP_UNMATCHED_SRC)

    def test_controller_hostname_is_preserved_for_tls(self):
        job = UnifiDataSource()
        integration = SimpleNamespace(
            remote_url="https://controller.example:8443",
            secrets_group=SimpleNamespace(get_secret_value=Mock(return_value="test-only")),
            verify_ssl=True,
            timeout=30,
        )
        job.controller = SimpleNamespace(name="Controller", external_integration=integration)
        job.default_location = SimpleNamespace(name="Home")
        job.default_location_type = SimpleNamespace(name="Site")
        with patch("nautobot_ssot_unifi.jobs.fqdn_to_ip", return_value="192.0.2.1", create=True):
            with patch.object(UnifiAdapter, "load") as load:
                job.load_source_adapter()
        self.assertEqual(load.call_args.kwargs["host"], "controller.example")
        self.assertEqual(load.call_args.kwargs["port"], 8443)
        self.assertIs(load.call_args.kwargs["verify_cert"], True)

"""The controller hostname must reach TLS without losing its certificate identity."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from diffsync.enum import DiffSyncFlags
from django.core.exceptions import ValidationError
from nautobot.ipam.models import Namespace
from nautobot_ssot.jobs.base import DataSource

from nautobot_ssot_unifi.jobs import UnifiDataSource
from nautobot_ssot_unifi.ssot.adapters import UnifiAdapter
from nautobot_ssot_unifi.ssot.address_scopes import AddressScopeResolver


class JobTransportTests(TestCase):
    def test_adapter_forwards_native_constructor_arguments(self):
        adapter = UnifiAdapter(
            job=SimpleNamespace(),
            controller_name="Controller",
            default_location_name="Site",
            default_location_type="Site",
            name="scoped-source",
            debug=True,
        )
        self.assertEqual(adapter.name, "scoped-source")
        self.assertTrue(adapter.debug)

    def test_controller_validation_does_not_echo_url_credentials(self):
        controller = SimpleNamespace(
            external_integration=SimpleNamespace(
                remote_url="ftp://synthetic-user:synthetic-password@controller.invalid"
            )
        )
        with patch.object(DataSource, "validate_data", return_value={"controller": controller}):
            with self.assertRaises(ValidationError) as error:
                UnifiDataSource.validate_data({})
        self.assertNotIn("synthetic-password", str(error.exception))
        self.assertNotIn("controller.invalid", str(error.exception))

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
        job.namespace = Namespace(name="Global")
        job.address_scope_resolver = AddressScopeResolver(None, "Global")
        with patch("nautobot_ssot_unifi.jobs.fqdn_to_ip", return_value="192.0.2.1", create=True):
            with patch.object(UnifiAdapter, "load") as load:
                job.load_source_adapter()
        self.assertEqual(load.call_args.kwargs["host"], "controller.example")
        self.assertEqual(load.call_args.kwargs["port"], 8443)
        self.assertIs(load.call_args.kwargs["verify_cert"], True)

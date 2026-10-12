"""Protected held output includes only approved identities and stable codes."""

import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from nautobot.ipam.models import Namespace
from nautobot_ssot.jobs.base import DataSource
from nautobot_ssot_unifi.jobs import UnifiDataSource


class HeldArtifactTests(TestCase):
    def test_job_publishes_closed_held_projection_without_source_configuration(self):
        job = UnifiDataSource()
        job.create_file = Mock()
        job.source_adapter = SimpleNamespace(
            _held=[
                {
                    "kind": "device",
                    "site_id": "site-a",
                    "source_id": "device-a",
                    "reason": "identity-conflict",
                    "password": "synthetic-secret-never-output",
                },
                {
                    "kind": "neighbor",
                    "site_id": "site-a",
                    "source_id": '["device-a","port_idx",1]',
                    "reason": "incomplete-neighbor-evidence",
                },
            ]
        )
        controller = SimpleNamespace(location=SimpleNamespace(location_type=object()))
        with patch.object(DataSource, "run"):
            job.run(
                True,
                False,
                controller,
                None,
                None,
                namespace=Namespace(name="Global"),
                sync_firmware=True,
                sync_neighbors=True,
            )
        self.assertTrue(job.create_file.called, "The native Job must publish its held artifact.")
        name, content = job.create_file.call_args.args
        self.assertEqual(name, "unifi-held.json")
        self.assertEqual(
            json.loads(content),
            {
                "schema_version": 1,
                "held": [
                    {"kind": "firmware", "source_id": "device-a", "reason": "identity-conflict"},
                    {"kind": "neighbor", "source_id": "device-a", "reason": "identity-conflict"},
                    {
                        "kind": "neighbor",
                        "source_id": '["device-a","port_idx",1]',
                        "reason": "incomplete-neighbor-evidence",
                    },
                ],
            },
        )
        self.assertNotIn("synthetic-secret", content)
        self.assertNotIn("site-a", content)

    def test_invalid_reason_and_secret_shaped_identity_fail_closed(self):
        from nautobot_ssot_unifi.ssot._held import _held_report

        for source_id, reason in (
            ("password=synthetic-secret", "missing-platform"),
            ("device-a", "arbitrary-source-error"),
            ("a" * 64, "missing-platform"),
        ):
            source = SimpleNamespace(_held=[{"kind": "firmware", "source_id": source_id, "reason": reason}])
            with self.subTest(source_id=source_id), self.assertRaises(ValueError) as error:
                _held_report(source)
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_disabled_extensions_do_not_project_binding_holds_as_authoritative_data(self):
        from nautobot_ssot_unifi.ssot._held import _held_report

        source = SimpleNamespace(_held=[{"kind": "device", "source_id": "device-a", "reason": "missing-binding"}])
        self.assertEqual(_held_report(source), {"schema_version": 1, "held": []})

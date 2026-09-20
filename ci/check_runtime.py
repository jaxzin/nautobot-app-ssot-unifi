"""Run the read-only connector compatibility checks without live credentials."""

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NAUTOBOT_CONFIG", str(ROOT / "development/compatibility_config.py"))
os.environ.setdefault("NAUTOBOT_ROOT", str(ROOT / ".runtime"))

import nautobot  # noqa: E402

nautobot.setup()

suite = unittest.defaultTestLoader.loadTestsFromNames(
    [
        "nautobot_ssot_unifi.tests.test_persistent_interfaces",
        "nautobot_ssot_unifi.tests.test_source_lifecycle",
        "nautobot_ssot_unifi.tests.test_client_transport",
        "nautobot_ssot_unifi.tests.test_job_transport",
        "nautobot_ssot_unifi.tests.test_primary_ips",
        "nautobot_ssot_unifi.tests.test_inventory_ownership",
    ]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())

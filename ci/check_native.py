"""Run rollback-only ORM regressions against the prepared disposable database."""

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("NAUTOBOT_ROOT", str(ROOT / ".runtime"))
if "NAUTOBOT_CONFIG" not in os.environ or sys.argv[1:2] != ["test"]:
    raise SystemExit("Provide the disposable NAUTOBOT_CONFIG and the test argument.")

import nautobot  # noqa: E402

nautobot.setup()

from django.conf import settings  # noqa: E402

database = settings.DATABASES["default"]
if database["NAME"] != "unifi_namespace_check" or database["HOST"] not in ("127.0.0.1", "localhost"):
    raise SystemExit("Native checks require the loopback unifi_namespace_check database.")

# Use the already migrated disposable database. Django TestCase rolls every
# fixture back; no database creation, migration, flush, or production endpoint.
suite = unittest.defaultTestLoader.loadTestsFromNames(
    sys.argv[2:] or ["nautobot_ssot_unifi.tests.test_native_namespaces"]
)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())

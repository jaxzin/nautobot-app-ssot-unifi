"""Firmware projections use reported versions, not invented image metadata."""

# White-box regressions intentionally exercise private connector boundaries.
# Repeated fixture arrangements keep each regression independently readable.
# pylint: disable=protected-access,duplicate-code

from unittest import TestCase

from nautobot_ssot_unifi.ssot import _firmware


class FirmwareTests(TestCase):
    """Verify firmware behavior."""

    def test_reported_version_is_trimmed_without_rewriting_it(self):
        self.assertEqual(_firmware._version("  7.2.34+build.1 \t"), "7.2.34+build.1")

    def test_missing_or_invalid_version_is_held_without_echoing_payload(self):
        for value in (None, "", " ", 123, "test-only-sensitive\nvalue", "a" * 257):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(_firmware._FirmwareHold) as error:
                    _firmware._version(value)
                self.assertEqual(error.exception.reason, "missing-version")
                self.assertNotIn("test-only-sensitive", str(error.exception))

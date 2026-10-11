"""Private reported-version validation and ownership gate for native firmware."""

from nautobot.core.constants import CHARFIELD_MAX_LENGTH


class _FirmwareHold(ValueError):
    def __init__(self, reason):
        if reason not in {"missing-version", "missing-platform", "foreign-owner"}:
            raise ValueError("Invalid firmware hold classification.")
        self.reason = reason
        super().__init__(reason)


def _version(value):
    if not isinstance(value, str):
        raise _FirmwareHold("missing-version")
    version = value.strip()
    if not version or len(version) > CHARFIELD_MAX_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in version):
        raise _FirmwareHold("missing-version")
    return version

"""Closed protected artifact projection; no source config or exception text."""

import json
import re
from nautobot_ssot_unifi.ssot._bindings import _source_key, _text

_KINDS = {"firmware", "vlan", "vlan-membership", "neighbor"}
_REASONS = {
    "missing-platform",
    "missing-version",
    "invalid-vlan",
    "duplicate-vlan",
    "missing-profile",
    "incomplete-membership",
    "unresolved-endpoint",
    "unsupported-endpoint",
    "foreign-owner",
    "missing-binding",
    "ambiguous-binding",
    "identity-conflict",
    "incomplete-neighbor-evidence",
}
_UNSAFE = re.compile(
    r"secret|password|passwd|token|credential|authorization|bearer|cookie|private.?key|"
    r"https?://|[A-Za-z0-9_+/=-]{40,}",
    re.I,
)


def _safe_identity(value):
    if not _text(value) or len(value) > 1024 or _UNSAFE.search(value):
        raise ValueError("Invalid protected UniFi held projection.")
    if value.startswith("["):
        _source_key("interface", "00000000-0000-0000-0000-000000000000", "validation", value)
    return value


def _held_report(source):
    """Classify base identity holds for enabled dependents; discard extra keys."""
    held, seen = [], set()
    for row in source._held:
        reason = row.get("reason")
        if reason not in _REASONS:
            raise ValueError("Invalid protected UniFi held projection.")
        identity = _safe_identity(row.get("source_id"))
        kind = row.get("kind")
        if kind in _KINDS:
            kinds = [kind]
        elif kind == "device":
            kinds = [
                name
                for flag, name in (("sync_firmware", "firmware"), ("sync_neighbors", "neighbor"))
                if getattr(source, flag, False)
            ]
        elif kind == "interface":
            kinds = [
                name
                for flag, name in (("sync_vlans", "vlan-membership"), ("sync_neighbors", "neighbor"))
                if getattr(source, flag, False)
            ]
        else:
            raise ValueError("Invalid protected UniFi held projection.")
        for name in kinds:
            key = name, identity, reason
            if key not in seen:
                held.append({"kind": name, "source_id": identity, "reason": reason})
                seen.add(key)
    return {"schema_version": 1, "held": held}


def _publish_held(job):
    source = job.source_adapter
    # Native Job inputs, not optional attributes on a test or partially built adapter.
    for flag in ("sync_firmware", "sync_vlans", "sync_neighbors"):
        setattr(source, flag, getattr(job, flag, False))
    job.held_report = _held_report(source)
    job.create_file("unifi-held.json", json.dumps(job.held_report, ensure_ascii=False, sort_keys=True))

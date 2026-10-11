"""Private exact-identity lookup for the connector's native ownership metadata."""

from collections import defaultdict
import json
from uuid import UUID

_OWNERSHIP_TYPE = "UniFi inventory ownership"
_CLAIM_PATHS = {
    "device": frozenset({"software_version"}),
    "interface": frozenset({"mode", "untagged_vlan", "tagged_vlans"}),
    "vlan": frozenset({"name", "vid", "vlan_group", "status"}),
}
_APP_LABELS = {"device": "dcim", "interface": "dcim", "vlan": "ipam"}


def _text(value):
    return (
        isinstance(value, str) and bool(value.strip()) and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _interface_id(device_id, *, port_idx=None, ifname=None):
    """Encode the exact parent and source port identity without ambiguous joins."""
    if not _text(device_id):
        raise ValueError("Invalid interface source identity.")
    if port_idx is not None:
        if type(port_idx) is not int or port_idx < 0:
            raise ValueError("Invalid interface source identity.")
        parts = [device_id, "port_idx", port_idx]
    elif _text(ifname):
        parts = [device_id, "ifname", ifname]
    else:
        raise ValueError("Invalid interface source identity.")
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=False)


def _uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid UniFi binding prerequisite.") from None


def _source_key(kind, integration_id, site_id, source_id):
    if kind not in _CLAIM_PATHS or not _text(site_id) or not _text(source_id):
        raise ValueError("Invalid UniFi binding prerequisite.")
    if kind == "interface":
        try:
            parts = json.loads(source_id)
            if not isinstance(parts, list) or len(parts) != 3:
                raise ValueError
            device_id, discriminator, value = parts
            if discriminator == "port_idx":
                canonical = _interface_id(device_id, port_idx=value)
            elif discriminator == "ifname":
                canonical = _interface_id(device_id, ifname=value)
            else:
                raise ValueError
            if canonical != source_id:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid UniFi binding prerequisite.") from None
    return kind, str(_uuid(integration_id)), site_id, source_id


class _BindingHold(ValueError):
    """An unresolved exact identity, safe for a protected held-item projection."""

    def __init__(self, reason):
        if reason not in {"missing-binding", "ambiguous-binding", "identity-conflict", "foreign-owner"}:
            raise ValueError("Invalid binding hold classification.")
        self.reason = reason
        super().__init__(reason)


class _BindingIndex:
    """Validate one metadata snapshot and resolve exact source keys to UUIDs.

    This does not authorize admission, claim fields, or write inventory. Native
    mutation callbacks must re-read/recheck the binding inside their transaction.
    """

    def __init__(self, records):
        self._by_source = defaultdict(list)
        self._by_target = {}
        for record in records:
            value = record.get("value")
            kind = record.get("assigned_object_type__model")
            if (
                not isinstance(kind, str)
                or kind not in _CLAIM_PATHS
                or record.get("assigned_object_type__app_label") != _APP_LABELS[kind]
                or record.get("scoped_fields") != []
                or not isinstance(value, dict)
                or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1
            ):
                raise ValueError("Invalid UniFi binding prerequisite.")
            paths = value.get("field_paths")
            if (
                not isinstance(paths, list)
                or any(not isinstance(path, str) for path in paths)
                or len(set(paths)) != len(paths)
                or not set(paths).issubset(_CLAIM_PATHS[kind])
            ):
                raise ValueError("Invalid UniFi binding prerequisite.")
            key = _source_key(kind, value.get("integration_id"), value.get("site_id"), value.get("source_object_id"))
            target = _uuid(record.get("assigned_object_id"))
            target_key = kind, target
            if target_key in self._by_target:
                # Invalid native cardinality is not a second owner to merge.
                raise ValueError("Invalid UniFi binding prerequisite.")
            self._by_target[target_key] = (key, frozenset(paths))
            self._by_source[key].append(target)

    @classmethod
    def from_orm(cls):
        """Read only this connector's native metadata, including invalid scopes."""
        from django.db.models import F
        from nautobot.extras.models import ObjectMetadata

        return cls(
            ObjectMetadata.objects.filter(metadata_type__name=_OWNERSHIP_TYPE).values(
                "assigned_object_type__app_label",
                "assigned_object_type__model",
                "assigned_object_id",
                "scoped_fields",
                value=F("_value"),
            )
        )

    def resolve(self, kind, integration_id, site_id, source_id):
        """Return exactly one UUID; a missing or duplicate identity is held."""
        matches = self._by_source.get(_source_key(kind, integration_id, site_id, source_id), ())
        if not matches:
            raise _BindingHold("missing-binding")
        if len(matches) != 1:
            raise _BindingHold("ambiguous-binding")
        return matches[0]

    def claimed_paths(self, kind, target_id):
        """Return only declared field ownership, not the whole-object scope."""
        claim = self._by_target.get((kind, _uuid(target_id)))
        return claim[1] if claim else frozenset()

    def assert_target(self, kind, target_id, integration_id, site_id, source_id):
        """Require the target's existing document to retain its exact owner/key."""
        key = _source_key(kind, integration_id, site_id, source_id)
        target = _uuid(target_id)
        claim = self._by_target.get((kind, target))
        if claim is None:
            raise _BindingHold("missing-binding")
        current_key, _paths = claim
        if current_key[1:3] != key[1:3]:
            raise _BindingHold("foreign-owner")
        if current_key != key:
            raise _BindingHold("identity-conflict")
        resolved = self.resolve(kind, integration_id, site_id, source_id)
        if resolved != target:
            raise _BindingHold("identity-conflict")
        return target

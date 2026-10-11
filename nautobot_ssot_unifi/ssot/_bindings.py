"""Private exact-identity lookup for the connector's native ownership metadata."""

from collections import defaultdict
import json
from uuid import UUID
from uuid import uuid4

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


def _management_id(device_id):
    """Identify the device-level address holder, never a physical interface."""
    if not _text(device_id):
        raise ValueError("Invalid interface source identity.")
    return json.dumps([device_id, "management", "config_network"], separators=(",", ":"), ensure_ascii=False)


def _physical_interface_id(source_id):
    """Validate the exact identity and exclude logical holders from neighbors."""
    _source_key("interface", "00000000-0000-0000-0000-000000000000", "validation", source_id)
    return json.loads(source_id)[1] != "management"


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
            elif discriminator == "management" and value == "config_network":
                canonical = _management_id(device_id)
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


class _BindingSession:
    """Plan UUID references read-only; recheck/admit under an integration lock."""

    def __init__(self, integration_id, index=None):
        self.integration_id = str(_uuid(integration_id))
        self.index = index if index is not None else _BindingIndex.from_orm()
        self.documents = {}
        self._collisions = {}
        self._seen = set()
        self._missing_os = set()

    def observe_interfaces(self, site_id, device_id, source_ids):
        """Hold OS-only substitutions when an old OS identity is also omitted."""
        for key in self.index._by_source:
            if key[:3] != ("interface", self.integration_id, site_id):
                continue
            parent, discriminator, _value = json.loads(key[3])
            if parent == device_id and discriminator == "ifname" and key[3] not in source_ids:
                self._missing_os.add((site_id, device_id))

    @staticmethod
    def _model(kind):
        from nautobot.dcim.models import Device, Interface
        from nautobot.ipam.models import VLAN

        return {"device": Device, "interface": Interface, "vlan": VLAN}[kind]

    def targets(self, kind):
        return [
            target
            for (model, target), (key, _paths) in self.index._by_target.items()
            if model == kind and key[1] == self.integration_id and len(self.index._by_source[key]) == 1
        ]

    def plan(self, kind, site_id, source_id, collision_lookup):
        """Resolve exact bindings; names only detect conflicts, never adopt."""
        key = _source_key(kind, self.integration_id, site_id, source_id)
        if key in self._seen:
            # Reject the complete source snapshot before any native sync starts.
            raise ValueError("Duplicate UniFi source identity.")
        self._seen.add(key)
        try:
            target = self.index.resolve(*key)
            paths = self.index.claimed_paths(kind, target)
        except _BindingHold as error:
            if error.reason != "missing-binding":
                raise
            if kind == "interface":
                parent, discriminator, _value = json.loads(source_id)
                if discriminator == "ifname" and (site_id, parent) in self._missing_os:
                    raise _BindingHold("identity-conflict") from None
            if self._model(kind).objects.filter(**collision_lookup).exists():
                raise _BindingHold("identity-conflict") from None
            target, paths = uuid4(), frozenset()
        self._collisions[(kind, target)] = dict(collision_lookup)
        self.documents[(kind, target)] = {
            "schema_version": 1,
            "integration_id": self.integration_id,
            "site_id": site_id,
            "source_object_id": source_id,
            "field_paths": sorted(paths),
        }
        return target

    def _document(self, kind, target):
        document = self.documents.get((kind, _uuid(target)))
        if document is None:
            raise _BindingHold("missing-binding")
        return document

    def check(self, kind, target):
        """Check the live binding, not the read-only preview snapshot."""
        from django.contrib.contenttypes.models import ContentType
        from nautobot.extras.models import ObjectMetadata

        document = self._document(kind, target)
        # Called only inside native mutation transactions. Lock the document so
        # CI adoption/other owners cannot change it between check and save.
        list(
            ObjectMetadata.objects.select_for_update()
            .filter(
                metadata_type__name=_OWNERSHIP_TYPE,
                assigned_object_type=ContentType.objects.get_for_model(self._model(kind)),
                assigned_object_id=target,
            )
            .values_list("pk", flat=True)
        )
        return _BindingIndex.from_orm().assert_target(
            kind, target, self.integration_id, document["site_id"], document["source_object_id"]
        )

    def check_parent(self, interface_id, device_id):
        """Require the Interface tuple and native Device binding to agree."""
        interface = self._document("interface", interface_id)
        device = self._document("device", device_id)
        self.check("device", device_id)
        if (
            interface["site_id"] != device["site_id"]
            or json.loads(interface["source_object_id"])[0] != device["source_object_id"]
        ):
            raise _BindingHold("identity-conflict")

    def check_native_parent(self, interface_id, device_id=None):
        """Check the locked native parent as well as both source documents."""
        from nautobot.dcim.models import Interface

        obj = Interface.objects.select_for_update().get(pk=interface_id)
        if device_id is not None and obj.device_id != _uuid(device_id):
            raise _BindingHold("identity-conflict")
        self.check_parent(interface_id, obj.device_id)

    def admit(self, kind, target, create):
        """Create the native record and its binding in the caller's transaction."""
        from nautobot.extras.models import MetadataType, ObjectMetadata

        document = self._document(kind, target)
        current = _BindingIndex.from_orm()
        try:
            current.resolve(kind, self.integration_id, document["site_id"], document["source_object_id"])
        except _BindingHold as error:
            if error.reason != "missing-binding":
                raise
        else:
            raise _BindingHold("identity-conflict")
        if self._model(kind).objects.filter(pk=target).exists():
            raise _BindingHold("identity-conflict")
        if self._model(kind).objects.filter(**self._collisions[(kind, _uuid(target))]).exists():
            raise _BindingHold("identity-conflict")
        result = create()
        obj = self._model(kind).objects.get(pk=target)
        metadata = ObjectMetadata(
            metadata_type=MetadataType.objects.get(name=_OWNERSHIP_TYPE), assigned_object=obj, scoped_fields=[]
        )
        metadata.value = document
        metadata.validated_save()
        return result

    def lock(self):
        """Serialize connector admissions/updates for this native integration."""
        from nautobot.extras.models import ExternalIntegration

        ExternalIntegration.objects.select_for_update().get(pk=self.integration_id)

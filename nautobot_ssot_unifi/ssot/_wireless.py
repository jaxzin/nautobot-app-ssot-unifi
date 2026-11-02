"""Private configured-SSID projection; source facts never configure the controller."""

from collections import Counter

from nautobot_ssot_unifi.ssot._bindings import _OWNERSHIP_TYPE, _text

_SOURCE_AUTHENTICATION_TYPE = "UniFi source authentication"
_SCOPED_FIELDS = ["authentication"]
_SECURITY_FIELDS = ("security", "wpa_mode", "wpa3_support", "wpa3_transition", "wpa3_enhanced_192")
_NAME_LENGTH = 255

# Exact controller security combinations with a native choice. A WPA2/WPA3
# transition network maps to its WPA3 choice; the raw source facts are kept in
# field-scoped metadata so the native value is never read as the whole truth.
_AUTHENTICATION = {
    ("open", None, False, False, False): ("Open", False),
    ("wpapsk", "wpa2", False, False, False): ("WPA2 Personal", False),
    ("wpapsk", "wpa2", True, True, False): ("WPA3 Personal", True),
    ("wpaeap", "wpa2", False, False, False): ("WPA2 Enterprise", False),
    ("wpaeap", "wpa2", True, True, False): ("WPA3 Enterprise", True),
}


class _WirelessHold(ValueError):
    """Safe classification, never source configuration or arbitrary exception text."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _prerequisites():
    """Require the operator-created schema; this connector never creates it."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.extras.models import MetadataType
    from nautobot.wireless.models import WirelessNetwork

    content_type = ContentType.objects.get_for_model(WirelessNetwork)
    for name in (_OWNERSHIP_TYPE, _SOURCE_AUTHENTICATION_TYPE):
        definition = MetadataType.objects.filter(name=name).first()
        if (
            definition is None
            or definition.data_type != "json"
            or not definition.content_types.filter(pk=content_type.pk).exists()
        ):
            raise ValueError("Invalid UniFi wireless schema prerequisite.")


def _authentication(row):
    """Return the native choice and, for a mapped transition mode, the raw facts."""
    if any(type(row.get(key)) is not bool for key in _SECURITY_FIELDS[2:]):
        raise _WirelessHold("unsupported-security")
    security = row.get("security")
    mode = None if security == "open" else row.get("wpa_mode")
    key = (security, mode, *(row[field] for field in _SECURITY_FIELDS[2:]))
    if key not in _AUTHENTICATION:
        raise _WirelessHold("unsupported-security")
    choice, mapped = _AUTHENTICATION[key]
    return choice, ({field: row.get(field) for field in _SECURITY_FIELDS} if mapped else None)


def _wireless_rows(rows, controller_name, site_name, networks, single_site):
    """Project complete SSID facts; hold each network or assignment that lacks evidence.

    networks: {source network id: VLAN UUID or None} from the same site's VLAN projection.
    Returns (rows, held) where held entries are (source_id, kind, reason).
    """
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not _text(row.get("_id")) for row in rows):
        raise ValueError("Invalid UniFi wireless channel.")
    ids = Counter(row["_id"] for row in rows)
    if any(count > 1 for count in ids.values()):
        raise ValueError("Duplicate UniFi wireless source identity.")
    names = Counter(row.get("name") for row in rows if _text(row.get("name")))
    projected, held = [], []
    for row in rows:
        source_id = row["_id"]
        ssid = row.get("name")
        name = f"{controller_name}/{site_name}/{ssid}"
        try:
            if (
                not _text(ssid)
                or type(row.get("enabled")) is not bool
                or type(row.get("hide_ssid")) is not bool
                or len(name) > _NAME_LENGTH
            ):
                raise _WirelessHold("invalid-wireless")
            if names[ssid] > 1:
                raise _WirelessHold("duplicate-wireless")
            authentication, source_authentication = _authentication(row)
        except _WirelessHold as error:
            held.append((source_id, "wireless-network", error.reason))
            continue
        result = {
            "source_id": source_id,
            "name": name,
            "ssid": ssid,
            "enabled": row["enabled"],
            "hidden": row["hide_ssid"],
            "authentication": authentication,
            "source_authentication": source_authentication,
            "vlan_id": None,
        }
        network = row.get("networkconf_id")
        if row.get("ap_group_mode") != "all" or not single_site:
            # AP-group or tag scoping needs proven disjoint membership; a
            # controller-wide group cannot express one site's "all APs".
            held.append((source_id, "wireless-assignment", "unsupported-scope"))
        elif not _text(network) or networks.get(network) is None:
            held.append((source_id, "wireless-assignment", "unresolved-network"))
        else:
            result["vlan_id"] = networks[network]
        projected.append(result)
    return projected, held


def _read_source_authentication(obj):
    """Return the connector's field-scoped raw security facts from prefetched metadata."""
    for metadata in obj.associated_object_metadata.all():
        if metadata.metadata_type.name == _SOURCE_AUTHENTICATION_TYPE and metadata.scoped_fields == _SCOPED_FIELDS:
            return metadata.value
    return None


def _write_source_authentication(obj, value):
    """Keep exactly one scoped record while the native choice is a mapping; remove it otherwise."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.extras.models import MetadataType, ObjectMetadata

    metadata_type = MetadataType.objects.get(name=_SOURCE_AUTHENTICATION_TYPE)
    existing = ObjectMetadata.objects.filter(
        metadata_type=metadata_type,
        assigned_object_type=ContentType.objects.get_for_model(type(obj)),
        assigned_object_id=obj.pk,
    )
    if value is None:
        existing.delete()
        return
    metadata = existing.first() or ObjectMetadata(metadata_type=metadata_type, assigned_object=obj)
    metadata.scoped_fields = list(_SCOPED_FIELDS)
    metadata.value = value
    metadata.validated_save()

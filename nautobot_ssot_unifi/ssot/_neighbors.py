"""Private complete-report gates and latest native neighbor observations."""

from datetime import datetime, timezone
from math import isfinite
from uuid import UUID

_OBSERVATION_TYPE = "UniFi neighbor observations"
_RELATIONSHIP_KEY = "unifi_observed_neighbor"


def _prerequisites():
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Interface
    from nautobot.extras.models import MetadataType, Relationship

    content_type = ContentType.objects.get_for_model(Interface)
    relationship = Relationship.objects.filter(key=_RELATIONSHIP_KEY).first()
    metadata_type = MetadataType.objects.filter(name=_OBSERVATION_TYPE).first()
    if (
        relationship is None
        or relationship.type != "symmetric-many-to-many"
        or relationship.source_type_id != content_type.pk
        or relationship.destination_type_id != content_type.pk
        or metadata_type is None
        or metadata_type.data_type != "json"
        or not metadata_type.content_types.filter(pk=content_type.pk).exists()
    ):
        raise ValueError("Invalid UniFi neighbor schema prerequisite.")


class _NeighborHold(ValueError):
    """Non-secret endpoint/evidence classification."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _source_time(value):
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise _NeighborHold("incomplete-neighbor-evidence")
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, ValueError, OSError):
        raise _NeighborHold("incomplete-neighbor-evidence") from None


def _utc(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid UniFi neighbor observation prerequisite.") from None


def _fresh(device, now, previous):
    seen = _source_time(device.get("last_seen"))
    deadline = _source_time(device.get("next_heartbeat_at"))
    if (
        type(device.get("state")) is not int
        or device["state"] != 1
        or not seen <= now <= deadline
        or (previous is not None and seen <= previous)
    ):
        raise _NeighborHold("incomplete-neighbor-evidence")
    return seen


def _evidence(device, port, now, previous, resolve):
    _fresh(device, now, previous)
    lldp = port.get("lldp_table")
    if not isinstance(lldp, list) or any(not isinstance(row, dict) for row in lldp):
        raise _NeighborHold("incomplete-neighbor-evidence")
    peers = {}
    for row in lldp:
        chassis = row.get("lldp_chassis_id", row.get("chassis_id"))
        remote = row.get("lldp_port_id", row.get("port_id"))
        if not isinstance(chassis, str) or not isinstance(remote, str) or not chassis or not remote:
            raise _NeighborHold("incomplete-neighbor-evidence")
        target = resolve(chassis, remote, "ifname")
        peers.setdefault(target, set()).add("lldp")
    uplink = device.get("uplink")
    if not isinstance(uplink, dict) or uplink.get("type") not in {"wire", "wireless", "none"}:
        raise _NeighborHold("incomplete-neighbor-evidence")
    if uplink["type"] == "wire":
        if type(port.get("is_uplink")) is not bool:
            raise _NeighborHold("incomplete-neighbor-evidence")
        if port["is_uplink"]:
            remote = uplink.get("uplink_remote_port")
            chassis = uplink.get("uplink_mac")
            if type(remote) is not int or remote < 0 or not isinstance(chassis, str) or not chassis:
                raise _NeighborHold("incomplete-neighbor-evidence")
            target = resolve(chassis, remote, "port_idx")
            peers.setdefault(target, set()).add("wired-uplink")
    return peers


def _pair(first, second):
    first, second = UUID(str(first)), UUID(str(second))
    if first == second:
        raise _NeighborHold("unsupported-endpoint")
    return tuple(sorted((first, second), key=str))


def _read_observation(target_id, integration_id, site_id, *, lock=False):
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Interface
    from nautobot.extras.models import ObjectMetadata

    queryset = ObjectMetadata.objects.filter(
        metadata_type__name=_OBSERVATION_TYPE,
        assigned_object_type=ContentType.objects.get_for_model(Interface),
        assigned_object_id=target_id,
    )
    if lock:
        queryset = queryset.select_for_update()
    rows = list(queryset)
    if len(rows) > 1 or any(row.scoped_fields != [] for row in rows):
        raise ValueError("Invalid UniFi neighbor observation prerequisite.")
    if not rows:
        return None
    value = rows[0].value
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Invalid UniFi neighbor observation prerequisite.")
    if value.get("integration_id") != str(integration_id) or value.get("site_id") != site_id:
        raise _NeighborHold("foreign-owner")
    _utc(value.get("last_complete_collection_at"))
    peers = value.get("peers")
    if not isinstance(peers, list):
        raise ValueError("Invalid UniFi neighbor observation prerequisite.")
    seen = set()
    for peer in peers:
        try:
            peer_id = UUID(peer["peer_interface_id"])
            if (
                peer_id in seen
                or peer_id == UUID(str(target_id))
                or peer["presence"] not in {"observed", "not-observed"}
                or not isinstance(peer["evidence"], list)
                or not peer["evidence"]
                or len(set(peer["evidence"])) != len(peer["evidence"])
                or not set(peer["evidence"]).issubset({"lldp", "wired-uplink"})
            ):
                raise ValueError
            times = [_utc(peer[key]) for key in ("first_observed_at", "last_observed_at", "last_checked_at")]
            if not times[0] <= times[1] <= times[2] <= _utc(value["last_complete_collection_at"]):
                raise ValueError
            seen.add(peer_id)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("Invalid UniFi neighbor observation prerequisite.") from None
    return value


def _observation(previous, integration_id, site_id, peers, now):
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    entries = {UUID(peer["peer_interface_id"]): dict(peer) for peer in (previous or {}).get("peers", [])}
    for peer_id, entry in entries.items():
        entry["last_checked_at"] = stamp
        entry["presence"] = "not-observed"
    for peer_id, evidence in peers.items():
        entry = entries.setdefault(peer_id, {"peer_interface_id": str(peer_id), "first_observed_at": stamp})
        entry.update(evidence=sorted(evidence), last_observed_at=stamp, last_checked_at=stamp, presence="observed")
    return {
        "schema_version": 1,
        "integration_id": str(integration_id),
        "site_id": site_id,
        "last_complete_collection_at": stamp,
        "peers": [entries[key] for key in sorted(entries, key=str)],
    }

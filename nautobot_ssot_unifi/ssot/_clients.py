"""Private client network/attachment projection, identity resolution and observation documents.

A client's effective network is only what that client's own report states
(`network_id`, cross-checked against its reported `vlan`). Neither an SSID's
default network nor the VLANs a trunk allows establishes it. Attachment is the
reporting switch port or AP/SSID. This module never sets interface mode or
VLAN membership and never invents a physical link.
"""

from datetime import datetime, timezone
from math import isfinite
import re

_MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")

_OBSERVATION_TYPE = "UniFi client observations"
_UNRESOLVED_TYPE = "UniFi unresolved client observations"
_RELATIONSHIP_KEY = "unifi_observed_client_attachment"
_VM_RELATIONSHIP_KEY = "unifi_observed_vm_client_attachment"

_OBSERVATION_KEYS = {
    "schema_version",
    "integration_id",
    "site_id",
    "mac",
    "presence",
    "last_seen_at",
    "network",
    "attachment",
}
_NETWORK_KEYS = {"source_network_id", "vlan", "vlan_id"}
_ATTACHMENT_KEYS = {"kind", "source_device_id", "interface_id", "port_idx", "essid", "radio_name"}
_ENTRY_KEYS = {"site_id", "mac", "reason", "candidates", "presence", "last_seen_at", "network", "attachment"}
_REASONS = {
    "no-match",
    "ambiguous-binding",
    "identity-conflict",
    "foreign-owner",
    "unresolved-endpoint",
    "incomplete-membership",
    "invalid-vlan",
}


class _ClientHold(ValueError):
    """Closed, non-secret review reason; `candidates` lists ambiguous native matches."""

    def __init__(self, reason, candidates=()):
        self.reason = reason
        self.candidates = list(candidates)
        super().__init__(reason)


def _mac(value):
    if not isinstance(value, str) or not _MAC.fullmatch(value):
        raise _ClientHold("unresolved-endpoint")
    return value.lower()


def _device(devices, mac):
    matches = [key for key, raw in devices.items() if isinstance(raw.get("mac"), str) and raw["mac"].lower() == mac]
    if len(matches) != 1:
        raise _ClientHold("unresolved-endpoint")
    return matches[0]


def _network(row, vids):
    """Return the reported network's source id after checking the VLAN cross-check."""
    network_id = row.get("network_id")
    if not isinstance(network_id, str) or network_id not in vids:
        raise _ClientHold("incomplete-membership")
    vid = vids[network_id]
    if vid is None:
        # Known network, but no VLAN could be established for it.
        raise _ClientHold("invalid-vlan")
    reported = row.get("vlan")
    if reported is not None and (type(reported) is not int or reported != vid):
        raise _ClientHold("incomplete-membership")
    return network_id


def _attachment(row, devices):
    if type(row.get("is_wired")) is not bool:
        raise _ClientHold("incomplete-membership")
    if row["is_wired"]:
        port = row.get("sw_port")
        if type(port) is not int or port < 0:
            raise _ClientHold("incomplete-membership")
        return {"kind": "wired", "device": _device(devices, _mac(row.get("sw_mac"))), "port_idx": port}
    essid = row.get("essid")
    if not isinstance(essid, str) or not essid.strip():
        raise _ClientHold("incomplete-membership")
    radio_name, radio = row.get("radio_name"), row.get("radio")
    return {
        "kind": "wireless",
        "device": _device(devices, _mac(row.get("ap_mac"))),
        "essid": essid,
        "radio_name": radio_name if isinstance(radio_name, str) and radio_name else None,
        "radio": radio if isinstance(radio, str) and radio else None,
    }


def _source_time(value):
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise _ClientHold("incomplete-membership")
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, ValueError, OSError):
        raise _ClientHold("incomplete-membership") from None


def _client_facts(row, vids, devices):
    """Project one `/stat/sta` row; raise `_ClientHold` unless every fact is explicit.

    vids: {source network id: VLAN id or None}; devices: {source id: raw managed device}.
    """
    if not isinstance(row, dict):
        raise _ClientHold("incomplete-membership")
    seen = row.get("last_seen")
    _source_time(seen)
    return {
        "mac": _mac(row.get("mac")),
        "last_seen": seen,
        "network_id": _network(row, vids),
        **_attachment(row, devices),
    }


def _stamp(moment):
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid UniFi client observation prerequisite.") from None


def _seen_stamp(value):
    """Source-reported client time as an ISO stamp, or None when not explicit."""
    try:
        return _stamp(_source_time(value))
    except _ClientHold:
        return None


def _prerequisites():
    """Require the operator-created schema; this connector never creates it."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Interface
    from nautobot.extras.models import ExternalIntegration, MetadataType, Relationship
    from nautobot.virtualization.models import VMInterface

    interface = ContentType.objects.get_for_model(Interface)
    vm_interface = ContentType.objects.get_for_model(VMInterface)
    integration = ContentType.objects.get_for_model(ExternalIntegration)
    observations = MetadataType.objects.filter(name=_OBSERVATION_TYPE).first()
    unresolved = MetadataType.objects.filter(name=_UNRESOLVED_TYPE).first()
    for key, destination in ((_RELATIONSHIP_KEY, interface), (_VM_RELATIONSHIP_KEY, vm_interface)):
        # Source is the attachment point (port/radio). Many clients share one,
        # while each client interface has at most one current attachment.
        relationship = Relationship.objects.filter(key=key).first()
        if (
            relationship is None
            or relationship.type != "one-to-many"
            or relationship.source_type_id != interface.pk
            or relationship.destination_type_id != destination.pk
        ):
            raise ValueError("Invalid UniFi client schema prerequisite.")
    if (
        observations is None
        or observations.data_type != "json"
        or not {interface.pk, vm_interface.pk}.issubset(set(observations.content_types.values_list("pk", flat=True)))
        or unresolved is None
        or unresolved.data_type != "json"
        or not unresolved.content_types.filter(pk=integration.pk).exists()
    ):
        raise ValueError("Invalid UniFi client schema prerequisite.")


def _valid_observation(value):
    try:
        network, attachment = value["network"], value["attachment"]
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or set(value) != _OBSERVATION_KEYS
            or not isinstance(value["mac"], str)
            or not _MAC.fullmatch(value["mac"])
            or value["mac"] != value["mac"].lower()
            or value["presence"] not in {"observed", "not-observed"}
            or not isinstance(value["site_id"], str)
            or not isinstance(network, dict)
            or set(network) != _NETWORK_KEYS
            or not isinstance(attachment, dict)
            or set(attachment) != _ATTACHMENT_KEYS
            or attachment["kind"] not in {"wired", "wireless"}
        ):
            raise ValueError
        _instant(value["last_seen_at"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid UniFi client observation prerequisite.") from None
    return value


def _model_for(kind):
    from nautobot.dcim.models import Interface
    from nautobot.virtualization.models import VMInterface

    return {"interface": Interface, "vminterface": VMInterface}[kind]


def _read_observation(model, target_id, integration_id, site_id, *, lock=False):
    """Return the one validated document for a client interface, or None."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.extras.models import ObjectMetadata

    queryset = ObjectMetadata.objects.filter(
        metadata_type__name=_OBSERVATION_TYPE,
        assigned_object_type=ContentType.objects.get_for_model(model),
        assigned_object_id=target_id,
    )
    if lock:
        queryset = queryset.select_for_update()
    rows = list(queryset)
    if len(rows) > 1 or any(row.scoped_fields != [] for row in rows):
        raise ValueError("Invalid UniFi client observation prerequisite.")
    if not rows:
        return None
    value = _valid_observation(rows[0].value if isinstance(rows[0].value, dict) else {})
    if value["integration_id"] != str(integration_id) or value["site_id"] != site_id:
        raise _ClientHold("foreign-owner")
    return value


def _bound_documents():
    """Every committed client observation: [(kind, interface pk, validated value)]."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Interface
    from nautobot.extras.models import ObjectMetadata
    from nautobot.virtualization.models import VMInterface

    kinds = {
        ContentType.objects.get_for_model(Interface).pk: "interface",
        ContentType.objects.get_for_model(VMInterface).pk: "vminterface",
    }
    rows = ObjectMetadata.objects.filter(metadata_type__name=_OBSERVATION_TYPE, assigned_object_type_id__in=list(kinds))
    result = []
    for row in rows:
        if row.scoped_fields != [] or not isinstance(row.value, dict):
            raise ValueError("Invalid UniFi client observation prerequisite.")
        result.append((kinds[row.assigned_object_type_id], row.assigned_object_id, _valid_observation(row.value)))
    return result


def _resolve(mac, integration_id, site_id):
    """Resolve a client to ONE native interface, or raise a review hold.

    An established binding (committed observation for this integration, site and
    MAC) wins. Otherwise exactly one exact MAC match across Interface and
    VMInterface, provided no other evidence contradicts it. Hostname and IP are
    never consulted.
    """
    from nautobot.dcim.models import Interface
    from nautobot.virtualization.models import VMInterface

    documents = _bound_documents()
    # A MAC claimed by another integration or site is never merged silently.
    bound = [(k, pk) for k, pk, v in documents if v["mac"] == mac and v["integration_id"] == str(integration_id)]
    foreign = [(k, pk) for k, pk, v in documents if v["mac"] == mac and v["integration_id"] != str(integration_id)]
    same_site = [
        (k, pk)
        for k, pk, v in documents
        if v["mac"] == mac and v["integration_id"] == str(integration_id) and v["site_id"] == site_id
    ]
    if len(bound) > 1:
        raise _ClientHold("ambiguous-binding", [{"kind": k, "id": str(pk)} for k, pk in bound])
    if len(bound) == 1:
        if len(same_site) != 1:
            raise _ClientHold("identity-conflict")
        kind, pk = bound[0]
        rows = list(_model_for(kind).objects.filter(pk=pk).values_list("mac_address", flat=True)[:1])
        if not rows:
            raise _ClientHold("identity-conflict")
        # An absent native MAC does not contradict the binding; only a different one does.
        current = re.sub(r"[^0-9a-f]", "", str(rows[0] or "").lower())
        if current and current != mac.replace(":", ""):
            raise _ClientHold("identity-conflict")
        return kind, pk
    matches = [
        ("interface", pk) for pk in Interface.objects.filter(mac_address__iexact=mac).values_list("pk", flat=True)
    ]
    matches += [
        ("vminterface", pk) for pk in VMInterface.objects.filter(mac_address__iexact=mac).values_list("pk", flat=True)
    ]
    if not matches:
        raise _ClientHold("no-match")
    if len(matches) > 1:
        raise _ClientHold("ambiguous-binding", [{"kind": k, "id": str(pk)} for k, pk in matches])
    if foreign:
        raise _ClientHold("foreign-owner")
    kind, pk = matches[0]
    if any(existing == pk for _k, existing, _v in documents):
        # The interface already carries another identity's observation.
        raise _ClientHold("identity-conflict")
    return kind, pk


def _observation(previous, integration_id, site_id, facts, attachment_id, vlan_id, vlan, now=None):
    """Next matched-client document; `facts is None` flips presence only."""
    if facts is None:
        # A client omitted from a complete report keeps its last-known facts.
        return {**previous, "presence": "not-observed"}
    return {
        "schema_version": 1,
        "integration_id": str(integration_id),
        "site_id": site_id,
        "mac": facts["mac"],
        "presence": "observed",
        "last_seen_at": _stamp(_source_time(facts["last_seen"])),
        "network": {
            "source_network_id": facts["network_id"],
            "vlan": vlan,
            "vlan_id": None if vlan_id is None else str(vlan_id),
        },
        "attachment": {
            "kind": facts["kind"],
            "source_device_id": facts["device"],
            "interface_id": None if attachment_id is None else str(attachment_id),
            "port_idx": facts.get("port_idx"),
            "essid": facts.get("essid"),
            "radio_name": facts.get("radio_name"),
        },
    }


def _entry(site_id, mac, reason, candidates, row, vids, devices, vlan_ids, attachment_id=None):
    """Unresolved-record entry from only explicit report fields; absent facts stay null."""
    network_id = row.get("network_id") if isinstance(row.get("network_id"), str) else None
    reported = row.get("vlan") if type(row.get("vlan")) is int else None
    vlan_id = vlan_ids.get(network_id) if network_id else None
    wired = row.get("is_wired") if type(row.get("is_wired")) is bool else None
    device = None
    try:
        if wired is True:
            device = _device(devices, _mac(row.get("sw_mac")))
        elif wired is False:
            device = _device(devices, _mac(row.get("ap_mac")))
    except _ClientHold:
        device = None
    port = row.get("sw_port") if wired is True and type(row.get("sw_port")) is int and row["sw_port"] >= 0 else None
    essid = row.get("essid") if wired is False and isinstance(row.get("essid"), str) and row["essid"].strip() else None
    radio_name = row.get("radio_name") if wired is False and isinstance(row.get("radio_name"), str) else None
    return {
        "site_id": site_id,
        "mac": mac,
        "reason": reason,
        "candidates": list(candidates),
        "presence": "observed",
        "last_seen_at": _seen_stamp(row.get("last_seen")),
        "network": {
            "source_network_id": network_id,
            "vlan": reported,
            "vlan_id": None if vlan_id is None else str(vlan_id),
        },
        "attachment": {
            "kind": None if wired is None else ("wired" if wired else "wireless"),
            "source_device_id": device,
            "interface_id": None if attachment_id is None else str(attachment_id),
            "port_idx": port,
            "essid": essid,
            "radio_name": radio_name,
        },
    }


def _unresolved_document(integration_id, entries):
    return {
        "schema_version": 1,
        "integration_id": str(integration_id),
        "clients": sorted(entries, key=lambda entry: (entry["site_id"], entry["mac"])),
    }


def _valid_unresolved(value, integration_id):
    try:
        if (
            set(value) != {"schema_version", "integration_id", "clients"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(value["clients"], list)
        ):
            raise ValueError
        seen = set()
        for entry in value["clients"]:
            key = entry["site_id"], entry["mac"]
            if (
                set(entry) != _ENTRY_KEYS
                or key in seen
                or not isinstance(entry["site_id"], str)
                or not isinstance(entry["mac"], str)
                or not _MAC.fullmatch(entry["mac"])
                or entry["reason"] not in _REASONS
                or entry["presence"] not in {"observed", "not-observed"}
                or not isinstance(entry["candidates"], list)
                or any(
                    not isinstance(c, dict) or set(c) != {"kind", "id"} or c["kind"] not in {"interface", "vminterface"}
                    for c in entry["candidates"]
                )
                or not isinstance(entry["network"], dict)
                or set(entry["network"]) != _NETWORK_KEYS
                or not isinstance(entry["attachment"], dict)
                or set(entry["attachment"]) != _ATTACHMENT_KEYS
            ):
                raise ValueError
            if entry["last_seen_at"] is not None:
                _instant(entry["last_seen_at"])
            seen.add(key)
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid UniFi unresolved client prerequisite.") from None
    if value["integration_id"] != str(integration_id):
        raise _ClientHold("foreign-owner")
    return value


def _read_unresolved(integration_id, *, lock=False):
    """The integration's single validated unresolved-client document, or None."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.extras.models import ExternalIntegration, ObjectMetadata

    queryset = ObjectMetadata.objects.filter(
        metadata_type__name=_UNRESOLVED_TYPE,
        assigned_object_type=ContentType.objects.get_for_model(ExternalIntegration),
        assigned_object_id=integration_id,
    )
    if lock:
        queryset = queryset.select_for_update()
    rows = list(queryset)
    if len(rows) > 1 or any(row.scoped_fields != [] for row in rows):
        raise ValueError("Invalid UniFi unresolved client prerequisite.")
    if not rows:
        return None
    return _valid_unresolved(rows[0].value if isinstance(rows[0].value, dict) else {}, integration_id)


def _retire(previous, document, retire, integration_id):
    """Drop a pending entry only after its matched observation is committed as intended.

    Any previous entry absent from the proposed document is retained unless its
    matched write is verified, so a failed matched write never loses observations.
    """
    entries = {(entry["site_id"], entry["mac"]): entry for entry in document["clients"]}
    for entry in (previous or {}).get("clients", []):
        key = entry["site_id"], entry["mac"]
        if key in entries:
            continue
        intended = retire.get(key)
        if intended is not None:
            kind, target_id, expected = intended
            try:
                committed = _read_observation(_model_for(kind), target_id, integration_id, expected["site_id"])
            except _ClientHold:
                committed = None
            if committed == expected:
                continue
        entries[key] = entry
    return _unresolved_document(integration_id, entries.values())


def _network_complete(network):
    return network["source_network_id"] is not None


def _attachment_complete(attachment):
    if attachment["source_device_id"] is None:
        return False
    if attachment["kind"] == "wired":
        return attachment["port_idx"] is not None
    return attachment["kind"] == "wireless" and attachment["essid"] is not None


def _merge_entry(previous, new):
    """Fold a new pending entry over the last-known one without erasing valid facts.

    An older report keeps the previous entry unchanged. The source time, network
    and attachment are separate groups; each replaces the previous group only
    when the new report's group is complete, and is otherwise retained whole, so
    a wired port is never mixed into a wireless attachment.
    """
    if previous is None:
        return new
    seen, before = new["last_seen_at"], previous["last_seen_at"]
    if seen is not None and before is not None and _instant(seen) < _instant(before):
        return previous
    merged = dict(new)
    if seen is None:
        merged["last_seen_at"] = before
    if not _network_complete(new["network"]):
        merged["network"] = previous["network"]
    if not _attachment_complete(new["attachment"]):
        merged["attachment"] = previous["attachment"]
    return merged

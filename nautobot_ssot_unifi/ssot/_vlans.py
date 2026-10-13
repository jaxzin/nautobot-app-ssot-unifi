"""Private presence-aware site/VLAN/membership projection."""

from collections import Counter
from uuid import UUID
from django.core.exceptions import ValidationError
from nautobot_ssot_unifi.ssot._bindings import _text


class _VlanHold(ValueError):
    """Safe classification, never source configuration or arbitrary exception text."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _group_bindings(entries, *, existing=None):
    if existing is None:
        from nautobot.ipam.models import VLANGroup

        existing = set(VLANGroup.objects.values_list("pk", flat=True))
    error = ValidationError(
        {"vlan_groups": "Require unique site_id/existing vlan_group_id entries, one group per site."}
    )
    if not isinstance(entries, list) or not entries:
        raise error
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"site_id", "vlan_group_id"} or not _text(entry["site_id"]):
            raise error
        try:
            group = UUID(entry["vlan_group_id"]) if isinstance(entry["vlan_group_id"], str) else None
        except (ValueError, AttributeError):
            raise error from None
        if group not in existing or entry["site_id"] in result or group in result.values():
            raise error
        result[entry["site_id"]] = group
    return result


def _included_sites(bindings, sites):
    if len(sites) != len(set(sites)) or set(bindings) != set(sites):
        raise ValidationError({"vlan_groups": "Bindings must match every included source site exactly."})


def _wan_without_vlan(row):
    return row.get("purpose") in ("wan", "wan2") and row.get("vlan") is None


def _non_vlan_network(row):
    return _wan_without_vlan(row) or (row.get("vlan_enabled") is False and row.get("vlan") is None)


def _networks(networks):
    if not isinstance(networks, list) or any(
        not isinstance(row, dict) or not _text(row.get("_id")) for row in networks
    ):
        raise ValueError("Invalid UniFi network configuration channel.")
    ids = Counter(row["_id"] for row in networks)
    candidates = [row for row in networks if not _non_vlan_network(row)]
    names = Counter(row.get("name") for row in candidates if isinstance(row.get("name"), str))
    vids = Counter(row.get("vlan") for row in candidates if type(row.get("vlan")) is int)
    rows, held = [], []
    for row in candidates:
        vid = row.get("vlan")
        if (
            ids[row["_id"]] > 1
            or (isinstance(row.get("name"), str) and names[row["name"]] > 1)
            or (type(vid) is int and vids[vid] > 1)
        ):
            held.append((row["_id"], "duplicate-vlan"))
        elif (
            type(vid) is not int
            or not 1 <= vid <= 4094
            or not _text(row.get("name"))
            or row.get("vlan_enabled") is False
        ):
            held.append((row["_id"], "invalid-vlan"))
        else:
            rows.append({"source_id": row["_id"], "vid": vid, "name": row["name"]})
    return rows, held


def _membership(port, profiles, overrides, networks):
    if (
        not isinstance(port, dict)
        or not isinstance(overrides, list)
        or any(not isinstance(row, dict) for row in overrides)
    ):
        raise _VlanHold("incomplete-membership")
    selected = [
        row for row in overrides if row.get("port_idx") is not None and row.get("port_idx") == port.get("port_idx")
    ]
    if len(selected) > 1:
        raise _VlanHold("incomplete-membership")
    override = selected[0] if selected else {}
    profile_id = override.get("portconf_id", port.get("portconf_id"))
    if profile_id and profile_id not in profiles:
        raise _VlanHold("missing-profile")
    effective = {**profiles.get(profile_id, {}), **port, **override}
    if "native_networkconf_id" not in effective:
        raise _VlanHold("incomplete-membership")
    native = effective["native_networkconf_id"]
    if not isinstance(native, str) or (native and native not in networks):
        raise _VlanHold("incomplete-membership")
    untagged = networks[native] if native else None
    if native and untagged is None:
        raise _VlanHold("incomplete-membership")
    tagged = []
    if "tagged_vlan_mgmt" in effective:
        policy = effective["tagged_vlan_mgmt"]
        if policy == "block_all":
            mode = "access"
        elif policy in {"auto", "all", "custom"}:
            excluded = effective.get("excluded_networkconf_ids")
            if not isinstance(excluded, list) or any(
                not isinstance(key, str) or key not in networks for key in excluded
            ):
                raise _VlanHold("incomplete-membership")
            if not excluded and policy != "custom":
                mode = "tagged-all"
            else:
                mode = "tagged"
                tagged = [value for key, value in networks.items() if key != native and key not in excluded]
        else:
            raise _VlanHold("incomplete-membership")
    elif effective.get("forward") in {"all", "native", "customize"}:
        mode = {"all": "tagged-all", "native": "access", "customize": "tagged"}[effective["forward"]]
        if mode == "tagged":
            keys = effective.get("tagged_networkconf_ids")
            if not isinstance(keys, list) or any(
                not isinstance(key, str) or key not in networks or key == native for key in keys
            ):
                raise _VlanHold("incomplete-membership")
            tagged = [networks[key] for key in keys]
    else:
        raise _VlanHold("incomplete-membership")
    if any(value is None for value in tagged):
        raise _VlanHold("incomplete-membership")
    return mode, untagged, sorted(set(tagged), key=str)

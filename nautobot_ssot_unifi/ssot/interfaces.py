"""Project controller interface tables without guessing aliases or capabilities."""

from nautobot.dcim.choices import InterfaceTypeChoices

from nautobot_ssot_unifi.const import UNIFI_SSOT_INTERFACE_TYPES


def _table(raw, key):
    rows = raw.get(key, [])
    if not isinstance(rows, list):
        raise ValueError("Invalid UniFi interface table.")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
            raise ValueError("UniFi interface has no source name.")
    return rows


def _port_id(row):
    value = row.get("port_idx")
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Invalid UniFi interface port identifier.")
    return value


def _media_type(row, default):
    media = row.get("media")
    return UNIFI_SSOT_INTERFACE_TYPES.get(media.lower() if isinstance(media, str) else "", default)


def _enrich(record, alias):
    media = _media_type(alias, InterfaceTypeChoices.TYPE_OTHER)
    if media != InterfaceTypeChoices.TYPE_OTHER:
        if record["type"] not in (InterfaceTypeChoices.TYPE_OTHER, media):
            raise ValueError("Conflicting UniFi interface capabilities.")
        record["type"] = media
    for field in ("ip", "netmask"):
        value = alias.get(field)
        if value:
            if record[field] and record[field] != value:
                raise ValueError("Conflicting UniFi interface addressing.")
            record[field] = value


def _interface_records(raw):
    records, origins, port_ids, ifnames = {}, {}, {}, {}

    def add(row, origin, default):
        record = {
            "name": row["name"],
            "port_idx": _port_id(row),
            "type": _media_type(row, default),
            "ip": row.get("ip"),
            "netmask": row.get("netmask"),
        }
        name = record["name"]
        if name in records and (records[name] != record or origins[name] != origin):
            raise ValueError("Conflicting UniFi interface identity.")
        records[name], origins[name] = record, origin
        return name

    def index(mapping, identity, name):
        if identity is None:
            return
        if identity in mapping and mapping[identity] != name:
            raise ValueError("Ambiguous UniFi interface alias.")
        mapping[identity] = name

    for row in _table(raw, "port_table"):
        name = add(row, "port", InterfaceTypeChoices.TYPE_OTHER)
        index(port_ids, _port_id(row), name)
        ifname = row.get("ifname")
        if ifname is not None:
            if not isinstance(ifname, str) or not ifname.strip():
                raise ValueError("Invalid UniFi interface alias.")
            index(ifnames, ifname, name)

    for row in _table(raw, "ethernet_table"):
        port_id = _port_id(row)
        by_id, by_name = port_ids.get(port_id), ifnames.get(row["name"])
        if by_name is not None and port_id is not None and by_id != by_name:
            raise ValueError("Conflicting UniFi interface aliases.")
        # Only explicit source identity links establish a duplicate. num_port,
        # matching MACs, and display-name similarity do not establish aliases.
        canonical = by_id or by_name
        if canonical is not None:
            if origins[canonical] != "port" and row["name"] != canonical:
                raise ValueError("Ambiguous UniFi Ethernet port identifier.")
            _enrich(records[canonical], row)
            continue
        name = add(row, "ethernet", InterfaceTypeChoices.TYPE_OTHER)
        index(port_ids, port_id, name)

    for row in _table(raw, "radio_table"):
        # A band or negotiated rate does not identify the supported Wi-Fi generation.
        add(row, "radio", InterfaceTypeChoices.TYPE_OTHER_WIRELESS)

    return list(records.values())

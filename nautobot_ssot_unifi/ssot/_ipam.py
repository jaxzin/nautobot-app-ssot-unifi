"""Private native subnet/DHCP extension; source facts never configure networking."""

from ipaddress import ip_interface, ip_address
from typing import Annotated, Any, ClassVar, Optional
from uuid import UUID

from nautobot.ipam.models import IPAddressRange, Namespace, Prefix
from nautobot_ssot.contrib import NautobotModel, ObjectMetadataAnnotation

from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _CLAIM_PATHS, _OWNERSHIP_TYPE
from nautobot_ssot_unifi.ssot._ownership import _claim_available
from nautobot_ssot_unifi.ssot.models import ActiveStatusMixin, _BoundModelMixin


def _parent_claim(adapter, target_id, attrs, kind, parent_kind, parent_field, model):
    session = adapter.job._unifi_bindings
    document = session._document(kind, target_id)
    parent = session._document(parent_kind, attrs[parent_field])
    if document["site_id"] != parent["site_id"]:
        raise _BindingHold("identity-conflict")
    session.check(parent_kind, attrs[parent_field])
    _claim_available(target_id, session.integration_id, lock=True, model=model, paths=_CLAIM_PATHS[kind])


class _NetworkPrefixModel(ActiveStatusMixin, _BoundModelMixin, NautobotModel):
    """A configured native Prefix, separate from address-derived Prefix models."""

    _model = Prefix
    _modelname = "network_prefix"
    _binding_kind: ClassVar[str] = "prefix"
    _identifiers = ("id",)
    _attributes = ("namespace__name", "network", "prefix_length", "type", "vlan__id", "unifi_ownership")
    id: UUID
    namespace__name: str
    network: str
    prefix_length: int
    type: str = "network"
    vlan__id: Optional[UUID] = None
    status_id: Optional[UUID] = None
    unifi_ownership: Annotated[
        Optional[dict[str, Any]], ObjectMetadataAnnotation(metadata_type_name=_OWNERSHIP_TYPE, data_type="json")
    ] = None

    @classmethod
    def _guard_extension_fields(cls, adapter, target_id, attrs):
        _parent_claim(adapter, target_id, attrs, "prefix", "vlan", "vlan__id", Prefix)

    def update(self, attrs):
        return super().update({"vlan__id": self.vlan__id, **attrs})


class _IPAddressRangeModel(ActiveStatusMixin, _BoundModelMixin, NautobotModel):
    """An owned native DHCP span; configuration does not imply utilization."""

    _model = IPAddressRange
    _modelname = "ip_address_range"
    _binding_kind: ClassVar[str] = "ipaddressrange"
    _identifiers = ("id",)
    _attributes = ("name", "start_host", "end_host", "parent__id", "unifi_ownership")
    id: UUID
    name: str
    start_host: str
    end_host: str
    parent__id: UUID
    status_id: Optional[UUID] = None
    unifi_ownership: Annotated[
        Optional[dict[str, Any]], ObjectMetadataAnnotation(metadata_type_name=_OWNERSHIP_TYPE, data_type="json")
    ] = None

    @classmethod
    def _guard_extension_fields(cls, adapter, target_id, attrs):
        _parent_claim(adapter, target_id, attrs, "ipaddressrange", "prefix", "parent__id", IPAddressRange)

    def update(self, attrs):
        return super().update({"parent__id": self.parent__id, **attrs})


def _project_network_graph(adapter, site_id, rows, networks):
    """Plan source-owned objects read-only; mutations run through native SSoT."""
    session = adapter.job._unifi_bindings
    for row in rows:
        vlan_id = networks.get(row["_id"])
        if vlan_id is None or "ip_subnet" not in row:
            continue
        try:
            if not isinstance(row["ip_subnet"], str):
                raise ValueError
            subnet = ip_interface(row["ip_subnet"])
            if subnet.version != 4:
                raise ValueError
            namespace_name = adapter.address_scope_resolver.resolve(str(subnet.ip))
            if any(
                adapter.address_scope_resolver.resolve(str(address)) != namespace_name
                for address in (subnet.network.network_address, subnet.network.broadcast_address)
            ):
                raise ValueError
            namespace = Namespace.objects.get(name=namespace_name)
        except (ValueError, TypeError):
            adapter._held.append(
                {"kind": "network_prefix", "site_id": site_id, "source_id": row["_id"], "reason": "invalid-network"}
            )
            continue
        source_id = f"{row['_id']}:ipv4:{namespace.pk}:{subnet.network}"
        try:
            prefix_id = session.plan(
                "prefix",
                site_id,
                source_id,
                {
                    "namespace_id": namespace.pk,
                    "network": str(subnet.network.network_address),
                    "prefix_length": subnet.network.prefixlen,
                },
            )
            _claim_available(prefix_id, session.integration_id, model=Prefix, paths=_CLAIM_PATHS["prefix"])
        except _BindingHold as error:
            adapter._held.append(
                {"kind": "network_prefix", "site_id": site_id, "source_id": source_id, "reason": error.reason}
            )
            continue
        document = session.documents[("prefix", prefix_id)]
        document["field_paths"] = sorted(_CLAIM_PATHS["prefix"])
        adapter.add(
            adapter.network_prefix(
                id=prefix_id,
                namespace__name=namespace_name,
                network=str(subnet.network.network_address),
                prefix_length=subnet.network.prefixlen,
                vlan__id=vlan_id,
                unifi_ownership=document,
            )
        )
        adapter._network_prefix_keys.add(
            (namespace_name, str(subnet.network.network_address), subnet.network.prefixlen)
        )
        if row.get("dhcpd_enabled") is not True:
            continue
        try:
            if not all(isinstance(row.get(key), str) for key in ("dhcpd_start", "dhcpd_stop")):
                raise ValueError
            start, end = ip_address(row["dhcpd_start"]), ip_address(row["dhcpd_stop"])
            if (
                start.version != 4
                or end.version != 4
                or start > end
                or start not in subnet.network
                or end not in subnet.network
            ):
                raise ValueError
        except (ValueError, TypeError):
            adapter._held.append(
                {"kind": "ip_address_range", "site_id": site_id, "source_id": source_id, "reason": "invalid-dhcp-range"}
            )
            continue
        range_source = source_id + ":dhcp4"
        try:
            range_id = session.plan(
                "ipaddressrange",
                site_id,
                range_source,
                {"parent_id": prefix_id, "start_host": str(start), "end_host": str(end)},
            )
            _claim_available(
                range_id, session.integration_id, model=IPAddressRange, paths=_CLAIM_PATHS["ipaddressrange"]
            )
        except _BindingHold as error:
            adapter._held.append(
                {"kind": "ip_address_range", "site_id": site_id, "source_id": range_source, "reason": error.reason}
            )
            continue
        document = session.documents[("ipaddressrange", range_id)]
        document["field_paths"] = sorted(_CLAIM_PATHS["ipaddressrange"])
        adapter.add(
            adapter.ip_address_range(
                id=range_id,
                name=f"{row['name']} DHCP",
                start_host=str(start),
                end_host=str(end),
                parent__id=prefix_id,
                unifi_ownership=document,
            )
        )

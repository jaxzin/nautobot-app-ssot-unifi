"""Private native subnet/DHCP extension; source facts never configure networking."""

from collections import Counter, defaultdict
from ipaddress import ip_interface, ip_address
from typing import Annotated, Any, ClassVar, Optional
from uuid import UUID

from diffsync.exceptions import ObjectNotCreated, ObjectNotUpdated
from django.db import transaction
from nautobot.ipam.models import IPAddressRange, Namespace, Prefix
from nautobot_ssot.contrib import NautobotModel, ObjectMetadataAnnotation

from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _CLAIM_PATHS, _OWNERSHIP_TYPE
from nautobot_ssot_unifi.ssot._ownership import _claim_available
from nautobot_ssot_unifi.ssot.models import ActiveStatusMixin, _BoundModelMixin


def _namespace_covers_subnet(resolver, subnet, namespace_id):
    """Require one namespace across every interval, including rule gaps."""
    if resolver.rules is None:
        return True
    first, stop = int(subnet.network_address), int(subnet.broadcast_address) + 1
    events = defaultdict(Counter)
    events[first]
    events[stop]
    for networks, rule_namespace_id, _name in resolver.rules:
        for network in networks:
            if network.version != subnet.version:
                continue
            start = max(first, int(network.network_address))
            end = min(stop, int(network.broadcast_address) + 1)
            if start < end:
                events[start][rule_namespace_id] += 1
                events[end][rule_namespace_id] -= 1
    active = Counter()
    points = sorted(events)
    for point in points[:-1]:
        active.update(events[point])
        if {key for key, count in active.items() if count > 0} != {namespace_id}:
            return False
    return True


def _ranges_reparented_by(namespace_id, network, prefix_length):
    """Mirror native Prefix.save()'s range adoption predicate before writing."""
    subnet = ip_interface(f"{network}/{prefix_length}").network
    return IPAddressRange.objects.filter(
        parent__namespace_id=namespace_id,
        ip_version=subnet.version,
        start_host__gte=str(subnet.network_address),
        end_host__lte=str(subnet.broadcast_address),
        parent__prefix_length__lt=prefix_length,
    )


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
    def get_queryset(cls):
        """The target loader restricts this queryset to exact reviewed bindings."""
        return Prefix.objects.all()

    @classmethod
    def _guard_extension_fields(cls, adapter, target_id, attrs):
        _parent_claim(adapter, target_id, attrs, "prefix", "vlan", "vlan__id", Prefix)
        existing = Prefix.objects.filter(pk=target_id).first()
        namespace = Namespace.objects.get(
            name=attrs.get("namespace__name", existing.namespace.name if existing else None)
        )
        network = attrs.get("network", existing.network if existing else None)
        length = attrs.get("prefix_length", existing.prefix_length if existing else None)
        if _ranges_reparented_by(namespace.pk, network, length).select_for_update().exists():
            raise _BindingHold("identity-conflict")

    @classmethod
    def create(cls, adapter, ids, attrs):
        with transaction.atomic():
            result = super().create(adapter, ids, attrs)
            # A range appearing after the precheck must not escape native save().
            if IPAddressRange.objects.filter(parent_id=ids["id"]).exists():
                raise ObjectNotCreated("identity-conflict")
            return result

    def update(self, attrs):
        with transaction.atomic():
            children = set(IPAddressRange.objects.filter(parent_id=self.id).values_list("pk", flat=True))
            result = super().update({"vlan__id": self.vlan__id, **attrs})
            if IPAddressRange.objects.filter(parent_id=self.id).exclude(pk__in=children).exists():
                raise ObjectNotUpdated("identity-conflict")
            return result


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
    def get_queryset(cls):
        """A tag is descriptive; the exact binding is the admission authority."""
        return IPAddressRange.objects.all()

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
        if vlan_id is None or "ip_subnet" not in row or row.get("purpose") in {"wan", "wan2"}:
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
            if not _namespace_covers_subnet(adapter.address_scope_resolver, subnet.network, namespace.pk):
                raise ValueError
        except (ValueError, TypeError):
            adapter._held.append(
                {"kind": "network_prefix", "site_id": site_id, "source_id": row["_id"], "reason": "invalid-network"}
            )
            continue
        source_id = f"{row['_id']}:ipv4:{namespace.pk}:{subnet.network}"
        try:
            if _ranges_reparented_by(
                namespace.pk, str(subnet.network.network_address), subnet.network.prefixlen
            ).exists():
                raise _BindingHold("identity-conflict")
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

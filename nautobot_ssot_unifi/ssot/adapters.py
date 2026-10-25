"""Adapters for diffsync models between Unifi and Nautobot."""

from typing import Any, Dict, List
from datetime import datetime, timezone
from collections import Counter
from uuid import UUID
from diffsync import Adapter

from asgiref.sync import sync_to_async, async_to_sync

from diffsync.diff import Diff
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotUpdated
from django.db import transaction
from django.core.exceptions import ValidationError
from nautobot.apps.jobs import Job
from nautobot.dcim.models import Device, Platform
from nautobot.ipam.models import IPAddress

from nautobot_ssot.contrib import NautobotAdapter
from structlog import BoundLogger

from nautobot_ssot_unifi.const import UNIFI_MAP, UNIFI_SSOT_INTERFACE_TYPES
from nautobot_ssot_unifi.ssot import models
from nautobot_ssot_unifi.ssot._ipam import _NetworkPrefixModel, _IPAddressRangeModel, _project_network_graph
from nautobot_ssot_unifi.ssot.address_scopes import AddressScopeResolver
from nautobot_ssot_unifi.ssot.interfaces import _interface_records
from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _BindingSession, _interface_id, _management_id
from nautobot_ssot_unifi.ssot._firmware import _FirmwareHold, _version
from nautobot_ssot_unifi.ssot._ownership import _claim_available
from nautobot_ssot_unifi.ssot._vlans import _included_sites, _networks, _membership_fields, _VlanHold, _wan_without_vlan
from nautobot_ssot_unifi.ssot._neighbors import _NeighborHold, _evidence, _observation, _read_observation, _utc, _pair
from nautobot_ssot_unifi.ssot._clients import (
    _MAC as _CLIENT_MAC,
    _ClientHold,
    _bound_documents,
    _client_facts,
    _entry as _client_entry,
    _instant,
    _merge_entry as _merge_client_entry,
    _observation as _client_observation,
    _read_observation as _read_client_observation,
    _read_unresolved as _read_unresolved_clients,
    _resolve as _resolve_client,
    _source_time,
    _unresolved_document,
)

from nautobot_ssot_unifi.unifi import Client

from netaddr import IPNetwork


class UnifiAdapterMixin:
    """Code common to both adapters."""

    site = models.SiteModel
    device_group = models.DeviceGroupModel
    device_type = models.DeviceTypeModel
    device = models.DeviceModel
    interface = models.InterfaceModel
    prefix = models.PrefixModel
    ip_address = models.IPAddressModel
    ip_address_to_interface = models.IPAddressToInterfaceModel

    top_level = (
        "site",
        "prefix",
        "ip_address",
        "device_group",
        "device_type",
        "device",
        "interface",
        "ip_address_to_interface",
    )

    def _configure_extensions(self, job):
        self.sync_firmware = getattr(job, "sync_firmware", False) is True
        if self.sync_firmware:
            self.device = models.FirmwareDeviceModel
            self.software_version = models.SoftwareVersionModel
            self.top_level = ("software_version",) + self.top_level
        self.sync_vlans = getattr(job, "sync_vlans", False) is True
        if self.sync_vlans:
            self.interface = models.VlanInterfaceModel
            self.vlan = models.VLANModel
            self.network_prefix = _NetworkPrefixModel
            self.ip_address_range = _IPAddressRangeModel
            self.top_level = ("vlan", "network_prefix", "ip_address_range") + self.top_level
        self.sync_neighbors = getattr(job, "sync_neighbors", False) is True
        if self.sync_neighbors:
            self.neighbor = models.NeighborModel
            self.top_level = self.top_level + ("neighbor",)
        self.sync_clients = getattr(job, "sync_clients", False) is True
        if self.sync_clients:
            self.client_observation = models.ClientObservationModel
            self.vm_client_observation = models.VMClientObservationModel
            self.unresolved_clients = models.UnresolvedClientsModel
            self.top_level = self.top_level + ("client_observation", "vm_client_observation", "unresolved_clients")


class UnifiNautobotAdapter(UnifiAdapterMixin, NautobotAdapter):
    """Adapter to connect to Nautobot."""

    _primary_ips: List[Dict[str, Any]]

    def __init__(self, *args, job, sync=None, **kwargs):
        """Initialize the adapter."""
        super().__init__(*args, job=job, sync=sync, **kwargs)
        self._primary_ips = []
        self._configure_extensions(job)

    def _load_objects(self, diffsync_model):
        if diffsync_model._modelname == "prefix" and self.sync_vlans:
            # Configured subnets have their own exact binding and DiffSync model.
            for obj in diffsync_model._get_queryset().exclude(pk__in=self.job._unifi_bindings.targets("prefix")):
                self._load_single_object(obj, diffsync_model, diffsync_model.get_synced_attributes())
            return
        if diffsync_model._modelname in {"client_observation", "vm_client_observation"}:
            targets = [
                pk for pk, proof in self.job._unifi_client_proofs.items() if proof["kind"] == diffsync_model._kind
            ]
            for obj in diffsync_model._get_queryset().filter(pk__in=targets):
                self._load_single_object(obj, diffsync_model, diffsync_model.get_synced_attributes())
            return
        if diffsync_model._modelname == "unresolved_clients":
            for obj in diffsync_model._get_queryset().filter(pk=self.job._unifi_bindings.integration_id):
                self._load_single_object(obj, diffsync_model, diffsync_model.get_synced_attributes())
            return
        if diffsync_model._modelname == "neighbor":
            targets = self.job._unifi_neighbor_proofs
            for obj in diffsync_model._get_queryset().filter(pk__in=targets):
                self._load_single_object(obj, diffsync_model, diffsync_model.get_synced_attributes())
            return
        if diffsync_model._modelname in {"device", "interface", "vlan"} or hasattr(diffsync_model, "_binding_kind"):
            targets = self.job._unifi_bindings.targets(
                getattr(diffsync_model, "_binding_kind", diffsync_model._modelname)
            )
            for obj in diffsync_model._get_queryset().filter(pk__in=targets):
                self._load_single_object(obj, diffsync_model, diffsync_model.get_synced_attributes())
                retained = self.job._unifi_bindings.retained_primaries.get(obj.pk)
                if retained is not None and diffsync_model._modelname == "device":
                    target = self.get("device", str(obj.pk))
                    for field in models.DeviceModel._attributes:
                        if field.startswith("primary_ip"):
                            setattr(retained, field, getattr(target, field))
                retained = self.job._unifi_bindings.retained_firmware.get(obj.pk)
                if retained is not None and diffsync_model._modelname == "device":
                    target = self.get("device", str(obj.pk))
                    for field in models.FirmwareDeviceModel._attributes[len(models.DeviceModel._attributes) :]:
                        setattr(retained, field, getattr(target, field))
                retained = self.job._unifi_bindings.retained_membership.get(obj.pk)
                if retained is not None and diffsync_model._modelname == "interface":
                    target = self.get("interface", str(obj.pk))
                    authoritative = self.job._unifi_bindings.membership_fields.get(obj.pk, frozenset())
                    for field in models.VlanInterfaceModel._attributes[len(models.InterfaceModel._attributes) :]:
                        if field in authoritative or (field == "unifi_ownership" and authoritative):
                            continue
                        setattr(retained, field, getattr(target, field))
            return
        if diffsync_model._modelname == "ip_address_to_interface":
            targets = self.job._unifi_bindings.targets("interface")
            for obj in diffsync_model._get_queryset().filter(interface_id__in=targets):
                self._load_single_object(obj, diffsync_model, diffsync_model.get_synced_attributes())
            return
        return super()._load_objects(diffsync_model)

    def sync_complete(
        self,
        source: Adapter,
        adapter: Diff,
        flags: DiffSyncFlags = DiffSyncFlags.NONE,
        logger: BoundLogger | None = None,
    ) -> None:
        """Update devices with their primary IPs once the sync is complete."""
        for info in self._primary_ips:
            try:
                with transaction.atomic():
                    self.job._unifi_bindings.lock()
                    device = models.DeviceModel.get_queryset().select_for_update().get(**info["device"])
                    self.job._unifi_bindings.check("device", device.pk)
                    for ip in ["primary_ip4", "primary_ip6"]:
                        if ip in info:
                            address = models.IPAddressModel.get_queryset().get(**info[ip]) if info[ip] else None
                            setattr(device, ip, address)
                    device.validated_save()
            except (Device.DoesNotExist, IPAddress.DoesNotExist, _BindingHold):
                raise ObjectNotUpdated(
                    "Primary selection requires an existing UniFi-owned device and address."
                ) from None
        self._primary_ips.clear()


class UnifiAdapter(UnifiAdapterMixin, Adapter):
    """Adapter to connect to Unifi."""

    def __init__(
        self,
        *args,
        job: Job,
        controller_name: str,
        default_location_type: str,
        default_location_name: str,
        namespace_name: str = "Global",
        address_scope_resolver=None,
        **kwargs,
    ):
        """Initialize the unifi source adapter.

        This adapter will read data from a Unifi Controller and create
        diffsync models based on the data received.

        Args:
            *args: Additional positional arguments needed by the parent DiffSync adapter.
            job (Job): The Nautobot job instance that is running this sync.
            controller_name (str): The device controller name.
            default_location_type (str): The name of the location type to use when creating new locations.
            default_location_name (str): The name of the location to use when the Unifi site is `default`.
            namespace_name (str): The existing Namespace for imported networks and addresses.
            address_scope_resolver: Validated optional per-address namespace rules.
            **kwargs: Additional keyword arguments needed by the parent DiffSync adapter.
        """
        self.debug = kwargs.pop("debug", False)
        super().__init__(*args, **kwargs)
        self.job = job
        self._configure_extensions(job)
        self.controller_name = controller_name
        self.default_location_type = default_location_type
        self.default_location_name = default_location_name
        self.namespace_name = namespace_name
        self.address_scope_resolver = address_scope_resolver or AddressScopeResolver(None, namespace_name)
        self._held = []
        self._neighbor_devices = {}
        self._neighbor_interfaces = {}
        self.job._unifi_neighbor_proofs = {}
        self.job._unifi_client_proofs = {}
        self.job._unifi_client_retire = {}
        self._client_entries = []
        self._client_reported = {}
        self._network_prefix_keys = set()

    @sync_to_async
    def _prepare_bindings(self):
        self.job._unifi_bindings = _BindingSession(self.job.controller.external_integration.pk)
        if self.sync_vlans:
            from nautobot.ipam.models import Prefix

            self._network_prefix_keys.update(
                (obj.namespace.name, str(obj.network), obj.prefix_length)
                for obj in Prefix.objects.filter(pk__in=self.job._unifi_bindings.targets("prefix")).select_related(
                    "namespace"
                )
            )
        if self.sync_neighbors:
            from nautobot_ssot_unifi.ssot._neighbors import _prerequisites

            _prerequisites()
        if self.sync_clients:
            from nautobot_ssot_unifi.ssot._clients import _prerequisites as _client_prerequisites

            _client_prerequisites()

    @sync_to_async
    def _target_id(self, kind, site_id, source_id, collision_lookup):
        try:
            return self.job._unifi_bindings.plan(kind, site_id, source_id, collision_lookup)
        except _BindingHold as error:
            self._held.append({"kind": kind, "site_id": site_id, "source_id": source_id, "reason": error.reason})
            return None

    @sync_to_async
    def _retain_primaries(self, device):
        """Mark unavailable evidence as non-authoritative, never a primary write."""
        self.job._unifi_bindings.retained_primaries[device.id] = device
        native = (
            Device.objects.filter(pk=device.id)
            .select_related("primary_ip4__parent__namespace", "primary_ip6__parent__namespace")
            .first()
        )
        if native is None:
            return
        for family in ("primary_ip4", "primary_ip6"):
            address = getattr(native, family)
            setattr(device, f"{family}__host", address.host if address else None)
            setattr(device, f"{family}__parent__namespace__name", address.parent.namespace.name if address else None)

    @sync_to_async
    def _load_firmware(self, device, site_id, source_id, reported_version):
        """Project shared version dependency and the exact Device field claim."""
        document = dict(self.job._unifi_bindings.documents[("device", device.id)])
        native = Device.objects.filter(pk=device.id).select_related("software_version__platform").first()
        if native is not None and native.software_version is not None:
            device.software_version__platform__name = native.software_version.platform.name
            device.software_version__version = native.software_version.version
        device.unifi_ownership = document
        try:
            version = _version(reported_version)
            if not Platform.objects.filter(name=device.platform__name).exists():
                raise _FirmwareHold("missing-platform")
            _claim_available(device.id, self.job._unifi_bindings.integration_id)
        except (_FirmwareHold, _BindingHold) as error:
            self._held.append({"kind": "firmware", "site_id": site_id, "source_id": source_id, "reason": error.reason})
            self.job._unifi_bindings.retained_firmware[device.id] = device
            return
        self.get_or_add_model_instance(self.software_version(platform__name=device.platform__name, version=version))
        device.software_version__platform__name = device.platform__name
        device.software_version__version = version
        document["field_paths"] = sorted(set(document["field_paths"]) | {"software_version"})
        self.job._unifi_bindings.documents[("device", device.id)] = document
        device.unifi_ownership = document

    @sync_to_async
    def _load_vlans(self, site_id, raw_networks):
        from django.db.models import Q
        from nautobot.ipam.models import VLAN

        group = self.job._unifi_vlan_groups[site_id]
        rows, held = _networks(raw_networks)
        self._held.extend(
            {"kind": "vlan", "site_id": site_id, "source_id": source_id, "reason": reason} for source_id, reason in held
        )
        # WAN configurations without a VLAN are not switch-tagging candidates.
        # Keep untagged/held LAN networks unresolved: no native VID was reported.
        networks = {row["_id"]: None for row in raw_networks if not _wan_without_vlan(row)}
        for row in rows:
            try:
                target_id = self.job._unifi_bindings.plan(
                    "vlan", site_id, row["source_id"], {"vlan_group_id": group, "vid": row["vid"]}
                )
                if (
                    VLAN.objects.filter(vlan_group_id=group)
                    .exclude(pk=target_id)
                    .filter(Q(vid=row["vid"]) | Q(name=row["name"]))
                    .exists()
                ):
                    raise _VlanHold("duplicate-vlan")
                _claim_available(
                    target_id,
                    self.job._unifi_bindings.integration_id,
                    model=VLAN,
                    paths=("name", "vid", "vlan_group", "status"),
                )
            except (_BindingHold, _FirmwareHold, _VlanHold) as error:
                self._held.append(
                    {"kind": "vlan", "site_id": site_id, "source_id": row["source_id"], "reason": error.reason}
                )
                continue
            document = self.job._unifi_bindings.documents[("vlan", target_id)]
            document["field_paths"] = ["name", "status", "vid", "vlan_group"]
            self.add(
                self.vlan(
                    id=target_id, name=row["name"], vid=row["vid"], vlan_group__id=group, unifi_ownership=document
                )
            )
            networks[row["source_id"]] = target_id
        return networks

    @sync_to_async
    def _load_network_graph(self, site_id, raw_networks, networks):
        _project_network_graph(self, site_id, raw_networks, networks)

    @sync_to_async
    def _load_membership(self, interface, site_id, source_id, port, profiles, overrides, networks):
        from nautobot.dcim.models import Interface

        document = dict(self.job._unifi_bindings.documents[("interface", interface.id)])
        interface.unifi_ownership = document
        fields, reason = _membership_fields(port, profiles, overrides, networks)
        if fields.get("untagged_vlan__id") is not None and "mode" not in fields:
            current_mode = Interface.objects.filter(pk=interface.id).values_list("mode", flat=True).first()
            if not current_mode:
                # Native validation requires a mode; incomplete source policy
                # cannot establish one or turn a known VLAN into an access port.
                fields.pop("untagged_vlan__id")
                reason = "incomplete-membership"
        try:
            if not fields:
                raise _VlanHold(reason or "incomplete-membership")
            _claim_available(
                interface.id,
                self.job._unifi_bindings.integration_id,
                model=Interface,
                paths=tuple(field.split("__")[0] for field in fields),
            )
        except (_VlanHold, _FirmwareHold, _BindingHold) as error:
            self._held.append(
                {"kind": "vlan-membership", "site_id": site_id, "source_id": source_id, "reason": error.reason}
            )
            self.job._unifi_bindings.retained_membership[interface.id] = interface
            return
        authoritative = frozenset(fields)
        self.job._unifi_bindings.membership_fields[interface.id] = authoritative
        document["field_paths"] = sorted(set(document["field_paths"]) | {field.split("__")[0] for field in fields})
        self.job._unifi_bindings.documents[("interface", interface.id)] = document
        if reason:
            self._held.append({"kind": "vlan-membership", "site_id": site_id, "source_id": source_id, "reason": reason})
            self.job._unifi_bindings.retained_membership[interface.id] = interface
        else:
            self.job._unifi_bindings.retained_membership.pop(interface.id, None)
        for field, value in fields.items():
            setattr(interface, field, [{"id": entry} for entry in value] if field == "tagged_vlans" else value)

    @sync_to_async
    def _load_neighbors(self):
        import re

        now = self._collection_completed_at
        session = self.job._unifi_bindings

        def endpoint(site_id, chassis, port, kind):
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", chassis):
                raise _NeighborHold("unresolved-endpoint")
            devices = self._neighbor_devices[site_id]
            matches = [
                source_id
                for source_id, raw in devices.items()
                if isinstance(raw.get("mac"), str) and raw["mac"].lower() == chassis.lower()
            ]
            if len(matches) != 1:
                raise _NeighborHold("unresolved-endpoint")
            source_device = matches[0]
            candidates = []
            for (site, device, source_id), (interface, record) in self._neighbor_interfaces.items():
                if site != site_id or device != source_device:
                    continue
                matched = record["port_idx"] == port if kind == "port_idx" else record["ifname"] == port
                # A peer's explicit Ethernet alias may refer to its stable port
                # index. This joins vendor source facts; never native names/MACs.
                if kind == "ifname" and not matched:
                    aliases = devices[source_device].get("ethernet_table", [])
                    matched = any(
                        isinstance(alias, dict)
                        and alias.get("name") == port
                        and type(alias.get("port_idx")) is int
                        and alias["port_idx"] == record["port_idx"]
                        for alias in aliases
                    )
                if matched:
                    candidates.append(interface)
            if len(candidates) != 1:
                raise _NeighborHold("unresolved-endpoint")
            if "base-" not in candidates[0].type:
                raise _NeighborHold("unsupported-endpoint")
            return candidates[0].id

        for (site_id, device_id, source_id), (interface, record) in self._neighbor_interfaces.items():
            try:
                if "base-" not in interface.type:
                    raise _NeighborHold("unsupported-endpoint")
                raw = self._neighbor_devices[site_id][device_id]
                port_rows = [
                    row
                    for row in raw.get("port_table", [])
                    if isinstance(row, dict)
                    and (
                        (record["port_idx"] is not None and row.get("port_idx") == record["port_idx"])
                        or (record["port_idx"] is None and row.get("ifname") == record["ifname"])
                    )
                ]
                port = dict(port_rows[0]) if len(port_rows) == 1 else {}
                if "lldp_table" in raw:
                    rows = raw["lldp_table"]
                    if not isinstance(rows, list) or any(
                        not isinstance(row, dict)
                        or (
                            type(row.get("local_port_idx")) is not int
                            and not isinstance(row.get("local_port_name"), str)
                        )
                        for row in rows
                    ):
                        raise _NeighborHold("incomplete-neighbor-evidence")
                    selected = [
                        row
                        for row in rows
                        if (record["port_idx"] is not None and row.get("local_port_idx") == record["port_idx"])
                        or (record["ifname"] is not None and row.get("local_port_name") == record["ifname"])
                    ]
                    if any(
                        (
                            record["port_idx"] is not None
                            and "local_port_idx" in row
                            and row["local_port_idx"] != record["port_idx"]
                        )
                        or (
                            record["ifname"] is not None
                            and "local_port_name" in row
                            and row["local_port_name"] != record["ifname"]
                        )
                        for row in selected
                    ):
                        raise _NeighborHold("incomplete-neighbor-evidence")
                    if any(row.get("is_wired") is not True for row in selected):
                        raise _NeighborHold("unsupported-endpoint")
                    if "lldp_table" in port:
                        local = port["lldp_table"]
                        if not isinstance(local, list) or any(not isinstance(row, dict) for row in local):
                            raise _NeighborHold("incomplete-neighbor-evidence")

                        def keys(entries):
                            return sorted(
                                (
                                    row.get("lldp_chassis_id", row.get("chassis_id", "")),
                                    row.get("lldp_port_id", row.get("port_id", "")),
                                )
                                for row in entries
                            )

                        if keys(local) != keys(selected):
                            raise _NeighborHold("incomplete-neighbor-evidence")
                    port["lldp_table"] = selected
                previous = _read_observation(interface.id, session.integration_id, site_id)
                peers = _evidence(
                    raw,
                    port,
                    now,
                    _utc(previous["last_complete_collection_at"]) if previous else None,
                    lambda chassis, port, kind: endpoint(site_id, chassis, port, kind),
                )
                for peer_id in peers:
                    _pair(interface.id, peer_id)
                self.job._unifi_neighbor_proofs[interface.id] = {
                    "device": {key: raw[key] for key in ("state", "last_seen", "next_heartbeat_at")},
                    "now": now,
                    "site_id": site_id,
                }
                document = _observation(previous, session.integration_id, site_id, peers, now)
                self.add(self.neighbor(id=interface.id, neighbor_observations=document))
            except (_NeighborHold, _BindingHold) as error:
                self._held.append(
                    {"kind": "neighbor", "site_id": site_id, "source_id": source_id, "reason": error.reason}
                )

    @staticmethod
    def _attachment_id(facts, attach):
        """Resolve the reporting port/radio Interface; an unresolved radio keeps only the SSID."""
        entry = attach.get(facts["device"], {})
        if facts["kind"] == "wired":
            target = entry.get("ports", {}).get(facts["port_idx"])
            if target is None:
                raise _ClientHold("unresolved-endpoint")
            return target
        radio = entry.get("radios", {}).get(facts.get("radio_name"))
        if radio is None:
            return None
        target, band = radio
        if facts.get("radio") is not None and band is not None and band != facts["radio"]:
            return None
        return target

    def _client_model(self, kind):
        return self.client_observation if kind == "interface" else self.vm_client_observation

    @sync_to_async
    def _load_clients(self, site_id, rows, networks, network_rows, devices, attach):
        """Project one complete site report; nothing is written when collection raised before this."""
        from nautobot.dcim.models import Interface
        from nautobot.virtualization.models import VMInterface

        session = self.job._unifi_bindings
        vids = {key: None for key in networks}
        for network in _networks(network_rows)[0]:
            if networks.get(network["source_id"]) is not None:
                vids[network["source_id"]] = network["vid"]
        if any(not isinstance(row.get("mac"), str) or not _CLIENT_MAC.fullmatch(row["mac"]) for row in rows):
            # An unidentifiable row cannot establish that an earlier client disappeared.
            raise ValueError("Invalid UniFi client channel.")
        if any(row.get("site_id") is not None and row["site_id"] != site_id for row in rows):
            # Contradictory explicit site evidence makes the whole report untrustworthy for this
            # site: nothing is stored and no absence is inferred. A missing site_id is not rejected.
            raise ValueError("Invalid UniFi client channel.")
        counts = Counter(row["mac"].lower() for row in rows)
        reported = self._client_reported.setdefault(site_id, set())
        for row in rows:
            mac = row["mac"].lower()
            if mac in reported:
                continue
            reported.add(mac)
            if counts[mac] > 1:
                # Conflicting rows for one MAC establish no single set of facts.
                self._client_entries.append(
                    _client_entry(site_id, mac, "ambiguous-binding", [], {}, vids, devices, networks)
                )
                continue
            attachment_id = None
            try:
                facts = _client_facts(row, vids, devices)
                attachment_id = self._attachment_id(facts, attach)
                kind, target = _resolve_client(mac, session.integration_id, site_id)
                model = Interface if kind == "interface" else VMInterface
                previous = _read_client_observation(model, target, session.integration_id, site_id)
                if previous is not None and _instant(previous["last_seen_at"]) > _source_time(facts["last_seen"]):
                    raise _ClientHold("incomplete-membership")
                vlan = row.get("vlan") if type(row.get("vlan")) is int else None
                document = _client_observation(
                    previous, session.integration_id, site_id, facts, attachment_id, networks[facts["network_id"]], vlan
                )
                self.job._unifi_client_proofs[target] = {"site_id": site_id, "kind": kind}
                self.job._unifi_client_retire[(site_id, mac)] = (kind, target, document)
                self.add(self._client_model(kind)(id=target, client_observations=document))
            except _ClientHold as error:
                self._client_entries.append(
                    _client_entry(
                        site_id, mac, error.reason, error.candidates, row, vids, devices, networks, attachment_id
                    )
                )

    @sync_to_async
    def _finalize_clients(self):
        """Flip presence for omitted clients and publish the integration's unresolved record."""
        session = self.job._unifi_bindings
        integration_id = session.integration_id
        collected = self._client_reported
        previous = _read_unresolved_clients(integration_id)
        prior = {(entry["site_id"], entry["mac"]): entry for entry in (previous or {}).get("clients", [])}
        entries = [_merge_client_entry(prior.get((e["site_id"], e["mac"])), e) for e in self._client_entries]
        for entry in (previous or {}).get("clients", []):
            site, mac = entry["site_id"], entry["mac"]
            if site not in collected:
                entries.append(entry)
            elif mac not in collected[site]:
                entries.append({**entry, "presence": "not-observed"})
        for kind, target, value in _bound_documents():
            site = value["site_id"]
            if (
                value["integration_id"] != integration_id
                or site not in collected
                or value["mac"] in collected[site]
                or value["presence"] != "observed"
            ):
                continue
            document = _client_observation(value, integration_id, site, None, None, None, None)
            self.job._unifi_client_proofs[target] = {"site_id": site, "kind": kind}
            self.add(self._client_model(kind)(id=target, client_observations=document))
        self.add(
            self.unresolved_clients(
                id=UUID(integration_id), unresolved_clients=_unresolved_document(integration_id, entries)
            )
        )

    @sync_to_async
    def _debug(self, *args, **kwargs):
        self.job.logger.debug(*args, **kwargs)

    @sync_to_async
    def _info(self, *args, **kwargs):
        self.job.logger.info(*args, **kwargs)

    def _create_interface(self, device, interface_name, interface_type, port_id, target_id):
        interface = self.interface(
            id=target_id,
            **{f"device__{key}": value for key, value in device.get_identifiers().items()},
            label=interface_name,
            type=interface_type,
            unifi_port_id=port_id,
        )
        if self.sync_vlans:
            interface.unifi_ownership = dict(self.job._unifi_bindings.documents[("interface", target_id)])
            self.job._unifi_bindings.retained_membership[target_id] = interface
        return interface

    async def _assign_ip(self, ip: str, netmask: str, interface: str) -> IPNetwork:
        ip_address = IPNetwork(f"{ip}/{netmask}")
        namespace_name = self.address_scope_resolver.resolve(str(ip_address.ip))
        prefix_key = (namespace_name, str(ip_address.network), ip_address.prefixlen)
        if prefix_key not in self._network_prefix_keys:
            prefix, created = self.get_or_add_model_instance(
                self.prefix(
                    namespace__name=namespace_name,
                    network=str(ip_address.network),
                    prefix_length=ip_address.prefixlen,
                )
            )
            if created:
                await self._debug("Added prefix %s", prefix)

        _, created = self.get_or_add_model_instance(
            self.ip_address(
                parent__namespace__name=namespace_name,
                host=str(ip_address.ip),
                mask_length=ip_address.prefixlen,
                parent__network=str(ip_address.network),
                parent__prefix_length=ip_address.prefixlen,
            )
        )
        self.add(interface)
        if created:
            assignment = self.ip_address_to_interface(
                ip_address__parent__namespace__name=namespace_name,
                **{f"interface__{key}": value for key, value in interface.get_identifiers().items()},
                ip_address__host=str(ip_address.ip),
            )
            self.add(assignment)
        return ip_address

    @async_to_sync
    async def load(self, host, port, username, password, verify_cert, timeout):
        """Asynchronously load data from unifi."""
        self.client = Client(
            host=host,
            port=port,
            username=username,
            password=password,
            verify_cert=verify_cert,
            timeout=timeout,
        )
        try:
            self._collection_channel = "bindings"
            await self._prepare_bindings()
            await self._load_sites()
            if self.sync_neighbors:
                self._collection_channel = "neighbor-evidence"
                await self._load_neighbors()
            if self.sync_clients:
                self._collection_channel = "client-associations"
                await self._finalize_clients()
        except ValidationError:
            raise
        except Exception:
            raise ValueError(f"UniFi {self._collection_channel} collection failed.") from None
        finally:
            await self.client.logout()

    async def _load_sites(self):
        """Load the selected controller's sites while its session is open."""
        await self._info("Loading data from the Unifi Controller %s", self.job.controller)
        self.add(
            self.device_group(
                name="default",
                controller__name=self.controller_name,
            )
        )
        self._collection_channel = "site-discovery"
        sites = list(await self.client.get_sites())
        if self.sync_vlans:
            _included_sites(self.job._unifi_vlan_groups, [site.site_id for site in sites])
        for site in sites:
            site_id = site.site_id
            site_name = site.name
            self.client.current_site = site_name
            if self.sync_vlans:
                self._collection_channel = "vlan-configuration"
                network_rows, profile_rows = await self.client._get_vlan_configuration()
                networks = await self._load_vlans(site_id, network_rows)
                await self._load_network_graph(site_id, network_rows, networks)
                profiles = {}
                for profile in profile_rows:
                    if not isinstance(profile.get("_id"), str) or not profile["_id"] or profile["_id"] in profiles:
                        raise ValueError("Invalid UniFi port-profile configuration channel.")
                    profiles[profile["_id"]] = profile
            location_type__name = self.default_location_type
            if site_name == "default":
                site_name = self.default_location_name
            site = self.site(name=site_name, location_type__name=location_type__name)
            await self._debug("Added site %s", site)
            self.add(site)

            self._collection_channel = "managed-devices"
            devices = list(await self.client.get_devices())
            if self.sync_neighbors:
                self._collection_completed_at = datetime.now(timezone.utc)
                self._neighbor_devices[site_id] = {device.raw["_id"]: device.raw for device in devices}
                if len(self._neighbor_devices[site_id]) != len(devices):
                    raise ValueError("Duplicate UniFi managed-device source identity.")
            attach = {}
            for unifi_device in devices:
                device_source_id = unifi_device.raw["_id"]
                device_id = await self._target_id(
                    "device",
                    site_id,
                    device_source_id,
                    {"name": unifi_device.name, "location__name": site.name},
                )
                if device_id is None:
                    continue
                unifi_info = self.job.hardware_models.get(unifi_device.model, {})
                unifi_type = unifi_device.type
                if unifi_type == "usw":
                    if "lite" in unifi_info.get("name", "").lower():
                        unifi_type = "usw_lite"
                    elif "flex" in unifi_info.get("name", "").lower():
                        unifi_type = "usw_flex"

                device_type = self.device_type(
                    model=unifi_device.model,
                    part_number=unifi_info.get("sku", ""),
                )
                _, created = self.get_or_add_model_instance(device_type)
                if created:
                    await self._debug("Added device type %s", device_type)

                device = self.device(
                    id=device_id,
                    name=unifi_device.name,
                    controller_managed_device_group__name="default",
                    controller_managed_device_group__controller__name=self.job.controller.name,
                    location__name=site.name,
                    device_type__model=unifi_device.model,
                    role__name=UNIFI_MAP[unifi_type]["role"],
                    serial=unifi_device.raw["serial"],
                    platform__name=UNIFI_MAP[unifi_type]["platform"],
                )
                await self._debug("Adding device %s", device)
                if self.sync_firmware:
                    await self._load_firmware(device, site_id, device_source_id, unifi_device.raw.get("version"))
                self.add(device)
                interfaces = _interface_records(unifi_device.raw)
                self.job._unifi_bindings.observe_interfaces(
                    site_id,
                    device_source_id,
                    {
                        _interface_id(device_source_id, port_idx=row["port_idx"], ifname=row["ifname"])
                        for row in interfaces
                    },
                )
                config_network = unifi_device.raw.get("config_network")
                if not config_network:
                    await self._retain_primaries(device)
                elif not isinstance(config_network, dict) or config_network.get("type") not in {"static", "dhcp"}:
                    raise ValueError("Invalid UniFi management addressing evidence.")
                if (
                    config_network
                    and config_network["type"] == "static"
                    and any(row["name"] == "mgmt" for row in interfaces)
                ):
                    # A source interface name alone cannot bind the controller's
                    # device-level address to that interface.
                    raise ValueError("Reported management interface conflicts with synthetic management interface.")
                for port in interfaces:
                    source_id = _interface_id(device_source_id, port_idx=port["port_idx"], ifname=port["ifname"])
                    target_id = await self._target_id(
                        "interface", site_id, source_id, {"device_id": device.id, "name": port["name"]}
                    )
                    if target_id is None:
                        continue
                    interface = self._create_interface(
                        device,
                        port["name"],
                        port["type"],
                        port["port_idx"],
                        target_id,
                    )
                    if self.sync_neighbors:
                        self._neighbor_interfaces[(site_id, device_source_id, source_id)] = (interface, port)
                    if self.sync_clients:
                        entry = attach.setdefault(device_source_id, {"ports": {}, "radios": {}})
                        radio_by_name = {
                            row.get("name"): row.get("radio")
                            for row in unifi_device.raw.get("radio_table", [])
                            if isinstance(row, dict)
                        }
                        if port["port_idx"] is not None:
                            entry["ports"][port["port_idx"]] = target_id
                        if port["port_idx"] is None and port["name"] in radio_by_name:
                            entry["radios"][port["name"]] = (target_id, radio_by_name[port["name"]])
                    if self.sync_vlans:
                        rows = [
                            row
                            for row in unifi_device.raw.get("port_table", [])
                            if isinstance(row, dict)
                            and (
                                (port["port_idx"] is not None and row.get("port_idx") == port["port_idx"])
                                or (port["port_idx"] is None and row.get("ifname") == port["ifname"])
                            )
                        ]
                        raw_port = rows[0] if len(rows) == 1 else {}
                        await self._load_membership(
                            interface,
                            site_id,
                            source_id,
                            raw_port,
                            profiles,
                            unifi_device.raw.get("port_overrides", []),
                            networks,
                        )
                    if port.get("ip") and port.get("netmask"):
                        await self._assign_ip(port["ip"], port["netmask"], interface)
                    else:
                        self.add(interface)

                if config_network and config_network["type"] == "static":
                    target_id = await self._target_id(
                        "interface", site_id, _management_id(device_source_id), {"device_id": device.id, "name": "mgmt"}
                    )
                    if target_id is None:
                        # An unresolved address holder must not clear last-good primaries.
                        await self._retain_primaries(device)
                        continue
                    interface = self._create_interface(
                        device, "mgmt", UNIFI_SSOT_INTERFACE_TYPES["other"], -1, target_id
                    )
                    ip_address = await self._assign_ip(
                        unifi_device.raw["config_network"]["ip"],
                        unifi_device.raw["config_network"]["netmask"],
                        interface,
                    )
                    if ip_address.version == 4:
                        device.primary_ip4__host = str(ip_address.ip)
                        device.primary_ip4__parent__namespace__name = self.address_scope_resolver.resolve(
                            str(ip_address.ip)
                        )
                    else:
                        device.primary_ip6__host = str(ip_address.ip)
                        device.primary_ip6__parent__namespace__name = self.address_scope_resolver.resolve(
                            str(ip_address.ip)
                        )

            if self.sync_clients:
                self._collection_channel = "client-associations"
                client_rows = await self.client._get_client_configuration()
                await self._load_clients(
                    site_id,
                    client_rows,
                    networks,
                    network_rows,
                    {device.raw["_id"]: device.raw for device in devices},
                    attach,
                )

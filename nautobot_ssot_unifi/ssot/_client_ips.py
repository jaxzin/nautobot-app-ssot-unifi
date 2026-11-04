"""Project and reconcile one proven IPv4 address per matched client."""

from collections import Counter
from dataclasses import dataclass
from ipaddress import ip_address, ip_interface
from typing import Optional
from uuid import UUID

from diffsync import DiffSyncModel
from diffsync.exceptions import ObjectCrudException, ObjectNotUpdated
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from nautobot.dcim.models import Interface
from nautobot.extras.models import MetadataType, ObjectMetadata, Status
from nautobot.ipam.models import IPAddress, IPAddressToInterface, Namespace, Prefix
from nautobot.virtualization.models import VMInterface
from nautobot_ssot.contrib import NautobotModel, ObjectMetadataAnnotation

from nautobot_ssot_unifi.ssot._bindings import _BindingHold
from nautobot_ssot_unifi.ssot._clients import _ClientHold, _instant, _read_observation, _resolve
from nautobot_ssot_unifi.ssot._ipam import _namespace_covers_subnet
from nautobot_ssot_unifi.ssot._ownership import _claim_available

_METADATA_TYPE = "UniFi client IP assignments"
_METADATA_FIELD = ObjectMetadataAnnotation(metadata_type_name=_METADATA_TYPE, data_type="json")
_MODELS = {"interface": Interface, "vminterface": VMInterface}
_ENDPOINT_FIELDS = {"interface": "interface_id", "vminterface": "vm_interface_id"}
_ROLE_FLAGS = ("source", "destination", "default", "preferred", "primary", "secondary", "standby")


def _prerequisites():
    types = ContentType.objects.get_for_models(*_MODELS.values())
    metadata = MetadataType.objects.filter(name=_METADATA_TYPE, data_type="json").first()
    if metadata is None or not {item.pk for item in types.values()}.issubset(
        metadata.content_types.values_list("pk", flat=True)
    ):
        raise ValueError("Invalid UniFi client IP assignments MetadataType prerequisite.")


def _facts(resolver, row, network):
    """Prove an address and subnet from the same source network and namespace."""
    if row.get("ip") in (None, ""):
        return None
    try:
        if (
            not isinstance(row["ip"], str)
            or not isinstance(network["ip_subnet"], str)
            or network.get("purpose") in ("wan", "wan2")
        ):
            raise ValueError
        host, gateway = ip_address(row["ip"]), ip_interface(network["ip_subnet"])
        subnet = gateway.network
        if (
            host.version != 4
            or gateway.version != 4
            or host not in subnet
            or host == gateway.ip
            or host.is_unspecified
            or host.is_multicast
            or host.is_link_local
            or host.is_loopback
            or (subnet.prefixlen < 31 and host in (subnet.network_address, subnet.broadcast_address))
        ):
            raise ValueError
        name = resolver.resolve(str(host))
        namespace = Namespace.objects.get(name=name)
        if not _namespace_covers_subnet(resolver, subnet, namespace.pk):
            raise ValueError
    except (KeyError, TypeError, ValueError, Namespace.DoesNotExist):
        raise _ClientHold("invalid-network") from None
    return {"host": str(host), "network": str(subnet), "namespace_id": str(namespace.pk)}


def _query(queryset, lock):
    return queryset.select_for_update(of=("self",)) if lock else queryset


def _metadata(interface, proof, lock):
    records = list(_query(interface.associated_object_metadata.filter(metadata_type__name=_METADATA_TYPE), lock)[:2])
    if not records:
        return None
    record = records[0]
    try:
        value = record.value
        if len(records) != 1 or record.scoped_fields or not isinstance(value, dict):
            raise ValueError
        if set(value) != {"schema_version", "integration_id", "site_id", "client_mac", "assignments"}:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if any(value[key] != proof[key] for key in ("integration_id", "site_id", "client_mac")):
            raise _ClientHold("foreign-owner")
        entries = value["assignments"]
        if not isinstance(entries, list):
            raise ValueError
        ids = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"assignment_id", "ip_address_id"}:
                raise ValueError
            assignment_id = UUID(entry["assignment_id"])
            UUID(entry["ip_address_id"])
            if assignment_id in ids:
                raise ValueError
            ids.add(assignment_id)
    except _ClientHold:
        raise
    except (TypeError, ValueError, AttributeError):
        raise _ClientHold("identity-conflict") from None
    return record


@dataclass
class _State:
    metadata: Optional[ObjectMetadata]
    address: Optional[IPAddress]
    link: Optional[IPAddressToInterface]
    obsolete: list[IPAddressToInterface]
    unchanged: bool

    def snapshot(self):
        """Compare the write prerequisites that may change after preview."""
        return (
            (self.metadata.pk, self.metadata.value) if self.metadata else None,
            self.address.pk if self.address else None,
            self.link.pk if self.link else None,
            tuple(link.pk for link in self.obsolete),
            self.unchanged,
        )

    def current_address(self, desired):
        """Supply a stable native diff value, including the former address when known."""
        if self.unchanged:
            return desired
        if len(self.obsolete) == 1:
            address = self.obsolete[0].ip_address
            former = {
                "host": address.host,
                "network": str(address.parent.prefix),
                "namespace_id": str(address.parent.namespace_id),
            }
            if former != desired:
                return former
        return None


def _inspect(interface, proof, desired, *, lock=False):
    """Check address compatibility and identify only exact, recorded links as removable."""
    metadata = _metadata(interface, proof, lock)
    endpoint = _ENDPOINT_FIELDS[proof["kind"]]
    other_endpoint = next(field for field in _ENDPOINT_FIELDS.values() if field != endpoint)
    host, namespace = desired["host"], desired["namespace_id"]
    mask = ip_interface(desired["network"]).network.prefixlen
    addresses = list(
        _query(
            IPAddress.objects.filter(parent__namespace_id=namespace, host=host).select_related("parent", "status"),
            lock,
        )[:2]
    )
    if len(addresses) > 1:
        raise _ClientHold("identity-conflict")
    address = addresses[0] if addresses else None
    link = None
    if address is not None:
        if (
            str(address.parent.prefix) != desired["network"]
            or address.mask_length != mask
            or address.status.name != "Active"
            or address.type not in ("host", "dhcp")
        ):
            raise _ClientHold("identity-conflict")
        links = list(_query(address.interface_assignments.all(), lock)[:2])
        if links and (
            len(links) != 1
            or getattr(links[0], endpoint) != interface.pk
            or getattr(links[0], other_endpoint) is not None
        ):
            raise _ClientHold("foreign-owner")
        link = links[0] if links else None
    obsolete = []
    entries = metadata.value["assignments"] if metadata else []
    kept = 0
    for entry in entries:
        owned = _query(IPAddressToInterface.objects.filter(pk=entry["assignment_id"]), lock).first()
        if owned is None:
            continue
        if (
            str(owned.ip_address_id) != entry["ip_address_id"]
            or getattr(owned, endpoint) != interface.pk
            or getattr(owned, other_endpoint) is not None
        ):
            raise _ClientHold("identity-conflict")
        if owned == link:
            kept += 1
            continue
        if any(getattr(owned, "is_" + flag) for flag in _ROLE_FLAGS) or (
            interface.parent.primary_ip4_id == owned.ip_address_id
        ):
            raise _ClientHold("foreign-owner")
        obsolete.append(owned)

    if link is None or obsolete:
        _claim_available(
            interface.pk,
            proof["integration_id"],
            lock=lock,
            model=type(interface),
            paths=("ip_addresses", "ip_address_assignments"),
        )
    for ip_id in {item.ip_address_id for item in obsolete} | ({address.pk} if address and link is None else set()):
        _claim_available(
            ip_id,
            proof["integration_id"],
            lock=lock,
            model=IPAddress,
            paths=("interfaces", "vm_interfaces", "interface_assignments"),
        )
    return _State(metadata, address, link, obsolete, link is not None and kept == len(entries))


def _prefix(desired, vlan_id, *, planned=False):
    prefixes = Prefix.objects.filter(namespace_id=desired["namespace_id"])
    try:
        parent = prefixes.get_closest_parent(desired["host"], include_self=True)
    except Prefix.DoesNotExist:
        parent = None
    if parent is not None and str(parent.prefix) == desired["network"]:
        if parent.vlan_id is not None and parent.vlan_id != vlan_id:
            raise _ClientHold("identity-conflict")
        return parent
    if planned and (parent is None or parent.prefix_length < ip_interface(desired["network"]).network.prefixlen):
        return None
    raise _ClientHold("unresolved-network")


def _project(adapter):
    """Resolve conflicts across the whole collected report before building the diff."""
    counts = Counter((address["namespace_id"], address["host"]) for _, _, address, _ in adapter._client_ip_candidates)
    targets = Counter(target for _, target, _, _ in adapter._client_ip_candidates)
    for proof, target, desired, namespace_name in adapter._client_ip_candidates:
        try:
            if counts[desired["namespace_id"], desired["host"]] != 1 or targets[target] != 1:
                raise _ClientHold("identity-conflict")
            if adapter.get_or_none(
                adapter.ip_address, {"parent__namespace__name": namespace_name, "host": desired["host"]}
            ):
                raise _ClientHold("foreign-owner")
            network = ip_interface(desired["network"]).network
            _prefix(
                desired,
                proof["vlan_id"],
                planned=(namespace_name, str(network.network_address), network.prefixlen)
                in adapter._network_prefix_keys,
            )
            kind = proof["kind"]
            interface = _MODELS[kind].objects.get(pk=target)
            state = _inspect(interface, proof, desired)
        except (_ClientHold, _BindingHold) as error:
            adapter._held.append({"kind": "client-ip", "source_id": str(target), "reason": error.reason})
            continue
        except (Interface.DoesNotExist, VMInterface.DoesNotExist):
            adapter._held.append({"kind": "client-ip", "source_id": str(target), "reason": "unresolved-endpoint"})
            continue
        current = state.current_address(desired)
        adapter.job._unifi_client_ip_plans[kind, target] = (proof, current, state.snapshot())
        model = adapter.client_ip if kind == "interface" else adapter.vm_client_ip
        adapter.add(model(id=target, address=desired))


class _ClientIPModel(NautobotModel):
    """One native SSoT change, with address creation and link replacement in one transaction."""

    _identifiers = ("id",)
    _attributes = ("address",)
    id: UUID
    address: Optional[dict[str, str]] = None

    def update(self, attrs):
        """Recheck evidence and ownership, then reconcile only the native address link."""
        job = self.adapter.job
        kind = self._model._meta.model_name
        proof, _, snapshot = job._unifi_client_ip_plans[kind, self.id]
        desired = attrs["address"]
        try:
            with transaction.atomic():
                job._unifi_bindings.lock()
                Namespace.objects.select_for_update().get(pk=desired["namespace_id"])
                interface = self._model.objects.select_for_update().get(pk=self.id)
                parent = interface.parent
                type(parent).objects.select_for_update().get(pk=parent.pk)
                self._validate_client(proof)
                prefix = _prefix(desired, proof["vlan_id"])
                state = _inspect(interface, proof, desired, lock=True)
                if state.snapshot() != snapshot:
                    raise _ClientHold("identity-conflict")
                self._reconcile(interface, proof, desired, prefix, state)
                self.adapter.cache.invalidate_cache()
                return DiffSyncModel.update(self, attrs)
        except (
            _ClientHold,
            _BindingHold,
            ValidationError,
            IntegrityError,
            ObjectCrudException,
            ObjectDoesNotExist,
        ):
            raise ObjectNotUpdated("UniFi client IP assignment could not be reconciled.") from None

    def _validate_client(self, proof):
        if _resolve(proof["client_mac"], proof["integration_id"], proof["site_id"]) != (proof["kind"], self.id):
            raise _ClientHold("identity-conflict")
        observation = _read_observation(self._model, self.id, proof["integration_id"], proof["site_id"], lock=True)
        if observation is not None and (
            _instant(observation["last_seen_at"]) > proof["seen"]
            or observation["network"]["source_network_id"] != proof["network_id"]
        ):
            raise _ClientHold("incomplete-membership")

    def _reconcile(self, interface, proof, desired, prefix, state):
        address = state.address
        if address is None:
            address = IPAddress(
                address=f"{desired['host']}/{prefix.prefix_length}",
                parent=prefix,
                status=Status.objects.get(name="Active"),
            )
            address.validated_save()

        entries = []
        if state.link is None:
            link = IPAddressToInterface(ip_address=address, **{_ENDPOINT_FIELDS[proof["kind"]]: interface.pk})
            link.validated_save()
            entries.append({"ip_address_id": str(address.pk), "assignment_id": str(link.pk)})
        elif state.metadata:
            entries = [
                entry for entry in state.metadata.value["assignments"] if entry["assignment_id"] == str(state.link.pk)
            ]
        for link in state.obsolete:
            link.delete()
        if entries or state.metadata:
            value = {
                "schema_version": 1,
                **{key: proof[key] for key in ("integration_id", "site_id", "client_mac")},
                "assignments": entries,
            }
            self._set_object_metadata_fields({"assignments": (_METADATA_FIELD, value)}, interface, self.adapter)


class _PhysicalClientIPModel(_ClientIPModel):
    _model = Interface
    _modelname = "client_ip"


class _VirtualClientIPModel(_ClientIPModel):
    _model = VMInterface
    _modelname = "vm_client_ip"

"""Nautobot DiffSync models for Unifi SSoT."""

from typing import TYPE_CHECKING, Annotated, Optional, Any, TypedDict
import uuid

from diffsync.exceptions import ObjectNotCreated, ObjectNotUpdated, ObjectNotDeleted
from django.db import transaction
from django.core.exceptions import ValidationError
from nautobot_ssot.contrib import NautobotModel, CustomFieldAnnotation, ObjectMetadataAnnotation

from nautobot.extras.models import Tag, Status, Relationship, RelationshipAssociation
from nautobot.dcim.models import DeviceType, Device, Location, ControllerManagedDeviceGroup, Interface, SoftwareVersion
from nautobot.ipam.models import IPAddress, Prefix, IPAddressToInterface, VLAN
from nautobot.ipam.choices import PrefixTypeChoices

from nautobot_ssot_unifi.const import UNIFI_MANUFACTURER, UNIFI_SSOT_TAG
from nautobot_ssot_unifi.ssot._bindings import _BindingHold, _OWNERSHIP_TYPE, _physical_interface_id
from nautobot_ssot_unifi.ssot._firmware import _FirmwareHold
from nautobot_ssot_unifi.ssot._ownership import _claim_available
from nautobot_ssot_unifi.ssot._neighbors import (
    _NeighborHold,
    _fresh,
    _pair,
    _read_observation,
    _utc,
    _OBSERVATION_TYPE,
    _RELATIONSHIP_KEY,
)

if TYPE_CHECKING:
    from nautobot_ssot_unifi.ssot.adapters import UnifiNautobotAdapter


class ActiveStatusMixin:
    """A mixin that sets the status to active upon creation."""

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """This overridden method makes sure to set the object status as `Active` when created."""
        attrs["status_id"] = Status.objects.get(name="Active").id
        return super().create(adapter, ids, attrs)


class UnifiModelMixin:
    """Mixin to provide standard functionality for all Unifi Nautobot models."""

    @classmethod
    def get_queryset(cls):
        """All synchronized objects are tagged with the UNIFI_SSOT_TAG."""
        return super().get_queryset().filter(tags__name=UNIFI_SSOT_TAG)

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """Create will either create or update a Nautobot object.

        This method will first look for a corresponding object in the
        database (by identifier). If found, rather than "creating" a
        new object the existing object must already have UNIFI_SSOT_TAG.
        Untagged objects are not adopted. If not found then the object is created.

        Args:
            adapter (UnifiNautobotAdapter): The diffsync adapter.
            ids (dict): Dictionary of fields and values to find the object.
            attrs (dict): Dictionary of fields and values that need to be assigned.

        Returns:
            NautobotModel: The diffsync model created/updated.
        """
        try:
            obj = cls._model.objects.get(**ids)
            if not obj.tags.filter(name=UNIFI_SSOT_TAG).exists():
                raise ObjectNotCreated("Matching inventory exists outside UniFi ownership; refusing to adopt it.")
            return cls(**{**ids, **attrs, "pk": obj.pk, "adapter": adapter})
        except cls._model.DoesNotExist:
            model = super().create(adapter, ids, attrs)
            cls._model.objects.get(**ids).tags.add(Tag.objects.get(name=UNIFI_SSOT_TAG))
            return model

    def delete(self):
        """Remove the UNIFI_SSOT_TAG from a model.

        Returns:
            NautobotModel: Returns `self`
        """
        self.get_from_db().tags.remove(Tag.objects.get(name=UNIFI_SSOT_TAG))
        if getattr(self, "_perform_delete", False):
            return super().delete()
        return self


class SiteModel(ActiveStatusMixin, UnifiModelMixin, NautobotModel):
    """Location model for sites."""

    _model = Location
    _modelname = "site"
    _identifiers = ("name",)
    _attributes = ("location_type__name",)

    name: str
    status_id: uuid.UUID = None
    location_type__name: str = ""


class DeviceTypeModel(UnifiModelMixin, NautobotModel):
    """DeviceType model."""

    _model = DeviceType
    _modelname = "device_type"
    _identifiers = (
        "manufacturer__name",
        "model",
    )
    _attributes = ("part_number",)

    manufacturer__name: str = UNIFI_MANUFACTURER
    model: str

    part_number: str = ""


class _BoundModelMixin:
    """Keep native SSoT writes, guarded by exact ownership in each transaction."""

    @classmethod
    def get_queryset(cls):
        return super().get_queryset().filter(tags__name=UNIFI_SSOT_TAG)

    @classmethod
    def create(cls, adapter, ids, attrs):
        session = adapter.job._unifi_bindings
        try:
            with transaction.atomic():
                session.lock()
                if cls._modelname == "interface":
                    session.check_parent(ids["id"], attrs["device__id"])
                cls._guard_extension_fields(adapter, ids["id"], attrs)
                result = session.admit(
                    cls._modelname, ids["id"], lambda: super(_BoundModelMixin, cls).create(adapter, ids, attrs)
                )
                obj = cls._model.objects.get(pk=ids["id"])
                obj.tags.add(Tag.objects.get(name=UNIFI_SSOT_TAG))
                result.pk = obj.pk
                return result
        except (_BindingHold, _FirmwareHold) as error:
            raise ObjectNotCreated(error.reason) from None

    def update(self, attrs):
        session = self.adapter.job._unifi_bindings
        try:
            with transaction.atomic():
                session.lock()
                session.check(self._modelname, self.id)
                self._model.objects.select_for_update().get(pk=self.id)
                # Do not reuse a stale ORM object cached during preview.
                self.adapter.cache.invalidate_cache()
                if self._modelname == "interface":
                    session.check_native_parent(self.id, attrs.get("device__id", self.device__id))
                self._guard_extension_fields(self.adapter, self.id, attrs)
                return super().update(attrs)
        except (_BindingHold, _FirmwareHold) as error:
            raise ObjectNotUpdated(error.reason) from None

    @classmethod
    def _guard_extension_fields(cls, adapter, target_id, attrs):
        ownership = attrs.get("unifi_ownership") or {}
        if cls._modelname == "device" and (
            any(key.startswith("software_version__") for key in attrs)
            or "software_version" in ownership.get("field_paths", [])
        ):
            _claim_available(target_id, adapter.job._unifi_bindings.integration_id, lock=True)
        if cls._modelname in {"interface", "vlan"}:
            paths = (
                ("mode", "untagged_vlan", "tagged_vlans")
                if cls._modelname == "interface"
                else ("name", "vid", "vlan_group", "status")
            )
            if "unifi_ownership" in attrs or any(key.split("__")[0] in paths for key in attrs):
                _claim_available(
                    target_id, adapter.job._unifi_bindings.integration_id, lock=True, model=cls._model, paths=paths
                )

    def delete(self):
        """This inventory increment never deletes a Device or Interface."""
        raise ObjectNotDeleted("Automatic UniFi inventory deletion is not enabled.")


class DeviceModel(ActiveStatusMixin, _BoundModelMixin, NautobotModel):
    """Device model."""

    _model = Device
    _modelname = "device"
    _identifiers = ("id",)
    _attributes = (
        "name",
        "controller_managed_device_group__name",
        "controller_managed_device_group__controller__name",
        "device_type__model",
        "role__name",
        "location__name",
        "serial",
        "platform__name",
        "primary_ip4__host",
        "primary_ip6__host",
        "primary_ip4__parent__namespace__name",
        "primary_ip6__parent__namespace__name",
    )
    _perform_delete = True

    id: uuid.UUID
    name: str
    controller_managed_device_group__name: str = None
    controller_managed_device_group__controller__name: str = None
    device_type__model: str
    role__name: str
    location__name: str
    serial: str
    platform__name: str
    primary_ip4__host: Optional[str] = None
    primary_ip6__host: Optional[str] = None
    primary_ip4__parent__namespace__name: Optional[str] = None
    primary_ip6__parent__namespace__name: Optional[str] = None

    status_id: uuid.UUID = None

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """Create the device.

        This overridden method removes the primary IP addresses since those
        cannot be set until after the interfaces are created. The primary IPs
        are set in the `sync_complete` callback of the adapter.

        Args:
            adapter (UnifiNautobotAdapter): The nautobot sync adapter.
            ids (dict[str, Any]): The natural keys for the device.
            attrs (dict[str, Any]): The attributes to assign to the newly created
                device.

        Returns:
            DeviceModel: The device model.
        """
        attrs = dict(attrs)
        primary_attrs = cls._pop_primary_attrs(attrs)
        result = super().create(adapter, ids, attrs)
        if result is not None:
            result._defer_primary_attrs(adapter, primary_attrs)
        return result

    @classmethod
    def _pop_primary_attrs(cls, attrs, current=None):
        primary_attrs = {}
        for family in ("primary_ip4", "primary_ip6"):
            host_field = f"{family}__host"
            namespace_field = f"{family}__parent__namespace__name"
            if host_field in attrs or namespace_field in attrs:
                host = attrs.pop(host_field, getattr(current, host_field, None))
                namespace = attrs.pop(namespace_field, getattr(current, namespace_field, None))
                primary_attrs[host_field] = host
                primary_attrs[namespace_field] = namespace if host else None
        return primary_attrs

    def _defer_primary_attrs(self, adapter, attrs):
        session = getattr(adapter.job, "_unifi_bindings", None)
        if not attrs or (session is not None and self.id in session.retained_primaries):
            return
        info = {"device": self.get_identifiers()}
        for family in ("primary_ip4", "primary_ip6"):
            if f"{family}__host" in attrs:
                host = attrs[f"{family}__host"]
                info[family] = (
                    {
                        "host": host,
                        "parent__namespace__name": attrs[f"{family}__parent__namespace__name"],
                    }
                    if host
                    else None
                )
        adapter._primary_ips.append(info)
        for field, value in attrs.items():
            setattr(self, field, value)

    def update(self, attrs):
        """Defer primary updates until native interface assignments exist."""
        attrs = dict(attrs)
        primary_attrs = self._pop_primary_attrs(attrs, current=self)
        result = super().update(attrs)
        if result is not None:
            self._defer_primary_attrs(self.adapter, primary_attrs)
        return result


class SoftwareVersionModel(ActiveStatusMixin, NautobotModel):
    """Shared native catalog identity; existing curated fields are never updated."""

    _model = SoftwareVersion
    _modelname = "software_version"
    _identifiers = ("platform__name", "version")
    _attributes = ()
    platform__name: str
    version: str
    status_id: Optional[uuid.UUID] = None

    @classmethod
    def create(cls, adapter, ids, attrs):
        """Reuse shared versions unchanged; create only an absent exact identity."""
        existing = cls._model.objects.filter(**ids).first()
        if existing is not None:
            return cls(adapter=adapter, pk=existing.pk, **ids)
        result = super().create(adapter, ids, attrs)
        result.pk = cls._model.objects.get(**ids).pk
        return result


class FirmwareDeviceModel(DeviceModel):
    """Opt-in firmware lookup and native metadata projection."""

    _attributes = DeviceModel._attributes + (
        "software_version__platform__name",
        "software_version__version",
        "unifi_ownership",
    )
    software_version__platform__name: Optional[str] = None
    software_version__version: Optional[str] = None
    unifi_ownership: Annotated[
        Optional[dict[str, Any]], ObjectMetadataAnnotation(metadata_type_name=_OWNERSHIP_TYPE, data_type="json")
    ] = None

    def update(self, attrs):
        """Supply both components of a changed platform-scoped version lookup."""
        attrs = dict(attrs)
        if self.id in self.adapter.job._unifi_bindings.retained_firmware:
            for field in self._attributes[len(DeviceModel._attributes) :]:
                attrs.pop(field, None)
        if any(key.startswith("software_version__") for key in attrs):
            for field in ("software_version__platform__name", "software_version__version"):
                attrs.setdefault(field, getattr(self, field))
        return super().update(attrs)


class DeviceGroupModel(UnifiModelMixin, NautobotModel):
    """DeviceGroup model."""

    _model = ControllerManagedDeviceGroup
    _modelname = "device_group"
    _identifiers = (
        "controller__name",
        "name",
    )
    _attributes = tuple()

    controller__name: str
    name: str


class InterfaceModel(ActiveStatusMixin, _BoundModelMixin, NautobotModel):
    """Native UUID identity, separate from the editable source label."""

    _model = Interface
    _modelname = "interface"
    _identifiers = ("id",)
    _attributes = (
        "label",
        "device__id",
        "type",
        "unifi_port_id",
    )
    _perform_delete = True

    id: uuid.UUID
    label: str
    name: str = ""
    device__id: uuid.UUID

    type: str
    unifi_port_id: Annotated[Optional[int], CustomFieldAnnotation(name="unifi_port_id")] = None

    status_id: uuid.UUID = None

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """Create a new interface.

        This overridden create will set the interface name. That way the interface name
        can be changed (so that Napalm stuff matches Nautobot) but the ssot job
        can still find the interfaces.
        """
        attrs = {**attrs, "name": attrs["label"]}
        return super().create(adapter, ids, attrs)


class _VlanReference(TypedDict):
    id: uuid.UUID


class VlanInterfaceModel(InterfaceModel):
    """Opt-in complete native VLAN membership with a transactional claim."""

    _attributes = InterfaceModel._attributes + ("mode", "untagged_vlan__id", "tagged_vlans", "unifi_ownership")
    mode: str = ""
    untagged_vlan__id: Optional[uuid.UUID] = None
    tagged_vlans: list[_VlanReference] = []
    unifi_ownership: Annotated[
        Optional[dict[str, Any]], ObjectMetadataAnnotation(metadata_type_name=_OWNERSHIP_TYPE, data_type="json")
    ] = None

    def update(self, attrs):
        """A held dataset cannot overwrite membership even after target loading."""
        attrs = dict(attrs)
        fields = self._attributes[len(InterfaceModel._attributes) :]
        if self.id in self.adapter.job._unifi_bindings.retained_membership:
            for field in fields:
                attrs.pop(field, None)
        elif any(field in attrs for field in fields):
            for field in fields:
                attrs.setdefault(field, getattr(self, field))
        return super().update(attrs)

    @classmethod
    def _update_obj_with_parameters(cls, obj, parameters, adapter):
        # Native validation sees the desired mode, not a stale tagged set that
        # would forbid an otherwise valid tagged -> access transition.
        if "mode" in parameters and obj.pk and Interface.objects.filter(pk=obj.pk).exists():
            obj.tagged_vlans.clear()
        return super()._update_obj_with_parameters(obj, parameters, adapter)


class VLANModel(ActiveStatusMixin, _BoundModelMixin, NautobotModel):
    """Exact source network identity, distinct from mutable VID and name."""

    _model = VLAN
    _modelname = "vlan"
    _identifiers = ("id",)
    _attributes = ("name", "vid", "vlan_group__id", "status__name", "unifi_ownership")
    id: uuid.UUID
    name: str
    vid: int
    vlan_group__id: uuid.UUID
    status__name: str = "Active"
    status_id: Optional[uuid.UUID] = None
    unifi_ownership: Annotated[
        dict[str, Any], ObjectMetadataAnnotation(metadata_type_name=_OWNERSHIP_TYPE, data_type="json")
    ]


class NeighborModel(NautobotModel):
    """Native metadata comparison; one atomic association/document write per reporter."""

    _model = Interface
    _modelname = "neighbor"
    _identifiers = ("id",)
    _attributes = ("neighbor_observations",)
    id: uuid.UUID
    neighbor_observations: Annotated[
        Optional[dict[str, Any]], ObjectMetadataAnnotation(metadata_type_name=_OBSERVATION_TYPE, data_type="json")
    ] = None

    @classmethod
    def get_queryset(cls):
        """Restrict observation targets to source-owned admitted Interfaces."""
        return super().get_queryset().filter(tags__name=UNIFI_SSOT_TAG)

    @classmethod
    def _update_obj_with_parameters(cls, obj, parameters, adapter):
        """Use the framework's metadata writer without saving the inventory NIC."""
        if set(parameters) != {"neighbor_observations"}:
            raise ValueError("Unsupported UniFi neighbor mutation.")
        cls._set_object_metadata_fields(
            {
                "neighbor_observations": (
                    cls.get_attr_annotation("neighbor_observations"),
                    parameters["neighbor_observations"],
                )
            },
            obj,
            adapter,
        )

    @classmethod
    def create(cls, adapter, ids, attrs):
        """Create an observation on an already admitted Interface, not a new NIC."""
        instance = cls(adapter=adapter, pk=ids["id"], **ids)
        try:
            return instance.update(attrs)
        except ObjectNotUpdated:
            raise ObjectNotCreated("UniFi neighbor reporter transaction failed.") from None

    def update(self, attrs):
        """Associations and metadata either commit together or both roll back."""
        from django.db.models import Q

        session = self.adapter.job._unifi_bindings
        proof = self.adapter.job._unifi_neighbor_proofs[self.id]
        document = attrs["neighbor_observations"]
        try:
            with transaction.atomic():
                session.lock()
                previous = _read_observation(self.id, session.integration_id, proof["site_id"], lock=True)
                if previous != self.neighbor_observations:
                    # This diff was computed against a different observation
                    # baseline. Fail this reporter instead of erasing history
                    # committed by a concurrent run; the next preview reloads it.
                    raise _NeighborHold("incomplete-neighbor-evidence")
                _fresh(
                    proof["device"], proof["now"], _utc(previous["last_complete_collection_at"]) if previous else None
                )
                session.check("interface", self.id)
                session.check_native_parent(self.id)
                from nautobot_ssot_unifi.ssot._neighbors import _prerequisites

                _prerequisites()
                relationship = Relationship.objects.get(key=_RELATIONSHIP_KEY)
                endpoints = {self.id} | {
                    uuid.UUID(peer["peer_interface_id"]) for peer in document["peers"] if peer["presence"] == "observed"
                }
                for target in sorted(endpoints, key=str):
                    session.check("interface", target)
                    session.check_native_parent(target)
                    if not _physical_interface_id(session._document("interface", target)["source_object_id"]):
                        raise _NeighborHold("unsupported-endpoint")
                    interface = Interface.objects.select_for_update().get(pk=target)
                    if not interface.type or "base-" not in interface.type:
                        raise _NeighborHold("unsupported-endpoint")
                for peer in document["peers"]:
                    if peer["presence"] != "observed":
                        continue
                    first, second = _pair(self.id, peer["peer_interface_id"])
                    associations = RelationshipAssociation.objects.filter(relationship=relationship).filter(
                        Q(source_id=first, destination_id=second) | Q(source_id=second, destination_id=first)
                    )
                    if associations.count() > 1:
                        raise ValueError("Invalid UniFi neighbor association prerequisite.")
                    if not associations.exists():
                        association = RelationshipAssociation(
                            relationship=relationship,
                            source_type=relationship.source_type,
                            destination_type=relationship.destination_type,
                            source_id=first,
                            destination_id=second,
                        )
                        association.validated_save()
                self.adapter.cache.invalidate_cache()
                return super().update(attrs)
        except (
            _BindingHold,
            _NeighborHold,
            ValueError,
            ValidationError,
            Interface.DoesNotExist,
            Relationship.DoesNotExist,
        ):
            raise ObjectNotUpdated("UniFi neighbor reporter transaction failed.") from None

    def delete(self):
        """Last-known edges and observation documents are not deleted by this Job."""
        raise ObjectNotDeleted("UniFi neighbor observations are retained.")


class PrefixModel(ActiveStatusMixin, UnifiModelMixin, NautobotModel):
    """DiffSync model for Prefix."""

    _model = Prefix
    _modelname = "prefix"
    _identifiers = ("namespace__name", "network", "prefix_length")
    _attributes = tuple()

    network: str
    prefix_length: int
    namespace__name: str = "Global"

    status_id: uuid.UUID = None
    type: str = None

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """This overridden method makes sure to set the prefix type when created."""
        attrs["type"] = PrefixTypeChoices.TYPE_NETWORK
        return super().create(adapter, ids, attrs)


class IPAddressModel(ActiveStatusMixin, UnifiModelMixin, NautobotModel):
    """DiffSync model for IP Addresses."""

    _model = IPAddress
    _modelname = "ip_address"
    _identifiers = (
        "parent__namespace__name",
        "host",
    )
    _attributes = (
        "mask_length",
        "parent__network",
        "parent__prefix_length",
    )

    host: str
    parent__namespace__name: str = "Global"
    mask_length: int
    parent__network: str
    parent__prefix_length: int

    status_id: uuid.UUID = None

    def update(self, attrs):
        """Resolve parent changes with their complete namespace-scoped identity."""
        if "parent__network" in attrs or "parent__prefix_length" in attrs:
            attrs = {
                "parent__namespace__name": self.parent__namespace__name,
                "parent__network": self.parent__network,
                "parent__prefix_length": self.parent__prefix_length,
                **attrs,
            }
        return super().update(attrs)


class IPAddressToInterfaceModel(NautobotModel):
    """DiffSync model for assigning IP Addresses to interfaces."""

    _model = IPAddressToInterface
    _modelname = "ip_address_to_interface"
    _identifiers = (
        "ip_address__parent__namespace__name",
        "ip_address__host",
        "interface__id",
    )

    _attributes = tuple()

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """Require owned endpoints even when native sync continues after a failed create."""
        with transaction.atomic():
            adapter.job._unifi_bindings.lock()
            return cls._create_assignment(adapter, ids, attrs)

    @classmethod
    def _create_assignment(cls, adapter, ids, attrs):
        parameters = {**ids, **attrs}
        for relation, model in (("ip_address", IPAddressModel), ("interface", InterfaceModel)):
            lookup = {
                key.removeprefix(f"{relation}__"): value
                for key, value in parameters.items()
                if key.startswith(f"{relation}__")
            }
            try:
                model.get_queryset().get(**lookup)
            except model._model.DoesNotExist:
                raise ObjectNotCreated("Assignment endpoints must exist and be UniFi-owned.") from None
        try:
            adapter.job._unifi_bindings.check("interface", parameters["interface__id"])
            adapter.job._unifi_bindings.check_native_parent(parameters["interface__id"])
        except _BindingHold as error:
            raise ObjectNotCreated(error.reason) from None
        return super().create(adapter, ids, attrs)

    @classmethod
    def get_queryset(cls):
        """Read only assignments whose address and interface are UniFi-owned."""
        # Native assignment records do not support tags. Ownership belongs to
        # both endpoints; loading unrelated assignments can fail when their
        # interfaces have no UniFi controller-managed device group.
        return (
            super()
            .get_queryset()
            .filter(
                interface__tags__name=UNIFI_SSOT_TAG,
                ip_address__tags__name=UNIFI_SSOT_TAG,
            )
            .distinct()
        )

    ip_address__host: str
    ip_address__parent__namespace__name: str = "Global"
    interface__id: uuid.UUID

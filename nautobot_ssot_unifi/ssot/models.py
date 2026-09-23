"""Nautobot DiffSync models for Unifi SSoT."""

from typing import TYPE_CHECKING, Annotated, Optional
import uuid

from diffsync.exceptions import ObjectNotCreated
from nautobot_ssot.contrib import NautobotModel, CustomFieldAnnotation

from nautobot.extras.models import Tag, Status
from nautobot.dcim.models import DeviceType, Device, Location, ControllerManagedDeviceGroup, Interface
from nautobot.ipam.models import IPAddress, Prefix, IPAddressToInterface
from nautobot.ipam.choices import PrefixTypeChoices

from nautobot_ssot_unifi.const import UNIFI_MANUFACTURER, UNIFI_SSOT_TAG

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


class DeviceModel(ActiveStatusMixin, UnifiModelMixin, NautobotModel):
    """Device model."""

    _model = Device
    _modelname = "device"
    _identifiers = (
        "name",
        "controller_managed_device_group__name",
        "controller_managed_device_group__controller__name",
    )
    _attributes = (
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
        if not attrs:
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


class InterfaceModel(ActiveStatusMixin, UnifiModelMixin, NautobotModel):
    """DeviceGroup model."""

    _model = Interface
    _modelname = "interface"
    _identifiers = (
        "label",
        "device__name",
        "device__controller_managed_device_group__name",
        "device__controller_managed_device_group__controller__name",
    )

    _attributes = (
        "type",
        "unifi_port_id",
    )
    _perform_delete = True

    label: str
    name: str = ""
    device__name: str
    device__controller_managed_device_group__name: str
    device__controller_managed_device_group__controller__name: str

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
        attrs["name"] = ids["label"]
        return super().create(adapter, ids, attrs)


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
        "interface__label",
        "interface__device__name",
        "interface__device__controller_managed_device_group__name",
        "interface__device__controller_managed_device_group__controller__name",
    )

    _attributes = tuple()

    @classmethod
    def create(cls, adapter: "UnifiNautobotAdapter", ids, attrs):
        """Require owned endpoints even when native sync continues after a failed create."""
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
    interface__label: str
    interface__device__name: str
    interface__device__controller_managed_device_group__name: str
    interface__device__controller_managed_device_group__controller__name: str

"""Private reported-version validation and ownership gate for native firmware."""

from uuid import UUID
from nautobot.core.constants import CHARFIELD_MAX_LENGTH

from nautobot_ssot_unifi.ssot._bindings import _OWNERSHIP_TYPE


class _FirmwareHold(ValueError):
    def __init__(self, reason):
        if reason not in {"missing-version", "missing-platform", "foreign-owner"}:
            raise ValueError("Invalid firmware hold classification.")
        self.reason = reason
        super().__init__(reason)


def _version(value):
    if not isinstance(value, str):
        raise _FirmwareHold("missing-version")
    version = value.strip()
    if not version or len(version) > CHARFIELD_MAX_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in version):
        raise _FirmwareHold("missing-version")
    return version


def _claim_available(target_id, integration_id, *, lock=False, model=None, paths=("software_version",)):
    """Recognize the shared v1 JSON field-claim envelope, not display-name suffixes."""
    from django.contrib.contenttypes.models import ContentType
    from nautobot.dcim.models import Device
    from nautobot.extras.models import ObjectMetadata

    model = model or Device

    documents = ObjectMetadata.objects.filter(
        assigned_object_type=ContentType.objects.get_for_model(model), assigned_object_id=target_id
    ).select_related("metadata_type")
    if lock:
        documents = documents.select_for_update(of=("self",))
    for document in documents:
        value = document.value
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            continue
        claims = value.get("field_paths")
        if not isinstance(claims, list) or not set(paths).intersection(
            claim for claim in claims if isinstance(claim, str)
        ):
            continue
        try:
            same_owner = UUID(str(value.get("integration_id"))) == UUID(str(integration_id))
        except (ValueError, TypeError, AttributeError):
            same_owner = False
        if document.metadata_type.name != _OWNERSHIP_TYPE or not same_owner:
            raise _FirmwareHold("foreign-owner")

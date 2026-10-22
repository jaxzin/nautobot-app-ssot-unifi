# Import firmware, VLANs, and neighbors

Use **Unifi to Nautobot** to populate reported firmware, site-scoped VLANs,
interface membership, and last-known neighbor relationships. This reference
applies to Nautobot 3.2.4 with SSoT 4.6.1. UniFi remains read-only.

## Prepare the inventory

Provision prerequisites through your deployment automation before running the
Job. The connector does not create the extension MetadataTypes, Relationships,
or VLANGroups listed below. SSoT manages its optional synchronization datetime
MetadataType separately.

| Prerequisite | Native definition |
| --- | --- |
| UniFi inventory ownership | JSON MetadataType on Device, Interface, and VLAN |
| UniFi neighbor observations | JSON MetadataType on Interface |
| Last-known UniFi neighbors | Relationship with key `unifi_observed_neighbor`, type `symmetric-many-to-many`, and both endpoints `dcim.interface` |
| Neighbor labels | Relationship, source, and destination label `Last-known UniFi neighbors`; neither side required |
| Firmware dependency | Existing Platform and an Active status applicable to SoftwareVersion |
| VLAN scope | One existing VLANGroup per included UniFi site |

Device bindings use the immutable source device ID. Interface bindings use the
parent source ID and an explicit port index or OS identifier. Provision exact
bindings for existing inventory; never automatically adopt by an editable name.
A genuinely new source-managed record acquires its binding in the same
transaction as its creation.

Ownership is connector policy stored in native ObjectMetadata. Its JSON fields
are connector-defined, not built-in SSoT arbitration. A binding-only document
has `scoped_fields=[]` and an empty `field_paths` array:

```json
{
  "schema_version": 1,
  "integration_id": "00000000-0000-4000-8000-000000000001",
  "site_id": "site-a",
  "source_object_id": "[\"device-a\",\"port_idx\",3]",
  "field_paths": []
}
```

The document's assigned object identifies the native UUID. A Device or VLAN
uses its opaque source ID instead of an Interface tuple. An OS-only Interface
uses `["device-a","ifname","eth0"]`. The logical management address-holder
uses `["device-a","management","config_network"]`; it is not a physical NIC
and cannot report or receive a physical-neighbor observation.

## Choose Job inputs

| Job data argument | Default | Enabled behavior |
| --- | --- | --- |
| `sync_firmware` | `false` | Create/reuse platform-scoped SoftwareVersion and assign owned Device.software_version |
| `sync_vlans` | `false` | Import explicit VLANs and complete effective Interface membership |
| `sync_neighbors` | `false` | Import exactly resolved last-known managed-interface neighbors |
| `vlan_groups` | `null` | Bind immutable source site IDs to existing native VLANGroup UUIDs |

For example, enable VLAN ingestion for one included site with this Job data:

```json
{
  "sync_vlans": true,
  "vlan_groups": [
    {
      "site_id": "site-a",
      "vlan_group_id": "00000000-0000-4000-8000-000000000004"
    }
  ]
}
```

Replace the example identities with your reviewed source site and native group.
Every included site must have exactly one binding, and distinct sites cannot
share a group. Static input errors fail before requests. Unknown or unbound
sites fail after read-only site discovery and before inventory writes.

1. Run with **Dryrun** enabled and the desired feature inputs.
2. Review the native SSoT diff and protected **unifi-held.json** artifact.
3. Apply the same inputs only when the intended changes and held records are understood.
4. Verify native inventory, metadata, and SSoT results; run another preview.

Disabled features are omitted from comparison. Turning a feature off does not
clear its previously imported fields.

## Track synchronization timestamps

Use SSoT's standard Object Metadata support to track synchronization datetime.
In the deployment-managed `PLUGINS_CONFIG`, include `UnifiDataSource` in
`nautobot_ssot.enable_metadata_for`. Preserve other enabled Job names and SSoT
settings. This configuration fragment shows the relevant setting:

```python
{
    "nautobot_ssot": {
        "enable_metadata_for": ["UnifiDataSource"],
    },
}
```

SSoT creates the datetime MetadataType **Last sync from Unifi**, using the Job's
data source name. Standard model create/update operations write the timestamp
and scope it to the model's synchronized fields. Browse an imported object's
**Metadata** tab to view it.

This timestamp records synchronization, not independent verification of every
scoped field. An unchanged object does not refresh it. An update to another
field can refresh the object-model timestamp while incomplete VLAN membership
is retained. Do not interpret it as proof that VLAN membership was verified.
No separate last-verified metadata is maintained.

Use the object's native change history to inspect inventory changes and SSoT
history to inspect synchronization outcomes. See [SSoT Object Metadata
support](https://docs.nautobot.com/projects/ssot/en/latest/user/app_use_cases/#object-metadata-support)
for the framework contract.

## Understand imported facts

Firmware uses the reported version after trimming surrounding whitespace and
the Device's existing Platform. Existing shared SoftwareVersion descriptions,
support dates, and other curated fields remain unchanged. No image is fabricated.
Missing Platform, missing version, or a foreign field claim preserves the last
Device assignment.

VLANs require explicit IDs from 1 through 4094. The connector does not guess
VLAN 1 when a source identifier is missing. Complete profiles and overrides map
to native access, tagged, or tagged-all mode. Explicit complete removal can
clear owned membership; missing profiles or fields cannot. Source omission
does not delete VLANs or move IPAM records between namespaces.

An explicit `forward=all` together with `tagged_vlan_mgmt=auto` or `all`
establishes tagged-all mode without an exclusion array. The native network
must still be explicitly identified and resolved. A present exclusion array
is validated and respected; an incomplete custom policy is held. Neither a
missing native network nor an untagged network without a resolvable VLAN ID
is treated as a default VLAN.

Neighbor relationships describe observations, not Cable segments or link state.
Passive patch panels and wall outlets are not auto-discovered. One fully resolved
report can establish an unordered Interface pair; reciprocal reports reuse it.
Interface status and physical Cable, FrontPort, and RearPort records are untouched.

## Distinguish fresh evidence from retained observations

A reporter is eligible only when its Device reports connected state, valid
`last_seen` and `next_heartbeat_at`, and collection completion falls between
those times. A repeat run also requires `last_seen` later than the previous
complete collection. Every applicable LLDP/uplink channel must be explicitly
present and completely resolved. A successful HTTP GET alone is insufficient.

Browse **UniFi neighbor observations** in the Interface Metadata screen. Each
peer has a native UUID, evidence kinds, first/last observation times, check time,
and `observed` or `not-observed` presence. These fields are connector-defined
JSON values; native MetadataType, ObjectMetadata, and SSoT results supply storage
and history.

An eligible omission marks only that reporter's peer entry `not-observed` and
advances its check time. It retains the edge and last evidence. Reappearance
reuses them. Failed, stale, or incomplete collection advances none of these
values and cannot rewrite the other endpoint's evidence.

## Resolve held records and failures

**unifi-held.json** contains only `schema_version: 1` and a `held` array of
`kind`, `source_id`, and stable `reason` codes. It contains no credentials,
source response bodies, or raw configuration. Artifact access uses native
JobResult/FileProxy permissions.

| Reason | Operator action |
| --- | --- |
| `missing-platform`, `missing-version` | Verify the native Platform and source-reported version; do not invent an assignment |
| `missing-binding`, `ambiguous-binding`, `identity-conflict` | Review the exact source/native pairing and deliver a corrected binding through deployment automation |
| `foreign-owner` | Resolve the conflicting ownership declaration; do not overwrite another source's fields |
| `invalid-vlan`, `duplicate-vlan` | Correct source VLAN IDs/names or reviewed native group conflicts |
| `missing-profile`, `incomplete-membership` | Verify complete supported source configuration; last-good membership is retained |
| `unresolved-endpoint`, `unsupported-endpoint` | Verify explicit source Device and port identities; only Ethernet neighbors are supported |
| `incomplete-neighbor-evidence` | Verify source timing and applicable channel completeness; do not treat missing fields as an empty report |

A required collection request failure prevents inventory writes for that source
run. Destination failures fail the Job but retain already committed independent
changes. Each reporting Interface's association and observation writes are
atomic together; whole-run rollback is not provided. Review the native results,
correct the cause, and preview before another apply.

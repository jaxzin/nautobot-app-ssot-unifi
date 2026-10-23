# Import firmware, VLANs, and neighbors

Use **Unifi to Nautobot** to populate reported firmware, site-scoped VLANs,
configured subnets and DHCP pools, interface membership, and last-known neighbor relationships. This reference
applies to Nautobot 3.2.4 with SSoT 4.6.1. UniFi remains read-only.

## Prepare the inventory

Provision prerequisites through your deployment automation before running the
Job. The connector does not create the extension MetadataTypes, Relationships,
or VLANGroups listed below. SSoT manages its optional synchronization datetime
MetadataType separately.

| Prerequisite | Native definition |
| --- | --- |
| UniFi inventory ownership | JSON MetadataType on Device, Interface, VLAN, Prefix, and IP Address Range |
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

A configured Prefix uses the source identity
`<network-id>:ipv4:<namespace-uuid>:<canonical-cidr>`. Its DHCP pool uses that
identity followed by `:dhcp4`. Provision these exact bindings when adopting
existing IPAM inventory. Source identities do not authorize automatic adoption
of a matching subnet or address span.

## Choose Job inputs

| Job data argument | Default | Enabled behavior |
| --- | --- | --- |
| `sync_firmware` | `false` | Create/reuse platform-scoped SoftwareVersion and assign owned Device.software_version |
| `sync_vlans` | `false` | Import VLANs, configured IPv4 subnets, explicit DHCP pools, and independently verified Interface membership fields |
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

VLAN IDs range from 1 through 4094. The protected default corporate LAN
(`purpose=corporate`, `attr_no_delete=true`, `vlan_enabled=false`, no explicit
VID) maps to VLAN 1. WANs and arbitrary untagged networks do not. A missing
port network reference is never replaced with an assumed VLAN 1.

Profiles, port facts, and overrides determine authoritative membership fields.
An explicit native network reference updates `Interface.untagged_vlan` even
when tagged policy is unavailable. Explicit `forward=all` establishes
`tagged-all` mode and an empty explicit tagged set without replacing an omitted
native assignment. A complete block-all policy establishes access mode.
Custom sets require complete native and tagged/exclusion evidence. Present
arrays are validated; omitted fields retain last-known values. Partial
progress can therefore coexist with an `incomplete-membership` hold.
Nautobot requires an interface mode before a native VLAN can be assigned.
If the source policy is incomplete and the existing mode is blank, the native
assignment remains held; the connector does not invent access or trunk mode.

### Browse configured subnets and DHCP pools

| Source fact | Native record |
| --- | --- |
| Network `ip_subnet`, such as `192.0.2.1/24` | Prefix `192.0.2.0/24`, linked through `Prefix.vlan` |
| Enabled DHCP with explicit `dhcpd_start` and `dhcpd_stop` | IP Address Range under that Prefix |

Every address in the subnet must resolve to the same existing Namespace;
uncovered or ambiguous interior ranges hold the subnet import. WAN
configuration is not imported as a LAN subnet. Browse a VLAN's
Prefixes, then a Prefix's **IP Address Ranges** tab. A pool is named after its
source network and contains the reported inclusive start/end addresses. It is
neither marked fully utilized nor exclusive: configured capacity is not a
record of leases, and the connector does not fetch household clients.

Network definitions have exact source ownership independently of prefixes
derived from managed-interface addresses. Existing Prefix adoption preserves
its UUID. A different CIDR or Namespace identifies a different Prefix; the
previous record is retained. DHCP endpoint or label changes within one Prefix
update the same range UUID. Missing or invalid subnet/pool facts preserve
last-known records. Source omission does not delete inventory or migrate it
between namespaces. A subnet import is held with `identity-conflict` if native
Prefix creation would move an existing DHCP range from another Prefix. The
existing range and its parent remain intact; review that inventory relationship
before retrying. Explicit complete membership removal can clear only its
owned interface fields.

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
| `invalid-network` | Verify the IPv4 subnet and that its gateway and bounds resolve to one Namespace |
| `invalid-dhcp-range` | Verify explicit IPv4 start/end addresses are ordered and contained within the configured subnet |
| `unresolved-endpoint`, `unsupported-endpoint` | Verify explicit source Device and port identities; only Ethernet neighbors are supported |
| `incomplete-neighbor-evidence` | Verify source timing and applicable channel completeness; do not treat missing fields as an empty report |

A required collection request failure prevents inventory writes for that source
run. Destination failures fail the Job but retain already committed independent
changes. Each reporting Interface's association and observation writes are
atomic together; whole-run rollback is not provided. Review the native results,
correct the cause, and preview before another apply.

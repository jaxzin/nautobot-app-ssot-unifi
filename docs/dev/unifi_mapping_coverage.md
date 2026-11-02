# UniFi Mapping Coverage

This page audits UniFi inventory and configuration mappings against the connector, native Nautobot models and available API evidence. Use it to select a mapping scope, distinguish implemented behavior from proposals, and identify the evidence still needed.

Recommended scopes on this page are **proposals**, not authorization to implement or deploy. The [strategy decisions](unifi_mapping_open_items.md#strategy-decisions) constrain those proposals. Source/API questions remain [research items](unifi_mapping_open_items.md#research).

## Versions assessed

| Component | Version |
|---|---|
| Nautobot | 3.2.4 support/CI baseline; connector lockfile: 3.2.7 |
| nautobot-ssot | 4.6.1 support baseline; connector lockfile: 4.7.0 |
| This app | `495184c268cbd987a4b31e01bf4e3ff8e2266691` (1.1.0a1), branch `codex/namespace-aware-unifi` |
| aiounifi | 83 in the connector lockfile; the API assessment also examined 85 |
| UniFi Network Integration API spec | 10.1.39–11.0.89, read from the third-party mirror [Art-of-WiFi/unifi-network-application-api-client@242d451](https://github.com/Art-of-WiFi/unifi-network-application-api-client/tree/242d4515b13419294fb13c0645df440e3635d606/open-api-docs) |
| UniFi Network application observed | 10.6.109 (read-only GET requests against one controller) |

Code reviewed 2026-11-02 against the pinned commit and Nautobot 3.2.4 model constraints. Later dependency versions are not assumed identical. UniFi API evidence is dated 2026-10-25 and retains its stated version; no new controller probe is part of this audit. Strategy decisions are dated 2026-11-01.

## Source modes

| Mode | Endpoints | Authentication | Used by this app |
|---|---|---|---|
| Controller API (`L`) | `/api/s/<site>/stat/...`, `/rest/...`, `/v2/api/site/<site>/...` | Local username and password | Yes, through aiounifi. GET only, with explicit field projection. |
| Integration API (`I`) | `/proxy/network/integration/v1/...` | `X-API-KEY` header | No. Authentication is username/password only. |

The modes do not share identifiers. Integration IDs are UUIDs; controller `_id` values are 24-hex ObjectIds. The only documented link is the site's `internalReference`, described as the "internal unique name of the site used in older APIs".

## Legend

| Gap type | Meaning |
|---|---|
| `U-SRC` | UniFi does not expose the fact. |
| `U-MODE` | Exposed only by the other API mode. |
| `N-NAT` | Nautobot 3.2.4 has no native model or field for it. |
| `NOT-IMPL` | Source and model both exist; this app does not map it. |

**Implemented** refers to code at the pinned commit, not deployment activation. **Job option** names the feature input; each defaults to `False`. `sync_clients` and `sync_wireless` require `sync_vlans`. See [Using the App](../user/app_use_cases.md) for prerequisites and supported behavior.

Priorities run from P0 (highest) to P3. Effort is S, M or L.

## Coverage matrix

### Sites, controller and devices

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 1 | Site | `L` `/self/sites`; `I` `/v1/sites` | `Location` of the configured type | Implemented | None. | – |
| 2 | Controller and device group | Job input; managed device type | `Controller`, `ControllerManagedDeviceGroup` | Implemented. Base devices use `default`; wireless sync places APs in its wireless-capable `access-points` child. | Arbitrary source AP groups are not mapped (row 15). Each Device has one group. | P1 / M for broader scope |
| 3 | Device model | `L` `model` and hardware library | `DeviceType`, `Manufacturer` | Implemented, part number only | Component templates remain `NOT-IMPL` (row 31). Shared catalog declarations describe capability, not installed components or cables. | P3 / M |
| 4 | Device identity | `L` `stat/device` (`serial`, `name`, `mac`, `type`); `I` `/devices` | `Device` (`serial`, `platform`, `role`) | Implemented. Bound by the `UniFi inventory ownership` metadata document keyed on source `_id`. | None. | – |
| 5 | Management IP | `L` `config_network` (static/dhcp) | `IPAddress` plus a logical `mgmt` `Interface`; `primary_ip4`/`primary_ip6` | Implemented. Namespace comes from the `address_scopes` job input. | Conflicting interface names are held, not guessed. | – |
| 6 | Device state and uptime | `L` `state`, `uptime`; `I` `state` | `Device.status` is a lifecycle field, not link state | Set on create only | **Boundary.** This is telemetry; keep it in monitoring. | – |
| 7 | Firmware | `L` `version`, `upgrade_to_firmware`; `I` `firmwareVersion`, `firmwareUpdatable` | `SoftwareVersion` (platform plus version) and `Device.software_version` | Implemented; job option `sync_firmware` | "Update available" has no native field (`N-NAT`). | P2 / S |

### Interfaces and switching

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 8 | Wired ports | `L` `port_table`, `ethernet_table` | `Interface`: name, label, type from `media`, source port identifier | Implemented. Identity is device plus `port_idx` or `ifname`. Alias conflicts are held. | MAC population is not implemented (row 32); using MACs for client matching is a separate read path. | – |
| 9 | Port speed and enabled state | `L` `port_table.speed`, `port_overrides`, `portconf`; `I` `speedMbps`, `maxSpeedMbps`, `state` | `Interface.speed` (Kbps), `duplex`, `enabled` | `NOT-IMPL` | `I` `speedMbps` is the negotiated speed and can be present on `DOWN` ports. `maxSpeedMbps` is a capability. Neither is configured speed. Map only configured speed and enabled state, and only on physical interfaces; Nautobot rejects `speed` on LAG, virtual and wireless interfaces; duplex has additional copper-interface restrictions. | P2 / S |
| 10 | PoE | `L` `poe_caps` (bitmask), `poe_mode` (`auto`, `pasv24`, `passthrough`, `off`); `I` `poe.standard` (`802.3af/at/bt`), `poe.type` (1–4), `poe.enabled`, `poe.state` | Proposed optional `Interface` custom fields: `poe_capable`, `poe_mode`, `poe_standard`, `poe_type`, `poe_enabled`; no native PoE fields in 3.2.4 (`N-NAT`) | `NOT-IMPL` | Source-neutral keys do not grant cross-source write authority. Missing values remain unset. Evaluate the API needed for each fact. `I` ports without PoE omit `poe` entirely; establish capability semantics before interpreting absence. Live state and power draw stay in monitoring. Native proposal: [nautobot discussion #9466](https://github.com/orgs/nautobot/discussions/9466). | P2 / S |
| 11 | AP radios | `L` `radio_table` (`name`, `radio`, `channel`, `ht`, `tx_power`); `I` `interfaces.radios` (`wlanStandard`, `frequencyGHz`, `channelWidthMHz`, `channel`) | `Interface` of an `ieee802.11*` type or `other-wireless` | Implemented. Type is always `other-wireless`. | The `I` field `wlanStandard` has no documented meaning: capability or configured standard. For a reliably established standard that lacks a native choice, use `other-wireless` and retain the precise source value in app-defined Object Metadata. Do not infer a standard from band or ambiguous fields. Nautobot 3.2.4 has no `802.11be` choice (`N-NAT`). `I` 10.6.109 has no transmit-power field. | P2 / M |
| 12 | LAG | `L` `port_table.aggregated_by`; `I` `features.switching.lags[].portIdxs`, `/switching/lags` | `Interface` type `lag`, plus `member.lag` | `NOT-IMPL` | Global subtypes include `LOCAL`, `SWITCH_STACK`, `MULTI_CHASSIS`. Nautobot permits members on the same device or within the same virtual chassis, subject to [native validation](https://github.com/nautobot/nautobot/blob/v3.2.4/nautobot/dcim/models/device_components.py#L1306). An arbitrary multi-chassis system is not automatically a native virtual chassis. | P3 / M |
| 13 | Switch stacks | `I` `/switching/switch-stacks` | `VirtualChassis` and `vc_position` | `NOT-IMPL` | `U-MODE` for `L`. | P3 / M |
| 14 | Uplink and topology | `L` `uplink`, `last_uplink`, `lldp_table`; `I` `uplink.deviceId` | Last-known neighbor observation in `UniFi neighbor observations` metadata plus the `unifi_observed_neighbor` Relationship | Implemented; job option `sync_neighbors` | UniFi evidence is dynamic, so it is not mapped to `Cable`. **Boundary:** passive patch-panel and wall-jack paths are outside UniFi's view. | P2 / S |

### Wireless

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 15 | AP groups | Connector grouping of APs; source groups via `L` `/v2/api/site/<site>/apgroups` ([endpoint reference](https://github.com/Art-of-WiFi/UniFi-API-client/tree/e1f7f63b541bbbb28fe51342d2349092d30fb82a)) | `ControllerManagedDeviceGroup` tree | Connector-owned `access-points` child implemented. Arbitrary source AP-group membership is `NOT-IMPL`. | No `v2/apgroups` reader exists here. Overlapping source groups cannot directly map to the Device's single group. R5 blocks broader filters, not the all-AP case. | P1 / M |
| 16 | Wireless networks (SSIDs) | `L` GET `rest/wlanconf`: `_id`, `name`, `enabled`, `hide_ssid`, security flags, `networkconf_id`, `ap_group_mode` | `WirelessNetwork`: UUID, generated `name`, `ssid`, `Central` mode, `enabled`, `authentication`, `hidden` | Implemented; `sync_wireless`, requires `sync_vlans` and wireless MetadataTypes | Integration/site/SSID `_id` establishes identity. Invalid fields, duplicate SSIDs and unsupported security are held. The reader does not preserve `ap_group_ids`. | Implemented; extensions P1 / M |
| 17 | SSID to VLAN | `L` `networkconf_id`, resolved through site network bindings | `ControllerManagedDeviceGroupWirelessNetworkAssignment.vlan` | Implemented with `sync_wireless` in the supported scope | Unresolved network identity holds the assignment. No VLAN is guessed from an SSID name. `I` `NATIVE` semantics remain R2 for a future Integration API mapping. | Implemented; API extension pending R2 |
| 18 | SSID to broadcasting APs | `L` `ap_group_mode=all`; exactly one site returned by discovery | Assignment to connector-owned `access-points` group | Implemented for single-site, all-AP scope | The SSID record can import while an unsupported assignment is held. Filtered groups, device/tag filters and multi-site assignments remain `NOT-IMPL`. Native SSIDs attach to groups, not radio Interfaces. Runtime `vap_table` is not configuration proof. | Existing case implemented; filters P1 / M |
| 19 | SSID security | `L` `security`, `wpa_mode`, `wpa3_support`, `wpa3_transition`, `wpa3_enhanced_192` | `WirelessNetwork.authentication`; field-scoped `UniFi source authentication` metadata | Implemented for five exact combinations: Open, WPA2 Personal, WPA2 Enterprise, and WPA2/WPA3 transition variants mapped to WPA3 choices | Mapped transitions retain raw security facts. Other combinations are held as `unsupported-security`. A native choice alone does not establish source mapping support. Native transition choices: [nautobot#9708](https://github.com/nautobot/nautobot/issues/9708). | Implemented subset; extensions need evidence |
| 20 | SSID secrets | Credential-bearing fields in WLAN responses | No credential ingestion | Allowlist projection and transport DEBUG suppression implemented | `_get_wireless_configuration()` returns only declared nonsecret fields. `_drop_transport_debug` filters aiounifi Connectivity DEBUG records before handlers receive payloads. Synthetic transport tests cover credential/WLAN-secret exposure. Preserve this boundary for new API channels. | Boundary, not a missing mapping |
| 21 | Radio profiles | No named reusable profile object in either mode; settings are per device and per WLAN | `RadioProfile` and its group assignment | `NOT-IMPL` | Treated as `U-SRC`. A synthetic profile per group would be invented data. | P3 |
| 22 | Supported data rates | `L` `minrate_*` on WLANs (unverified on 10.6.109); `I` none on 10.6.109 | `SupportedDataRate` (standard plus rate) | `NOT-IMPL` | A minimum or basic rate is not a supported-rate list. Different semantics. | P3 |

### Networks and addressing

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 23 | Networks and VLANs | `L` `rest/networkconf` (`vlan`, `vlan_enabled`, `purpose`); `I` `/networks` (`vlanId`, `management`, `default`) | `VLAN` in a `VLANGroup`, bound by source `_id` | Implemented; job option `sync_vlans` | None. | – |
| 24 | Configured subnet and DHCP range | `L` `ip_subnet`, `dhcpd_start`, `dhcpd_stop`; `I` gateway-managed `ipv4Configuration` and explicit DHCP range | `Prefix` type network with VLAN; `IPAddressRange` | Implemented from `L`; `sync_vlans`, IPv4 only | Host bits select the namespace, not a separate gateway IPAddress (row 30). Configured IPv6 prefixes/pools and additional subnets are not mapped. Missing DHCP bounds are never derived. | IPv4 graph implemented; extensions P2 / M |
| 25 | Port VLAN membership | `L` `rest/portconf` (`native_networkconf_id`, `tagged_vlan_mgmt`, `excluded_networkconf_ids`, `forward`, `tagged_networkconf_ids`), `port_overrides` | `Interface.mode`, `untagged_vlan`, `tagged_vlans` | Implemented; job option `sync_vlans`. Held when the mode is unknown or evidence is incomplete. | `I` has no port profiles or per-port VLANs (`U-MODE`). | – |
| 26 | Wired and Wi-Fi clients | `L` `stat/sta` (`mac`, `sw_mac`, `sw_port`, `ap_mac`, `essid`, `radio_name`, `network_id`, `vlan`); `I` `/clients` (`macAddress`, `uplinkDeviceId`, `ipAddress`, `type`) | `UniFi client observations` metadata on the matched `Interface` or `VMInterface`; `unifi_observed_client_attachment` Relationship; unmatched clients in `UniFi unresolved client observations` on the `ExternalIntegration` | Implemented; job option `sync_clients` (requires `sync_vlans`) | `I` client records carry no port, SSID or network (`U-MODE`). | – |
| 27 | Client IP | `L` `stat/sta.ip`; `I` `ipAddress` | Proposed `IPAddress` plus `IPAddressToInterface` on an already-matched client | `NOT-IMPL`; the current client reader discards address fields | Include dynamic addresses without using IPs to establish identity. Reuse network namespaces and respect ownership. The management-IP assignment model requires both endpoints to be UniFi-owned; a client mapping needs a guarded assignment to the matched physical/VM interface without taking ownership of that interface. Positive address evidence and retirement evidence are separate prerequisites (R11). | Proposed; scope positive assignment before retirement |
| 28 | Fixed DHCP reservations | `L` `rest/user` (`use_fixedip`, `fixed_ip`) | `IPAddress` (type DHCP) | `NOT-IMPL` | Use the existing namespace for the network and the same scoping rules as other imports. Respect existing record ownership; do not create a separate reservation namespace. | P2 / M |
| 29 | Device tags | `L` `rest/tag`; `I` `/device-tags` | `Tag` | `NOT-IMPL` | Useful only as the target of `DEVICE_TAGS` broadcast filters. | P3 / S |
| 30 | Configured gateway address | `L` host portion of `ip_subnet`; `I` `ipv4Configuration.hostIpAddress` | `IPAddress`; assignment only with a proven Interface | `NOT-IMPL` as a network-configuration mapping | Management IPs may exist independently. Prefix import does not preserve a gateway relationship. Establish endpoint identity and ownership; do not guess a VLAN subinterface. See R12. | P2 / M (proposal) |
| 31 | Hardware component catalog | Device-model declarations and community device specifications | `InterfaceTemplate`, `PowerPortTemplate`, other applicable component templates | `NOT-IMPL`; current mapping records model/part number | The NetBox v0.3.27 release refreshes Device Type Library data and power-port handling. Verify exact hardware matches and preserve curated shared templates. Catalog declarations do not prove installed components or cabling. [Native templates](https://github.com/nautobot/nautobot/blob/v3.2.4/nautobot/dcim/models/device_component_templates.py). | P3 / M (proposal) |
| 32 | Managed-interface MAC address | Per-interface controller or Integration API evidence, where explicitly reported | `Interface.mac_address` | `NOT-IMPL`; `InterfaceModel` imports label, parent, type and source port ID, not MAC | Source device/client MACs already participate in identity and observation reads, but are not proof of every port's address. Establish per-port/radio evidence and preserve foreign-owned values. Never copy the device base MAC to every interface. See R13. | P2 / M (proposal) |

### Out of scope

| Entity | Reason |
|---|---|
| Firewall policies, ACL rules and DNS policies | No corresponding native policy model is established here. Do not force rules into inventory fields. |
| WAN/VPN configuration | No protocol/policy mapping is implemented. Explicit interfaces, addresses and prefixes may be assessed as inventory facts; rules and credentials remain outside scope. |
| CPU, memory, traffic counters, DPI, PoE power draw | Telemetry. Collect with a metrics pipeline. |
| Passphrases, RADIUS secrets, VPN keys | Secrets. Never ingest. |
| Passive cabling (patch panels, wall jacks) | Not visible to UniFi. Model it as intent in Nautobot directly. |

## Wireless constraints

These Nautobot 3.2.4 rules shape any wireless mapping:

- `WirelessNetwork.name` is `unique=True` across the whole database and the model has no location field. Identity must be the source ID held in the ownership document; the name is a generated label.
- `WirelessNetwork.mode` is required. Its choices are `Central`, `Fabric`, `Standalone (Autonomous)`, `Local (Flex)`, `Mesh` and `Bridge`. UniFi exposes no equivalent field.
- `WirelessNetwork.authentication` choices are `Open`, `WPA2 Personal`, `WPA2 Enterprise`, `Enhanced Open`, `WPA3 Personal`, `WPA3 SAE`, `WPA3 Enterprise` and `WPA3 Enterprise 192Bit`.
- SSIDs and radio profiles attach to a `ControllerManagedDeviceGroup` through assignment models, which are unique per group and network. There is no SSID-to-interface link.
- Both assignment models have `is_metadata_associable_model = False`, so ownership metadata cannot attach to an assignment row. Ownership must be derived from the owned `WirelessNetwork` and group.
- A device has exactly one group, so overlapping UniFi AP groups or tag-based broadcast filters cannot be represented without loss.

## Identity, ownership and failure retention

The app's current contracts apply unchanged to any new scope:

- **Identity.** A record is bound by integration, site and source `_id` in the `UniFi inventory ownership` metadata document. Clients match by a prior binding or an unambiguous exact MAC across `Interface` and `VMInterface`. Hostname and IP never adopt identity.
- **Creation.** Client observations never create `Device`, `VirtualMachine` or `Interface` records.
- **Ownership.** A record owned by another source or another integration is held with reason `foreign-owner`, not merged.
- **Evidence.** Collection reads only listed fields. A value that is absent or ambiguous is held, never defaulted.
- **Failure retention.** A failed or incomplete source channel preserves the prior facts. Only a fully parsed, successful report can record absence.
- **Client VLAN.** A client-reported VLAN is observation metadata. It never overwrites configured interface mode or VLAN membership.
- **Held artifacts.** Held records are published with a reason so an operator can resolve them.

Framework features reused rather than rebuilt: `NautobotModel` and `NautobotAdapter`, `ObjectMetadataAnnotation`, DiffSync flags, and native `Sync` and `SyncLogEntry` history. The metadata types, Relationship keys, held artifacts and observation schemas are contracts defined by this app, not built into nautobot-ssot.

When other sources (for example, a Prometheus or Tailscale integration) write the same models, each owns its own records. Host NIC facts belong to the host source; UniFi owns infrastructure devices, their interfaces, VLANs, prefixes, ranges and wireless configuration.

## Comparable projects

| Project | License | Evidence (2026-10-25) | Use |
|---|---|---|---|
| [abates/nautobot-app-ssot-unifi](https://github.com/abates/nautobot-app-ssot-unifi/tree/4a15b62a147214efcde2377068c07736bc6cb8e7) v1.0.4 | Apache-2.0 | Last commit checked 2026-11-02: 2025-03-14. Sites, devices, ports and management IPs. | Upstream lineage; this fork adds explicit ownership and gated mappings. |
| [jdrew82/nautobot-app-ssot-unifi](https://github.com/jdrew82/nautobot-app-ssot-unifi) | No license detected | Last push 2023-12. | None. |
| [unifi2netbox/netbox-unifi-sync](https://github.com/unifi2netbox/netbox-unifi-sync) | MIT | Latest release checked 2026-11-02: v0.3.27 (2026-10-31). Examples below pin v0.3.25 commit `71234c0`; its tests use fake endpoints. The README warns against production use without validation. | Reuse mapping ideas/tests; breadth is not runtime proof or Nautobot compatibility. |
| [unpoller/unifi](https://github.com/unpoller/unifi/tree/7da59ee9bdb60182db0ab51afaff24f217cd5585) and unpoller | MIT | Active. Go. | Telemetry only. aiounifi already covers the needed controller endpoints. |

No bundled nautobot-ssot 4.6.1 integration writes `WirelessNetwork`. GitHub search found no other Nautobot UniFi connector; that does not rule out unpublished work.

### Patterns worth adapting from netbox-unifi-sync

Links point at `netbox_unifi_sync/services/sync_engine.py` at commit `71234c0`. The [v0.3.26–0.3.27 changelog](https://github.com/unifi2netbox/netbox-unifi-sync/blob/v0.3.27/CHANGELOG.md) also documents controller-scoped mapping separation, concurrency isolation, honest dry-run failure when models/database are unavailable, and refreshed device specifications. Preserve controller/site identity and per-run isolation; do not copy shared mutable runtime settings.

| Pattern | Adaptation for this app |
|---|---|
| [`_wlan_vlan_id`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1093) normalizes SSID network references across both modes | Resolve through this app's network bindings by source ID, not by VID. |
| [`_wlan_broadcast_device_ids`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1114) normalizes broadcast scope shapes | Map scope to a group assignment, never to interfaces. |
| [`tests/test_wlan_relationships.py`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/tests/test_wlan_relationships.py) keeps existing links when broadcast or device evidence is unavailable | Port the cases as new tests against this app's retention contract. |
| [`normalize_port_data`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1796) and [`normalize_radio_data`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1872) | Reference for shape differences between modes only. |

Do not adopt these behaviors; each contradicts this app's evidence or ownership rules:

- Unknown security falls back to `wpa-personal` ([L1238–L1249](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1238-L1249)).
- Band guesses the Wi-Fi generation ([L1643–L1649](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1643-L1649)).
- The last physical port is assumed to be the uplink ([L774](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L774)), and uplink cables are created and deleted.
- Client IPs are deleted after a period offline ([`sync_client_ips`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1436)). Native client-IP records are in scope, but offline duration alone does not prove an address unused. Retain retired records for reactivation.
- DHCP ranges are derived when start/stop is absent; `maxSpeed` is written to `speed`; DHCP reservations are written back to UniFi.

NetBox-to-Nautobot model differences to account for:

| NetBox | Nautobot 3.2.4 |
|---|---|
| `Site` | `Location` of a configured type |
| `WirelessLAN` with group, VLAN and interfaces | `WirelessNetwork` plus a group assignment carrying the VLAN |
| `IPRange` | `IPAddressRange`, which requires a parent `Prefix` |
| `MACAddress` object | `Interface.mac_address` text field |
| Interface PoE fields | None |
| REST writes through pynetbox | ORM writes through SSoT `NautobotModel` |

## Proposed scopes

Ordered by prerequisites, then value. Each keeps UniFi access read-only and the existing ownership boundaries.

### Phase 0: source evidence (no writes)

- Select a mapping and its unresolved evidence first. Choose the API from the facts needed, rather than collecting every endpoint in either mode.
- For a controller-API mapping, collect only the necessary fields from its configuration/device/client channel. Use `v2/apgroups` evidence only for a selected filtered-AP scope.
- For an Integration API mapping, first establish read-only access and any required identity crosswalk. Limit collection to that mapping; adding this channel is not a blanket migration.
- **Acceptance:** synthetic fixtures covering the selected questions in [UniFi Mapping Open Items](unifi_mapping_open_items.md). Strip credentials and replace real MACs, SSIDs and addresses before saving. No live probe is authorized by this strategy document.

### Wireless scope: implemented behavior and remaining work

The pinned connector imports single-site, all-AP wireless configuration through `sync_wireless` and `sync_vlans`. It resolves VLANs by source network ID, uses a wireless-capable AP child group, and preserves raw transition-security facts in field-scoped metadata. Unsupported assignments are held without inventing broadcast scope.

Future scopes are filtered AP groups, multi-site assignments and additional security combinations. R5 blocks source-defined AP-group support; R2 and R10 apply only to a selected Integration API wireless mapping. Preserve name uniqueness and ownership. The existing wireless path is not a new implementation deliverable.

**Acceptance for an extension:** preview the selected records and held cases; preserve UUIDs and group membership outside scope; prove failure retention and secret exclusion; obtain separate preview approval before apply; verify native results and a follow-up diff.

### Phase 2: port and radio facts (P1–P2)

- Optional, source-neutral PoE custom fields on `Interface`: `poe_capable`, `poe_mode`, `poe_standard`, `poe_type`, `poe_enabled`. Keep absent values unset and live power draw in monitoring. Choose an API per mapping after checking field semantics, identity matching and read-only access; do not commit all mappings to one API mode.
- Configured `enabled` and `speed` on physical ports only.
- Radio interface type only once the source semantics are confirmed. When the exact standard lacks a native choice, use `other-wireless` and retain the precise standard in app-defined Object Metadata. Design its schema before implementation.
- **Acceptance:** preview and apply diffs on a controlled device set. Nautobot `clean()` errors must become actionable holds: speed on a LAG/virtual/wireless interface, invalid duplex, or a LAG relationship outside allowed device/virtual-chassis scope. Include valid virtual-chassis and invalid unrelated-device cases.

### Commission implemented mappings

- Include both firmware-version inventory and neighbor observations in the activation strategy. Review preview evidence before commissioning either apply path.
- Neighbor observations must not invent physical cable paths through passive wall jacks or patch panels.
- A planned scope does not authorize its implementation, activation or deployment.

### Phase 3: client addressing and additional mappings (P2–P3)

- Include client addresses, including DHCP addresses, as native `IPAddress` records with interface assignments on already-matched clients. IP addresses never establish identity or admit clients.
- Use the existing namespace for each network for client addresses and fixed DHCP reservations. Reuse appropriate existing records, respect ownership and hold conflicts instead of creating duplicates.
- Resolve an address's network and any required prefix/mask information from authoritative source facts, not a fabricated host mask. Define a client-assignment model using native SSoT writes and the established client match; do not relax the existing management-IP model's ownership guard or claim the client's NIC configuration.
- Implement positive address observations and assignments once their source evidence is established. Remove obsolete assignments using native SSoT history. Enable retirement only when complete authoritative evidence proves an importer-owned address unused and it has no remaining assignments. Retain its record with a retired inventory status and reactivate the same record when observed again. Collection failure, incomplete reports and active-client omission are insufficient evidence. R11 separates these prerequisites; an unresolved retirement signal is not a blanket reason to discard positive address facts.
- Evaluate APIs per additional mapping, including LAGs, stacks, network/radio facts, gateway addresses, IPv6 networks, managed-interface MACs and device tags. Model-library templates are a separate catalog scope, not live discovery. Use an Integration API channel only where its facts, read-only access and identity requirements are established.

# UniFi Mapping Coverage

This page maps every UniFi inventory and configuration entity that can plausibly land in Nautobot against what this app implements, what Nautobot models natively, and what the UniFi APIs actually expose. Use it to choose the next mapping scope and to avoid re-deriving source and model constraints.

Recommended scopes on this page are **proposals**. None is approved work. Unresolved questions live in [UniFi Mapping Open Items](unifi_mapping_open_items.md).

## Versions assessed

| Component | Version |
|---|---|
| Nautobot | 3.2.4 |
| nautobot-ssot | 4.6.1 |
| This app | `1582ff0e716136253ad295920237272c7e5ce3a9` (1.1.0a1) |
| aiounifi | 85 |
| UniFi Network Integration API spec | 10.1.39–11.0.89, read from the third-party mirror [Art-of-WiFi/unifi-network-application-api-client@242d451](https://github.com/Art-of-WiFi/unifi-network-application-api-client/tree/242d4515b13419294fb13c0645df440e3635d606/open-api-docs) |
| UniFi Network application observed | 10.6.109 (read-only GET requests against one controller) |

Assessed 2026-10-25. Field names and line references drift between releases.

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

**Job option** names the `UnifiDataSource` input that enables an implemented mapping. Each option defaults to `False`; which options a deployment enables is deployment-specific.

Priorities run from P0 (highest) to P3. Effort is S, M or L.

## Coverage matrix

### Sites, controller and devices

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 1 | Site | `L` `/self/sites`; `I` `/v1/sites` | `Location` of the configured type | Implemented | None. | – |
| 2 | Controller and device group | Job input | `Controller`, `ControllerManagedDeviceGroup` | Implemented. Every device goes to one group named `default`. | UniFi AP groups are not mapped (row 15). `Device.controller_managed_device_group` is a single foreign key, so a device belongs to exactly one group. | P1 / M |
| 3 | Device model | `L` `model` plus the hardware library | `DeviceType`, `Manufacturer` | Implemented, part number only | `InterfaceTemplate` is native but `NOT-IMPL`. Device types are a shared catalog, so writing templates is risky. | P3 / M |
| 4 | Device identity | `L` `stat/device` (`serial`, `name`, `mac`, `type`); `I` `/devices` | `Device` (`serial`, `platform`, `role`) | Implemented. Bound by the `UniFi inventory ownership` metadata document keyed on source `_id`. | None. | – |
| 5 | Management IP | `L` `config_network` (static/dhcp) | `IPAddress` plus a logical `mgmt` `Interface`; `primary_ip4`/`primary_ip6` | Implemented. Namespace comes from the `address_scopes` job input. | Conflicting interface names are held, not guessed. | – |
| 6 | Device state and uptime | `L` `state`, `uptime`; `I` `state` | `Device.status` is a lifecycle field, not link state | Set on create only | **Boundary.** This is telemetry; keep it in monitoring. | – |
| 7 | Firmware | `L` `version`, `upgrade_to_firmware`; `I` `firmwareVersion`, `firmwareUpdatable` | `SoftwareVersion` (platform plus version) and `Device.software_version` | Implemented; job option `sync_firmware` | "Update available" has no native field (`N-NAT`). | P2 / S |

### Interfaces and switching

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 8 | Wired ports | `L` `port_table`, `ethernet_table` | `Interface`: name, label, type from `media`, MAC | Implemented. Identity is device plus `port_idx` or `ifname`. Alias conflicts are held. | None. | – |
| 9 | Port speed and enabled state | `L` `port_table.speed`, `port_overrides`, `portconf`; `I` `speedMbps`, `maxSpeedMbps`, `state` | `Interface.speed` (Kbps), `duplex`, `enabled` | `NOT-IMPL` | `I` `speedMbps` is the negotiated speed and can be present on `DOWN` ports. `maxSpeedMbps` is a capability. Neither is configured speed. Map only configured speed and enabled state, and only on physical interfaces; Nautobot rejects `speed` on LAG and virtual interfaces. | P2 / S |
| 10 | PoE | `L` `poe_caps` (bitmask), `poe_mode` (`auto`, `pasv24`, `passthrough`, `off`); `I` `poe.standard` (`802.3af/at/bt`), `poe.type` (1–4), `poe.enabled`, `poe.state` | No native PoE fields on `Interface` in 3.2.4 (`N-NAT`) | `NOT-IMPL` | Custom fields fit. `I` ports without PoE omit `poe` entirely. `poe.state` is operational, so leave it to monitoring. A native proposal is [nautobot discussion #9466](https://github.com/orgs/nautobot/discussions/9466). | P2 / S |
| 11 | AP radios | `L` `radio_table` (`name`, `radio`, `channel`, `ht`, `tx_power`); `I` `interfaces.radios` (`wlanStandard`, `frequencyGHz`, `channelWidthMHz`, `channel`) | `Interface` of an `ieee802.11*` type or `other-wireless` | Implemented. Type is always `other-wireless`. | The `I` field `wlanStandard` has no documented meaning: capability or configured standard. Nautobot 3.2.4 has no `802.11be` interface type (`N-NAT`). `I` 10.6.109 has no transmit-power field. | P2 / M |
| 12 | LAG | `L` `port_table.aggregated_by`; `I` `features.switching.lags[].portIdxs`, `/switching/lags` | `Interface` of type `lag` plus `member.lag` | `NOT-IMPL` | `I` is explicit. The global LAG schema is split into `LOCAL`, `SWITCH_STACK` and `MULTI_CHASSIS` subtypes. Nautobot requires LAG members on the same device. | P3 / M |
| 13 | Switch stacks | `I` `/switching/switch-stacks` | `VirtualChassis` and `vc_position` | `NOT-IMPL` | `U-MODE` for `L`. | P3 / M |
| 14 | Uplink and topology | `L` `uplink`, `last_uplink`, `lldp_table`; `I` `uplink.deviceId` | Last-known neighbor observation in `UniFi neighbor observations` metadata plus the `unifi_observed_neighbor` Relationship | Implemented; job option `sync_neighbors` | UniFi evidence is dynamic, so it is not mapped to `Cable`. **Boundary:** passive patch-panel and wall-jack paths are outside UniFi's view. | P2 / S |

### Wireless

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 15 | AP groups | `L` `/v2/api/site/<site>/apgroups` (undocumented; see [Art-of-WiFi/UniFi-API-client@e1f7f63](https://github.com/Art-of-WiFi/UniFi-API-client/tree/e1f7f63b541bbbb28fe51342d2349092d30fb82a)) | `ControllerManagedDeviceGroup` (tree) | `NOT-IMPL` | aiounifi 85 has no AP-group support. `I` has no AP-group endpoint, only `DEVICE_TAGS` broadcast filters (`U-MODE`). Overlapping groups cannot be represented, because each device has one group. | P1 / M |
| 16 | Wireless networks (SSIDs) | `L` `rest/wlanconf` (`_id`, `name`, `enabled`, `security`, `wpa_mode`, `hide_ssid`, `networkconf_id`, `ap_group_ids`); `I` `/wifi/broadcasts` list (`name`, `enabled`, `network`, `securityConfiguration.type`, `broadcastingDeviceFilter`, `broadcastingFrequenciesGHz`) and detail (adds `hideName`) | `WirelessNetwork` (`name` globally unique, `ssid`, `mode` required, `enabled`, `authentication`, `hidden`, optional `secrets_group`) | `NOT-IMPL` | aiounifi 85 reads `rest/wlanconf`. Its WLAN helpers also include PUT requests, so use only the list request. Both `rest/wlanconf` and the `I` broadcast detail response carry passphrases, so both fall under the boundary in row 20. `L` is preferred because `networkconf_id` matches the existing network bindings. See [Wireless constraints](#wireless-constraints). | **P0 / L** |
| 17 | SSID to VLAN | `L` `networkconf_id`, `vlan`; `I` `network.type` `SPECIFIC` with `networkId`, or `NATIVE` | `ControllerManagedDeviceGroupWirelessNetworkAssignment.vlan` | `NOT-IMPL` | Resolve through the existing network bindings by source ID. `NATIVE` carries no network ID and its meaning is undocumented; hold it. | P0 (with 16) |
| 18 | SSID to broadcasting APs | `L` `ap_group_ids`, device `vap_table`; `I` `broadcastingDeviceFilter` (absent, `DEVICES` or `DEVICE_TAGS`) | Assignment to a `ControllerManagedDeviceGroup` | `NOT-IMPL` | Nautobot links SSIDs to groups, never to interfaces (`N-NAT`). `vap_table` is runtime state, not configuration. | P0 (with 16) |
| 19 | SSID security | `L` `security`, `wpa_mode`; `I` `securityConfiguration.type` (`OPEN`, `WPA2_PERSONAL`, `WPA3_PERSONAL`, `WPA2_WPA3_PERSONAL`, `WPA2_ENTERPRISE`, `WPA3_ENTERPRISE`, `WPA2_WPA3_ENTERPRISE`) | `WirelessNetwork.authentication` | `NOT-IMPL` | Nautobot has no WPA2/WPA3 transition choice (`N-NAT`); requested in [nautobot#9708](https://github.com/nautobot/nautobot/issues/9708). | P0 (with 16) |
| 20 | SSID secrets | `L` `rest/wlanconf` `x_passphrase`, `x_iapp_key`; `I` broadcast detail `securityConfiguration.passphrase` | `WirelessNetwork.secrets_group` | – | **Boundary: never ingest.** Every WLAN response that carries a needed field also carries secrets. Collection must project each row to an allowlist of fields immediately on receipt, before any caching, logging, diff, metadata or fixture, as `_get_vlan_configuration()` already does for `rest/networkconf` and `rest/portconf`. Projection alone is not enough: aiounifi 85's `Connectivity` logs login JSON, session cookies and complete raw response bodies at DEBUG before returning, so that logger's DEBUG output must be suppressed. Tests must drive the real transport with DEBUG enabled and assert that a synthetic secret appears in neither the projection nor any log record. | – |
| 21 | Radio profiles | No named reusable profile object in either mode; settings are per device and per WLAN | `RadioProfile` and its group assignment | `NOT-IMPL` | Treated as `U-SRC`. A synthetic profile per group would be invented data. | P3 |
| 22 | Supported data rates | `L` `minrate_*` on WLANs (unverified on 10.6.109); `I` none on 10.6.109 | `SupportedDataRate` (standard plus rate) | `NOT-IMPL` | A minimum or basic rate is not a supported-rate list. Different semantics. | P3 |

### Networks and addressing

| # | Entity | Source fields (mode) | Nautobot target | This app | Gap and evidence | Pri / effort |
|---|---|---|---|---|---|---|
| 23 | Networks and VLANs | `L` `rest/networkconf` (`vlan`, `vlan_enabled`, `purpose`); `I` `/networks` (`vlanId`, `management`, `default`) | `VLAN` in a `VLANGroup`, bound by source `_id` | Implemented; job option `sync_vlans` | None. | – |
| 24 | Subnet, gateway, DHCP range | `L` `ip_subnet`, `dhcpd_start`, `dhcpd_stop`; `I` gateway-managed network detail `ipv4Configuration` (`hostIpAddress`, `prefixLength`, `additionalHostIpSubnets`, `dhcpConfiguration.ipAddressRange.start/stop`) | `Prefix` (type network, `vlan`), `IPAddressRange` | Implemented from `L`; job option `sync_vlans` | `I` exposes the same facts on gateway-managed networks, with an explicit range. A missing range is never derived. | – |
| 25 | Port VLAN membership | `L` `rest/portconf` (`native_networkconf_id`, `tagged_vlan_mgmt`, `excluded_networkconf_ids`, `forward`, `tagged_networkconf_ids`), `port_overrides` | `Interface.mode`, `untagged_vlan`, `tagged_vlans` | Implemented; job option `sync_vlans`. Held when the mode is unknown or evidence is incomplete. | `I` has no port profiles or per-port VLANs (`U-MODE`). | – |
| 26 | Wired and Wi-Fi clients | `L` `stat/sta` (`mac`, `sw_mac`, `sw_port`, `ap_mac`, `essid`, `radio_name`, `network_id`, `vlan`); `I` `/clients` (`macAddress`, `uplinkDeviceId`, `ipAddress`, `type`) | `UniFi client observations` metadata on the matched `Interface` or `VMInterface`; `unifi_observed_client_attachment` Relationship; unmatched clients in `UniFi unresolved client observations` on the `ExternalIntegration` | Implemented; job option `sync_clients` (requires `sync_vlans`) | `I` client records carry no port, SSID or network (`U-MODE`). | – |
| 27 | Client IP | `L` `stat/sta.ip`; `I` `ipAddress` | `IPAddress` plus `IPAddressToInterface` | **Boundary.** Not mapped: identity is MAC only, and the observation document records the network. | Dynamic addresses do not belong in owned IPAM. | – |
| 28 | Fixed DHCP reservations | `L` `rest/user` (`use_fixedip`, `fixed_ip`) | `IPAddress` (type DHCP) | `NOT-IMPL` | Needs an ownership and namespace design. | P2 / M |
| 29 | Device tags | `L` `rest/tag`; `I` `/device-tags` | `Tag` | `NOT-IMPL` | Useful only as the target of `DEVICE_TAGS` broadcast filters. | P3 / S |

### Out of scope

| Entity | Reason |
|---|---|
| WAN, firewall policies and zones, ACL rules, VPN, DNS policies | Security and configuration state with no inventory model. Mapping them into inventory types would misstate them. |
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
| [abates/nautobot-app-ssot-unifi](https://github.com/abates/nautobot-app-ssot-unifi/tree/4a15b62a147214efcde2377068c07736bc6cb8e7) v1.0.4 | Apache-2.0 | Last commit 2025-03-14. Sites, devices, ports and management IPs. | The upstream of this app; this app is a strict superset. |
| [jdrew82/nautobot-app-ssot-unifi](https://github.com/jdrew82/nautobot-app-ssot-unifi) | No license detected | Last push 2023-12. | None. |
| [unifi2netbox/netbox-unifi-sync](https://github.com/unifi2netbox/netbox-unifi-sync/tree/71234c024d8f0f103ee25347bd74e735f355343a) v0.3.25 | MIT | NetBox 4.2+ plug-in. About 34 test files built on fake endpoints. CI runs lint, bandit and pytest without installing NetBox. The README warns against production use without validation. | Mapping ideas and test cases. |
| [unpoller/unifi](https://github.com/unpoller/unifi/tree/7da59ee9bdb60182db0ab51afaff24f217cd5585) and unpoller | MIT | Active. Go. | Telemetry only. aiounifi already covers the needed controller endpoints. |

No bundled nautobot-ssot 4.6.1 integration writes `WirelessNetwork`. GitHub search found no other Nautobot UniFi connector; that does not rule out unpublished work.

### Patterns worth adapting from netbox-unifi-sync

Links point at `netbox_unifi_sync/services/sync_engine.py` at commit `71234c0`.

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
- Client IPs are created and deleted after a period offline ([`sync_client_ips`](https://github.com/unifi2netbox/netbox-unifi-sync/blob/71234c024d8f0f103ee25347bd74e735f355343a/netbox_unifi_sync/services/sync_engine.py#L1436)).
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

- **0a, controller API.** Capture sanitized responses for `rest/wlanconf`, `rest/networkconf`, `stat/device` (`radio_table`, `vap_table`, `port_table`), `stat/sta` and, best effort, `v2/apgroups`. Strip secrets, MACs, SSIDs and addresses before saving; keep structure and synthetic values.
- **0b, Integration API.** Capture the same entities plus LAGs, stacks and device tags.
- **Acceptance:** committed fixtures with no secret, MAC, SSID or address values, covering the answers listed in [UniFi Mapping Open Items](unifi_mapping_open_items.md).

### Phase 1: wireless networks (P0)

- Read `rest/wlanconf` through the existing controller-API reader with an allowlist projection on receipt (row 20). The Integration API is not used in this phase.
- Create `WirelessNetwork` per source SSID `_id`, with the name `<controller>/<site>/<ssid>`, rewritten when the controller or site name changes. `ssid` holds the broadcast name; `hidden` comes from `hide_ssid`.
- Set `mode` to `Central` for every network. UniFi has no equivalent field; this is a fixed modeling choice for controller-managed APs.
- Map `WPA2_WPA3` transition modes to `WPA3 Personal` or `WPA3 Enterprise`. Record the raw source value in a new MetadataType scoped to the `authentication` field (`ObjectMetadata.scoped_fields`), written only when the value was mapped. Hold any other unrecognized security value.
- Resolve the VLAN through the existing network bindings by `networkconf_id`.
- Create a connector-owned child group of `default` containing only access points, move the access points into it, and assign SSIDs there. SSIDs scoped by `ap_group_ids` are assigned only when group membership is proven disjoint; otherwise they are held.
- Build test fixtures from one read-only `rest/wlanconf` capture, converted to synthetic values before saving.
- **Acceptance:** a preview diff of creates and the access-point group move only; held reasons for unresolved items; no secret value in logs, diffs, metadata or fixtures; a second run with no diff; prior wireless facts kept when the WLAN channel fails.
- **Risks:** global name collisions and the undocumented `v2/apgroups` endpoint.

### Phase 2: port and radio facts (P1–P2)

- PoE custom fields on `Interface`: capability, mode, standard, type and enabled. Standard, type and enabled need the Integration API.
- Configured `enabled` and `speed` on physical ports only.
- Radio interface type only once `wlanStandard` semantics are confirmed.
- **Acceptance:** preview and apply diffs on a controlled device set. Nautobot `clean()` validation errors (speed on LAG, cross-device LAG members) surface as holds.

### Phase 3: optional (P2–P3)

- Fixed DHCP reservations as annotations on existing `IPAddress` records, creating a UniFi-owned `IPAddress` only when none exists.
- LAGs and stacks from the Integration API.
- The Integration API as a second source for networks and radios, once an identity crosswalk exists.
- Device tags.

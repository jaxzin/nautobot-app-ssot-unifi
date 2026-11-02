# UniFi Mapping Open Items

This page defines the strategy decisions and research questions for [UniFi Mapping Coverage](unifi_mapping_coverage.md). Decisions constrain future mapping designs; they do not authorize implementation, production activation or deployment. Close research items with source documentation, code or approved read-only evidence.

Connector coverage reviewed 2026-11-02 at `495184c268cbd987a4b31e01bf4e3ff8e2266691`, against the Nautobot 3.2.4 support baseline. UniFi API evidence is dated 2026-10-25 and versioned 10.6.109. Strategy decisions are dated 2026-11-01.

## Research

| ID | Question | Blocks | Known | How to close |
|---|---|---|---|---|
| R1 | How do Integration API IDs map to controller `_id` values? | Any use of the Integration API next to the controller API. Without a crosswalk, two channels claim one object under two identities. | Integration IDs are UUIDs; controller IDs are 24-hex ObjectIds. Only `Site.internalReference` documents a link. Devices can probably be matched by exact MAC; this is unconfirmed. | Capture both modes for the same site (Phase 0) and look for a stable shared key per entity. |
| R2 | Does SSID `network.type = NATIVE` mean the network flagged `default: true`? | A future Integration API mapping for `NATIVE` SSIDs. The implemented controller-API path uses `networkconf_id` and is not blocked. | Undocumented. A site has exactly one network with `default: true`. | Compare source fields for the same SSIDs, or find a Ubiquiti statement. |
| R3 | Does radio `wlanStandard` report capability or the configured standard? | Radio interface type. | The spec defines an enum only. Observed values are consistent with each AP's hardware, so they do not distinguish the readings. | A Ubiquiti statement. An empirical test needs a configuration change and is not proposed. |
| R4 | Do Integration API keys inherit the creating admin's role, and can a view-only admin create one? | Keeping Integration access read-only. | Unverified. | Ubiquiti documentation or the console. |
| R5 | Does `v2/apgroups` exist on the target controller, and can filtered memberships be represented without loss? | Extending assignment beyond the implemented single-site, all-AP scope. | No AP-group reader exists here. The `access-points` child is connector-defined, not an imported source AP group. | Approved GET evidence or documentation; verify disjoint groups and multi-site behavior. |
| R6 | Does the presence of an Integration `poe` object reliably mean PoE capability, and how does it align with `poe_caps`? | The PoE capability field. | Ports without PoE omit `poe`. | Phase 0 comparison with controller `poe_caps`. |
| R7 | Which controller fields hold configured port speed, autonegotiation and enabled state? | Port speed and enabled mapping. | Integration `speedMbps` is negotiated and can appear on `DOWN` ports. | Phase 0a: read `port_overrides` and `portconf`. |
| R8 | Does the controller API expose per-WLAN minimum rates or named radio settings on the deployed version? | Radio profile and data-rate rows. | The Integration API on 10.6.109 has no data-rate or transmit-power fields. | Phase 0a. If absent, close both rows as unsupported by source. |
| R9 | Which fields hold members in the global LAG subtypes (`LOCAL`, `SWITCH_STACK`, `MULTI_CHASSIS`)? | LAG mapping. | Per-device `features.switching.lags[].portIdxs` is known. | Read the subtype schemas in the spec. |
| R10 | Is an Integration Wi-Fi broadcast `name` exactly the broadcast SSID? | `WirelessNetwork.ssid` from the Integration API. | The record has `name` and no separate SSID field. | Phase 0: compare with controller `wlanconf.name`. |
| R11 | Which source evidence establishes a client address/network and, separately, authoritative non-use? | Positive native client-IP assignment; address retirement has a separate evidence gate. | Source client addresses exist but the current client reader drops them. The management-IP model guards both endpoints as UniFi-owned; client assignments must instead respect the established physical/VM client match. Active-client omission does not prove an address unused. | Prove positive address, network/prefix and identity facts for ingestion. Separately prove lease/assignment completeness before retirement. Use native SSoT writes/history and inventory status; do not invent a lifecycle field or broaden NIC ownership. Unknown retirement evidence does not invalidate a proven positive observation. |
| R12 | Which source identity and endpoint establish a configured gateway address? | Gateway IPAddress and Interface assignment (coverage row 30). | `ip_subnet` contains the IPv4 host, but the current graph creates a Prefix and optional DHCP range only. | Prove network-to-Device/Interface identity, namespace and ownership; do not guess a VLAN subinterface or duplicate an address. |
| R13 | Which fields establish a MAC for each managed port or radio? | Managed-interface MAC population (coverage row 32). | The current Interface model does not import MACs. Device/client MAC reads serve other identity/observation purposes. | Verify per-interface source fields and ownership before mapping; a device base MAC or neighbor client's MAC does not establish every port's address. |

## Dependencies by mapping

Research blocks only the scope that needs its answer. The table does not authorize collection, implementation or commissioning.

| Mapping scope | Research dependency | Can proceed without it |
|---|---|---|
| Existing single-site, all-AP wireless import | None of R1–R13 is a new prerequisite for its current supported path. | Preserve the implemented path and its documented holds. |
| Filtered AP groups and multi-site broadcast assignment | R5; R1/R4/R10 as applicable if an Integration API channel is selected | Existing all-AP SSID/VLAN mapping |
| Integration API wireless network/VLAN facts | R1, R2, R4, R10, as needed for the selected fields | Controller-API wireless mapping |
| PoE fields | R6; R1/R4 for facts collected through the Integration API | Define the approved optional keys without inventing absent values. |
| Configured physical-port speed, autonegotiation and enabled state | R7 | Existing port identity, type and membership mapping |
| Precise radio standard; reusable profiles/rates | R3 for standard semantics; R8 for profile/rate availability | Existing generic radio interfaces; D6 defines fallback once an exact standard is established. |
| Native client IPs and reservation lifecycle | R11; R1/R4 if their mapping needs the Integration API | Existing client network/attachment observations; D7 defines namespace policy. |
| LAGs and switch stacks | R9; R1/R4 for an Integration API implementation | Existing independently identified interfaces |
| Configured gateway addresses | R12; R1/R4 if using the Integration API | Existing configured IPv4 Prefix/DHCP range mapping |
| Managed-interface MAC population | R13; R1/R4 if using the Integration API | Existing port identities and client MAC matching |
| Firmware and neighbor commissioning | No new API research is required for the implemented mappings; D8 requires reviewed previews. | Strategy and preview review, without activating apply |
| Hardware component templates | Exact model-library matching and curated-catalog ownership need a scoped implementation design; no new UniFi API dependency is established. | Existing model/part-number inventory |

## Strategy decisions

| ID | Decision | Required behavior |
|---|---|---|
| D1 | Evaluate API access per missing mapping. | Check the facts each mapping needs before choosing the controller API, Integration API or both. A new API channel requires established field semantics, read-only access and a proven identity crosswalk where it overlaps existing objects. There is no blanket commitment to an API mode. |
| D5 | Store PoE configuration in optional Interface custom fields with source-neutral keys. | Use `poe_capable`, `poe_mode`, `poe_standard`, `poe_type` and `poe_enabled`. Keep the fields visible and filterable with other port properties. Missing facts remain unset. Naming does not grant an importer authority over another source's values. Keep live power draw in monitoring; establish exact value normalization in the implementation design. |
| D6 | Use a generic native type for a reliably known standard without an exact native choice. | Set `other-wireless` and retain the precise source-reported standard in app-defined Object Metadata. Ambiguous source fields do not establish a standard. Design the metadata schema before implementation. |
| D7 | Use existing network namespaces for DHCP reservations. | Apply the same network-scoping rules as other imports. Do not create a separate reservation namespace or silently choose a default for an unresolved scope. |
| D8 | Include both firmware versions and neighbor observations in the commissioning strategy. | Review preview evidence before activating either apply path. Neighbor observations must not invent cables through wall jacks or patch panels. This strategy does not authorize deployment. |
| D9 | Include native client-IP records and interface assignments, including DHCP addresses. | Attach addresses only to already-matched clients; an IP address does not establish identity. Use the existing network namespace and respect ownership. Remove obsolete assignments with native history. On complete authoritative evidence, retire an unused, importer-owned IPAddress with no remaining assignments, retain the record and reactivate that same record when observed again. Failure, incomplete collection or active-client omission is not proof of non-use. Resolve R11 before commissioning this mapping. |

## Upstream

| Item | Effect when resolved |
|---|---|
| [nautobot#9708](https://github.com/nautobot/nautobot/issues/9708), "Add WPA2/WPA3 transition-mode options to WirelessNetworkAuthenticationChoices" | Transition-mode SSIDs use a native choice; the raw-value record from D4 becomes unnecessary. |
| [nautobot discussion #9466](https://github.com/orgs/nautobot/discussions/9466), "Add PoE capability to Interface and InterfaceTemplate" | PoE custom fields from D5 migrate to native fields. |
| No Nautobot interface type for `802.11be` | Wi-Fi 7 radios can be typed natively. No upstream request exists yet. |

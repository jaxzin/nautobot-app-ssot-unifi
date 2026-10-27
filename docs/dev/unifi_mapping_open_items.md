# UniFi Mapping Open Items

This page lists the questions that still block the proposed scopes in [UniFi Mapping Coverage](unifi_mapping_coverage.md). Each item names what it blocks and how to close it. Close research items with evidence; close decision items with an operator choice.

Assessed 2026-10-25 against Nautobot 3.2.4, nautobot-ssot 4.6.1 and UniFi Network 10.6.109.

## Research

| ID | Question | Blocks | Known | How to close |
|---|---|---|---|---|
| R1 | How do Integration API IDs map to controller `_id` values? | Any use of the Integration API next to the controller API. Without a crosswalk, two channels claim one object under two identities. | Integration IDs are UUIDs; controller IDs are 24-hex ObjectIds. Only `Site.internalReference` documents a link. Devices can probably be matched by exact MAC; this is unconfirmed. | Capture both modes for the same site (Phase 0) and look for a stable shared key per entity. |
| R2 | Does SSID `network.type = NATIVE` mean the network flagged `default: true`? | VLAN on `NATIVE` SSIDs read from the Integration API. Phase 1 reads `networkconf_id` from the controller API and does not depend on it. | Undocumented. A site has exactly one network with `default: true`. | Compare controller `wlanconf.networkconf_id` and `vlan` for the same SSIDs, or find a Ubiquiti statement. |
| R3 | Does radio `wlanStandard` report capability or the configured standard? | Radio interface type. | The spec defines an enum only. Observed values are consistent with each AP's hardware, so they do not distinguish the readings. | A Ubiquiti statement. An empirical test needs a configuration change and is not proposed. |
| R4 | Do Integration API keys inherit the creating admin's role, and can a view-only admin create one? | Keeping Integration access read-only. | Unverified. | Ubiquiti documentation or the console. |
| R5 | Does `v2/apgroups` exist on the deployed controller version, and are memberships disjoint? | AP-group scope. Matters only when an SSID has a broadcast filter. | Undocumented endpoint. aiounifi 85 has no AP-group support, so collection needs a raw GET. | Phase 0a capture. |
| R6 | Does the presence of an Integration `poe` object reliably mean PoE capability, and how does it align with `poe_caps`? | The PoE capability field. | Ports without PoE omit `poe`. | Phase 0 comparison with controller `poe_caps`. |
| R7 | Which controller fields hold configured port speed, autonegotiation and enabled state? | Port speed and enabled mapping. | Integration `speedMbps` is negotiated and can appear on `DOWN` ports. | Phase 0a: read `port_overrides` and `portconf`. |
| R8 | Does the controller API expose per-WLAN minimum rates or named radio settings on the deployed version? | Radio profile and data-rate rows. | The Integration API on 10.6.109 has no data-rate or transmit-power fields. | Phase 0a. If absent, close both rows as unsupported by source. |
| R9 | Which fields hold members in the global LAG subtypes (`LOCAL`, `SWITCH_STACK`, `MULTI_CHASSIS`)? | LAG mapping. | Per-device `features.switching.lags[].portIdxs` is known. | Read the subtype schemas in the spec. |
| R10 | Is an Integration Wi-Fi broadcast `name` exactly the broadcast SSID? | `WirelessNetwork.ssid` from the Integration API. | The record has `name` and no separate SSID field. | Phase 0: compare with controller `wlanconf.name`. |

## Decisions

| ID | Decision | Options and constraints | Blocks |
|---|---|---|---|
| D1 | Should the app read the Integration API in production? | Requires API-key authentication in this app, a deployed read-only key (R4), and R1. Options: controller API only; Integration API as a second source for selected fields; Integration API only, which loses client attachment detail. Research and authentication work can proceed alongside Phase 1, which does not depend on it. | PoE standard, type and enabled; radio standard; LAGs and stacks. |
| D5 | PoE custom field keys and choices | Capability (boolean), mode (`auto`, `pasv24`, `passthrough`, `off`), standard (`802.3af`, `802.3at`, `802.3bt`), type (1–4), enabled (boolean). Choose names that can migrate to native fields. | PoE scope. |
| D6 | Interface type for radios Nautobot cannot type natively | `802.11be` has no interface type in 3.2.4. Options: `other-wireless`, or hold. | Radio typing. |
| D7 | Namespace for DHCP reservation addresses | Options include the `address_scopes` rules used for management addresses, or a single namespace. | DHCP reservation scope. |
| D8 | Enable `sync_firmware` and `sync_neighbors` in scheduled apply | Both are implemented. Review preview diffs first. | Firmware and topology rows. |

## Upstream

| Item | Effect when resolved |
|---|---|
| [nautobot#9708](https://github.com/nautobot/nautobot/issues/9708), "Add WPA2/WPA3 transition-mode options to WirelessNetworkAuthenticationChoices" | Transition-mode SSIDs use a native choice; the raw-value record from D4 becomes unnecessary. |
| [nautobot discussion #9466](https://github.com/orgs/nautobot/discussions/9466), "Add PoE capability to Interface and InterfaceTemplate" | PoE custom fields from D5 migrate to native fields. |
| No Nautobot interface type for `802.11be` | Wi-Fi 7 radios can be typed natively. No upstream request exists yet. |

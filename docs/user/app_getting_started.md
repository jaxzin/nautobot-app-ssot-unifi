# Getting Started with the App

Use the **Unifi to Nautobot** Job to import controller inventory into existing Nautobot namespaces. The connector reads UniFi; it does not configure the controller or its devices.

## Install the App

To install the App, please follow the instructions detailed in the [Installation Guide](../admin/install.md).

## Prerequisites

- An existing Controller with a location and an External Integration for the UniFi endpoint.
- A Secrets Group on that integration containing HTTP username and password entries for a read-only account.
- Existing destination Namespaces. Leaving **Namespace** empty selects the existing `Global` namespace.

The Job uses the External Integration's TLS verification and timeout settings. Keep certificate verification enabled for a controller with a trusted certificate.

## Run an import

1. Open **Unifi to Nautobot** and select the Controller.
2. Optionally set **Default location** and **Location type**. Their defaults come from the Controller's location.
3. Select **Namespace** for an import into one namespace, or supply **Address scopes** to select namespaces by address.
4. Run with **Dryrun** enabled. Review the native SSoT diff, including namespace-qualified prefixes, IP addresses, and assignments.
5. If the diff is correct, run with the same inputs and Dryrun disabled. Verify the imported addresses, their interface assignments, and each device's primary address in the intended namespace.

Objects already present outside UniFi ownership are not adopted. When a source host is reported more than once in the same namespace, its first source assignment wins. Destination objects absent from the source are retained.

## Select namespaces by address

**Address scopes** (`address_scopes` in Job data) accepts a JSON list. Each rule has exactly two keys:

| Key | Value |
| --- | --- |
| `prefixes` | A nonempty list of canonical IPv4 or IPv6 CIDRs, such as `192.0.2.0/24` or `2001:db8::/32`. Host bits, omitted prefix lengths, dotted netmasks, and noncanonical spelling are rejected. |
| `namespace_id` | The UUID of an existing Nautobot Namespace. |

For this synthetic example, assume `routing-a` has UUID `00000000-0000-4000-8000-000000000004` and `routing-b` has UUID `00000000-0000-4000-8000-000000000005`. Replace those UUIDs with the existing namespaces in your installation:

```json
[
  {
    "prefixes": ["192.0.2.0/24", "2001:db8:1::/48"],
    "namespace_id": "00000000-0000-4000-8000-000000000004"
  },
  {
    "prefixes": ["198.51.100.0/24", "2001:db8:2::/48"],
    "namespace_id": "00000000-0000-4000-8000-000000000005"
  }
]
```

With these rules, `192.0.2.10` and `2001:db8:1::10` go to `routing-a`; `198.51.100.10` and `2001:db8:2::10` go to `routing-b`. The address's reported subnet mask determines the imported Prefix and IP mask; rule CIDRs select the namespace only.

Every reported address must match exactly one distinct namespace. Overlapping rules for the same namespace are allowed. Matching different namespaces is ambiguous, even when one rule is more specific. Rule order does not select a winner. The matching namespace applies to the Prefix, IP address, interface assignment, and primary-IP lookup.

| Address scopes input | Behavior |
| --- | --- |
| Omitted, blank form field, or JSON `null` | Use the selected Namespace, defaulting to existing `Global`. |
| `[]` | Admit no IPs. Any reported address fails collection. Inventory with no reported addresses can still be collected. |
| Nonempty rule list | Require a unique matching namespace for every address. Namespace is not a fallback. |

An IP is identified by namespace and host, so a mask change updates that address instead of replacing its identity. Primary addresses are saved only after native interface assignments exist. IPv4 and IPv6 changes, including clearing a primary, are independent; native Nautobot validation remains enforced.

## Repair a failed mapping

Malformed rules or references to missing namespaces fail before source collection. An unmatched or ambiguous address fails during source collection, before inventory writes. The native Job reports the failure; it does not import a partial subset or silently fall back to Global. Existing inventory is retained.

For an unmatched address, add a verified CIDR mapped to its intended existing namespace. For an ambiguous address, correct overlapping rules so that all matches select the same namespace. For invalid input, correct the rule keys, CIDR spelling, or namespace UUID. Then rerun Dryrun and review the complete diff before applying it.

Changing routing rules does not move existing inventory between namespaces. Namespace migrations and their identity-preservation checks must be handled separately before importing into a changed scope.

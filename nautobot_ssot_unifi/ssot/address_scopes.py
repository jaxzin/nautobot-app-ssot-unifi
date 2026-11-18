"""Validate address rules once, then resolve source hosts without database writes."""

from ipaddress import ip_address, ip_network
from uuid import UUID

from nautobot.ipam.models import Namespace


class AddressScopeResolver:
    """Resolve a host to exactly one existing namespace, or reject collection."""

    def __init__(self, rules, namespace_name):
        """Validate native JSON input and resolve all referenced Namespace UUIDs."""
        self.namespace_name = namespace_name
        self.rules = None
        if rules is None:
            return
        if not isinstance(rules, list):
            raise ValueError("address_scopes must be null or a list of rules.")
        self.rules = []
        namespaces = {}
        for index, rule in enumerate(rules):
            message = f"Invalid address_scopes rule {index + 1}"
            if not isinstance(rule, dict) or set(rule) != {"prefixes", "namespace_id"}:
                raise ValueError(f"{message}: expected exactly prefixes and namespace_id.")
            if not isinstance(rule["prefixes"], list) or not rule["prefixes"]:
                raise ValueError(f"{message}: prefixes must be a nonempty list of canonical CIDRs.")
            networks = []
            for prefix in rule["prefixes"]:
                try:
                    if not isinstance(prefix, str) or "%" in prefix:
                        raise ValueError
                    network = ip_network(prefix, strict=True)
                    if str(network) != prefix:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"{message}: prefixes must contain canonical IPv4 or IPv6 CIDRs.") from None
                networks.append(network)
            try:
                if not isinstance(rule["namespace_id"], str):
                    raise ValueError
                namespace_id = UUID(rule["namespace_id"])
            except ValueError:
                raise ValueError(f"{message}: namespace_id must be a Namespace UUID.") from None
            if namespace_id not in namespaces:
                try:
                    namespaces[namespace_id] = Namespace.objects.get(pk=namespace_id).name
                except Namespace.DoesNotExist:
                    raise ValueError(f"{message}: referenced Namespace does not exist.") from None
            self.rules.append((networks, namespace_id, namespaces[namespace_id]))

    def resolve(self, host):
        """Return the matching namespace name; rule order never breaks a tie."""
        if self.rules is None:
            return self.namespace_name
        address = ip_address(host)
        matches = {
            namespace_id: name
            for networks, namespace_id, name in self.rules
            if any(address in network for network in networks)
        }
        if not matches:
            raise ValueError("Source address has no matching address_scopes rule; repair rules and rerun.")
        if len(matches) != 1:
            raise ValueError("Source address has ambiguous address_scopes matches; repair rules and rerun.")
        return next(iter(matches.values()))

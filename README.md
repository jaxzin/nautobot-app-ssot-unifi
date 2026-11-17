# Nautobot SSoT for UniFi

Import UniFi inventory into Nautobot through Single Source of Truth (SSoT) jobs.
The connector can synchronize devices, interfaces, VLANs, subnets, DHCP ranges,
wireless networks, and client observations. Review proposed changes in an SSoT
dry run before enabling writes.

This is the [jaxzin/nautobot-app-ssot-unifi](https://github.com/jaxzin/nautobot-app-ssot-unifi)
fork of [abates/nautobot-app-ssot-unifi](https://github.com/abates/nautobot-app-ssot-unifi).
The default branch, `develop`, carries the maintained fork.

- [Install the fork](docs/admin/install.md)
- [Configure a UniFi controller](docs/user/app_getting_started.md)
- [Choose what to synchronize](docs/user/app_use_cases.md)
- [Contribute a change](CONTRIBUTING.md)

## Tested runtime

CI tests Python 3.12, Nautobot 3.2.4, and Nautobot SSoT 4.6.1. The database
matrix covers PostgreSQL and MySQL.

## License

[Apache License 2.0](LICENSE).

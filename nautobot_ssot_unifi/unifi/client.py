"""The unifi client definition for SSoT."""

from typing import Iterable, TYPE_CHECKING
import ssl

import aiohttp

from aiounifi.controller import Controller as UnifiController
from aiounifi.models.configuration import Configuration as UnifiConfiguration
from aiounifi.models.device import Device, DeviceListRequest
from aiounifi.models.api import ApiRequest

if TYPE_CHECKING:
    from aiounifi.models.site import Site


def require_login(method):
    """Decorator that ensures a session is logged in.

    This decorator will make sure that the session is logged in prior
    to continuing the method call.

    Args:
        method (callable): The method that requires a valid authenticated
            session.
    """

    async def wrapper(self, *args, **kwargs):
        if not self.logged_in:
            await self.api.login()
            self.logged_in = True
        return await method(self, *args, **kwargs)

    return wrapper


class Client:
    """Unifi API client."""

    def __init__(
        self, host: str, username: str, password, port=443, verify_cert=True, timeout=30
    ):  # pylint:disable=too-many-arguments,too-many-positional-arguments
        """Create a new Unfi API client.

        Args:
            host (str): The Unifi cloud controller to connect to.
            username (str): Username for logged in sessions.
            password (str): Password for logged in sessions.
            port (int, optional): The port to connect to the cloud key. Defaults to 443.
            verify_cert (bool, optional): Whether or not to perform TLS verification on the
                certificate. Defaults to True.
            timeout (int, optional): The timeout (in seconds) for requests. Defaults to 30.
        """
        ssl_context = ssl.create_default_context() if verify_cert else False
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        self.config = UnifiConfiguration(
            self.session,
            host=host,
            username=username,
            password=password,
            port=port,
            site="default",
            ssl_context=ssl_context,
        )
        self.api = UnifiController(self.config)
        self.logged_in = False

    async def logout(self):
        """Terminate the session."""
        await self.session.close()

    @property
    def current_site(self):
        """Get the site name from the current config.

        The site is set for all operations on the Unifi controller. Calls
        to any method will use the currently configured site. This property
        will return whatever the currently selected site is.

        Returns:
            str: The name of the site from the current configuration.
        """
        return self.config.site

    @current_site.setter
    def current_site(self, site: str):
        self.config.site = site

    @require_login
    async def get_sites(self) -> Iterable["Site"]:
        """Get an iterable of devices for the current site."""
        await self.api.sites.update()
        return self.api.sites.values()

    @require_login
    async def get_devices(self) -> Iterable["Device"]:
        """Get an iterable of devices for the current site."""
        # A fresh snapshot avoids the event cache retaining devices from a previous site.
        response = await self.api.request(DeviceListRequest.create())
        return [Device(raw) for raw in response["data"]]

    @require_login
    async def _get_vlan_configuration(self):
        """GET only; discard unrelated/credential-bearing configuration fields."""
        projections = []
        for path, fields in (
            (
                "/rest/networkconf",
                {
                    "_id",
                    "name",
                    "purpose",
                    "vlan",
                    "vlan_enabled",
                    "attr_no_delete",
                    "ip_subnet",
                    "dhcpd_enabled",
                    "dhcpd_start",
                    "dhcpd_stop",
                },
            ),
            (
                "/rest/portconf",
                {
                    "_id",
                    "native_networkconf_id",
                    "tagged_vlan_mgmt",
                    "excluded_networkconf_ids",
                    "forward",
                    "tagged_networkconf_ids",
                },
            ),
        ):
            response = await self.api.request(ApiRequest(method="get", path=path))
            rows = response.get("data")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Invalid UniFi VLAN configuration channel.")
            projections.append([{key: row[key] for key in fields if key in row} for row in rows])
        return tuple(projections)

    @require_login
    async def _get_client_configuration(self):
        """GET only `/stat/sta`; keep association/network evidence, discard everything else.

        A fully parsed, successful report is the only basis for recording a client's
        absence; an exception here leaves last-known observations untouched.
        """
        fields = {
            "_id",
            "mac",
            "is_wired",
            "sw_mac",
            "sw_port",
            "ap_mac",
            "essid",
            "radio",
            "radio_name",
            "network_id",
            "vlan",
            "last_seen",
            "site_id",
        }
        response = await self.api.request(ApiRequest(method="get", path="/stat/sta"))
        rows = response.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Invalid UniFi client channel.")
        return [{key: row[key] for key in fields if key in row} for row in rows]

"""The unifi client definition for SSoT."""

from typing import Iterable, TYPE_CHECKING
import ssl

import aiohttp

from aiounifi.controller import Controller as UnifiController
from aiounifi.models.configuration import Configuration as UnifiConfiguration
from aiounifi.models.device import Device, DeviceListRequest

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
            await self.api.clients.update()
            self.logged_in = True
        return await method(self, *args, **kwargs)

    return wrapper


class Client:
    """Unifi API client."""

    def __init__(
        self, host: str, username: str, password, port=443, verify_cert=True, timeout=30
    ):  # pylint:disable=too-many-arguments
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

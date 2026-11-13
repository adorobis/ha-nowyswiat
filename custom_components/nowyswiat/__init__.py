"""Radio Nowy Świat: patron podcasts and live radio as a media source."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from . import info
from .api import AuthError, NowySwiatClient, NowySwiatError
from .audio import NowySwiatAudioView, NowySwiatImageView
from .const import DOMAIN

type NowySwiatConfigEntry = ConfigEntry[NowySwiatClient]


def create_client(hass: HomeAssistant, username: str, password: str) -> NowySwiatClient:
    """Return a client with its own cookie jar (it holds the login)."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    return NowySwiatClient(session, username, password)


async def async_setup_entry(hass: HomeAssistant, entry: NowySwiatConfigEntry) -> bool:
    """Log in and expose the media source."""
    client = create_client(hass, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    try:
        await client.async_login()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except NowySwiatError as err:
        raise ConfigEntryNotReady(str(err)) from err
    entry.runtime_data = client

    if not hass.data.get(f"{DOMAIN}_view"):
        hass.http.register_view(NowySwiatAudioView(hass))
        hass.http.register_view(NowySwiatImageView(hass))
        hass.data[f"{DOMAIN}_view"] = True
    info.register(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NowySwiatConfigEntry) -> bool:
    """Nothing to tear down; the session is closed with Home Assistant."""
    return True

"""Now-playing metadata for players (title, hosts, cover, duration).

Registered in ``hass.data["media_info_providers"]["nowyswiat"]``. A player
integration that pushes URLs itself (for example to a DLNA renderer) can look
a media-source ID up there and send title, cover and duration along with the
URL. Home Assistant itself does not use this; it is an optional hook.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

import aiohttp
from homeassistant.core import HomeAssistant

from .api import NowySwiatError
from .audio import async_segments, duration_of, image_path
from .const import DOMAIN, LIVE_STREAMS, LOGO

_LOGGER = logging.getLogger(__name__)

PREFIX = f"media-source://{DOMAIN}/"
STATION = "Radio Nowy Świat"
PROVIDERS_KEY = "media_info_providers"


async def async_media_info(hass: HomeAssistant, media_content_id: str) -> dict[str, Any] | None:
    """Return metadata for one of our media-source IDs, None if not ours.

    Keys: title, artist, album, image (HA path or None), duration (s or
    None), size (bytes or None), mime, seekable.
    """
    if not media_content_id.startswith(PREFIX):
        return None
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    client = entries[0].runtime_data
    parts = [unquote(p) for p in media_content_id[len(PREFIX) :].split("/")]

    if len(parts) == 2 and parts[0] == "live" and parts[1] in LIVE_STREAMS:
        title, _, mime = LIVE_STREAMS[parts[1]]
        return {
            "title": title,
            "artist": STATION,
            "album": STATION,
            "image": image_path(LOGO),
            "duration": None,
            "size": None,
            "mime": mime,
            "seekable": False,
        }
    if parts[0] not in ("ep", "part") or len(parts) < 2:
        return None

    slug = parts[1]
    part = int(parts[2]) if parts[0] == "part" and len(parts) == 3 and parts[2].isdigit() else None
    try:
        episode = await client.async_episode(slug)
        title = episode.title
        if part is not None and 1 <= part <= len(episode.parts):
            title = episode.parts[part - 1].name or f"{title} cz. {part}"
        info: dict[str, Any] = {
            "title": title,
            "artist": episode.subtitle,
            "album": STATION,
            "image": image_path(episode.image),
            "duration": None,
            "size": None,
            "mime": "audio/mpeg",
            "seekable": True,
        }
        segments = await async_segments(client, slug, part)
    except (NowySwiatError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("No metadata for %s: %s", media_content_id, err)
        return None
    info["duration"] = duration_of(segments)
    info["size"] = sum(s.length for s in segments)
    return info


def register(hass: HomeAssistant) -> None:
    """Make the provider available to player integrations."""
    hass.data.setdefault(PROVIDERS_KEY, {})[DOMAIN] = async_media_info

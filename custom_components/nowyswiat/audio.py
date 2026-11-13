"""Audio proxy: streams podcasts and live radio through Home Assistant.

Players (MusicCast's DLNA renderer in particular) then get a plain LAN URL,
need no site cookies and don't have to deal with HTTPS. The URL is signed
by Home Assistant when a media player plays it, so ``requires_auth`` holds.

Long shows are published in parts (``cz. 1`` … ``cz. 4``). ``ep`` URLs
serve all parts as one continuous MP3: sizes are probed up front so the
response has a Content-Length and byte ranges (seeking) map onto the right
part. The ID3 header of every part after the first is skipped so decoders
don't stumble at the joins. ``part`` URLs serve a single part.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import logging
import re
import time
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BASE_URL, NowySwiatError, Part
from .const import AUDIO_VIEW_URL, DOMAIN, IMAGE_VIEW_URL, LIVE_STREAMS

if TYPE_CHECKING:
    from .api import NowySwiatClient

_LOGGER = logging.getLogger(__name__)

# sock_read well below the ~100 s speaker buffer, so a stalled download is
# retried before the speaker runs dry.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=15, sock_read=20)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=20)
SEGMENTS_TTL = 3600
CHUNK = 64 * 1024
_RANGE = re.compile(r"bytes=(\d*)-(\d*)$")


def _b64(slug: str) -> str:
    return base64.urlsafe_b64encode(slug.encode()).decode().rstrip("=")


def _unb64(key: str) -> str:
    try:
        return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as err:
        raise web.HTTPNotFound from err


def audio_path(kind: str, key: str, part: int | None = None) -> str:
    """Return the proxy path.

    kind "ep": key is the episode slug (all parts), "part": slug + part
    number (1-based), "live": a LIVE_STREAMS key. Slugs may contain "/", so
    they travel base64url-encoded.
    """
    if kind == "live":
        ext = "aac" if LIVE_STREAMS[key][2] == "audio/aac" else "mp3"
        return f"{AUDIO_VIEW_URL}/live/{key}.{ext}"
    if kind == "part":
        return f"{AUDIO_VIEW_URL}/part/{_b64(key)}.{part}.mp3"
    return f"{AUDIO_VIEW_URL}/ep/{_b64(key)}.mp3"


@dataclass(slots=True)
class Segment:
    """A byte range of one upstream file inside the virtual stream."""

    url: str
    start: int  # first byte used in the upstream file
    length: int
    bitrate: int | None = None  # bits per second, from the first MP3 frame


def _id3_size(head: bytes) -> int:
    """Return the size of an ID3v2 tag at the start of a file, else 0."""
    if len(head) >= 10 and head[:3] == b"ID3":
        size = 0
        for byte in head[6:10]:
            size = (size << 7) | (byte & 0x7F)
        return 10 + size + (10 if head[5] & 0x10 else 0)
    return 0


_L3_BITRATES = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),  # MPEG-1
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),  # MPEG-2
    0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),  # MPEG-2.5
}


def _mp3_bitrate(header: bytes) -> int | None:
    """Return the bitrate of an MPEG layer III frame header, in bit/s."""
    if len(header) < 4 or header[0] != 0xFF or header[1] & 0xE0 != 0xE0:
        return None
    version, layer, index = (header[1] >> 3) & 3, (header[1] >> 1) & 3, header[2] >> 4
    if layer != 1 or version not in _L3_BITRATES or not 0 < index < 15:
        return None
    return _L3_BITRATES[version][index] * 1000


async def _read_range(
    client: NowySwiatClient, url: str, first: int, last: int
) -> tuple[bytes, str]:
    headers = {**client.headers, "Range": f"bytes={first}-{last}"}
    async with client.session.get(url, headers=headers, timeout=PROBE_TIMEOUT) as resp:
        if resp.status >= 400:
            raise NowySwiatError(f"Upstream HTTP {resp.status}")
        data = await resp.content.read(last - first + 1)
        total = resp.headers.get("Content-Range", "").rpartition("/")[2]
        if not total.isdigit():
            total = resp.headers.get("Content-Length", "")
        return data, total


async def _probe(client: NowySwiatClient, part: Part, skip_id3: bool) -> Segment:
    head, total = await _read_range(client, part.url, 0, 9)
    if not total.isdigit():
        raise NowySwiatError("Upstream size unknown")
    id3 = _id3_size(head)
    frame = head[:4] if not id3 else (await _read_range(client, part.url, id3, id3 + 3))[0]
    skip = id3 if skip_id3 else 0
    return Segment(part.url, skip, int(total) - skip, _mp3_bitrate(frame))


_SEGMENTS: dict[tuple[str, int | None], tuple[float, list[Segment]]] = {}


async def async_segments(
    client: NowySwiatClient, slug: str, part: int | None = None
) -> list[Segment]:
    """Return the byte segments of an episode (all parts) or of one part.

    Raises NowySwiatError (no access, bad part) or aiohttp errors.
    """
    cached = _SEGMENTS.get((slug, part))
    if cached and time.monotonic() - cached[0] < SEGMENTS_TTL:
        return cached[1]
    parts = await client.async_parts(slug)
    if part is not None:
        if not 1 <= part <= len(parts):
            raise NowySwiatError(f"{slug} has no part {part}")
        parts = [parts[part - 1]]
    segments = list(await asyncio.gather(*(_probe(client, p, i > 0) for i, p in enumerate(parts))))
    _SEGMENTS[(slug, part)] = (time.monotonic(), segments)
    return segments


def duration_of(segments: list[Segment]) -> float | None:
    """Return the playing time in seconds (the files are constant bitrate)."""
    if not segments or any(not s.bitrate for s in segments):
        return None
    return sum(s.length * 8 / s.bitrate for s in segments)


MAX_RETRIES = 6
RETRY_RESET_BYTES = 2_000_000  # this much streamed fine resets the retry count
UPSTREAM_ERRORS = (
    aiohttp.ClientPayloadError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ServerTimeoutError,
    aiohttp.ClientOSError,
    TimeoutError,
)


class _ClientGone(Exception):
    """The player closed its connection."""


class _UpstreamFailed(Exception):
    """nowyswiat.online / revma kept failing."""


async def _write(response: web.StreamResponse, chunk: bytes) -> None:
    try:
        await response.write(chunk)
    except (ConnectionResetError, aiohttp.ClientConnectionResetError, RuntimeError) as err:
        raise _ClientGone from err


async def _pipe_range(
    client: NowySwiatClient,
    response: web.StreamResponse,
    url: str,
    lo: int,
    hi: int,
    label: str,
) -> None:
    """Copy bytes lo..hi of an upstream file, reconnecting where it broke.

    The CDN sometimes drops a long download (the player reads slowly once
    its buffer is full). Ending the response early leaves MusicCast
    "playing" silence, so pick up again from the next missing byte.
    """
    pos, failures, since_failure = lo, 0, 0
    while pos <= hi:
        try:
            headers = {**client.headers, "Range": f"bytes={pos}-{hi}"}
            async with client.session.get(url, headers=headers, timeout=STREAM_TIMEOUT) as resp:
                if resp.status >= 400:
                    raise _UpstreamFailed(f"HTTP {resp.status}")
                async for chunk in resp.content.iter_chunked(CHUNK):
                    await _write(response, chunk)
                    pos += len(chunk)
                    since_failure += len(chunk)
                    if since_failure > RETRY_RESET_BYTES:
                        failures = 0
            if pos <= hi:
                raise _UpstreamFailed(f"short body, {hi - pos + 1} bytes missing")
        except (_UpstreamFailed, *UPSTREAM_ERRORS) as err:
            failures += 1
            since_failure = 0
            if failures > MAX_RETRIES:
                raise _UpstreamFailed(f"{err!r} after {MAX_RETRIES} retries") from err
            _LOGGER.info(
                "Upstream dropped %s at byte %s (%r), reconnecting (%s)", label, pos, err, failures
            )
            await asyncio.sleep(min(2 * failures, 10))


class NowySwiatAudioView(HomeAssistantView):
    """Stream audio from nowyswiat.online with the logged-in session."""

    url = AUDIO_VIEW_URL + "/{kind}/{name}"
    name = "api:nowyswiat:audio"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    def _client(self) -> NowySwiatClient:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise web.HTTPServiceUnavailable(text="Radio Nowy Świat is not set up")
        return entries[0].runtime_data

    async def _episode_segments(
        self, client: NowySwiatClient, kind: str, name: str
    ) -> list[Segment]:
        stem = name.removesuffix(".mp3")
        part = None
        if kind == "part":
            stem, _, number = stem.rpartition(".")
            if not number.isdigit():
                raise web.HTTPNotFound
            part = int(number)
        try:
            return await async_segments(client, _unb64(stem), part)
        except NowySwiatError as err:
            raise web.HTTPForbidden(text=str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise web.HTTPBadGateway(text=str(err)) from err

    @staticmethod
    def _range(request: web.Request, total: int) -> tuple[int, int] | None:
        """Return the requested inclusive byte range, or None for all."""
        match = _RANGE.match(request.headers.get("Range", "").strip())
        if not match or not (match.group(1) or match.group(2)):
            return None
        if not match.group(1):  # suffix range: last N bytes
            first, last = max(0, total - int(match.group(2))), total - 1
        else:
            first = int(match.group(1))
            last = min(int(match.group(2)), total - 1) if match.group(2) else total - 1
        if first >= total or first > last:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{total}"})
        return first, last

    async def _serve_episode(
        self, request: web.Request, kind: str, name: str, *, body: bool
    ) -> web.StreamResponse:
        client = self._client()
        segments = await self._episode_segments(client, kind, name)
        total = sum(s.length for s in segments)
        wanted = self._range(request, total)
        first, last = wanted or (0, total - 1)
        response = web.StreamResponse(status=206 if wanted else 200)
        response.headers["Content-Type"] = "audio/mpeg"
        response.headers["Accept-Ranges"] = "bytes"
        response.content_length = last - first + 1
        if wanted:
            response.headers["Content-Range"] = f"bytes {first}-{last}/{total}"
        await response.prepare(request)
        if not body:
            return response

        offset = 0  # position of the current segment in the virtual stream
        try:
            for seg in segments:
                seg_first, seg_last = offset, offset + seg.length - 1
                offset += seg.length
                if seg_last < first or seg_first > last:
                    continue
                lo = seg.start + max(first, seg_first) - seg_first
                hi = seg.start + min(last, seg_last) - seg_first
                await _pipe_range(client, response, seg.url, lo, hi, f"{kind}/{name}")
        except _ClientGone:
            pass  # players drop the connection to seek or stop; that's normal
        except _UpstreamFailed as err:
            # The player sees a short body; it will request the rest again.
            _LOGGER.warning("Giving up on %s/%s: %s", kind, name, err)
        return response

    async def _serve_live(self, request: web.Request, name: str) -> web.StreamResponse:
        key = name.rsplit(".", 1)[0]
        if key not in LIVE_STREAMS:
            raise web.HTTPNotFound
        client = self._client()
        _, src, mime = LIVE_STREAMS[key]
        response = web.StreamResponse()
        response.headers["Content-Type"] = mime
        await response.prepare(request)
        failures = 0
        # Reconnect for as long as the player listens: MP3/AAC decoders
        # resync on the next frame, so a reconnect is just a short gap.
        while failures <= MAX_RETRIES:
            try:
                async with client.session.get(
                    src, headers={**client.headers, "Icy-MetaData": "0"}, timeout=STREAM_TIMEOUT
                ) as resp:
                    if resp.status >= 400:
                        raise _UpstreamFailed(f"HTTP {resp.status}")
                    received = 0
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        await _write(response, chunk)
                        received += len(chunk)
                        if received > RETRY_RESET_BYTES:
                            failures, received = 0, 0
                raise _UpstreamFailed("stream ended")
            except _ClientGone:
                return response
            except (_UpstreamFailed, *UPSTREAM_ERRORS) as err:
                failures += 1
                _LOGGER.info("Live stream %s dropped (%r), reconnecting (%s)", key, err, failures)
                await asyncio.sleep(min(2 * failures, 10))
        _LOGGER.warning("Live stream %s: giving up after %s reconnects", key, MAX_RETRIES)
        return response

    async def head(self, request: web.Request, kind: str, name: str) -> web.StreamResponse:
        """Answer HEAD probes from renderers without streaming the body."""
        if kind == "live":
            if name.rsplit(".", 1)[0] not in LIVE_STREAMS:
                raise web.HTTPNotFound
            return web.Response(headers={"Content-Type": LIVE_STREAMS[name.rsplit(".", 1)[0]][2]})
        if kind not in ("ep", "part"):
            raise web.HTTPNotFound
        return await self._serve_episode(request, kind, name, body=False)

    async def get(self, request: web.Request, kind: str, name: str) -> web.StreamResponse:
        """Stream audio, honouring Range for seeking."""
        if kind == "live":
            return await self._serve_live(request, name)
        if kind not in ("ep", "part"):
            raise web.HTTPNotFound
        return await self._serve_episode(request, kind, name, body=True)


IMAGE_PREFIXES = ("/userfiles/", "/assets/")


def image_path(url: str | None) -> str | None:
    """Return the proxy path for a nowyswiat.online image, else None."""
    if not url or not url.startswith(BASE_URL + "/"):
        return None
    path = url[len(BASE_URL) :]
    if not path.startswith(IMAGE_PREFIXES):
        return None
    return f"{IMAGE_VIEW_URL}/{_b64(path)}.jpg"


class NowySwiatImageView(HomeAssistantView):
    """Serve the site's cover images over the LAN for speakers.

    Speakers fetch album art without Home Assistant credentials, so this view
    is public. It only relays public images from nowyswiat.online.
    """

    url = IMAGE_VIEW_URL + "/{name}"
    name = "api:nowyswiat:image"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    async def get(self, request: web.Request, name: str) -> web.Response:
        """Relay one image."""
        path = _unb64(name.rsplit(".", 1)[0])
        if not path.startswith(IMAGE_PREFIXES) or ".." in path:
            raise web.HTTPNotFound
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(BASE_URL + path, timeout=PROBE_TIMEOUT) as resp:
                if resp.status != 200 or not resp.content_type.startswith("image/"):
                    raise web.HTTPNotFound
                body = await resp.read()
                return web.Response(
                    body=body,
                    content_type=resp.content_type,
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise web.HTTPBadGateway(text=str(err)) from err

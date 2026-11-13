"""Minimal client for the Radio Nowy Świat website (nowyswiat.online).

The site has no public API, so this reads the server-rendered HTML. Every
podcast on a page is rendered as a player element carrying
``data-neo-player-*`` attributes (audio source, title, image), which is what
the parsers below rely on. Logged out (or without a patron subscription)
the audio source is a short preview under ``/userfiles/podcasts/mp3previews``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import html
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://nowyswiat.online"
LOGIN_PATH = "/konto/zaloguj"
ACCOUNT_PATH = "/konto/ustawienia"
FAVOURITES_PATH = "/konto/ulubione/podcasty"
PODCASTS_PATH = "/podcasty"
REPORTAGE_PREFIX = "reportaze/"

# Podcast catalogues (key, title, listing path). "Bez ogródek" is left out:
# it only holds articles with YouTube videos, nothing a speaker can play.
CATEGORIES: dict[str, tuple[str, str]] = {
    "anteny": ("Podcasty z anteny", "/podcasty"),
    "extra": ("Podcasty extra", "/podcasty/podcasty-extra"),
    "extraplus": ("Podcasty extra plus", "/podcasty/podcasty-extra-plus"),
    "otwarte": ("Podcasty otwarte", "/podcasty/podcasty-otwarte"),
    "reportaze": ("Reportaże", "/reportaze"),
}
FILTERS = ("rbroadcast", "author")
PREVIEW_MARKER = "/mp3previews/"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=30)
PROGRAMMES_TTL = 12 * 3600
AUDIO_URL_TTL = 3600

_PLAYER_TAG = re.compile(r"<[a-z]+\b[^>]*\bdata-neo-player-src=\"[^\"]*\"[^>]*>", re.S)
_ATTR = re.compile(r"([a-zA-Z0-9_:-]+)(?:=\"([^\"]*)\")?")
_HREF_EPISODE = re.compile(r"nowyswiat\.online/(podcasty|reportaze)/([^\"'?#]+)")
_FAV_ID = re.compile(r"/ulubione/podcast/(\d+)")
_DESC = re.compile(r"class=\"rns-post-card-desc\">(.*?)</p>", re.S)
_PAGE = re.compile(r"[?&]page=(\d+)")
_CSRF = re.compile(r"name=\"csrf_neocms\"\s+value=\"([^\"]+)\"")
_FILTER_LINK = (
    r"href=\"https://nowyswiat\.online[^\"?]*\?{kind}=([^\"&]+)\"[^>]*>"
    r".*?<img[^>]*src=\"([^\"]*)\"[^>]*>"
    r".*?rns-search-dropdown-title\">([^<]*)</h2>"
    r"(?:\s*<p class=\"rns-search-dropdown-number\">([^<]*)</p>)?"
)
_TAGS = re.compile(r"<[^>]+>")
_CARD_LINK = re.compile(r"href=\"(https://nowyswiat\.online/(?:podcasty|reportaze)/[^\"'?#]+)\"")
_FIRST_PART = re.compile(r"\s+cz\.\s*1$")


class NowySwiatError(Exception):
    """Talking to the website failed."""


class AuthError(NowySwiatError):
    """The username or password was rejected."""


@dataclass(slots=True)
class Episode:
    """One podcast episode as listed on the site."""

    slug: str
    title: str
    subtitle: str
    image: str | None
    audio_url: str
    favourite_id: str | None = None
    description: str = ""

    @property
    def is_preview(self) -> bool:
        """Return True when only the preview clip is exposed."""
        return PREVIEW_MARKER in self.audio_url


@dataclass(slots=True)
class Part:
    """One audio file of an episode (long shows come in several parts)."""

    name: str
    url: str


@dataclass(slots=True)
class Programme:
    """A programme (``rbroadcast``) or an author that groups episodes."""

    slug: str
    title: str
    image: str | None
    count: str


@dataclass(slots=True)
class EpisodeInfo:
    """What an episode page says about the episode."""

    title: str
    subtitle: str
    image: str | None
    parts: list[Part]


@dataclass(slots=True)
class EpisodePage:
    """One page of an episode listing."""

    episodes: list[Episode]
    page: int
    last_page: int


def _slug(text: str) -> str | None:
    """Return the episode key from a link: podcast slug or reportaze/<slug>."""
    match = _HREF_EPISODE.search(text)
    if match is None:
        return None
    return match.group(2) if match.group(1) == "podcasty" else REPORTAGE_PREFIX + match.group(2)


def episode_path(slug: str) -> str:
    """Return the site path of an episode page."""
    if slug.startswith(REPORTAGE_PREFIX):
        return "/" + slug
    return f"{PODCASTS_PATH}/{slug}"


def _text(value: str) -> str:
    return " ".join(_TAGS.sub(" ", html.unescape(value)).split())


def _abs(url: str | None) -> str | None:
    if not url or url == "-":
        return None
    return urljoin(BASE_URL + "/", url)


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1): html.unescape(m.group(2) or "") for m in _ATTR.finditer(tag)}


def parse_episodes(page: str) -> list[Episode]:
    """Return the episodes rendered on a page, in page order, deduplicated."""
    tags = list(_PLAYER_TAG.finditer(page))
    found: dict[str, Episode] = {}
    for idx, match in enumerate(tags):
        attrs = _attrs(match.group(0))
        src = attrs.get("data-neo-player-src", "")
        title_html = attrs.get("data-neo-player-title", "")
        slug = _slug(title_html)
        if slug is None and "neo-player-stream" not in match.group(0):
            # Favourites cards have a plain-text title; the card link holds the slug.
            links = _CARD_LINK.findall(page, max(0, match.start() - 3000), match.start())
            slug = _slug(links[-1]) if links else None
        if not src.startswith("http") or slug is None:
            continue  # live stream buttons, empty players
        if slug in found:
            continue
        # The card's extra details sit between this player and the next one.
        end = tags[idx + 1].start() if idx + 1 < len(tags) else len(page)
        tail = page[match.end() : min(end, match.end() + 8000)]
        fav = _FAV_ID.search(tail)
        desc = _DESC.search(tail)
        found[slug] = Episode(
            slug=slug,
            title=_FIRST_PART.sub("", _text(title_html)) or slug,
            subtitle=_text(attrs.get("data-neo-player-subtitle", "")),
            image=_abs(attrs.get("data-neo-player-img")),
            audio_url=src,
            favourite_id=fav.group(1) if fav else None,
            description=_text(desc.group(1)) if desc else "",
        )
    return list(found.values())


def parse_parts(page: str) -> list[Part]:
    """Return all audio parts of the episode shown on an episode page."""
    info = parse_episode_page(page)
    return info.parts if info else []


def parse_episode_page(page: str) -> EpisodeInfo | None:
    """Return title, hosts, cover and audio parts from an episode page."""
    tags = [m.group(0) for m in _PLAYER_TAG.finditer(page)]
    main = [t for t in tags if "neoplayer-podcast-main-play" in t]
    # Reportage pages have no main player, only the card of the reportage.
    others = [t for t in tags if "neo-player-stream" not in t and "podcast-media" in t]
    for tag in (main or others)[:1]:
        attrs = _attrs(tag)
        parts: list[Part] = []
        try:
            for item in json.loads(attrs.get("data-neo-player-list") or "[]"):
                if str(item.get("src", "")).startswith("http"):
                    parts.append(Part(_text(item.get("name", "")), item["src"]))
        except (ValueError, TypeError, AttributeError):
            _LOGGER.debug("Unparsable part list: %.200s", attrs.get("data-neo-player-list"))
        title = _text(attrs.get("data-neo-player-title", ""))
        if not parts and attrs.get("data-neo-player-src", "").startswith("http"):
            parts = [Part(title, attrs["data-neo-player-src"])]
        return EpisodeInfo(
            title=re.sub(r"\s+cz\.\s*\d+$", "", title),
            subtitle=_text(attrs.get("data-neo-player-subtitle", "")),
            image=_abs(attrs.get("data-neo-player-img")),
            parts=parts,
        )
    return None


def parse_last_page(page: str, current: int) -> int:
    """Return the highest page number linked from a listing."""
    numbers = [int(n) for n in _PAGE.findall(html.unescape(page))]
    return max([current, *numbers])


def parse_filter(page: str, kind: str = "rbroadcast") -> list[Programme]:
    """Return programmes (rbroadcast) or authors from a filter dropdown."""
    found: dict[str, Programme] = {}
    pattern = re.compile(_FILTER_LINK.format(kind=kind), re.S)
    for slug, img, title, count in pattern.findall(page):
        if slug not in found:
            found[slug] = Programme(
                slug=slug,
                title=_text(title),
                image=_abs(img),
                count=_text(count or ""),
            )
    return sorted(found.values(), key=lambda p: p.title.casefold())


class NowySwiatClient:
    """Logged-in session against nowyswiat.online."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        """Initialize the client.

        The session must have its own cookie jar; it carries the login.
        """
        self.session = session
        self._username = username
        self._password = password
        self._login_lock = asyncio.Lock()
        self._logged_in = False
        self._filters: dict[tuple[str, str], tuple[float, list[Programme]]] = {}
        self._episodes: dict[str, tuple[float, EpisodeInfo]] = {}

    @property
    def headers(self) -> dict[str, str]:
        """Headers sent with every request."""
        return {"User-Agent": USER_AGENT, "Referer": BASE_URL + "/"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[str, str, int]:
        try:
            async with self.session.request(
                method,
                urljoin(BASE_URL, path),
                headers=self.headers,
                timeout=TIMEOUT,
                **kwargs,
            ) as resp:
                body = await resp.text(errors="replace")
                return body, str(resp.url), resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NowySwiatError(f"Request to {path} failed: {err}") from err

    async def async_is_logged_in(self) -> bool:
        """Return True when the session cookie is still valid."""
        _, _, status = await self._request("GET", ACCOUNT_PATH, allow_redirects=False)
        return status == 200

    async def async_login(self) -> None:
        """Log in with the stored credentials."""
        async with self._login_lock:
            self.session.cookie_jar.clear()
            page, _, _ = await self._request("GET", LOGIN_PATH)
            csrf = _CSRF.search(page)
            if csrf is None:
                raise NowySwiatError("Login form not found (site layout changed?)")
            await self._request(
                "POST",
                LOGIN_PATH,
                data={
                    "csrf_neocms": csrf.group(1),
                    "login": self._username,
                    "password": self._password,
                    "remember_me": "1",
                    "submit": "",
                },
            )
            if not await self.async_is_logged_in():
                self._logged_in = False
                raise AuthError("Login rejected")
            self._logged_in = True
            self._episodes.clear()
            _LOGGER.debug("Logged in to nowyswiat.online as %s", self._username)

    async def _get_page(
        self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False
    ) -> str:
        """Fetch a page, logging in again once if the session expired."""
        if auth and not self._logged_in:
            await self.async_login()
        body, url, status = await self._request("GET", path, params=params)
        if urlparse(url).path.startswith(LOGIN_PATH) and auth:
            await self.async_login()
            body, url, status = await self._request("GET", path, params=params)
            if urlparse(url).path.startswith(LOGIN_PATH):
                raise AuthError("Session not accepted after logging in again")
        if status >= 400:
            raise NowySwiatError(f"{path} returned HTTP {status}")
        return body

    async def _listing(
        self, path: str, params: dict[str, Any], page: int, *, auth: bool = False
    ) -> EpisodePage:
        if page > 1:
            params = {**params, "page": page}
        body = await self._get_page(path, params, auth=auth)
        episodes = parse_episodes(body)
        return EpisodePage(episodes, page, parse_last_page(body, page))

    async def async_category(self, category: str, page: int = 1, **filters: str) -> EpisodePage:
        """Return one page of a catalogue, optionally filtered.

        Filters: rbroadcast (programme), author, date (YYYY-MM-DD), search.
        """
        path = CATEGORIES[category][1]
        return await self._listing(path, {k: v for k, v in filters.items() if v}, page, auth=True)

    async def async_latest(self, page: int = 1) -> EpisodePage:
        """Return the newest podcasts."""
        return await self.async_category("anteny", page)

    async def async_programme(self, slug: str, page: int = 1) -> EpisodePage:
        """Return the episodes of one programme."""
        return await self.async_category("anteny", page, rbroadcast=slug)

    async def async_search(
        self, query: str, page: int = 1, category: str = "anteny"
    ) -> EpisodePage:
        """Search podcasts (title, guests, description)."""
        return await self.async_category(category, page, search=query)

    async def async_favourites(self, page: int = 1) -> EpisodePage:
        """Return the podcasts the account marked as favourite."""
        return await self._listing(FAVOURITES_PATH, {}, page, auth=True)

    async def async_filter_values(self, category: str, kind: str) -> list[Programme]:
        """Return the programmes or authors of a catalogue, cached for hours."""
        cached = self._filters.get((category, kind))
        if cached and time.monotonic() - cached[0] < PROGRAMMES_TTL:
            return cached[1]
        page = await self._get_page(CATEGORIES[category][1], auth=True)
        for each in FILTERS:
            values = parse_filter(page, each)
            if values:
                self._filters[(category, each)] = (time.monotonic(), values)
        return self._filters.get((category, kind), (0, []))[1]

    async def async_programmes(self) -> list[Programme]:
        """Return all programmes of the main catalogue."""
        return await self.async_filter_values("anteny", "rbroadcast")

    async def async_parts(self, slug: str) -> list[Part]:
        """Return the full-length audio parts of an episode."""
        return (await self.async_episode(slug)).parts

    async def async_episode(self, slug: str) -> EpisodeInfo:
        """Return an episode's title, hosts, cover and full-length parts.

        Raises NowySwiatError when the site only offers the preview, which
        means the account has no active patron subscription.
        """
        cached = self._episodes.get(slug)
        if cached and time.monotonic() - cached[0] < AUDIO_URL_TTL:
            return cached[1]
        info = None
        for attempt in range(2):
            info = parse_episode_page(await self._get_page(episode_path(slug), auth=True))
            if info and info.parts and not any(PREVIEW_MARKER in p.url for p in info.parts):
                self._episodes[slug] = (time.monotonic(), info)
                return info
            if attempt == 0:
                # A lapsed session looks the same as no subscription; retry once.
                await self.async_login()
        if not info or not info.parts:
            raise NowySwiatError(f"No audio found for {slug}")
        raise NowySwiatError(
            "Only a preview is available for this podcast: the account seems "
            "to have no active patron subscription"
        )

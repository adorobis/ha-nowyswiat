"""Media source for Radio Nowy Świat podcasts and live radio.

Identifiers (after ``media-source://nowyswiat/``), segments URL-quoted:
    favs/<page>                          favourite podcasts of the account
    cat/<cat>                            a catalogue: filters + newest episodes
    list/<cat>/<filter>/<value>/<page>   episodes; filter is all, rbroadcast
                                         (programme), author, date or search
    filter/<cat>/<rbroadcast|author>     programmes or authors of a catalogue
    dates/<cat>[/<year>[/<month>]]       date picker
    live, live/<key>                     live channels
    ep/<slug>                            one episode: plays all parts, expands
    part/<slug>/<n>                      one part of an episode (1-based)
Older ids (latest/<page>, progs, prog/<slug>/<page>, search/<page>/<q>)
still resolve, so saved shortcuts keep working.
"""

from __future__ import annotations

import asyncio
import calendar
from datetime import date, timedelta
import re
from urllib.parse import quote, unquote

from homeassistant.components.media_player import (
    BrowseError,
    MediaClass,
    MediaType,
    SearchMedia,
    SearchMediaQuery,
)
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import CATEGORIES, Episode, EpisodePage, NowySwiatClient, NowySwiatError
from .audio import audio_path
from .const import DOMAIN, LIVE_STREAMS, LOGO

TITLE = "Radio Nowy Świat"
# Players such as MusicCast hide playable items whose content type isn't
# an audio/* MIME type, so every playable item carries one.
AUDIO_MPEG = "audio/mpeg"
FILTER_TITLES = {"rbroadcast": "Audycje", "author": "Autorzy"}
FILTER_SINGULAR = {"rbroadcast": "audycja", "author": "autor"}
NO_DATE_FILTER = {"reportaze"}  # the site ignores ?date= there
SEARCHABLE = ("anteny", "extra", "extraplus", "otwarte")
RECENT_DAYS = 14
FIRST_YEAR = 2020  # the station started in July 2020
MONTHS = (
    "",
    "styczeń",
    "luty",
    "marzec",
    "kwiecień",
    "maj",
    "czerwiec",
    "lipiec",
    "sierpień",
    "wrzesień",
    "październik",
    "listopad",
    "grudzień",
)
WEEKDAYS = ("pon.", "wt.", "śr.", "czw.", "pt.", "sob.", "niedz.")


async def async_get_media_source(hass: HomeAssistant) -> NowySwiatMediaSource:
    """Set up the media source."""
    return NowySwiatMediaSource(hass)


def _ident(*parts: str | int) -> str:
    return "/".join(quote(str(p), safe="") for p in parts)


def _list_id(cat: str, kind: str = "all", value: str = "-", page: int = 1) -> str:
    return _ident("list", cat, kind, value or "-", page)


def _folder(ident: str, title: str, thumbnail: str | None = None) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=ident,
        media_class=MediaClass.DIRECTORY,
        media_content_type=MediaType.PLAYLIST,
        title=title,
        can_play=False,
        can_expand=True,
        thumbnail=thumbnail,
    )


def _episode(ep: Episode) -> BrowseMediaSource:
    title = ep.title
    if ep.subtitle and ep.subtitle not in title:
        title = f"{title} — {ep.subtitle}"
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=_ident("ep", ep.slug),
        media_class=MediaClass.PODCAST,
        media_content_type=AUDIO_MPEG,
        title=title,
        can_play=True,
        can_expand=True,
        thumbnail=ep.image,
    )


def _track(ident: str, title: str) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=ident,
        media_class=MediaClass.TRACK,
        media_content_type=AUDIO_MPEG,
        title=title,
        can_play=True,
        can_expand=False,
    )


def _day_title(day: date) -> str:
    return f"{day.day} {MONTHS[day.month][:3]}. {day.year} ({WEEKDAYS[day.weekday()]})"


class NowySwiatMediaSource(MediaSource):
    """Browse and play nowyswiat.online."""

    name = TITLE

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the source."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _client(self) -> NowySwiatClient:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise BrowseError("Radio Nowy Świat is not set up")
        return entries[0].runtime_data

    # ------------------------------------------------------------- playing

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Return the proxied audio URL."""
        parts = [unquote(p) for p in (item.identifier or "").split("/")]
        if len(parts) == 2 and parts[0] == "live" and parts[1] in LIVE_STREAMS:
            return PlayMedia(audio_path("live", parts[1]), LIVE_STREAMS[parts[1]][2])
        if parts[0] in ("ep", "part") and len(parts) >= 2:
            try:
                # Fail here, with a readable message, rather than in the proxy.
                episode_parts = await self._client().async_parts(parts[1])
            except (NowySwiatError, BrowseError) as err:
                raise Unresolvable(str(err)) from err
            if parts[0] == "ep" and len(parts) == 2:
                return PlayMedia(audio_path("ep", parts[1]), "audio/mpeg")
            if len(parts) == 3 and parts[2].isdigit() and 1 <= int(parts[2]) <= len(episode_parts):
                return PlayMedia(audio_path("part", parts[1], int(parts[2])), "audio/mpeg")
        raise Unresolvable(f"Not playable: {item.identifier}")

    # ------------------------------------------------------------ browsing

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Return a folder."""
        parts = [unquote(p) for p in item.identifier.split("/")] if item.identifier else []
        try:
            return await self._browse(self._client(), parts)
        except NowySwiatError as err:
            raise BrowseError(str(err)) from err

    async def _browse(self, client: NowySwiatClient, parts: list[str]) -> BrowseMediaSource:
        kind, args = (parts[0], parts[1:]) if parts else ("", [])

        # Older identifiers, kept so saved shortcuts keep working.
        if kind == "latest":
            return await self._browse(client, ["list", "anteny", "all", "-", *args[:1]])
        if kind == "progs":
            return await self._browse(client, ["filter", "anteny", "rbroadcast"])
        if kind == "prog" and args:
            return await self._browse(client, ["list", "anteny", "rbroadcast", *args[:2]])
        if kind == "search" and len(args) >= 2:
            return await self._browse(client, ["list", "anteny", "search", args[1], args[0]])

        if not kind:
            return self._root()
        if kind == "live":
            return self._live()
        if kind == "favs":
            return self._listing(
                lambda page: _ident("favs", page),
                "Ulubione",
                await client.async_favourites(_page(args, 0)),
            )
        if kind == "cat" and args and args[0] in CATEGORIES:
            return await self._category(client, args[0])
        if kind == "list" and len(args) >= 3 and args[0] in CATEGORIES:
            return await self._filtered(client, args[0], args[1], args[2], _page(args, 3))
        if kind == "filter" and len(args) == 2 and args[0] in CATEGORIES:
            return await self._filter_values(client, args[0], args[1])
        if kind == "dates" and args and args[0] in CATEGORIES:
            return self._dates(args[0], args[1:])
        if kind == "ep" and len(args) == 1:
            return await self._episode_folder(client, args[0])
        raise BrowseError(f"Unknown folder: {'/'.join(parts)}")

    def _root(self) -> BrowseMediaSource:
        root = _folder("", TITLE, LOGO)
        root.children = [
            _folder(_ident("favs", 1), "Ulubione"),
            *(_folder(_ident("cat", key), title) for key, (title, _) in CATEGORIES.items()),
            _folder("live", "Na żywo", LOGO),
        ]
        return root

    def _live(self) -> BrowseMediaSource:
        folder = _folder("live", "Na żywo", LOGO)
        folder.children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_ident("live", key),
                media_class=MediaClass.CHANNEL,
                media_content_type=mime,
                title=title,
                can_play=True,
                can_expand=False,
                thumbnail=LOGO,
            )
            for key, (title, _, mime) in LIVE_STREAMS.items()
        ]
        return folder

    async def _category(self, client: NowySwiatClient, cat: str) -> BrowseMediaSource:
        """Filters on top, then the newest episodes of the catalogue."""
        title = CATEGORIES[cat][0]
        progs, authors = await asyncio.gather(
            client.async_filter_values(cat, "rbroadcast"),
            client.async_filter_values(cat, "author"),
        )
        filters = []
        if progs:
            filters.append(_folder(_ident("filter", cat, "rbroadcast"), f"Audycje ({len(progs)})"))
        if authors:
            filters.append(_folder(_ident("filter", cat, "author"), f"Autorzy ({len(authors)})"))
        if cat not in NO_DATE_FILTER:
            filters.append(_folder(_ident("dates", cat), "Według daty"))
        folder = self._listing(
            lambda page: _list_id(cat, page=page),
            title,
            await client.async_category(cat),
        )
        folder.identifier = _ident("cat", cat)
        folder.media_content_id = f"media-source://{DOMAIN}/{folder.identifier}"
        folder.children = [*filters, *(folder.children or [])]
        return folder

    async def _filtered(
        self, client: NowySwiatClient, cat: str, kind: str, value: str, page: int
    ) -> BrowseMediaSource:
        cat_title = CATEGORIES[cat][0]
        if kind == "all":
            title = cat_title
            result = await client.async_category(cat, page)
        elif kind in FILTER_TITLES:
            values = await client.async_filter_values(cat, kind)
            name = next((v.title for v in values if v.slug == value), value)
            title = f"{name} · {cat_title}"
            result = await client.async_category(cat, page, **{kind: value})
        elif kind == "date":
            try:
                day = date.fromisoformat(value)
            except ValueError as err:
                raise BrowseError(f"Bad date: {value}") from err
            title = f"{_day_title(day)} · {cat_title}"
            result = await client.async_category(cat, page, date=value)
        elif kind == "search":
            title = f"Szukaj: {value} · {cat_title}"
            result = await client.async_category(cat, page, search=value)
        else:
            raise BrowseError(f"Unknown filter: {kind}")
        return self._listing(lambda p: _list_id(cat, kind, value, p), title, result)

    async def _filter_values(
        self, client: NowySwiatClient, cat: str, kind: str
    ) -> BrowseMediaSource:
        if kind not in FILTER_TITLES:
            raise BrowseError(f"Unknown filter: {kind}")
        folder = _folder(
            _ident("filter", cat, kind), f"{FILTER_TITLES[kind]} · {CATEGORIES[cat][0]}"
        )
        folder.children = [
            _folder(
                _list_id(cat, kind, v.slug),
                f"{v.title} ({v.count})" if v.count else v.title,
                v.image,
            )
            for v in await client.async_filter_values(cat, kind)
        ]
        return folder

    def _dates(self, cat: str, args: list[str]) -> BrowseMediaSource:
        """Recent days and a year → month → day tree."""
        today = dt_util.now().date()
        cat_title = CATEGORIES[cat][0]
        if not args:
            folder = _folder(_ident("dates", cat), f"Według daty · {cat_title}")
            recent = [today - timedelta(days=n) for n in range(RECENT_DAYS)]
            folder.children = [
                *(_folder(_list_id(cat, "date", d.isoformat()), _day_title(d)) for d in recent),
                *(
                    _folder(_ident("dates", cat, year), str(year))
                    for year in range(today.year, FIRST_YEAR - 1, -1)
                ),
            ]
            return folder
        year = int(args[0])
        if len(args) == 1:
            folder = _folder(_ident("dates", cat, year), f"{year} · {cat_title}")
            last = today.month if year == today.year else 12
            first = 7 if year == FIRST_YEAR else 1
            folder.children = [
                _folder(_ident("dates", cat, year, month), f"{MONTHS[month]} {year}")
                for month in range(last, first - 1, -1)
            ]
            return folder
        month = int(args[1])
        folder = _folder(_ident("dates", cat, year, month), f"{MONTHS[month]} {year} · {cat_title}")
        days = [
            date(year, month, d)
            for d in range(calendar.monthrange(year, month)[1], 0, -1)
            if date(year, month, d) <= today
        ]
        folder.children = [
            _folder(_list_id(cat, "date", d.isoformat()), _day_title(d)) for d in days
        ]
        return folder

    async def _episode_folder(self, client: NowySwiatClient, slug: str) -> BrowseMediaSource:
        episode_parts = await client.async_parts(slug)
        title = episode_parts[0].name if episode_parts else slug
        title = re.sub(r"\s+cz\.\s*\d+$", "", title)
        folder = BrowseMediaSource(
            domain=DOMAIN,
            identifier=_ident("ep", slug),
            media_class=MediaClass.PODCAST,
            media_content_type=AUDIO_MPEG,
            title=title,
            can_play=True,
            can_expand=True,
            children_media_class=MediaClass.TRACK,
        )
        children = []
        if len(episode_parts) > 1:
            children.append(_track(_ident("ep", slug), f"Całość ({len(episode_parts)} części)"))
        children.extend(
            _track(_ident("part", slug, n), part.name or f"Część {n}")
            for n, part in enumerate(episode_parts, 1)
        )
        folder.children = children
        return folder

    def _listing(self, ident, title: str, result: EpisodePage) -> BrowseMediaSource:
        """Return a page of episodes with previous/next page entries."""
        if result.last_page > 1:
            title = f"{title} ({result.page}/{result.last_page})"
        folder = _folder(ident(result.page), title)
        folder.children_media_class = MediaClass.PODCAST
        children: list[BrowseMediaSource] = []
        if result.page > 1:
            children.append(_folder(ident(result.page - 1), "« Poprzednia strona"))
        children.extend(_episode(ep) for ep in result.episodes)
        if result.page < result.last_page:
            children.append(_folder(ident(result.page + 1), "Następna strona »"))
        if not result.episodes:
            children.append(_folder("", "(brak podcastów)"))
        folder.children = children
        return folder

    # ------------------------------------------------------------ searching

    async def async_search_media(
        self, item: MediaSourceItem, query: SearchMediaQuery
    ) -> SearchMedia:
        """Search all podcast catalogues, programmes and authors."""
        text = query.search_query.strip()
        if not text:
            return SearchMedia(result=[])
        client = self._client()
        try:
            pages = await asyncio.gather(
                *(client.async_category(cat, search=text) for cat in SEARCHABLE)
            )
            filters = await asyncio.gather(
                *(
                    client.async_filter_values(cat, kind)
                    for cat in CATEGORIES
                    for kind in FILTER_TITLES
                )
            )
        except NowySwiatError as err:
            raise BrowseError(str(err)) from err

        folded = text.casefold()
        matches: list[BrowseMediaSource] = []
        pairs = [(cat, kind) for cat in CATEGORIES for kind in FILTER_TITLES]
        for (cat, kind), values in zip(pairs, filters, strict=True):
            matches.extend(
                _folder(
                    _list_id(cat, kind, v.slug),
                    f"{v.title} · {FILTER_SINGULAR[kind]}, {CATEGORIES[cat][0]}",
                    v.image,
                )
                for v in values
                if folded in v.title.casefold()
            )
        seen: set[str] = set()
        for cat, result in zip(SEARCHABLE, pages, strict=True):
            for ep in result.episodes:
                if ep.slug not in seen:
                    seen.add(ep.slug)
                    matches.append(_episode(ep))
            if result.last_page > 1:
                matches.append(
                    _folder(
                        _list_id(cat, "search", text, 2),
                        f"Więcej wyników w: {CATEGORIES[cat][0]} »",
                    )
                )
        return SearchMedia(result=matches)


def _page(args: list[str], index: int) -> int:
    try:
        return max(1, int(args[index]))
    except (IndexError, ValueError):
        return 1

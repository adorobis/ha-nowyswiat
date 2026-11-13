# Radio Nowy Świat for Home Assistant

[![hacs][hacs-badge]][hacs-url]
[![Validate][validate-badge]][validate-url]

Unofficial Home Assistant integration for [Radio Nowy Świat](https://nowyswiat.online):
browse and play the patron podcasts and the live stream on any media player
(MusicCast, Sonos, Chromecast, the browser, …) from the Home Assistant media browser.

> **Not affiliated with Radio Nowy Świat.** The station offers no public API; this
> integration reads the same web pages your browser does, logged in with your own
> account. Full-length podcasts need an active patron subscription. If the website
> changes, the integration may stop working until it is updated.

[Po polsku ↓](#po-polsku)

## Features

- **Media source** "Radio Nowy Świat" in the media browser:
  - **Ulubione**: podcasts marked as favourite in your account
  - **Podcasty z anteny**, **Podcasty extra**, **Podcasty extra plus**,
    **Podcasty otwarte**, **Reportaże**: each with the newest episodes and
    folders to filter by **programme** (Audycje), **host** (Autorzy) and **date**
  - **Na żywo**: live radio at 256 kbps MP3, 128 kbps MP3 and 64 kbps AAC
- **Search** across all podcast catalogues, programmes and hosts.
- **Multi-part shows** (e.g. a four-hour morning show published as four files) open
  as a folder: play the whole show as one continuous, seekable stream, or a single part.
- **Audio proxy**: players get a plain local `http://` URL from Home Assistant, so they
  need no website cookies and no HTTPS support. If the podcast server drops the
  connection, the proxy reconnects at the exact byte and the player doesn't notice.
- Polish and English UI translations.

## Requirements

- Home Assistant 2026.11 or newer.
- A [nowyswiat.online](https://nowyswiat.online) account. Without a patron subscription
  the site only offers short previews; the integration then reports that only a
  preview is available instead of playing it.
- Players must be able to reach Home Assistant's local URL
  (Settings → System → Network).

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/adorobis/ha-nowyswiat`,
   type **Integration**.
2. Search for **Radio Nowy Świat**, download it and restart Home Assistant.

[![Open in HACS][my-hacs-badge]][my-hacs-url]

### Manual

Copy `custom_components/nowyswiat` into your `config/custom_components/` folder and
restart Home Assistant.

## Configuration

Settings → Devices & services → **Add integration** → **Radio Nowy Świat**, then log in
with your nowyswiat.online username (or e-mail) and password. If the password is
changed later, Home Assistant asks you to log in again.

## Usage

Open **Media** in the sidebar (or *Browse media* on a player card), pick the player and
go to **Radio Nowy Świat**.

To play from automations, scripts or dashboard buttons use `media_player.play_media`:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room
data:
  media_content_id: media-source://nowyswiat/live/hq
  media_content_type: music
```

| `media_content_id` | Plays |
| --- | --- |
| `media-source://nowyswiat/live/hq` | Live, 256 kbps MP3 (also `live/mp3`, `live/aac`) |
| `media-source://nowyswiat/ep/<slug>` | An episode, all parts in a row |
| `media-source://nowyswiat/part/<slug>/<n>` | Part *n* of an episode |

`<slug>` is the last part of the episode's address on the website, e.g.
`poranna-manna-307` for `https://nowyswiat.online/podcasty/poranna-manna-307`
(reportages: `reportaze/<slug>`).

### For player integrations

Players that push URLs themselves (for example to a DLNA renderer) can get the title,
hosts, cover and duration of an item from the optional provider registered in
`hass.data["media_info_providers"]["nowyswiat"]`
(`async (hass, media_content_id) -> dict | None`, see
[`info.py`](custom_components/nowyswiat/info.py)).

## Privacy and security

- The username and password are stored in the Home Assistant config entry, like any
  other integration's credentials, and are only sent to nowyswiat.online.
- Audio is served at `/api/nowyswiat/audio/…`, which requires Home Assistant
  authentication (players get a signed URL).
- Cover images are relayed at `/api/nowyswiat/image/…` **without** authentication,
  because speakers fetch album art without credentials. It only relays public images
  from nowyswiat.online.

## Not included

*Bez ogródek* is not included: it only contains articles with YouTube videos, nothing an
audio player can play.

## Troubleshooting

Enable debug logging and include the log in an issue:

```yaml
logger:
  logs:
    custom_components.nowyswiat: debug
```

---

## Po polsku

Nieoficjalna integracja Home Assistant dla Radia Nowy Świat. Pozwala przeglądać i
odtwarzać podcasty dla patronów oraz radio na żywo na dowolnym odtwarzaczu (MusicCast,
Sonos, Chromecast, przeglądarka…) z poziomu przeglądarki mediów w Home Assistant.

- **Ulubione**, **Podcasty z anteny / extra / extra plus / otwarte** i **Reportaże**,
  z filtrowaniem po audycji, autorze i dacie, oraz **wyszukiwanie**.
- Audycje w kilku częściach można odtworzyć w całości jako jeden plik z przewijaniem
  albo po jednej części.
- Radio **na żywo** w trzech jakościach.
- Wymagane konto na nowyswiat.online. Pełne podcasty wymagają aktywnego patronatu.

**Instalacja:** HACS → ⋮ → *Custom repositories* → `https://github.com/adorobis/ha-nowyswiat`
(typ *Integration*) → pobierz **Radio Nowy Świat** → restart HA → Ustawienia →
Urządzenia i usługi → *Dodaj integrację* → **Radio Nowy Świat** → zaloguj się.

Integracja nie jest powiązana z Radiem Nowy Świat. Korzysta ze strony internetowej tak
jak przeglądarka, więc zmiany na stronie mogą wymagać jej aktualizacji.

## License

[MIT](LICENSE)

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[validate-badge]: https://github.com/adorobis/ha-nowyswiat/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/adorobis/ha-nowyswiat/actions/workflows/validate.yml
[my-hacs-badge]: https://my.home-assistant.io/badges/hacs_repository.svg
[my-hacs-url]: https://my.home-assistant.io/redirect/hacs_repository/?owner=adorobis&repository=ha-nowyswiat&category=integration

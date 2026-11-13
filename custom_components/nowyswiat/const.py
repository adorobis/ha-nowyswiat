"""Constants for the Radio Nowy Świat integration."""

DOMAIN = "nowyswiat"
AUDIO_VIEW_URL = "/api/nowyswiat/audio"
IMAGE_VIEW_URL = "/api/nowyswiat/image"

# Live channels as published on nowyswiat.online (key -> (title, url, mime)).
LIVE_STREAMS: dict[str, tuple[str, str, str]] = {
    "hq": (
        "Radio Nowy Świat (na żywo, 256 kbps)",
        "https://stream.rcs.revma.com/4md4m0a0fs8uv",
        "audio/mpeg",
    ),
    "mp3": (
        "Radio Nowy Świat (na żywo, 128 kbps)",
        "https://stream.rcs.revma.com/ypqt40u0x1zuv",
        "audio/mpeg",
    ),
    "aac": (
        "Radio Nowy Świat (na żywo, 64 kbps AAC)",
        "https://stream.rcs.revma.com/5ws0kfn1x1zuv",
        "audio/aac",
    ),
}
LOGO = "https://nowyswiat.online/assets/images/mid.jpg"

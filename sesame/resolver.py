"""Convierte lo que escribe el usuario en un stream de audio reproducible.

Seguridad:
- Solo se aceptan enlaces de dominios conocidos (allowlist). Cualquier otra URL se rechaza
  antes de tocar la red: evita SSRF (p. ej. 169.254.169.254, localhost) y el extractor
  genérico de yt-dlp.
- El stream final debe ser http(s); FFmpeg además limita los protocolos (ver audio.py).
- Se rechazan directos (sin duración) y pistas más largas que el máximo configurado.

Diseño: cada consulta genera un plan ordenado de intentos (estrategias); se prueba uno
tras otro hasta que alguno devuelve audio válido.
"""

from __future__ import annotations

import enum
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger("sesame.resolver")

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"})
SPOTIFY_HOSTS = frozenset({"open.spotify.com"})
SOUNDCLOUD_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"})
_SPOTIFY_TRACK = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?track/[A-Za-z0-9]{10,40}/?$")
_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Kind(enum.Enum):
    TEXT = "texto"
    YOUTUBE = "YouTube"
    SPOTIFY = "Spotify"
    SOUNDCLOUD = "SoundCloud"
    BANDCAMP = "Bandcamp"


class ResolveError(Exception):
    """Error con un mensaje apto para el usuario."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class TrackTooLong(ResolveError):
    def __init__(self, seconds: int, limit: int) -> None:
        super().__init__(f"Esa pista dura {seconds // 60} min; el máximo es {limit // 60} min.")
        self.seconds = seconds


@dataclass(frozen=True)
class Attempt:
    source: str   # etiqueta visible: SoundCloud, YouTube...
    target: str   # URL o "scsearch1:..." para yt-dlp


@dataclass(frozen=True)
class ResolvedTrack:
    title: str
    stream_url: str
    source: str
    duration: int
    headers: dict[str, str] = field(default_factory=dict)


def clean_text(text: str, limit: int) -> str:
    text = _CONTROL.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()[:limit]


def _host_ok(host: str, allowed: frozenset[str]) -> bool:
    return host in allowed


def classify(query: str) -> tuple[Kind, str]:
    """Devuelve el tipo de consulta y la consulta normalizada. Lanza ResolveError si no se admite."""
    query = query.strip().strip("<>")  # Discord permite <url> para no generar vista previa
    lowered = query.lower()
    if not lowered.startswith(("http://", "https://")):
        if "://" in lowered or lowered.startswith(("file:", "data:", "javascript:", "spotify:")):
            raise ResolveError("Ese tipo de enlace no está permitido.")
        return Kind.TEXT, query

    try:
        parts = urlsplit(query)
        port = parts.port
    except ValueError:
        raise ResolveError("Ese enlace no es válido.") from None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.username or parts.password or port not in (None, 80, 443) or not host:
        raise ResolveError("Ese enlace no está permitido.")

    if _host_ok(host, YOUTUBE_HOSTS):
        return Kind.YOUTUBE, query
    if _host_ok(host, SPOTIFY_HOSTS):
        if not _SPOTIFY_TRACK.match(parts.path):
            raise ResolveError("De Spotify solo acepto enlaces de **canciones** (no álbumes ni playlists).")
        return Kind.SPOTIFY, f"https://open.spotify.com{parts.path}"
    if _host_ok(host, SOUNDCLOUD_HOSTS):
        return Kind.SOUNDCLOUD, query
    if host.endswith(".bandcamp.com") and host.count(".") == 2:
        return Kind.BANDCAMP, query
    raise ResolveError("Solo acepto enlaces de SoundCloud, YouTube, Spotify o Bandcamp, o el nombre de la canción.")


def plan(kind: Kind, query: str, oembed_title: str | None) -> list[Attempt]:
    """Orden de fuentes a probar según el tipo de consulta (cadena de estrategias)."""
    if kind is Kind.TEXT:
        return [Attempt("SoundCloud", f"scsearch1:{query}"), Attempt("YouTube", f"ytsearch1:{query}")]
    if kind is Kind.YOUTUBE:
        attempts = [Attempt("YouTube", query)]
        if oembed_title:
            attempts.append(Attempt("SoundCloud", f"scsearch1:{oembed_title}"))
        return attempts
    if kind is Kind.SPOTIFY:
        if not oembed_title:
            return []
        return [
            Attempt("SoundCloud", f"scsearch1:{oembed_title}"),
            Attempt("YouTube", f"ytsearch1:{oembed_title}"),
        ]
    return [Attempt(kind.value, query)]


def validate_info(info: dict | None, source: str, max_seconds: int) -> ResolvedTrack:
    """Comprueba el resultado de yt-dlp y lo convierte en ResolvedTrack."""
    if not info:
        raise ResolveError("sin resultado")
    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("sin resultados")
        info = entries[0]
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise ResolveError("No reproduzco transmisiones en directo.")
    duration = int(info.get("duration") or 0)
    if duration <= 0:
        raise ResolveError("No pude saber cuánto dura esa pista.")
    if duration > max_seconds:
        raise TrackTooLong(duration, max_seconds)
    stream_url = info.get("url") or ""
    if not stream_url.lower().startswith(("https://", "http://")):
        raise ResolveError("La fuente no devolvió un stream http(s).")
    headers = {
        str(k): str(v)
        for k, v in (info.get("http_headers") or {}).items()
        if "\r" not in str(k) + str(v) and "\n" not in str(k) + str(v)
    }
    title = clean_text(str(info.get("title") or "Sin título"), 100) or "Sin título"
    return ResolvedTrack(title=title, stream_url=stream_url, source=source, duration=duration, headers=headers)


def fetch_oembed_title(kind: Kind, url: str, *, timeout: float = 8.0) -> str | None:
    """Lee 'artista canción' de un enlace de YouTube o Spotify ya validado (endpoint fijo)."""
    if kind is Kind.YOUTUBE:
        endpoint = f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}"
    elif kind is Kind.SPOTIFY:
        endpoint = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
    else:
        return None
    try:
        req = Request(endpoint, headers={"User-Agent": "Mozilla/5.0 (SesameBot)"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - endpoint fijo de la allowlist
            data = json.loads(resp.read(64_000).decode("utf-8", errors="replace"))
    except Exception as exc:
        log.info("oEmbed %s falló: %s", kind.value, exc)
        return None
    title = clean_text(str(data.get("title") or ""), 150)
    author = clean_text(str(data.get("author_name") or ""), 60)
    if author and author.lower() not in title.lower() and not author.lower().endswith(" - topic"):
        title = f"{author} {title}"
    return title or None


def ydl_options() -> dict:
    return {
        "format": "bestaudio/best",
        "noplaylist": True,
        "playlistend": 1,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "cachedir": False,
        "check_formats": False,
        "concurrent_fragment_downloads": 1,
    }


class AudioResolver:
    """Resuelve consultas de forma síncrona (se ejecuta en un hilo acotado)."""

    def __init__(self, max_track_seconds: int, max_query_chars: int) -> None:
        self.max_track_seconds = max_track_seconds
        self.max_query_chars = max_query_chars

    def resolve(self, raw_query: str) -> ResolvedTrack:
        import yt_dlp  # importación diferida: los tests no necesitan red

        query = clean_text(raw_query, self.max_query_chars)
        if not query:
            raise ResolveError("Escribe el nombre de una canción o un enlace.")
        kind, query = classify(query)
        title = fetch_oembed_title(kind, query) if kind in (Kind.YOUTUBE, Kind.SPOTIFY) else None
        attempts = plan(kind, query, title)
        if not attempts:
            raise ResolveError("No pude leer el nombre de esa canción.")

        too_long: TrackTooLong | None = None
        for attempt in attempts:
            try:
                with yt_dlp.YoutubeDL(ydl_options()) as ydl:
                    info = ydl.extract_info(attempt.target, download=False)
                track = validate_info(info, attempt.source, self.max_track_seconds)
                log.info("Audio encontrado en %s: %s (%ss)", attempt.source, track.title, track.duration)
                return track
            except TrackTooLong as exc:
                too_long = exc
            except Exception as exc:  # una fuente caída no debe tumbar las demás
                log.info("Fuente %s falló: %s", attempt.source, str(exc)[:200])
        if too_long is not None:
            raise too_long
        raise ResolveError(
            "No encontré ese audio. Prueba con *artista + canción* o con un enlace de SoundCloud."
        )

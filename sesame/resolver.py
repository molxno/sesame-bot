"""Convierte lo que escribe el usuario en un stream de audio reproducible.

Seguridad:
- Solo se aceptan enlaces de dominios conocidos (allowlist). Cualquier otra URL se rechaza
  antes de tocar la red: evita SSRF (p. ej. 169.254.169.254, localhost) y el extractor
  genérico de yt-dlp.
- El stream final debe ser http(s); FFmpeg además limita los protocolos (ver audio.py).
- Se rechazan directos (sin duración) y pistas más largas que el máximo configurado.

Diseño: cada consulta genera un plan ordenado de intentos (estrategias); se prueba uno
tras otro hasta que alguno devuelve audio válido. En SoundCloud se piden varios resultados,
se descartan adelantos de 30 s, se ordenan por parecido con lo pedido y se prueban hasta
encontrar uno sin DRM.
"""

from __future__ import annotations

import enum
import html
import json
import logging
import re
import unicodedata
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
    source: str           # etiqueta visible: SoundCloud, YouTube...
    target: str           # URL directa o texto a buscar
    search: bool = False  # True: `target` es texto de búsqueda


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


def plan(kind: Kind, query: str, link_title: str | None) -> list[Attempt]:
    """Orden de fuentes a probar según el tipo de consulta (cadena de estrategias).

    YouTube bloquea los servidores en la nube, así que queda como último recurso.
    """
    if kind is Kind.TEXT:
        return [Attempt("SoundCloud", query, search=True), Attempt("YouTube", query, search=True)]
    if kind is Kind.YOUTUBE:
        attempts = [Attempt("SoundCloud", link_title, search=True)] if link_title else []
        return attempts + [Attempt("YouTube", query)]
    if kind is Kind.SPOTIFY:
        if not link_title:
            return []
        return [Attempt("SoundCloud", link_title, search=True), Attempt("YouTube", link_title, search=True)]
    return [Attempt(kind.value, query)]


# --- Búsqueda y ranking ---

_NOISE = re.compile(
    r"\((?:[^)]*)\)|\[(?:[^\]]*)\]|\b(?:official|oficial|video|videoclip|lyrics?|letra|audio|hd|hq|4k)\b",
    re.IGNORECASE,
)
_STOPWORDS = frozenset({"de", "la", "el", "los", "las", "y", "a", "en", "the", "of", "and", "ft", "feat", "con"})
_UNWANTED = frozenset(
    {"cover", "karaoke", "instrumental", "remix", "live", "vivo", "intro", "slowed", "sped", "8d", "pista",
     "nightcore", "reverb", "acapella", "mashup", "edit", "tutorial", "mix"}
)
PREVIEW_SECONDS = 31  # SoundCloud entrega adelantos de 30 s de canciones de pago


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t not in _STOPWORDS}


def search_terms(title: str) -> str:
    """Limpia títulos de YouTube/Spotify ('Artista - Canción (Video Oficial)') para buscar."""
    return clean_text(_NOISE.sub(" ", title).replace(" - ", " "), 150)


def match_score(query: str, title: str, uploader: str = "") -> float:
    """Qué tanto se parece un resultado a lo pedido (0..1, con penalización por versiones raras)."""
    wanted = tokens(query)
    if not wanted:
        return 0.0
    found = tokens(f"{title} {uploader}")
    score = len(wanted & found) / len(wanted)
    extras = (found & _UNWANTED) - wanted
    return score - 0.15 * len(extras)


def rank_candidates(query: str, entries: list[dict], max_seconds: int) -> list[dict]:
    """Quita adelantos, demasiado largos y sin URL; ordena por parecido (empate: orden de SoundCloud)."""
    usable = []
    for index, entry in enumerate(entries):
        if not entry or not entry.get("url"):
            continue
        duration = entry.get("duration")
        if duration is not None and (duration <= PREVIEW_SECONDS or duration > max_seconds):
            continue
        score = match_score(query, str(entry.get("title") or ""), str(entry.get("uploader") or ""))
        usable.append((score, index, entry))
    usable.sort(key=lambda item: (-item[0], item[1]))
    return [entry for score, _, entry in usable if score >= MIN_MATCH]


MIN_MATCH = 0.6  # al menos ~2/3 de las palabras pedidas


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


def _http_get(url: str, *, timeout: float, limit: int) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - solo URLs de la allowlist
        return resp.read(limit).decode("utf-8", errors="replace")


def parse_spotify_page(page: str) -> str | None:
    """'Canción Artista' desde las etiquetas og: de la página pública de una canción."""
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    desc = re.search(r'<meta property="og:description" content="([^"]*)"', page)
    if not title:
        return None
    song = html.unescape(title.group(1))
    artist = html.unescape(desc.group(1)).split("·")[0].strip() if desc else ""
    return clean_text(f"{song} {artist}", 150) or None


def fetch_link_title(kind: Kind, url: str, *, timeout: float = 8.0) -> str | None:
    """Nombre de la canción (y artista) de un enlace de YouTube o Spotify ya validado."""
    try:
        if kind is Kind.SPOTIFY:
            found = parse_spotify_page(_http_get(url, timeout=timeout, limit=600_000))
            if found:
                return found
            endpoint = f"https://open.spotify.com/oembed?url={quote(url, safe='')}"
        elif kind is Kind.YOUTUBE:
            endpoint = f"https://www.youtube.com/oembed?format=json&url={quote(url, safe='')}"
        else:
            return None
        data = json.loads(_http_get(endpoint, timeout=timeout, limit=64_000))
    except Exception as exc:
        log.info("No pude leer el título de %s: %s", kind.value, exc)
        return None
    title = clean_text(str(data.get("title") or ""), 150)
    author = clean_text(str(data.get("author_name") or ""), 60)
    if author and author.lower() not in title.lower() and not author.lower().endswith(" - topic"):
        title = f"{author} {title}"
    return search_terms(title) or None


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

    SEARCH_RESULTS = 10   # resultados que se piden a SoundCloud (búsqueda ligera, sin streams)
    MAX_TRIES = 4         # candidatos que se intentan abrir como máximo

    def __init__(self, max_track_seconds: int, max_query_chars: int) -> None:
        self.max_track_seconds = max_track_seconds
        self.max_query_chars = max_query_chars

    def resolve(self, raw_query: str) -> ResolvedTrack:
        query = clean_text(raw_query, self.max_query_chars)
        if not query:
            raise ResolveError("Escribe el nombre de una canción o un enlace.")
        kind, query = classify(query)
        title = fetch_link_title(kind, query) if kind in (Kind.YOUTUBE, Kind.SPOTIFY) else None
        attempts = plan(kind, query, title)
        if not attempts:
            raise ResolveError("No pude leer el nombre de esa canción.")

        too_long: TrackTooLong | None = None
        for attempt in attempts:
            try:
                track = self._try(attempt)
                log.info("Audio encontrado en %s: %s (%ss)", attempt.source, track.title, track.duration)
                return track
            except TrackTooLong as exc:
                too_long = exc
            except Exception as exc:  # una fuente caída no debe tumbar las demás
                log.info("Fuente %s falló: %s", attempt.source, str(exc)[:200])
        if too_long is not None:
            raise too_long
        raise ResolveError(
            "No encontré una versión reproducible. Prueba con *canción + artista* o con un enlace de SoundCloud."
        )

    def _try(self, attempt: Attempt) -> ResolvedTrack:
        if attempt.search and attempt.source == "SoundCloud":
            return self._soundcloud(attempt.target)
        target = f"ytsearch1:{attempt.target}" if attempt.search else attempt.target
        return validate_info(self._extract(target), attempt.source, self.max_track_seconds)

    def _soundcloud(self, query: str) -> ResolvedTrack:
        found = self._extract(f"scsearch{self.SEARCH_RESULTS}:{query}", flat=True) or {}
        candidates = rank_candidates(query, list(found.get("entries") or []), self.max_track_seconds)
        if not candidates:
            raise ResolveError("sin coincidencias en SoundCloud")
        last_error: Exception | None = None
        for entry in candidates[: self.MAX_TRIES]:
            try:  # las subidas oficiales suelen tener DRM: se prueba la siguiente
                return validate_info(self._extract(entry["url"]), "SoundCloud", self.max_track_seconds)
            except TrackTooLong:
                raise
            except Exception as exc:
                last_error = exc
                log.debug("Candidato descartado (%s): %s", entry.get("title"), str(exc)[:120])
        raise ResolveError(f"ningún candidato reproducible: {last_error}")

    @staticmethod
    def _extract(target: str, *, flat: bool = False) -> dict | None:
        import yt_dlp  # importación diferida: los tests no necesitan red

        options = ydl_options()
        if flat:
            options.update(extract_flat="in_playlist", ignoreerrors=True)
            options.pop("playlistend", None)
            options.pop("noplaylist", None)
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(target, download=False)

"""Fuentes de audio: streams vía FFmpeg, TTS y el mezclador que atenúa la música."""

from __future__ import annotations

import io
import logging
import re
import shlex
import threading
from collections import deque

import audioop
import discord
from gtts import gTTS

from .resolver import ResolvedTrack

log = logging.getLogger("sesame.audio")

# Solo protocolos de red: FFmpeg nunca abrirá file:, concat:, pipe:, etc. desde una URL remota.
_PROTOCOLS = "https,http,tls,tcp,crypto,hls"
_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


def mix_duck(music: bytes, voice: bytes, duck: float) -> bytes:
    """Mezcla voz a volumen pleno con música atenuada (PCM s16le estéreo)."""
    n = max(len(music), len(voice))
    n -= n % 2
    if n <= 0:
        return voice or music
    music = music[:n].ljust(n, b"\x00")
    voice = voice[:n].ljust(n, b"\x00")
    return audioop.add(audioop.mul(music, 2, duck), voice, 2)


class StreamSource(discord.FFmpegPCMAudio):
    """FFmpeg con protocolos restringidos, tope de duración y captura de errores."""

    def __init__(self, track: ResolvedTrack, max_seconds: int) -> None:
        self.errors = io.BytesIO()
        before = (
            f"-nostdin -loglevel error -protocol_whitelist {_PROTOCOLS} "
            "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 15000000"
        )
        if track.headers:
            header_blob = "".join(f"{k}: {v}\r\n" for k, v in track.headers.items())
            before += f" -headers {shlex.quote(header_blob)}"
        super().__init__(
            track.stream_url,
            before_options=before,
            options=f"-vn -sn -dn -threads 1 -t {int(max_seconds)}",
            stderr=self.errors,
        )

    def failed(self) -> str | None:
        text = self.errors.getvalue().decode("utf-8", errors="replace").strip()
        if not text:
            return None
        lowered = text.lower()
        if any(w in lowered for w in ("error", "invalid", "403", "404", "denied", "not allowed")):
            return text[:500]
        return None


def tone_source() -> discord.AudioSource:
    return discord.FFmpegPCMAudio(
        "sine=frequency=440:duration=3", before_options="-nostdin -f lavfi", options="-vn -threads 1"
    )


def tts_text(author: str, content: str, max_chars: int) -> str:
    content = _URL_IN_TEXT.sub("enlace", content)
    content = " ".join(content.split())[:max_chars]
    return f"{author[:32]} dice: {content}" if content else ""


def build_tts_source(text: str, lang: str) -> discord.AudioSource:
    """Genera la voz (bloqueante: llamar desde un hilo)."""
    buffer = io.BytesIO()
    gTTS(text=text, lang=lang, timeout=10).write_to_fp(buffer)
    if buffer.tell() > 2_000_000:  # 200 caracteres nunca deberían pasar de ~100 KB
        raise ValueError("TTS demasiado grande")
    buffer.seek(0)
    return discord.FFmpegPCMAudio(buffer, pipe=True, before_options="-nostdin -loglevel error", options="-vn")


class TtsQueue:
    """Cola de voces pendientes compartida entre el bucle asyncio y el hilo de audio."""

    def __init__(self, max_pending: int) -> None:
        self.max_pending = max_pending
        self._items: deque[discord.AudioSource] = deque()
        self._lock = threading.Lock()

    def push(self, source: discord.AudioSource) -> bool:
        with self._lock:
            if len(self._items) >= self.max_pending:
                accepted = False
            else:
                self._items.append(source)
                accepted = True
        if not accepted:
            _safe_cleanup(source)
        return accepted

    def pop(self) -> discord.AudioSource | None:
        with self._lock:
            return self._items.popleft() if self._items else None

    def is_full(self) -> bool:
        with self._lock:
            return len(self._items) >= self.max_pending

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            items, self._items = list(self._items), deque()
        for item in items:
            _safe_cleanup(item)


class MixerSource(discord.AudioSource):
    """Reproduce la música y, cuando hay TTS en cola, lo mezcla bajando la canción."""

    def __init__(self, music: discord.AudioSource | None, tts: TtsQueue, duck: float) -> None:
        self.music = music
        self.tts = tts
        self.duck = duck
        self._current: discord.AudioSource | None = None
        self._lock = threading.Lock()

    @property
    def speaking(self) -> bool:
        with self._lock:
            return self._current is not None

    def _tts_frame(self) -> bytes:
        for _ in range(self.tts.max_pending + 1):
            with self._lock:
                if self._current is None:
                    self._current = self.tts.pop()
                current = self._current
            if current is None:
                return b""
            data = current.read()
            if data:
                return data
            _safe_cleanup(current)
            with self._lock:
                self._current = None
        return b""

    def read(self) -> bytes:
        voice = self._tts_frame()
        music = self.music.read() if self.music is not None else b""
        if voice and music:
            return mix_duck(music, voice, self.duck)
        return voice or music

    def is_opus(self) -> bool:
        return False

    def drop_tts(self) -> None:
        with self._lock:
            current, self._current = self._current, None
        if current is not None:
            _safe_cleanup(current)

    def cleanup(self) -> None:
        self.drop_tts()
        if self.music is not None:
            _safe_cleanup(self.music)
            self.music = None


def _safe_cleanup(source: discord.AudioSource) -> None:
    try:
        source.cleanup()
    except Exception:
        log.debug("cleanup de fuente falló", exc_info=True)

"""Configuración leída una sola vez desde variables de entorno.

Todos los límites de consumo viven aquí para poder ajustarlos en Azure sin tocar código.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    token: str
    version: str = "dev"
    prefix: str = "!"

    # Servidores
    max_guilds: int = 10                 # si lo invitan a más servidores, se sale
    allowed_guild_ids: frozenset[int] = field(default_factory=frozenset)  # vacío = cualquiera
    show_invite: bool = True

    # Voz y cola
    max_voice_guilds: int = 3            # servidores reproduciendo a la vez
    max_queue: int = 20
    max_track_seconds: int = 900         # 15 min
    max_query_chars: int = 200
    idle_leave_seconds: int = 300        # sale si no suena nada en 5 min
    empty_leave_seconds: int = 15        # sale si el canal se queda sin personas
    dave_ready_timeout: float = 6.0

    # Extracción de audio (yt-dlp): hilos acotados
    extract_workers: int = 2
    extract_timeout: int = 40
    extract_max_pending: int = 6

    # Lectura del chat (TTS)
    tts_lang: str = "es"
    tts_max_chars: int = 200
    tts_duck: float = 0.22               # volumen de la música mientras habla el TTS
    tts_workers: int = 2
    tts_max_pending: int = 5
    tts_user_rate: int = 3               # mensajes leídos por usuario...
    tts_user_window: int = 15            # ...en esta ventana (s)
    tts_guild_rate: int = 12
    tts_guild_window: int = 60

    # Anti-spam de comandos
    cmd_user_rate: int = 6
    cmd_user_window: int = 20
    play_user_rate: int = 3
    play_user_window: int = 30
    play_guild_rate: int = 10
    play_guild_window: int = 60

    # Logs
    log_level: str = "INFO"
    log_file: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        default_log_file = "bot.log" if os.name == "nt" else None
        return cls(
            token=os.getenv("DISCORD_TOKEN", "").strip(),
            version=os.getenv("SESAME_VERSION", "dev").strip() or "dev",
            prefix=(os.getenv("COMMAND_PREFIX", "!").strip() or "!")[:3],
            max_guilds=_int("MAX_GUILDS", 10, minimum=1, maximum=100),
            allowed_guild_ids=_ids("ALLOWED_GUILD_IDS"),
            show_invite=_bool("SHOW_INVITE", True),
            max_voice_guilds=_int("MAX_VOICE_GUILDS", 3, minimum=1, maximum=20),
            max_queue=_int("MAX_QUEUE", 20, minimum=1, maximum=100),
            max_track_seconds=_int("MAX_TRACK_SECONDS", 900, minimum=30, maximum=3 * 3600),
            max_query_chars=_int("MAX_QUERY_CHARS", 200, minimum=20, maximum=500),
            idle_leave_seconds=_int("IDLE_LEAVE_SECONDS", 300, minimum=30, maximum=3600),
            empty_leave_seconds=_int("EMPTY_LEAVE_SECONDS", 15, minimum=5, maximum=600),
            extract_workers=_int("EXTRACT_WORKERS", 2, minimum=1, maximum=4),
            extract_timeout=_int("EXTRACT_TIMEOUT", 40, minimum=10, maximum=120),
            extract_max_pending=_int("EXTRACT_MAX_PENDING", 6, minimum=1, maximum=30),
            tts_lang=(os.getenv("TTS_LANG", "es").strip() or "es")[:5],
            tts_max_chars=_int("TTS_MAX_CHARS", 200, minimum=20, maximum=400),
            tts_duck=_float("TTS_DUCK", 0.22),
            tts_workers=_int("TTS_WORKERS", 2, minimum=1, maximum=4),
            tts_max_pending=_int("MAX_TTS_PENDING", 5, minimum=1, maximum=20),
            cmd_user_rate=_int("CMD_USER_RATE", 6, minimum=1, maximum=60),
            play_user_rate=_int("PLAY_USER_RATE", 3, minimum=1, maximum=30),
            play_guild_rate=_int("PLAY_GUILD_RATE", 10, minimum=1, maximum=100),
            log_level=(os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"),
            log_file=os.getenv("LOG_FILE", default_log_file) or None,
        )

    def guild_allowed(self, guild_id: int) -> bool:
        return not self.allowed_guild_ids or guild_id in self.allowed_guild_ids

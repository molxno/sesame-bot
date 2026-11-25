import asyncio
import io
import logging
import os
import shlex
import struct
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import audioop
import discord
import yt_dlp
from discord.ext import commands
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import VoiceConnectionState
from dotenv import load_dotenv
from gtts import gTTS

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

TTS_LANG = "es"
TTS_MAX_CHARS = 200
TTS_DUCK = 0.22  # volumen de la música (0-1) mientras habla el TTS
PREFIX = "!"
EMPTY_LEAVE_DELAY = 15  # segundos solo en el canal antes de salir

# --- Límites de consumo (protegen el crédito de Azure) ---
# Cada límite se puede ajustar con una variable de entorno del mismo nombre.
def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default

MAX_VOICE_GUILDS = _env_int("MAX_VOICE_GUILDS", 3)        # servidores con voz activa a la vez
MAX_QUEUE = _env_int("MAX_QUEUE", 20)                     # canciones en cola por servidor
MAX_TRACK_SECONDS = _env_int("MAX_TRACK_SECONDS", 900)    # duración máxima por canción (15 min)
IDLE_LEAVE_SECONDS = _env_int("IDLE_LEAVE_SECONDS", 300)  # sale de voz tras 5 min sin sonar nada
MAX_TTS_PENDING = _env_int("MAX_TTS_PENDING", 5)          # mensajes de TTS en espera por servidor
PLAY_COOLDOWN_SECONDS = _env_int("PLAY_COOLDOWN_SECONDS", 5)
DAVE_READY_TIMEOUT = 6  # segundos máximos esperando el cifrado de voz antes de emitir
BOT_COLOR = 0xC4A35A
BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    connect=True,
    speak=True,
    use_voice_activation=True,
    use_application_commands=True,
)

log = logging.getLogger("bot")


class _RedactTokenFilter(logging.Filter):
    """Evita que el token acabe en bot.log o en consola."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not TOKEN:
            return True
        try:
            text = record.getMessage()
        except Exception:
            return True
        if TOKEN in text:
            record.msg = text.replace(TOKEN, "[REDACTED]")
            record.args = ()
        return True


def setup_logging() -> None:
    redact = _RedactTokenFilter()
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    production = bool(os.getenv("RAILWAY_ENVIRONMENT"))
    level_name = os.getenv("LOG_LEVEL", "INFO" if production else "DEBUG").upper()
    level = getattr(logging, level_name, logging.INFO)
    voice_level = logging.DEBUG if not production else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if not production:
        file_handler = logging.FileHandler("bot.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redact)
        root.addHandler(file_handler)

    if sys.stdout is not None:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO if not production else level)
        console.setFormatter(fmt)
        console.addFilter(redact)
        root.addHandler(console)

    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.INFO)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("discord.voice_state").setLevel(voice_level)
    logging.getLogger("discord.voice_client").setLevel(voice_level)
    logging.getLogger("discord.player").setLevel(voice_level)


def _patch_davey_020() -> None:
    """discord.py 2.7.1 no llama prepare/execute_transition de davey 0.2.0."""
    try:
        import davey
    except ImportError:
        log.warning("davey no está instalado; la voz E2EE no funcionará")
        return

    parts = davey.__version__.split(".")
    ver = tuple(int(p) for p in parts[:2] if p.isdigit())
    if ver < (0, 2):
        log.info("davey %s: no hace falta el puente DAVE", davey.__version__)
        return

    orig_ready = DiscordVoiceWebSocket.send_transition_ready
    orig_execute = VoiceConnectionState._execute_transition
    orig_binary = DiscordVoiceWebSocket.received_binary_message

    async def send_transition_ready(self, transition_id: int):
        state = self._connection
        version = state.dave_pending_transitions.get(transition_id)
        session = state.dave_session
        if session is not None and version is not None:
            try:
                session.prepare_transition(transition_id, version)
                log.debug("DAVE prepare_transition id=%s version=%s", transition_id, version)
            except Exception:
                log.exception("DAVE prepare_transition(%s) falló", transition_id)
        await orig_ready(self, transition_id)

    async def execute_transition(self, transition_id: int):
        await orig_execute(self, transition_id)
        session = self.dave_session
        if session is not None and transition_id != 0:
            try:
                session.execute_transition(transition_id)
                log.debug("DAVE execute_transition id=%s", transition_id)
            except Exception:
                log.exception("DAVE execute_transition(%s) falló", transition_id)

    async def received_binary_message(self, msg: bytes):
        op = msg[2] if len(msg) > 2 else None
        transition_id = struct.unpack_from(">H", msg, 3)[0] if len(msg) >= 5 else None
        await orig_binary(self, msg)
        session = self._connection.dave_session
        if (
            session is not None
            and op in (
                DiscordVoiceWebSocket.MLS_ANNOUNCE_COMMIT_TRANSITION,
                DiscordVoiceWebSocket.MLS_WELCOME,
            )
            and transition_id == 0
        ):
            try:
                session.prepare_transition(0, self._connection.dave_protocol_version)
                log.debug("DAVE prepare_transition id=0 (commit/welcome)")
            except Exception:
                log.exception("DAVE prepare_transition(0) falló")

    DiscordVoiceWebSocket.send_transition_ready = send_transition_ready  # type: ignore[method-assign]
    VoiceConnectionState._execute_transition = execute_transition  # type: ignore[method-assign]
    DiscordVoiceWebSocket.received_binary_message = received_binary_message  # type: ignore[method-assign]
    log.info("Puente DAVE activado para davey %s (discord.py 2.7.1 no llama prepare_transition)", davey.__version__)


def _dave_snapshot(vc: discord.VoiceClient) -> str:
    conn = getattr(vc, "_connection", None)
    session = getattr(conn, "dave_session", None) if conn is not None else None
    if conn is None:
        return "sin _connection"
    if session is None:
        return (
            f"mode={getattr(conn, 'mode', '?')} dave_ver={getattr(conn, 'dave_protocol_version', '?')} "
            f"session=None can_encrypt={getattr(conn, 'can_encrypt', False)}"
        )
    return (
        f"mode={getattr(conn, 'mode', '?')} dave_ver={getattr(conn, 'dave_protocol_version', '?')} "
        f"ready={getattr(session, 'ready', None)} sess_can_encrypt={getattr(session, 'can_encrypt', None)} "
        f"epoch={getattr(session, 'epoch', None)} conn_can_encrypt={conn.can_encrypt}"
    )


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

async def _wait_dave_ready(vc: discord.VoiceClient, timeout: float = DAVE_READY_TIMEOUT) -> None:
    """Espera a que el cifrado E2EE (DAVE) esté listo; si se emite antes, Discord descarta el audio."""
    conn = getattr(vc, "_connection", None)
    if conn is None or not getattr(conn, "dave_protocol_version", 0):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        session = getattr(conn, "dave_session", None)
        if session is not None and getattr(session, "ready", False) and getattr(conn, "can_encrypt", False):
            return
        await asyncio.sleep(0.25)
    log.warning("DAVE no quedó listo en %ss; emito de todos modos: %s", timeout, _dave_snapshot(vc))


YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    # YouTube bloquea las IPs de servidores en la nube ("Sign in to confirm you're not a bot"),
    # así que las búsquedas por texto van a SoundCloud.
    "default_search": "scsearch1",
    "no_warnings": True,
    "socket_timeout": 15,
    "cachedir": False,
    "concurrent_fragment_downloads": 1,
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn -threads 1",
}

tts_channels: dict[int, int] = {}
players: dict[int, "GuildPlayer"] = {}
_leave_tasks: dict[int, asyncio.Task] = {}


@dataclass
class Track:
    title: str
    kind: str  # music | tts | test
    make_source: Callable[[], discord.AudioSource]


def _mix_duck(music: bytes, voice: bytes, duck: float) -> bytes:
    """Mezcla voz a volumen pleno con música atenuada (PCM s16le)."""
    n = max(len(music), len(voice))
    n -= n % 2
    if n <= 0:
        return voice or music
    if len(music) < n:
        music = music + b"\x00" * (n - len(music))
    else:
        music = music[:n]
    if len(voice) < n:
        voice = voice + b"\x00" * (n - len(voice))
    else:
        voice = voice[:n]
    return audioop.add(audioop.mul(music, 2, duck), voice, 2)


class InterruptiblePCMSource(discord.AudioSource):
    """Reproduce música y, si hay TTS, lo mezcla atenuando la canción (sin pausarla)."""

    def __init__(self, player: "GuildPlayer", music: discord.AudioSource | None):
        self.player = player
        self.music = music
        self._tts: discord.AudioSource | None = None

    def _next_tts_frame(self) -> bytes:
        with self.player._tts_lock:
            if self._tts is None and self.player.tts_pending:
                self._tts = self.player.tts_pending.popleft()
                log.debug("TTS: atenúa música")
            tts = self._tts
        if tts is None:
            return b""
        data = tts.read()
        if data:
            return data
        tts.cleanup()
        with self.player._tts_lock:
            self._tts = None
        log.debug("TTS: volumen de música restaurado")
        return self._next_tts_frame()

    def read(self) -> bytes:
        tts_data = self._next_tts_frame()
        music_data = self.music.read() if self.music is not None else b""
        if tts_data and music_data:
            return _mix_duck(music_data, tts_data, TTS_DUCK)
        return tts_data or music_data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        if self._tts is not None:
            self._tts.cleanup()
            self._tts = None
        if self.music is not None:
            self.music.cleanup()
            self.music = None

    def drop_tts(self) -> None:
        if self._tts is not None:
            try:
                self._tts.cleanup()
            except Exception:
                pass
            self._tts = None


class GuildPlayer:
    """Cola de música por servidor; el TTS se mezcla atenuando la canción."""

    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.now_playing: Track | None = None
        self.volume: float = 1.0
        self.tts_pending: deque[discord.AudioSource] = deque()
        self._tts_lock = threading.Lock()
        self._wakeup = asyncio.Event()
        self._mixer: InterruptiblePCMSource | None = None
        self.announce_channel_id: int | None = None
        self.task = asyncio.create_task(self._loop())

    async def _announce(self, text: str) -> None:
        if self.announce_channel_id is None:
            return
        channel = self.guild.get_channel(self.announce_channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        try:
            await channel.send(text)
        except Exception:
            log.debug("No pude avisar en el canal de texto")

    def _has_tts(self) -> bool:
        with self._tts_lock:
            return bool(self.tts_pending) or (self._mixer is not None and self._mixer._tts is not None)

    def enqueue(self, track: Track) -> bool:
        if track.kind == "music" and len(self.queue) >= MAX_QUEUE:
            return False
        self.queue.append(track)
        self._wakeup.set()
        return True

    def speak(self, source: discord.AudioSource) -> None:
        with self._tts_lock:
            if len(self.tts_pending) >= MAX_TTS_PENDING:
                source.cleanup()
                return
            self.tts_pending.append(source)
        vc = self.guild.voice_client
        if vc is None or not (vc.is_playing() or vc.is_paused()):
            self._wakeup.set()

    def set_volume(self, percent: int) -> int:
        percent = max(0, min(100, percent))
        self.volume = percent / 100.0
        vc = self.guild.voice_client
        if vc is not None and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.volume
        return percent

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                vc = self.guild.voice_client
                if not self.queue and not self._has_tts():
                    self.now_playing = None
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=IDLE_LEAVE_SECONDS)
                    except asyncio.TimeoutError:
                        current = self.guild.voice_client
                        if current is not None and not (current.is_playing() or current.is_paused()):
                            log.info("Sin actividad %ss en guild=%s, salgo de voz", IDLE_LEAVE_SECONDS, self.guild.id)
                            await self._announce("Me salgo del canal de voz por inactividad.")
                            asyncio.create_task(cleanup_guild(self.guild, disconnect=True))
                            return
                        continue
                    self._wakeup.clear()
                    continue
                if vc is None or not vc.is_connected():
                    await asyncio.sleep(0.4)
                    continue
                if vc.is_playing() or vc.is_paused():
                    await asyncio.sleep(0.1)
                    continue

                track: Track | None = None
                music_source: discord.AudioSource | None = None
                if self.queue:
                    track = self.queue.popleft()
                    self.now_playing = track
                    try:
                        music_source = track.make_source()
                    except Exception:
                        log.exception("No se pudo abrir la fuente de audio")
                        self.now_playing = None
                        continue

                mixer = InterruptiblePCMSource(self, music_source)
                self._mixer = mixer
                wrapped = discord.PCMVolumeTransformer(mixer, volume=self.volume)
                done = asyncio.Event()

                def _after(error: Exception | None) -> None:
                    if error:
                        log.error("Error de reproducción: %s", error, exc_info=error)
                    loop.call_soon_threadsafe(done.set)

                await _wait_dave_ready(vc)
                try:
                    log.info("vc.play() guild=%s %s", self.guild.id, _dave_snapshot(vc))
                    vc.play(wrapped, after=_after)
                except Exception:
                    log.exception("vc.play() falló")
                    mixer.cleanup()
                    self._mixer = None
                    self.now_playing = None
                    continue
                await done.wait()
                self._mixer = None
                self.now_playing = None
                errbuf = getattr(music_source, "_errbuf", None)
                stderr_txt = ""
                if errbuf is not None:
                    stderr_txt = errbuf.getvalue().decode("utf-8", errors="replace").strip()
                if stderr_txt:
                    log.warning("ffmpeg stderr: %s", stderr_txt[:800])
                failed = bool(stderr_txt) and any(
                    word in stderr_txt.lower()
                    for word in ("error", "unrecognized", "invalid", "403", "404", "denied")
                )
                if failed and track is not None and track.kind == "music":
                    await self._announce(f"No pude reproducir **{track.title}**.")
                log.info("Reproducción terminada guild=%s", self.guild.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error en la cola de audio")
                await asyncio.sleep(1)

    def clear(self) -> None:
        self.queue.clear()
        with self._tts_lock:
            while self.tts_pending:
                src = self.tts_pending.popleft()
                try:
                    src.cleanup()
                except Exception:
                    pass
        if self._mixer is not None:
            self._mixer.drop_tts()

    def close(self) -> None:
        self.clear()
        self.task.cancel()
        self._wakeup.set()


def get_player(guild: discord.Guild) -> GuildPlayer:
    if guild.id not in players:
        players[guild.id] = GuildPlayer(guild)
    return players[guild.id]


async def cleanup_guild(guild: discord.Guild, *, disconnect: bool) -> None:
    task = _leave_tasks.pop(guild.id, None)
    if task is not None:
        task.cancel()
    player = players.pop(guild.id, None)
    if player is not None:
        player.close()
    tts_channels.pop(guild.id, None)
    vc = guild.voice_client
    if disconnect and vc is not None:
        await vc.disconnect()


async def ensure_voice(ctx: commands.Context) -> discord.VoiceClient | None:
    if ctx.author.voice is None:
        await ctx.send("Primero entra a un canal de voz.")
        return None
    assert ctx.guild is not None
    vc = ctx.guild.voice_client
    if vc is None:
        if len(bot.voice_clients) >= MAX_VOICE_GUILDS:
            log.warning("Límite de voz alcanzado (%s servidores)", MAX_VOICE_GUILDS)
            await ctx.send("Estoy ocupado en otros servidores ahora mismo. Inténtalo en un rato.")
            return None
        vc = await ctx.author.voice.channel.connect()
        log.info("Conectado a voz %s: %s", ctx.author.voice.channel, _dave_snapshot(vc))
        get_player(ctx.guild)  # arranca el temporizador de inactividad
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)
        log.info("Movido a voz %s: %s", ctx.author.voice.channel, _dave_snapshot(vc))
    return vc


def build_tts_source(text: str) -> discord.FFmpegPCMAudio:
    fp = io.BytesIO()
    gTTS(text=text, lang=TTS_LANG).write_to_fp(fp)
    fp.seek(0)
    return discord.FFmpegPCMAudio(fp, pipe=True)


class TrackTooLong(Exception):
    def __init__(self, seconds: int):
        super().__init__(seconds)
        self.seconds = seconds


def _is_youtube(query: str) -> bool:
    q = query.lower()
    return "youtube.com/" in q or "youtu.be/" in q or "music.youtube.com/" in q


def extract_audio(query: str) -> tuple[str, str, dict[str, str]]:
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if info is None:
            raise RuntimeError("yt-dlp no devolvió resultado")
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise RuntimeError("sin resultados")
            info = entries[0]
        duration = info.get("duration") or 0
        if duration and duration > MAX_TRACK_SECONDS:
            raise TrackTooLong(int(duration))
        url = info.get("url")
        if not url:
            raise RuntimeError("sin URL de audio")
        headers = dict(info.get("http_headers") or {})
        return url, info.get("title") or "Sin título", headers


def make_ffmpeg(url: str, headers: dict[str, str] | None = None) -> Callable[[], discord.AudioSource]:
    def _inner() -> discord.AudioSource:
        before = FFMPEG_OPTS["before_options"]
        if headers:
            hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            before = f"{before} -headers {shlex.quote(hdr)}"
        errbuf = io.BytesIO()
        source = discord.FFmpegPCMAudio(
            url,
            before_options=before,
            options=FFMPEG_OPTS["options"],
            stderr=errbuf,
        )
        source._errbuf = errbuf  # type: ignore[attr-defined]
        return source

    return _inner


def make_tone() -> discord.AudioSource:
    return discord.FFmpegPCMAudio(
        "sine=frequency=440:duration=3",
        before_options="-f lavfi",
        options="-vn",
    )


def _humans_in(channel: discord.VoiceChannel | discord.StageChannel | None) -> list[discord.Member]:
    if channel is None:
        return []
    return [m for m in channel.members if not m.bot]


def _friendly_error(error: BaseException) -> str | None:
    if isinstance(error, commands.CommandNotFound):
        return None
    if isinstance(error, commands.CommandOnCooldown):
        return f"Espera {error.retry_after:.0f} s antes de usar ese comando otra vez."
    if isinstance(error, commands.MissingRequiredArgument):
        return "Falta un argumento. Prueba `!help` o escribe `/`."
    if isinstance(error, commands.BadArgument):
        return "Ese valor no es válido."
    if isinstance(error, commands.NoPrivateMessage):
        return "Ese comando solo funciona en un servidor."
    if isinstance(error, commands.CommandInvokeError) and error.original is not error:
        inner = _friendly_error(error.original)
        return inner or "No pude completar el comando."
    if isinstance(error, discord.app_commands.CommandInvokeError) and error.original is not error:
        inner = _friendly_error(error.original)
        return inner or "No pude completar el comando."
    if isinstance(error, discord.ClientException):
        return "No pude reproducir audio. ¿Sigo en el canal de voz?"
    return "Algo falló. Revisa bot.log."


# ---------- Música ----------

@bot.hybrid_command(name="play", description="Reproduce una canción (búsqueda o enlace)")
@commands.guild_only()
@commands.cooldown(1, PLAY_COOLDOWN_SECONDS, commands.BucketType.user)
async def play(ctx: commands.Context, *, query: str):
    query = query.strip()[:300]
    if _is_youtube(query):
        await ctx.send(
            "YouTube bloquea a los bots en servidores en la nube. "
            "Escribe el nombre de la canción (busco en SoundCloud) o pega un enlace de SoundCloud."
        )
        return
    assert ctx.guild is not None
    existing = players.get(ctx.guild.id)
    if existing is not None and len(existing.queue) >= MAX_QUEUE:
        await ctx.send(f"La cola está llena ({MAX_QUEUE} canciones). Espera a que avance.")
        return
    if await ensure_voice(ctx) is None:
        return
    try:
        if ctx.interaction is not None:
            await ctx.defer()
            url, title, headers = await asyncio.to_thread(extract_audio, query)
        else:
            async with ctx.typing():
                url, title, headers = await asyncio.to_thread(extract_audio, query)
    except TrackTooLong as exc:
        await ctx.send(f"Esa pista dura {exc.seconds // 60} min; el máximo es {MAX_TRACK_SECONDS // 60} min.")
        return
    except Exception as exc:
        log.info("extract_audio falló: %s", exc)
        await ctx.send("No encontré ese audio. Prueba con otro nombre o un enlace de SoundCloud.")
        return
    player = get_player(ctx.guild)
    player.announce_channel_id = ctx.channel.id
    if not player.enqueue(Track(title=title, kind="music", make_source=make_ffmpeg(url, headers))):
        await ctx.send(f"La cola está llena ({MAX_QUEUE} canciones).")
        return
    await ctx.send(f"En cola: **{title}**")


@bot.hybrid_command(name="skip", description="Salta a la siguiente canción de la cola", aliases=["next", "siguiente"])
@commands.guild_only()
async def skip(ctx: commands.Context):
    vc = ctx.guild.voice_client if ctx.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        log.info("skip -> vc.stop()")
        vc.stop()
        await ctx.send("Saltado.")
        return
    await ctx.send("No hay nada sonando.")


@bot.hybrid_command(name="stop", description="Detiene todo y vacía la cola")
@commands.guild_only()
async def stop(ctx: commands.Context):
    assert ctx.guild is not None
    vc = ctx.guild.voice_client
    if vc:
        get_player(ctx.guild).clear()
        log.info("stop -> vc.stop()")
        vc.stop()
        await ctx.send("Detenido.")
        return
    await ctx.send("No estoy en un canal de voz.")


@bot.hybrid_command(name="leave", description="Sale del canal de voz")
@commands.guild_only()
async def leave(ctx: commands.Context):
    assert ctx.guild is not None
    if ctx.guild.voice_client:
        log.info("leave -> disconnect")
        await cleanup_guild(ctx.guild, disconnect=True)
        await ctx.send("Me salgo.")
        return
    await ctx.send("No estoy en un canal de voz.")


@bot.hybrid_command(name="queue", description="Muestra la cola de reproducción")
@commands.guild_only()
async def queue_cmd(ctx: commands.Context):
    assert ctx.guild is not None
    player = players.get(ctx.guild.id)
    if player is None or (player.now_playing is None and not player.queue):
        await ctx.send("La cola está vacía.")
        return
    lines: list[str] = []
    if player.now_playing is not None:
        lines.append(f"Sonando: **{player.now_playing.title}**")
    if player.queue:
        upcoming = list(player.queue)[:10]
        listed = "\n".join(f"{i}. {t.title}" for i, t in enumerate(upcoming, start=1))
        extra = f"\n… y {len(player.queue) - 10} más" if len(player.queue) > 10 else ""
        lines.append(f"Sigue:\n{listed}{extra}")
    await ctx.send("\n".join(lines))


@bot.hybrid_command(name="nowplaying", description="Muestra lo que está sonando")
@commands.guild_only()
async def nowplaying(ctx: commands.Context):
    assert ctx.guild is not None
    player = players.get(ctx.guild.id)
    vc = ctx.guild.voice_client
    if player is None or player.now_playing is None or vc is None or not (vc.is_playing() or vc.is_paused()):
        await ctx.send("No hay nada sonando.")
        return
    vol = int(player.volume * 100)
    await ctx.send(f"Sonando: **{player.now_playing.title}** · volumen {vol}%")


@bot.hybrid_command(name="volume", description="Cambia el volumen (0-100)")
@commands.guild_only()
async def volume(ctx: commands.Context, percent: int):
    assert ctx.guild is not None
    applied = get_player(ctx.guild).set_volume(percent)
    await ctx.send(f"Volumen: **{applied}%**")


@bot.hybrid_command(name="test_audio", description="Reproduce un tono de 3 segundos para diagnóstico")
@commands.guild_only()
async def test_audio(ctx: commands.Context):
    vc = await ensure_voice(ctx)
    if vc is None:
        return
    snap = _dave_snapshot(vc)
    log.info("!test_audio %s opus_loaded=%s", snap, discord.opus.is_loaded())
    assert ctx.guild is not None
    get_player(ctx.guild).enqueue(Track(title="Tono de prueba (3s)", kind="test", make_source=make_tone))
    await ctx.send(f"Tono de 3s. DAVE: `{snap}`")


# ---------- Lectura del chat (TTS) ----------

@bot.hybrid_command(name="tts_on", description="Lee en voz alta el chat de este canal")
@commands.guild_only()
async def tts_on(ctx: commands.Context):
    if await ensure_voice(ctx) is None:
        return
    assert ctx.guild is not None
    tts_channels[ctx.guild.id] = ctx.channel.id
    await ctx.send("Voy a leer los mensajes de este canal.")


@bot.hybrid_command(name="help", description="Lista todos los comandos")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="Sesame",
        description="Música en el canal de voz. El chat se oye; la canción no se para.",
        color=BOT_COLOR,
    )
    embed.add_field(
        name="Música",
        value=(
            "`!play <canción>` — busca en SoundCloud y reproduce (no YouTube)\n"
            "`!skip` / `!next` — siguiente de la cola\n"
            "`!stop` — para todo y vacía la cola\n"
            "`!queue` · `!nowplaying` · `!volume <0-100>`\n"
            "`!leave` — sale del canal de voz"
        ),
        inline=False,
    )
    embed.add_field(
        name="Chat en voz",
        value="`!tts_on` — lee este canal (baja la música)\n`!tts_off` — deja de leer",
        inline=False,
    )
    embed.add_field(name="Diagnóstico", value="`!test_audio` — tono de 3 s", inline=False)
    if bot.user is not None:
        invite = discord.utils.oauth_url(
            bot.user.id,
            permissions=BOT_PERMISSIONS,
            scopes=("bot", "applications.commands"),
        )
        embed.add_field(name="Invitar", value=f"[Añadir Sesame a un servidor]({invite})", inline=False)
    embed.set_footer(text="También con /  ·  open source")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="tts_off", description="Deja de leer el chat")
@commands.guild_only()
async def tts_off(ctx: commands.Context):
    assert ctx.guild is not None
    tts_channels.pop(ctx.guild.id, None)
    await ctx.send("Lectura de chat desactivada.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    await bot.process_commands(message)

    if message.content.startswith(PREFIX):
        return
    if tts_channels.get(message.guild.id) != message.channel.id:
        return

    vc = message.guild.voice_client
    if vc is None or message.author.voice is None or message.author.voice.channel != vc.channel:
        return

    text = message.clean_content.strip()[:TTS_MAX_CHARS]
    if not text:
        return

    spoken = f"{message.author.display_name} dice: {text}"
    try:
        source = await asyncio.to_thread(build_tts_source, spoken)
    except Exception:
        log.exception("gTTS falló")
        return
    get_player(message.guild).speak(source)


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    guild = member.guild
    vc = guild.voice_client
    if vc is None or vc.channel is None:
        return

    if bot.user is not None and member.id == bot.user.id and after.channel is None:
        await cleanup_guild(guild, disconnect=False)
        return

    async def _maybe_leave() -> None:
        await asyncio.sleep(EMPTY_LEAVE_DELAY)
        current = guild.voice_client
        if current is None or current.channel is None:
            return
        if _humans_in(current.channel):
            return
        log.info("Canal de voz vacío, me desconecto")
        await cleanup_guild(guild, disconnect=True)

    if _humans_in(vc.channel):
        pending = _leave_tasks.pop(guild.id, None)
        if pending is not None:
            pending.cancel()
        return

    old = _leave_tasks.get(guild.id)
    if old is not None and not old.done():
        return
    _leave_tasks[guild.id] = asyncio.create_task(_maybe_leave())


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    msg = _friendly_error(error)
    if msg is None:
        return
    if not isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.NoPrivateMessage)):
        log.exception("Error de comando: %s", error)
    try:
        await ctx.send(msg)
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    msg = _friendly_error(error) or "Algo falló. Revisa bot.log."
    if not isinstance(error, discord.app_commands.CommandOnCooldown):
        log.exception("Error slash: %s", error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_guild_join(guild: discord.Guild):
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info("Slash sincronizados en %s", guild.name)
    me = guild.me
    for channel in guild.text_channels:
        if me is None or not channel.permissions_for(me).send_messages:
            continue
        await channel.send(
            "Hola, soy **Sesame**. Entra a un canal de voz y usa `/play` o `!help`."
        )
        break


@bot.event
async def on_ready():
    try:
        import davey
        davey_ver = davey.__version__
    except ImportError:
        davey_ver = "no instalado"
    log.info(
        "Conectado como %s | python=%s discord.py=%s davey=%s opus_loaded=%s",
        bot.user,
        sys.version.split()[0],
        discord.__version__,
        davey_ver,
        discord.opus.is_loaded(),
    )
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name="/play · !help")
    )
    try:
        await bot.tree.sync()
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        log.info("Comandos slash sincronizados en %s servidor(es)", len(bot.guilds))
    except Exception:
        log.exception("No se pudieron sincronizar los slash commands")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta DISCORD_TOKEN en el archivo .env")
    setup_logging()
    _patch_davey_020()
    bot.run(TOKEN, log_handler=None)

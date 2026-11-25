"""Reproductor por servidor y registro que controla conexiones de voz y su ciclo de vida."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import discord

from . import dave
from .audio import MixerSource, StreamSource, TtsQueue, tone_source
from .config import Settings
from .resolver import ResolvedTrack

log = logging.getLogger("sesame.player")


@dataclass(frozen=True)
class QueueItem:
    title: str
    kind: str                                   # music | test
    open: Callable[[], discord.AudioSource]     # crea la fuente justo antes de sonar

    @classmethod
    def music(cls, track: ResolvedTrack, settings: Settings) -> "QueueItem":
        return cls(track.title, "music", lambda: StreamSource(track, settings.max_track_seconds))

    @classmethod
    def tone(cls) -> "QueueItem":
        return cls("Tono de prueba (3 s)", "test", tone_source)


class VoiceError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def humans_in(channel: discord.abc.Connectable | None) -> list[discord.Member]:
    members = getattr(channel, "members", None) or []
    return [m for m in members if not m.bot]


class GuildPlayer:
    """Cola de un servidor. Un único bucle asyncio decide qué suena; el TTS se mezcla encima."""

    def __init__(
        self,
        guild: discord.Guild,
        settings: Settings,
        on_idle: Callable[[discord.Guild], None],
    ) -> None:
        self.guild = guild
        self.settings = settings
        self.queue: deque[QueueItem] = deque()
        self.tts = TtsQueue(settings.tts_max_pending)
        self.now_playing: QueueItem | None = None
        self.volume = 1.0
        self.announce_channel_id: int | None = None
        self._mixer: MixerSource | None = None
        self._wakeup = asyncio.Event()
        self._on_idle = on_idle
        self._task = asyncio.create_task(self._run(), name=f"sesame-player-{guild.id}")

    # --- API usada por los comandos ---
    def enqueue(self, item: QueueItem) -> bool:
        if item.kind == "music" and len(self.queue) >= self.settings.max_queue:
            return False
        self.queue.append(item)
        self._wakeup.set()
        return True

    def speak(self, source: discord.AudioSource) -> bool:
        accepted = self.tts.push(source)
        if accepted:
            self._wakeup.set()
        return accepted

    def set_volume(self, percent: int) -> int:
        percent = max(0, min(100, int(percent)))
        self.volume = percent / 100
        vc = self.guild.voice_client
        if isinstance(vc, discord.VoiceClient) and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.volume
        return percent

    def clear(self) -> None:
        self.queue.clear()
        self.tts.clear()
        if self._mixer is not None:
            self._mixer.drop_tts()

    def close(self) -> None:
        self.clear()
        self._task.cancel()

    # --- Bucle interno ---
    def _has_work(self) -> bool:
        return bool(self.queue) or len(self.tts) > 0

    async def _announce(self, text: str) -> None:
        if self.announce_channel_id is None:
            return
        channel = self.guild.get_channel(self.announce_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(text)
            except discord.HTTPException:
                log.debug("No pude avisar en el canal %s", self.announce_channel_id)

    async def _run(self) -> None:
        while True:
            try:
                self._wakeup.clear()
                if not self._has_work():
                    self.now_playing = None
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=self.settings.idle_leave_seconds)
                    except asyncio.TimeoutError:
                        vc = self.guild.voice_client
                        if vc is None or not (vc.is_playing() or vc.is_paused()):
                            log.info("guild=%s sin actividad, salgo de voz", self.guild.id)
                            await self._announce("Me salgo del canal de voz por inactividad.")
                            self._on_idle(self.guild)
                            return
                    continue

                vc = self.guild.voice_client
                if not isinstance(vc, discord.VoiceClient) or not vc.is_connected():
                    await asyncio.sleep(0.5)
                    continue
                if vc.is_playing() or vc.is_paused():
                    await asyncio.sleep(0.25)
                    continue
                await self._play_next(vc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Error en la cola de audio guild=%s", self.guild.id)
                await asyncio.sleep(1)

    async def _play_next(self, vc: discord.VoiceClient) -> None:
        item = self.queue.popleft() if self.queue else None
        music: discord.AudioSource | None = None
        if item is not None:
            self.now_playing = item
            try:
                music = item.open()
            except Exception:
                log.exception("No pude abrir la fuente de audio")
                self.now_playing = None
                await self._announce(f"No pude reproducir **{discord.utils.escape_markdown(item.title)}**.")
                return

        mixer = MixerSource(music, self.tts, self.settings.tts_duck)
        self._mixer = mixer
        loop = asyncio.get_running_loop()
        done = asyncio.Event()

        def _after(error: Exception | None) -> None:
            if error:
                log.error("Error de reproducción: %s", error)
            loop.call_soon_threadsafe(done.set)

        await dave.wait_until_ready(vc, self.settings.dave_ready_timeout)
        try:
            vc.play(discord.PCMVolumeTransformer(mixer, volume=self.volume), after=_after)
        except Exception:
            log.exception("vc.play() falló")
            mixer.cleanup()
            return
        finally:
            log.debug("vc.play() guild=%s %s", self.guild.id, dave.snapshot(vc))

        try:
            await done.wait()
        finally:
            self._mixer = None
            self.now_playing = None

        if isinstance(music, StreamSource) and item is not None:
            problem = music.failed()
            if problem:
                log.warning("ffmpeg: %s", problem)
                await self._announce(f"No pude reproducir **{discord.utils.escape_markdown(item.title)}**.")


class PlayerRegistry:
    """Fábrica y dueño de los reproductores; único sitio que conecta y desconecta la voz."""

    def __init__(self, client: discord.Client, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._players: dict[int, GuildPlayer] = {}
        self._tts_channels: dict[int, int] = {}
        self._empty_timers: dict[int, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._connect_lock = asyncio.Lock()

    # --- Reproductores ---
    def get(self, guild: discord.Guild) -> GuildPlayer:
        player = self._players.get(guild.id)
        if player is None:
            player = GuildPlayer(guild, self.settings, on_idle=self._release_soon)
            self._players[guild.id] = player
        return player

    def peek(self, guild_id: int) -> GuildPlayer | None:
        return self._players.get(guild_id)

    # --- TTS ---
    def tts_channel(self, guild_id: int) -> int | None:
        return self._tts_channels.get(guild_id)

    def set_tts_channel(self, guild_id: int, channel_id: int | None) -> None:
        if channel_id is None:
            self._tts_channels.pop(guild_id, None)
        else:
            self._tts_channels[guild_id] = channel_id

    # --- Voz ---
    async def connect(self, member: discord.Member, *, can_override: bool) -> discord.VoiceClient:
        """Conecta (o mueve) el bot al canal del usuario respetando los límites de consumo."""
        if member.voice is None or member.voice.channel is None:
            raise VoiceError("Primero entra a un canal de voz.")
        target = member.voice.channel
        guild = member.guild
        async with self._connect_lock:
            vc = guild.voice_client
            if isinstance(vc, discord.VoiceClient) and vc.is_connected():
                if vc.channel != target:
                    if humans_in(vc.channel) and not can_override:
                        raise VoiceError(f"Estoy ocupado en **{vc.channel.name}**. Entra ahí para usarme.")
                    await vc.move_to(target)
                    log.info("Movido a voz guild=%s canal=%s", guild.id, target.id)
                return vc
            active = sum(1 for v in self.client.voice_clients if v.is_connected())
            if active >= self.settings.max_voice_guilds:
                log.warning("Límite de voz alcanzado (%s servidores)", self.settings.max_voice_guilds)
                raise VoiceError("Estoy ocupado en otros servidores ahora mismo. Prueba en un rato.")
            perms = target.permissions_for(guild.me)
            if not (perms.connect and perms.speak):
                raise VoiceError("No tengo permiso para entrar o hablar en ese canal.")
            vc = await target.connect(timeout=20, reconnect=True, self_deaf=True)
            log.info("Conectado a voz guild=%s canal=%s %s", guild.id, target.id, dave.snapshot(vc))
            self.get(guild)  # arranca el temporizador de inactividad
            return vc

    async def release(self, guild: discord.Guild, *, disconnect: bool = True) -> None:
        timer = self._empty_timers.pop(guild.id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        player = self._players.pop(guild.id, None)
        if player is not None:
            player.close()
        self._tts_channels.pop(guild.id, None)
        vc = guild.voice_client
        if disconnect and vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:
                log.debug("disconnect falló guild=%s", guild.id, exc_info=True)

    def _release_soon(self, guild: discord.Guild) -> None:
        self.spawn(self.release(guild, disconnect=True))

    def watch_empty_channel(self, guild: discord.Guild) -> None:
        """Si el bot se queda sin personas en su canal, sale tras unos segundos."""
        vc = guild.voice_client
        if vc is None or vc.channel is None:
            return
        if humans_in(vc.channel):
            timer = self._empty_timers.pop(guild.id, None)
            if timer is not None:
                timer.cancel()
            return
        if guild.id in self._empty_timers and not self._empty_timers[guild.id].done():
            return

        async def _later() -> None:
            await asyncio.sleep(self.settings.empty_leave_seconds)
            current = guild.voice_client
            if current is not None and current.channel is not None and not humans_in(current.channel):
                log.info("Canal vacío guild=%s, me desconecto", guild.id)
                await self.release(guild, disconnect=True)

        self._empty_timers[guild.id] = self.spawn(_later())

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Crea una tarea guardando la referencia (evita que el GC la cancele)."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def shutdown(self) -> None:
        for guild_id in list(self._players):
            guild = self.client.get_guild(guild_id)
            if guild is not None:
                await self.release(guild)

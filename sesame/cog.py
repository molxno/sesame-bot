"""Comandos de Sesame (prefijo y slash) y escucha del chat para el TTS."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from . import dave
from .audio import build_tts_source, tts_text
from .guards import NotInBotChannel, RateLimited, RateLimiter, SesameCheckFailure
from .player import PlayerRegistry, QueueItem, VoiceError
from .resolver import AudioResolver, ResolveError, ResolvedTrack

if TYPE_CHECKING:
    from .app import SesameBot

log = logging.getLogger("sesame.cog")


def _escape(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _is_moderator(member: discord.abc.User) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.manage_guild or perms.move_members or perms.administrator))


def in_bot_channel():
    """El autor debe estar en el canal de voz del bot (o ser moderador)."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        vc = ctx.guild.voice_client
        if vc is None or vc.channel is None or _is_moderator(ctx.author):
            return True
        voice = getattr(ctx.author, "voice", None)
        if voice is not None and voice.channel == vc.channel:
            return True
        raise NotInBotChannel()

    return commands.check(predicate)


class BusyError(Exception):
    pass


class Music(commands.Cog):
    """Música, cola y lectura del chat."""

    def __init__(self, bot: "SesameBot") -> None:
        self.bot = bot
        s = bot.settings
        self.settings = s
        self.registry = PlayerRegistry(bot, s)
        self.resolver = AudioResolver(s.max_track_seconds, s.max_query_chars)
        # Hilos acotados: nadie puede forzar más trabajo pesado en paralelo que esto.
        self._extract_pool = ThreadPoolExecutor(s.extract_workers, thread_name_prefix="sesame-extract")
        self._tts_pool = ThreadPoolExecutor(s.tts_workers, thread_name_prefix="sesame-tts")
        self._extract_pending = 0
        self._tts_pending = 0
        # Límites anti-spam
        self.play_user = RateLimiter(s.play_user_rate, s.play_user_window)
        self.play_guild = RateLimiter(s.play_guild_rate, s.play_guild_window)
        self.tts_user = RateLimiter(s.tts_user_rate, s.tts_user_window)
        self.tts_guild = RateLimiter(s.tts_guild_rate, s.tts_guild_window)

    async def cog_unload(self) -> None:
        await self.registry.shutdown()
        self._extract_pool.shutdown(wait=False, cancel_futures=True)
        self._tts_pool.shutdown(wait=False, cancel_futures=True)

    # ---------- Utilidades ----------
    async def _resolve(self, query: str) -> ResolvedTrack:
        if self._extract_pending >= self.settings.extract_max_pending:
            raise BusyError()
        self._extract_pending += 1
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(self._extract_pool, self.resolver.resolve, query),
                timeout=self.settings.extract_timeout,
            )
        finally:
            self._extract_pending -= 1

    def _player_if_connected(self, ctx: commands.Context):
        if ctx.guild is None or ctx.guild.voice_client is None:
            return None
        return self.registry.peek(ctx.guild.id)

    # ---------- Música ----------
    @commands.hybrid_command(name="play", description="Reproduce una canción (nombre o enlace)")
    @app_commands.describe(query="Nombre de la canción o enlace de SoundCloud, YouTube, Spotify o Bandcamp")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        for limiter, key in ((self.play_user, ctx.author.id), (self.play_guild, ctx.guild.id)):
            wait = limiter.hit(key)
            if wait:
                raise RateLimited(wait)
        existing = self.registry.peek(ctx.guild.id)
        if existing is not None and len(existing.queue) >= self.settings.max_queue:
            await ctx.send(f"La cola está llena ({self.settings.max_queue} canciones). Espera a que avance.")
            return
        try:
            await self.registry.connect(ctx.author, can_override=_is_moderator(ctx.author))
        except VoiceError as exc:
            await ctx.send(exc.user_message)
            return

        try:
            async with ctx.typing():
                track = await self._resolve(query)
        except ResolveError as exc:
            await ctx.send(exc.user_message)
            return
        except BusyError:
            await ctx.send("Estoy procesando muchas canciones a la vez. Prueba en unos segundos.")
            return
        except asyncio.TimeoutError:
            await ctx.send("La búsqueda tardó demasiado. Prueba otra vez o con otro nombre.")
            return

        if ctx.guild.voice_client is None:  # se desconectó mientras buscábamos
            return
        player = self.registry.get(ctx.guild)
        player.announce_channel_id = ctx.channel.id
        if not player.enqueue(QueueItem.music(track, self.settings)):
            await ctx.send(f"La cola está llena ({self.settings.max_queue} canciones).")
            return
        minutes, seconds = divmod(track.duration, 60)
        await ctx.send(f"En cola: **{_escape(track.title)}** · {minutes}:{seconds:02d} · _{track.source}_")

    @commands.hybrid_command(name="skip", aliases=["next", "siguiente"], description="Salta a la siguiente canción")
    @commands.guild_only()
    @in_bot_channel()
    async def skip(self, ctx: commands.Context) -> None:
        vc = ctx.guild.voice_client if ctx.guild else None
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await ctx.send("Saltado.")
        else:
            await ctx.send("No hay nada sonando.")

    @commands.hybrid_command(name="stop", description="Detiene todo y vacía la cola")
    @commands.guild_only()
    @in_bot_channel()
    async def stop(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        vc = ctx.guild.voice_client
        if vc is None:
            await ctx.send("No estoy en un canal de voz.")
            return
        player = self.registry.peek(ctx.guild.id)
        if player is not None:
            player.clear()
        vc.stop()
        await ctx.send("Detenido.")

    @commands.hybrid_command(name="leave", description="Sale del canal de voz")
    @commands.guild_only()
    @in_bot_channel()
    async def leave(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        if ctx.guild.voice_client is None:
            await ctx.send("No estoy en un canal de voz.")
            return
        await self.registry.release(ctx.guild)
        await ctx.send("Me salgo.")

    @commands.hybrid_command(name="queue", description="Muestra la cola de reproducción")
    @commands.guild_only()
    async def queue(self, ctx: commands.Context) -> None:
        player = self._player_if_connected(ctx)
        if player is None or (player.now_playing is None and not player.queue):
            await ctx.send("La cola está vacía.")
            return
        lines: list[str] = []
        if player.now_playing is not None:
            lines.append(f"Sonando: **{_escape(player.now_playing.title)}**")
        if player.queue:
            upcoming = list(player.queue)[:10]
            lines.append("Sigue:")
            lines.extend(f"{i}. {_escape(item.title)}" for i, item in enumerate(upcoming, start=1))
            if len(player.queue) > 10:
                lines.append(f"… y {len(player.queue) - 10} más")
        await ctx.send("\n".join(lines)[:1900])

    @commands.hybrid_command(name="nowplaying", description="Muestra lo que está sonando")
    @commands.guild_only()
    async def nowplaying(self, ctx: commands.Context) -> None:
        player = self._player_if_connected(ctx)
        vc = ctx.guild.voice_client if ctx.guild else None
        if player is None or player.now_playing is None or vc is None or not vc.is_playing():
            await ctx.send("No hay nada sonando.")
            return
        await ctx.send(f"Sonando: **{_escape(player.now_playing.title)}** · volumen {round(player.volume * 100)}%")

    @commands.hybrid_command(name="volume", description="Cambia el volumen (0-100)")
    @app_commands.describe(percent="Volumen entre 0 y 100")
    @commands.guild_only()
    @in_bot_channel()
    async def volume(self, ctx: commands.Context, percent: commands.Range[int, 0, 100]) -> None:
        player = self._player_if_connected(ctx)
        if player is None:
            await ctx.send("No estoy en un canal de voz.")
            return
        await ctx.send(f"Volumen: **{player.set_volume(percent)}%**")

    @commands.hybrid_command(name="test_audio", description="Tono de 3 s para diagnóstico (solo moderadores)")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def test_audio(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        try:
            vc = await self.registry.connect(ctx.author, can_override=True)
        except VoiceError as exc:
            await ctx.send(exc.user_message)
            return
        player = self.registry.get(ctx.guild)
        player.announce_channel_id = ctx.channel.id
        player.enqueue(QueueItem.tone())
        await ctx.send(f"Tono de 3 s. DAVE: `{dave.snapshot(vc)}`")

    # ---------- Chat en voz ----------
    @commands.hybrid_command(name="tts_on", description="Lee en voz alta el chat de este canal")
    @commands.guild_only()
    @in_bot_channel()
    async def tts_on(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        try:
            await self.registry.connect(ctx.author, can_override=_is_moderator(ctx.author))
        except VoiceError as exc:
            await ctx.send(exc.user_message)
            return
        self.registry.set_tts_channel(ctx.guild.id, ctx.channel.id)
        await ctx.send(
            "Voy a leer los mensajes de este canal de quienes estén en mi canal de voz. `!tts_off` para parar."
        )

    @commands.hybrid_command(name="tts_off", description="Deja de leer el chat")
    @commands.guild_only()
    @in_bot_channel()
    async def tts_off(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        self.registry.set_tts_channel(ctx.guild.id, None)
        await ctx.send("Lectura de chat desactivada.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id is not None:
            return
        if message.content.startswith(self.settings.prefix):
            return
        if self.registry.tts_channel(guild.id) != message.channel.id:
            return
        vc = guild.voice_client
        voice = getattr(message.author, "voice", None)
        if vc is None or voice is None or voice.channel != vc.channel:
            return
        player = self.registry.peek(guild.id)
        if player is None or player.tts.is_full():
            return
        if self.tts_user.hit(message.author.id) or self.tts_guild.hit(guild.id):
            return  # spam: se ignora en silencio
        if self._tts_pending >= self.settings.tts_workers * 2:
            return
        text = tts_text(message.author.display_name, message.clean_content, self.settings.tts_max_chars)
        if not text:
            return
        self._tts_pending += 1
        try:
            loop = asyncio.get_running_loop()
            source = await asyncio.wait_for(
                loop.run_in_executor(self._tts_pool, build_tts_source, text, self.settings.tts_lang),
                timeout=15,
            )
        except Exception as exc:
            log.info("TTS falló: %s", exc)
            return
        finally:
            self._tts_pending -= 1
        if self.registry.peek(guild.id) is player:
            player.speak(source)
        else:
            source.cleanup()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if self.bot.user is not None and member.id == self.bot.user.id and after.channel is None:
            await self.registry.release(guild, disconnect=False)
            return
        self.registry.watch_empty_channel(guild)

    # ---------- Ayuda ----------
    @commands.hybrid_command(name="help", description="Lista todos los comandos")
    async def help(self, ctx: commands.Context) -> None:
        p = self.settings.prefix
        embed = discord.Embed(
            title="Sesame",
            description="Música en el canal de voz. El chat se oye; la canción no se para.",
            color=0xC4A35A,
        )
        embed.add_field(
            name="Música",
            value=(
                f"`{p}play <canción o enlace>` — SoundCloud, YouTube, Spotify o Bandcamp\n"
                f"`{p}skip` / `{p}next` — siguiente de la cola\n"
                f"`{p}stop` — para todo y vacía la cola\n"
                f"`{p}queue` · `{p}nowplaying` · `{p}volume <0-100>`\n"
                f"`{p}leave` — sale del canal de voz"
            ),
            inline=False,
        )
        embed.add_field(
            name="Chat en voz",
            value=f"`{p}tts_on` — lee este canal (baja la música)\n`{p}tts_off` — deja de leer",
            inline=False,
        )
        embed.add_field(
            name="Límites",
            value=(
                f"Canciones de hasta {self.settings.max_track_seconds // 60} min · "
                f"cola de {self.settings.max_queue} · salgo tras "
                f"{self.settings.idle_leave_seconds // 60} min sin música"
            ),
            inline=False,
        )
        if self.settings.show_invite and self.bot.user is not None:
            invite = discord.utils.oauth_url(
                self.bot.user.id, permissions=self.bot.invite_permissions, scopes=("bot", "applications.commands")
            )
            embed.add_field(name="Invitar", value=f"[Añadir Sesame a un servidor]({invite})", inline=False)
        embed.set_footer(text=f"También con /  ·  open source  ·  {self.settings.version}")
        await ctx.send(embed=embed)


__all__ = ["Music", "SesameCheckFailure"]

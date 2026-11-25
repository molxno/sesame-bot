"""Bot principal: intents mínimos, menciones bloqueadas, control de servidores y anti-spam global."""

from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands

from . import dave
from .cog import Music
from .config import Settings
from .guards import RateLimited, RateLimiter, SesameCheckFailure
from .logging_setup import setup_logging
from .player import VoiceError
from .resolver import ResolveError

log = logging.getLogger("sesame")

INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    read_message_history=True,
    connect=True,
    speak=True,
    use_voice_activation=True,
    use_application_commands=True,
)

_QUIET_ERRORS = (
    commands.CommandNotFound,
    commands.MissingRequiredArgument,
    commands.BadArgument,
    commands.NoPrivateMessage,
    commands.CheckFailure,
    commands.UserInputError,
)


class SesameBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True   # comandos con "!" y lectura del chat
        intents.voice_states = True
        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.prefix),
            intents=intents,
            help_command=None,
            case_insensitive=True,
            max_messages=None,                                  # sin caché de mensajes: menos RAM
            allowed_mentions=discord.AllowedMentions.none(),    # nunca @everyone ni @usuario
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
            chunk_guilds_at_startup=False,
        )
        self.settings = settings
        self.invite_permissions = INVITE_PERMISSIONS
        self._command_limiter = RateLimiter(settings.cmd_user_rate, settings.cmd_user_window)
        self._notice_limiter = RateLimiter(1, 30)  # avisar de un error como mucho cada 30 s por usuario
        self._cleaned_guild_commands = False
        self.add_check(self._global_check)

    # ---------- Ciclo de vida ----------
    async def setup_hook(self) -> None:
        dave.install_bridge()
        await self.add_cog(Music(self))
        try:
            synced = await self.tree.sync()
            log.info("Comandos slash globales sincronizados: %s", len(synced))
        except discord.HTTPException:
            log.exception("No se pudieron sincronizar los comandos slash")

    async def on_ready(self) -> None:
        log.info(
            "Conectado como %s | versión=%s | python=%s discord.py=%s | servidores=%s",
            self.user, self.settings.version, sys.version.split()[0], discord.__version__, len(self.guilds),
        )
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play · !help"))
        for guild in list(self.guilds):
            if not self.settings.guild_allowed(guild.id):
                await self._leave(guild, "no está en ALLOWED_GUILD_IDS")
        if not self._cleaned_guild_commands:
            self._cleaned_guild_commands = True
            # Versiones anteriores registraban copias por servidor: se borran para no duplicar comandos.
            for guild in self.guilds[: self.settings.max_guilds]:
                try:
                    await self.tree.sync(guild=guild)
                except discord.HTTPException:
                    log.debug("No pude limpiar comandos del servidor %s", guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not self.settings.guild_allowed(guild.id):
            await self._leave(guild, "no está en ALLOWED_GUILD_IDS")
            return
        if len(self.guilds) > self.settings.max_guilds:
            await self._leave(guild, f"límite de {self.settings.max_guilds} servidores")
            return
        log.info("Añadido a servidor %s (%s miembros)", guild.id, guild.member_count)

    async def _leave(self, guild: discord.Guild, reason: str) -> None:
        log.warning("Salgo del servidor %s: %s", guild.id, reason)
        try:
            await guild.leave()
        except discord.HTTPException:
            log.exception("No pude salir del servidor %s", guild.id)

    # ---------- Anti-spam global ----------
    async def _global_check(self, ctx: commands.Context) -> bool:
        if ctx.author.bot:
            return False
        if ctx.guild is not None and not self.settings.guild_allowed(ctx.guild.id):
            return False
        wait = self._command_limiter.hit(ctx.author.id)
        if wait:
            raise RateLimited(wait)
        return True

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = getattr(error, "original", error)
        message = self._user_message(original)
        if message is None:
            return
        if not isinstance(original, (*_QUIET_ERRORS, ResolveError, VoiceError)):
            log.error("Error en %s: %r", ctx.command, original, exc_info=original)
        if self._notice_limiter.hit((ctx.author.id, type(original).__name__)):
            return  # no responder a cada mensaje de spam
        try:
            if ctx.interaction is not None:
                await ctx.send(message, ephemeral=True)
            else:
                await ctx.send(message)
        except discord.HTTPException:
            pass

    @staticmethod
    def _user_message(error: BaseException) -> str | None:
        if isinstance(error, commands.CommandNotFound):
            return None
        if isinstance(error, SesameCheckFailure):
            return error.user_message
        if isinstance(error, commands.MissingPermissions):
            return "Ese comando es solo para moderadores."
        if isinstance(error, commands.MissingRequiredArgument):
            return "Falta un argumento. Prueba `!help` o escribe `/`."
        if isinstance(error, (commands.BadArgument, commands.RangeError)):
            return "Ese valor no es válido."
        if isinstance(error, commands.NoPrivateMessage):
            return "Ese comando solo funciona en un servidor."
        if isinstance(error, commands.CheckFailure):
            return None
        if isinstance(error, (ResolveError, VoiceError)):
            return error.user_message
        if isinstance(error, discord.ClientException):
            return "No pude reproducir audio. ¿Sigo en el canal de voz?"
        return "Algo falló. Inténtalo de nuevo en un momento."


def run() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.log_file, settings.token)
    if not settings.token:
        raise SystemExit("Falta DISCORD_TOKEN (archivo .env o secret de Azure).")
    SesameBot(settings).run(settings.token, log_handler=None)

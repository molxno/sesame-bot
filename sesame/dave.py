"""Compatibilidad con el cifrado de voz E2EE de Discord (DAVE).

discord.py 2.7.1 no llama prepare/execute_transition de davey 0.2.0; este módulo añade
ese puente y utilidades para esperar a que el cifrado esté listo antes de emitir audio.
"""

from __future__ import annotations

import asyncio
import logging
import struct

import discord
from discord.gateway import DiscordVoiceWebSocket
from discord.voice_state import VoiceConnectionState

log = logging.getLogger("sesame.dave")


_installed = False


def install_bridge() -> None:
    """discord.py 2.7.1 no llama prepare/execute_transition de davey 0.2.0. Idempotente."""
    global _installed
    if _installed:
        return
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
    _installed = True
    log.info("Puente DAVE activado para davey %s (discord.py 2.7.1 no llama prepare_transition)", davey.__version__)


def snapshot(vc: discord.VoiceClient) -> str:
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


async def wait_until_ready(vc: discord.VoiceClient, timeout: float) -> None:
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
    log.warning("DAVE no quedó listo en %ss; emito de todos modos: %s", timeout, snapshot(vc))

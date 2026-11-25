"""Logs a consola (y archivo rotativo opcional) sin filtrar nunca el token."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

# Forma de un token de bot de Discord: tres bloques base64 separados por puntos.
_TOKEN_SHAPE = re.compile(r"[A-Za-z\d_-]{23,28}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,}")


class RedactSecrets(logging.Filter):
    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:
            return True
        redacted = text
        for secret in self._secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = _TOKEN_SHAPE.sub("[REDACTED]", redacted)
        if redacted != text:
            record.msg, record.args = redacted, ()
        return True


def setup_logging(level_name: str, log_file: str | None, *secrets: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    redact = RedactSecrets(*secrets)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if sys.stdout is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console.addFilter(redact)
        root.addHandler(console)

    if log_file:
        file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redact)
        root.addHandler(file_handler)

    for noisy in ("discord", "discord.http", "discord.gateway"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))

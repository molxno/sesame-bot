"""Límites de frecuencia en memoria (ventana deslizante) y errores de control de acceso."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable

from discord.ext import commands


class RateLimiter:
    """Permite como máximo `limit` eventos por clave dentro de `window` segundos.

    La memoria está acotada: cuando hay demasiadas claves se purgan las caducadas y,
    si aun así sobran, las más antiguas.
    """

    def __init__(
        self,
        limit: int,
        window: float,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window <= 0:
            raise ValueError("limit >= 1 y window > 0")
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}

    def hit(self, key: Hashable) -> float:
        """Registra un intento. Devuelve 0 si se permite o los segundos que faltan si no."""
        now = self._clock()
        bucket = self._hits.get(key)
        if bucket is None:
            if len(self._hits) >= self.max_keys:
                self._prune(now)
            bucket = deque()
            self._hits[key] = bucket
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return max(0.0, bucket[0] + self.window - now)
        bucket.append(now)
        return 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        stale = [k for k, b in self._hits.items() if not b or b[-1] <= cutoff]
        for k in stale:
            del self._hits[k]
        overflow = len(self._hits) - self.max_keys + 1
        if overflow > 0:
            oldest = sorted(self._hits, key=lambda k: self._hits[k][-1])[:overflow]
            for k in oldest:
                del self._hits[k]

    def __len__(self) -> int:
        return len(self._hits)


class SesameCheckFailure(commands.CheckFailure):
    """Fallo de control con un mensaje ya pensado para el usuario."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class RateLimited(SesameCheckFailure):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Vas muy rápido. Espera {max(1, round(retry_after))} s.")
        self.retry_after = retry_after


class NotInBotChannel(SesameCheckFailure):
    def __init__(self) -> None:
        super().__init__("Tienes que estar en mi mismo canal de voz para hacer eso.")

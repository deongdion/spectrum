"""Background token renewal at 80% of the token TTL (spectrum-ts ``createTokenRenewal``)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from ..core.utils import jittered_backoff

log = logging.getLogger("spectrum.cloud.renewal")

RENEWAL_RATIO = 0.8
MIN_RENEWAL_DELAY = 5.0
FORCE_REFRESH_MIN_INTERVAL = 5.0


class TokenRenewal:
    """Schedules ``refresh()`` before the current token expires.

    ``expires_in`` returns the TTL (seconds) of the token obtained by the most
    recent refresh. Failures retry with jittered backoff; the old token stays
    in use until a refresh succeeds.
    """

    def __init__(
        self, name: str, refresh: Callable[[], Awaitable[None]], expires_in: Callable[[], int]
    ) -> None:
        self._name = name
        self._refresh = refresh
        self._expires_in = expires_in
        self._deadline = time.monotonic() + max(expires_in(), 0)
        self._last_refresh = time.monotonic()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def deadline(self) -> float:
        return self._deadline

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"spectrum:renew:{self._name}")

    async def _loop(self) -> None:
        attempt = 0
        while True:
            ttl = self._expires_in()
            delay = max(ttl * RENEWAL_RATIO, MIN_RENEWAL_DELAY) if attempt == 0 else jittered_backoff(attempt)
            await asyncio.sleep(delay)
            try:
                await self._do_refresh()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                log.warning("%s token refresh failed (attempt %d)", self._name, attempt, exc_info=True)

    async def _do_refresh(self) -> None:
        async with self._lock:
            await self._refresh()
            now = time.monotonic()
            self._last_refresh = now
            self._deadline = now + self._expires_in()
            log.debug("%s token refreshed; expires in %ss", self._name, self._expires_in())

    async def refresh_if_needed(self) -> None:
        """Refresh synchronously if the token has already expired (e.g. after a suspended laptop)."""
        if time.monotonic() >= self._deadline:
            await self._do_refresh()

    async def force_refresh(self) -> None:
        """Refresh now (rate-limited), e.g. after an ``UNAUTHENTICATED`` response."""
        if time.monotonic() - self._last_refresh < FORCE_REFRESH_MIN_INTERVAL:
            return
        await self._do_refresh()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

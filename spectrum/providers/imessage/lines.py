"""iMessage line inventory and routing.

Cloud mode (project credentials) mints line tokens from
``POST /projects/{id}/imessage/tokens``:

* **shared** (Free/Pro): one gRPC client to ``imessage.spectrum.photon.codes:443``,
  phone label ``"shared"``.
* **dedicated** (Business): one client per line instance at
  ``{instanceId}.imsg.photon.codes:443``, phone = the line's E.164 number.

Tokens renew at 80% of their TTL; each renewal reconciles the line set
(new instance ids attach, missing ones detach), so lines provisioned while the
process runs appear at the next renewal.
"""

from __future__ import annotations

import logging
import os
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...cloud.client import CloudClient
from ...cloud.renewal import TokenRenewal
from ...cloud.types import IMessageTokens, LineMode
from ...core.errors import ConfigurationError
from ...core.utils import LRUCache
from .rpc import DEFAULT_RETRY, IMessageRpc, RetryPolicy

log = logging.getLogger("spectrum.imessage.lines")

SHARED_PHONE = "shared"
SHARED_ADDRESS = "imessage.spectrum.photon.codes:443"
DEDICATED_ADDRESS = "{instance_id}.imsg.photon.codes:443"


@dataclass(frozen=True, slots=True)
class LineConfig:
    """Explicit line (self-hosted server, or a pinned cloud line). Tokens are not renewed."""

    address: str
    token: str
    phone: str = SHARED_PHONE
    tls: bool = True

    def __repr__(self) -> str:
        return f"LineConfig(address={self.address!r}, phone={self.phone!r}, token='***')"


@dataclass(eq=False, slots=True)
class LineHandle:
    """A connected line and its per-line caches."""

    phone: str
    rpc: IMessageRpc
    instance_id: str | None = None
    message_cache: LRUCache[str, Any] = field(default_factory=lambda: LRUCache(2000))
    poll_cache: LRUCache[str, Any] = field(default_factory=lambda: LRUCache(500))
    seen_sequences: LRUCache[int, bool] = field(default_factory=lambda: LRUCache(5000))
    cursor: int | None = None

    @property
    def is_shared(self) -> bool:
        return self.phone == SHARED_PHONE

    def owns(self, address: str | None) -> bool:
        """Whether ``address`` is this line's own handle (never true for the shared sentinel)."""
        return not self.is_shared and address is not None and address == self.phone

    @property
    def key(self) -> str:
        return self.instance_id or self.phone


LineObserver = Callable[[LineHandle], Awaitable[None] | None]


class LineManager:
    def __init__(
        self,
        *,
        cloud: CloudClient | None = None,
        explicit: Sequence[LineConfig] | None = None,
        retry: RetryPolicy | None = DEFAULT_RETRY,
        timeout: float | None = 60.0,
        cache_size: int = 2000,
    ) -> None:
        if not cloud and not explicit:
            raise ConfigurationError("iMessage needs project credentials or explicit lines=[LineConfig(...)]")
        self._cloud = cloud
        self._explicit = list(explicit or [])
        self._retry = retry
        self._timeout = timeout
        self._cache_size = cache_size
        self._lines: dict[str, LineHandle] = {}
        self._tokens: IMessageTokens | None = None
        self._renewal: TokenRenewal | None = None
        self._on_attach: list[LineObserver] = []
        self._on_detach: list[LineObserver] = []

    # ------------------------------------------------------------------ properties

    @property
    def lines(self) -> tuple[LineHandle, ...]:
        return tuple(self._lines.values())

    @property
    def phones(self) -> tuple[str, ...]:
        return tuple(line.phone for line in self._lines.values())

    @property
    def mode(self) -> LineMode:
        if self.is_shared:
            return LineMode.SHARED
        return LineMode.DEDICATED

    @property
    def is_shared(self) -> bool:
        lines = self.lines
        return len(lines) == 1 and lines[0].is_shared

    def on_attach(self, observer: LineObserver) -> None:
        self._on_attach.append(observer)

    def on_detach(self, observer: LineObserver) -> None:
        self._on_detach.append(observer)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._explicit:
            for cfg in self._explicit:
                rpc = IMessageRpc(
                    cfg.address, cfg.token, tls=cfg.tls, retry=self._retry, timeout=self._timeout
                )
                await self._attach(self._new_handle(cfg.phone, rpc, None))
            return
        assert self._cloud is not None
        tokens = await self._cloud.issue_imessage_tokens()
        self._tokens = tokens
        log.info("imessage tokens issued: %r", tokens)
        renewal = TokenRenewal(
            "imessage", self._refresh, lambda: self._tokens.expires_in if self._tokens else 60
        )
        self._renewal = renewal
        if tokens.mode is LineMode.SHARED:
            address = os.environ.get("SPECTRUM_IMESSAGE_ADDRESS") or SHARED_ADDRESS
            rpc = IMessageRpc(
                address,
                self._shared_token,
                retry=self._retry,
                timeout=self._timeout,
                on_unauthenticated=self._force_refresh,
            )
            await self._attach(self._new_handle(SHARED_PHONE, rpc, None))
        else:
            await self._reconcile(tokens)
        renewal.start()

    async def close(self) -> None:
        if self._renewal:
            await self._renewal.close()
        for line in list(self._lines.values()):
            await line.rpc.close()
        self._lines.clear()

    # ------------------------------------------------------------------ routing

    def for_phone(self, phone: str | None) -> LineHandle:
        lines = self.lines
        if not lines:
            raise ConfigurationError("no iMessage lines are available")
        if self.is_shared:
            return lines[0]
        if phone is None:
            if len(lines) == 1:
                return lines[0]
            raise ConfigurationError(
                f"multiple dedicated lines; pass phone=. Available: {', '.join(self.phones)}"
            )
        for line in lines:
            if line.phone == phone:
                return line
        raise ConfigurationError(
            f"no iMessage line serves {phone}. Available: {', '.join(self.phones) or '<none>'}"
        )

    def random_line(self) -> LineHandle:
        lines = self.lines
        if not lines:
            raise ConfigurationError("no iMessage lines are available")
        return random.choice(lines)

    # ------------------------------------------------------------------ token handling

    def _new_handle(self, phone: str, rpc: IMessageRpc, instance_id: str | None) -> LineHandle:
        return LineHandle(
            phone=phone,
            rpc=rpc,
            instance_id=instance_id,
            message_cache=LRUCache(self._cache_size),
        )

    async def _shared_token(self) -> str:
        if self._renewal:
            await self._renewal.refresh_if_needed()
        assert self._tokens and self._tokens.token
        return self._tokens.token

    def _dedicated_token(self, instance_id: str, initial: str) -> Callable[[], Awaitable[str]]:
        async def provider() -> str:
            if self._renewal:
                await self._renewal.refresh_if_needed()
            if self._tokens and self._tokens.mode is LineMode.DEDICATED:
                return self._tokens.auth.get(instance_id, initial)
            return initial

        return provider

    async def _refresh(self) -> None:
        assert self._cloud is not None
        tokens = await self._cloud.issue_imessage_tokens()
        self._tokens = tokens
        if tokens.mode is LineMode.DEDICATED:
            try:
                await self._reconcile(tokens)
            except Exception:
                log.exception("imessage line reconcile failed")

    async def _force_refresh(self) -> None:
        if self._renewal:
            await self._renewal.force_refresh()

    async def _reconcile(self, tokens: IMessageTokens) -> None:
        removed = 0
        for instance_id in [k for k in self._lines if k not in tokens.auth]:
            line = self._lines.pop(instance_id)
            removed += 1
            await self._notify(self._on_detach, line)
            await line.rpc.close()
        added = 0
        for instance_id, token in tokens.auth.items():
            phone = tokens.numbers.get(instance_id)
            existing = self._lines.get(instance_id)
            if existing:
                if phone:
                    existing.phone = phone
                continue
            if not phone:
                log.warning("skipping imessage line %s without a phone number", instance_id)
                continue
            rpc = IMessageRpc(
                DEDICATED_ADDRESS.format(instance_id=instance_id),
                self._dedicated_token(instance_id, token),
                retry=self._retry,
                timeout=self._timeout,
                on_unauthenticated=self._force_refresh,
            )
            await self._attach(self._new_handle(phone, rpc, instance_id))
            added += 1
        if added or removed:
            log.info("imessage lines reconciled: +%d -%d (total %d)", added, removed, len(self._lines))

    async def _attach(self, line: LineHandle) -> None:
        self._lines[line.key] = line
        await self._notify(self._on_attach, line)

    @staticmethod
    async def _notify(observers: list[LineObserver], line: LineHandle) -> None:
        for observer in observers:
            try:
                result = observer(line)
                if result is not None:
                    await result
            except Exception:
                log.exception("line observer failed")

"""Framework-agnostic webhook handling: verify -> parse -> dispatch -> HTTP response."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core.errors import ConfigurationError, WebhookError
from ..core.utils import LRUCache
from .parser import WebhookEvent, WebhookParser
from .verify import DEFAULT_TOLERANCE, VerifiedDelivery, verify

if TYPE_CHECKING:
    from ..models.message import Message
    from ..models.space import Space

log = logging.getLogger("spectrum.webhook")

MessageHandler = Callable[["Space", "Message"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "text/plain; charset=utf-8"})

    @classmethod
    def text(cls, status: int, message: str) -> WebhookResponse:
        return cls(status, message.encode())


@dataclass(frozen=True, slots=True)
class WebhookResult:
    response: WebhookResponse
    delivery: VerifiedDelivery | None = None
    event: WebhookEvent | None = None
    duplicate: bool = False


class WebhookHandler:
    """Verifies and parses one delivery. Dedupes on ``webhookId:message.id`` (at-least-once delivery)."""

    def __init__(
        self,
        secret: str | None,
        parser: WebhookParser,
        *,
        tolerance: int = DEFAULT_TOLERANCE,
        dedupe_size: int = 10_000,
    ) -> None:
        self._secret = secret
        self._parser = parser
        self._tolerance = tolerance
        self._seen: LRUCache[str, bool] = LRUCache(dedupe_size)

    @property
    def secret(self) -> str | None:
        return self._secret

    @secret.setter
    def secret(self, value: str | None) -> None:
        self._secret = value

    @property
    def tolerance(self) -> int:
        return self._tolerance

    @tolerance.setter
    def tolerance(self, value: int) -> None:
        if value <= 0:
            raise ValueError("tolerance must be positive")
        self._tolerance = value

    def process(self, body: bytes, headers: Mapping[str, str] | list[tuple[str, str]]) -> WebhookResult:
        """Verify + parse. Never raises for delivery problems — they become HTTP responses."""
        if not self._secret:
            log.error("webhook delivery received but no webhook secret is configured")
            return WebhookResult(WebhookResponse.text(500, "webhook secret not configured"))
        try:
            delivery = verify(self._secret, body, headers, tolerance=self._tolerance)
        except WebhookError as exc:
            return WebhookResult(WebhookResponse.text(exc.status, str(exc)))
        try:
            event = self._parser.parse(body)
        except WebhookError as exc:
            log.warning("unparseable webhook body: %s", exc)
            return WebhookResult(WebhookResponse.text(400, str(exc)), delivery)
        duplicate = False
        if event.message is not None:
            duplicate = not self._seen.add(f"{delivery.webhook_id}:{event.message.id}")
        return WebhookResult(
            WebhookResponse(200, json.dumps({"ok": True}).encode(), {"Content-Type": "application/json"}),
            delivery,
            event,
            duplicate,
        )


def require_secret(secret: str | None) -> str:
    if not secret:
        raise ConfigurationError(
            "webhook_secret (or SPECTRUM_WEBHOOK_SECRET) is required to receive webhooks"
        )
    return secret

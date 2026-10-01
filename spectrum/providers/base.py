"""Provider abstraction — the Python counterpart of spectrum-ts ``definePlatform``.

A provider owns one platform's transport. It yields inbound ``Message`` objects
from ``stream()`` and handles every outbound ``Content`` in a single
``send(space, content)`` dispatcher. ``Space`` / ``Message`` actions are all
sugar over ``send``, so implementing ``send`` is enough to support them.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from ..core.errors import ConfigurationError, UnsupportedError
from ..models.content import Attachment, Content, ContentInput
from ..models.enums import ContentType, Direction, Platform, SpaceType
from ..models.message import Message
from ..models.space import AvatarData, Space
from ..models.user import User

if TYPE_CHECKING:
    from ..cloud.client import CloudClient
    from ..cloud.types import ProjectInfo
    from ..core.config import Credentials

# Content whose failure on an unsupported platform degrades to a logged no-op
# (spectrum-ts: reply / react / typing / read "no-op silently").
SOFT_UNSUPPORTED = frozenset({ContentType.REPLY, ContentType.REACTION, ContentType.TYPING, ContentType.READ})


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """What a provider receives at setup time."""

    credentials: Credentials | None
    cloud: CloudClient | None
    project: ProjectInfo | None
    dispatch: Callable[..., object] | None = None
    """Emit a client event (``dispatch("name", *args)`` -> ``on_name``)."""


class Provider(ABC):
    platform: ClassVar[Platform | str]
    requires_project: ClassVar[bool] = False

    def __init__(self) -> None:
        self._ctx: ProviderContext | None = None
        self._log = logging.getLogger(f"spectrum.{self.platform}")

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_ready(self) -> bool:
        return self._ctx is not None

    @property
    def context(self) -> ProviderContext:
        if self._ctx is None:
            raise ConfigurationError(f"{self.platform} provider is not set up (call client.login())")
        return self._ctx

    async def setup(self, ctx: ProviderContext) -> None:
        """Create clients / authenticate. Called once by ``Client.login()``."""
        if self.requires_project and ctx.credentials is None:
            raise ConfigurationError(
                f"{self.platform} requires project credentials "
                "(project_id/project_secret or SPECTRUM_PROJECT_ID/SPECTRUM_PROJECT_SECRET)"
            )
        self._ctx = ctx

    async def close(self) -> None:
        """Tear down clients. Must be idempotent."""
        self._ctx = None

    # ------------------------------------------------------------------ inbound

    def stream(self) -> AsyncIterator[Message]:
        """Inbound messages. Webhook-only providers keep the default empty stream."""
        return _empty()

    # ------------------------------------------------------------------ outbound

    @abstractmethod
    async def send(self, space: Space, content: Content) -> Message | None:
        """Dispatch one piece of content. Return the sent ``Message`` or ``None`` for signals."""

    async def deliver(self, space: Space, content: ContentInput) -> Message | None:
        """Send pipeline used by ``Space.send``: coerce, dispatch, normalize the result."""
        from ..content.builders import coerce

        item = coerce(content)
        try:
            result = await self.send(space, item)
        except UnsupportedError as exc:
            if item.type in SOFT_UNSUPPORTED:
                self._log.warning("%s skipped: %s", item.type.value, exc)
                return None
            raise
        if item.type.is_fire_and_forget:
            return None
        return result

    # ------------------------------------------------------------------ resolution

    async def resolve_user(self, user_id: str) -> User:
        return User(user_id, self.platform)

    async def create_space(self, users: Sequence[User | str], **params: Any) -> Space:
        raise UnsupportedError.action("space creation", str(self.platform))

    async def get_space(self, space_id: str, **params: Any) -> Space:
        return self.make_space(space_id)

    async def get_message(self, space: Space, message_id: str) -> Message | None:
        raise UnsupportedError.action("get_message", str(self.platform))

    async def get_members(self, space: Space) -> list[User]:
        raise UnsupportedError.action("get_members", str(self.platform))

    async def get_avatar(self, space: Space) -> AvatarData | None:
        raise UnsupportedError.action("get_avatar", str(self.platform))

    async def get_display_name(self, space: Space) -> str | None:
        raise UnsupportedError.action("get_display_name", str(self.platform))

    async def fetch_attachment(self, attachment_id: str, *, space: Space | None = None) -> Attachment | None:
        """Lazy byte source for metadata-only attachments (e.g. webhook deliveries)."""
        raise UnsupportedError.action("fetch_attachment", str(self.platform))

    # ------------------------------------------------------------------ helpers

    def make_space(
        self, space_id: str, *, type: SpaceType | None = None, phone: str | None = None, **extras: Any
    ) -> Space:
        return Space(space_id, self.platform, type=type, phone=phone, extras=extras or None, provider=self)

    def make_user(self, user_id: str, **fields: Any) -> User:
        return User(user_id, self.platform, **fields)

    def make_message(
        self,
        *,
        id: str,
        content: Content,
        space: Space,
        timestamp: datetime,
        direction: Direction = Direction.INBOUND,
        sender: User | None = None,
        **kwargs: Any,
    ) -> Message:
        return Message(
            id=id,
            content=content,
            space=space,
            timestamp=timestamp,
            direction=direction,
            sender=sender,
            platform=self.platform,
            **kwargs,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} platform={self.platform!s} ready={self.is_ready}>"


async def _empty() -> AsyncIterator[Message]:
    return
    yield  # pragma: no cover

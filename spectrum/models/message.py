"""An incoming or outgoing message."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, overload

from .base import Model
from .content import Content, ContentInput, Text
from .enums import ContentType, Direction, Platform

if TYPE_CHECKING:
    from .space import Space
    from .user import User


@dataclass(init=False, repr=False, eq=False, slots=True)
class Message(Model):
    """Message envelope. ``content`` is the discriminated payload (see ``ContentType``).

    Messages are compared by ``(platform, id)``.
    """

    __repr_fields__ = ("id", "platform", "direction", "sender", "content", "timestamp")
    _id: str
    _content: Content
    _direction: Direction
    _platform: Platform | str
    _sender: User | None
    _space: Space
    _timestamp: datetime
    _part_index: int | None
    _parent_id: str | None
    _metadata: dict[str, Any]
    _raw: Any

    def __init__(
        self,
        *,
        id: str,
        content: Content,
        space: Space,
        timestamp: datetime,
        direction: Direction | str = Direction.INBOUND,
        sender: User | None = None,
        platform: Platform | str | None = None,
        part_index: int | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        raw: Any = None,
    ) -> None:
        self._id = id
        self._content = content
        self._space = space
        self._timestamp = timestamp
        self._direction = Direction(direction)
        self._sender = sender
        self._platform = platform if platform is not None else space.platform
        self._part_index = part_index
        self._parent_id = parent_id
        self._metadata = dict(metadata or {})
        self._raw = raw

    # ------------------------------------------------------------------ properties

    @property
    def id(self) -> str:
        """Opaque, stable id. Dedupe on it; do not parse it."""
        return self._id

    @property
    def content(self) -> Content:
        return self._content

    @property
    def type(self) -> ContentType:
        """Shortcut for ``message.content.type``."""
        return self._content.type

    @property
    def text(self) -> str | None:
        """Text of a ``text`` message (or of a reply wrapping text); ``None`` otherwise."""
        content = self._content
        if content.type is ContentType.REPLY:
            content = content.content  # type: ignore[attr-defined]
        return content.text if isinstance(content, Text) else None

    @property
    def direction(self) -> Direction:
        return self._direction

    @property
    def is_inbound(self) -> bool:
        return self._direction is Direction.INBOUND

    @property
    def is_outbound(self) -> bool:
        return self._direction is Direction.OUTBOUND

    @property
    def platform(self) -> Platform | str:
        return self._platform

    @property
    def sender(self) -> User | None:
        """Who sent / acted. ``None`` when the platform recorded no actor."""
        return self._sender

    author = sender  # alias

    @property
    def space(self) -> Space:
        return self._space

    channel = space  # alias

    @property
    def timestamp(self) -> datetime:
        return self._timestamp

    created_at = timestamp  # alias

    @property
    def part_index(self) -> int | None:
        """Bubble index when this message is one part of a multipart message."""
        return self._part_index

    @property
    def parent_id(self) -> str | None:
        return self._parent_id

    @property
    def metadata(self) -> dict[str, Any]:
        """Provider-native metadata (e.g. iMessage delivery state)."""
        return self._metadata

    @property
    def raw(self) -> Any:
        """The provider's original object (e.g. the iMessage protobuf), if retained."""
        return self._raw

    # ------------------------------------------------------------------ actions

    @overload
    async def reply(self, content: ContentInput, /) -> Message | None: ...
    @overload
    async def reply(
        self, content: ContentInput, second: ContentInput, /, *rest: ContentInput
    ) -> list[Message]: ...

    async def reply(self, *contents: ContentInput) -> Message | None | list[Message]:
        """Threaded reply. No-op on platforms without threads (it is *not* downgraded to a send)."""
        from ..content.builders import reply

        return await self._space.send(*[reply(c, self) for c in contents])  # type: ignore[return-value]

    async def react(self, emoji: str) -> Message | None:
        """React; returns the reaction message (keep it to ``unsend()`` later)."""
        from ..content.builders import reaction

        return await self._space.send(reaction(emoji, self))

    async def edit(self, content: ContentInput) -> None:
        if not self.is_outbound:
            raise ValueError("only outbound messages can be edited")
        await self._space.edit(self, content)

    async def unsend(self) -> None:
        await self._space.unsend(self)

    async def read(self) -> None:
        """Mark this message (and everything before it) as read."""
        await self._space.read(self)

    mark_read = read

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Message) and other._id == self._id and other._platform == self._platform

    def __hash__(self) -> int:
        return hash((str(self._platform), self._id))

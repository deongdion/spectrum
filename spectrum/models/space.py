"""A conversation (DM, group chat, terminal session)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, overload

from .base import Model
from .content import (
    AddMember,
    ContentInput,
    Edit,
    LeaveSpace,
    Reaction,
    RemoveMember,
    Rename,
    Typing,
)
from .enums import Platform, SpaceType, TypingState

if TYPE_CHECKING:
    from ..providers.base import Provider
    from .message import Message
    from .user import User

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class AvatarData:
    data: bytes
    mime_type: str


@dataclass(init=False, repr=False, eq=False, slots=True)
class Space(Model):
    """Conversation bound to the provider that owns it.

    Every action is sugar for ``space.send(<builder>)`` so a single provider
    ``send`` dispatcher handles them all.
    """

    __repr_fields__ = ("id", "platform", "type", "phone")
    _id: str
    _platform: Platform | str
    _type: SpaceType | None
    _phone: str | None
    _extras: dict[str, Any]
    _provider: Provider | None

    def __init__(
        self,
        id: str,
        platform: Platform | str,
        *,
        type: SpaceType | str | None = None,
        phone: str | None = None,
        extras: dict[str, Any] | None = None,
        provider: Provider | None = None,
    ) -> None:
        self._id = id
        self._platform = platform
        self._type = SpaceType(type) if type is not None else None
        self._phone = phone
        self._extras = dict(extras or {})
        self._provider = provider

    # ------------------------------------------------------------------ properties

    @property
    def id(self) -> str:
        return self._id

    @property
    def platform(self) -> Platform | str:
        return self._platform

    @property
    def type(self) -> SpaceType | None:
        return self._type

    @property
    def is_dm(self) -> bool:
        return self._type is SpaceType.DM

    @property
    def is_group(self) -> bool:
        return self._type is SpaceType.GROUP

    @property
    def phone(self) -> str | None:
        """iMessage: the line handling this conversation (E.164, or ``"shared"`` on pooled lines)."""
        return self._phone

    @phone.setter
    def phone(self, value: str | None) -> None:
        self._phone = value

    @property
    def extras(self) -> dict[str, Any]:
        return self._extras

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            from ..core.errors import ConfigurationError

            raise ConfigurationError(
                f"space {self._id!r} is not bound to a running {self._platform} provider "
                "(register the provider on the client and call client.login())"
            )
        return self._provider

    @property
    def is_bound(self) -> bool:
        return self._provider is not None

    def bind(self, provider: Provider) -> Space:
        self._provider = provider
        return self

    # ------------------------------------------------------------------ sending

    @overload
    async def send(self, content: Reaction, /) -> Message | None: ...
    @overload
    async def send(self, content: ContentInput, /) -> Message | None: ...
    @overload
    async def send(
        self, content: ContentInput, second: ContentInput, /, *rest: ContentInput
    ) -> list[Message]: ...

    async def send(self, *contents: ContentInput) -> Message | None | list[Message]:
        """Send content into the space. Several items are sent sequentially as separate messages."""
        if not contents:
            raise ValueError("send() requires at least one content item")
        results = [await self.provider.deliver(self, item) for item in contents]
        if len(contents) == 1:
            return results[0]
        return [m for m in results if m is not None]

    async def edit(self, message: Message | None, content: ContentInput) -> None:
        from ..content.builders import coerce

        await self.send(Edit(coerce(content), _require(message, "edit")))

    async def unsend(self, message: Message | None) -> None:
        from ..content.builders import unsend

        await self.send(unsend(_require(message, "unsend")))

    async def read(self, message: Message) -> None:
        from ..content.builders import read

        await self.send(read(message))

    async def start_typing(self) -> None:
        await self.send(Typing(TypingState.START))

    async def stop_typing(self) -> None:
        await self.send(Typing(TypingState.STOP))

    @contextlib.asynccontextmanager
    async def typing(self) -> AsyncGenerator[None, None]:
        """``async with space.typing(): ...`` — the indicator is cleared even on error."""
        await self.start_typing()
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await self.stop_typing()

    async def responding(self, fn: Callable[[], Awaitable[T] | T]) -> T:
        async with self.typing():
            result = fn()
            if isinstance(result, Awaitable):
                return await result  # type: ignore[no-any-return]
            return result

    # ------------------------------------------------------------------ chat management

    async def rename(self, display_name: str) -> None:
        await self.send(Rename(display_name))

    async def set_avatar(self, source: str | bytes | None, *, mime_type: str | None = None) -> None:
        """Set the group icon from a path / bytes, or clear it with ``None`` / ``"clear"``."""
        from ..content.builders import avatar

        await self.send(avatar(source, mime_type=mime_type))

    async def add(self, users: Any) -> None:
        await self.send(AddMember(_members(users)))

    async def remove(self, users: Any) -> None:
        await self.send(RemoveMember(_members(users)))

    async def leave(self) -> None:
        await self.send(LeaveSpace())

    async def fetch_members(self) -> list[User]:
        return await self.provider.get_members(self)

    async def fetch_avatar(self) -> AvatarData | None:
        return await self.provider.get_avatar(self)

    async def fetch_display_name(self) -> str | None:
        return await self.provider.get_display_name(self)

    async def fetch_message(self, message_id: str) -> Message | None:
        return await self.provider.get_message(self, message_id)

    # spectrum-ts naming aliases
    get_members = fetch_members
    get_avatar = fetch_avatar
    get_display_name = fetch_display_name
    get_message = fetch_message

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Space) and other._id == self._id and other._platform == self._platform

    def __hash__(self) -> int:
        return hash((str(self._platform), self._id))


def _require(message: Message | None, action: str) -> Message:
    if message is None:
        raise ValueError(f"{action}() target message is None")
    return message


def _members(users: Any) -> list[Any]:
    if isinstance(users, str) or not isinstance(users, Iterable):
        return [users]
    return list(users)

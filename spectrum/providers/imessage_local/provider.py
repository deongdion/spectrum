"""iMessage on a Mac you control (no Photon cloud): read ``chat.db``, send with AppleScript.

Requirements on the Mac:
* Messages signed in to the Apple ID the bot should use
* the terminal / IDE running Python has **Full Disk Access**
* allow "control Messages" the first time a message is sent (Automation permission)

Supported: text, markdown (sent as plain text), attachments, voice notes, contact cards,
links, replies (sent as normal messages), inbound tapbacks. Not available through AppleScript:
sending tapbacks, edit, unsend, read receipts, typing indicators, effects, group management.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from ...content.formatting import to_plain_text
from ...content.vcard import to_vcard, vcard_file_name
from ...core.errors import ConfigurationError, UnsupportedError
from ...core.utils import LRUCache, utcnow
from ...models.content import (
    App,
    Attachment,
    Contact,
    Content,
    Custom,
    Effect,
    Group,
    Markdown,
    Reaction,
    Reply,
    RichLink,
    StreamText,
    Text,
    Typing,
    Voice,
)
from ...models.enums import AddressService, Direction, Platform, SpaceType, Tapback
from ...models.message import Message
from ...models.space import Space
from ...models.user import User
from ..base import Provider, ProviderContext
from . import applescript
from .database import DEFAULT_DB, AttachmentRow, MessageRow, MessagesDatabase

log = logging.getLogger("spectrum.local_imessage")

# associated_message_type: 2000-2005 add a tapback, 3000-3005 remove it
_TAPBACKS = {
    0: Tapback.LOVE,
    1: Tapback.LIKE,
    2: Tapback.DISLIKE,
    3: Tapback.LAUGH,
    4: Tapback.EMPHASIZE,
    5: Tapback.QUESTION,
}
_SERVICES = {"iMessage": AddressService.IMESSAGE, "SMS": AddressService.SMS, "RCS": AddressService.RCS}


class LocalIMessage(Provider):
    """iMessage through the Messages app on this Mac.

    Args:
        database: path to ``chat.db`` (default ``~/Library/Messages/chat.db``).
        poll_interval: seconds between database checks for new messages.
    """

    platform = Platform.LOCAL_IMESSAGE

    def __init__(self, *, database: str | Path = DEFAULT_DB, poll_interval: float = 1.0) -> None:
        super().__init__()
        self._db = MessagesDatabase(Path(database))
        self._poll_interval = poll_interval
        self._cache: LRUCache[str, Message] = LRUCache(2000)
        self._streaming = False

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @poll_interval.setter
    def poll_interval(self, value: float) -> None:
        if value <= 0:
            raise ValueError("poll_interval must be positive")
        self._poll_interval = value

    # ------------------------------------------------------------------ lifecycle

    async def setup(self, ctx: ProviderContext) -> None:
        if sys.platform != "darwin":
            raise ConfigurationError(
                "LocalIMessage only runs on macOS (it reads the Messages app's database)"
            )
        try:
            await asyncio.to_thread(self._db.open)
        except Exception as exc:
            raise ConfigurationError(
                f"cannot read {self._db.path}: {exc}. Grant Full Disk Access to this terminal/IDE in "
                "System Settings → Privacy & Security → Full Disk Access, then restart it."
            ) from exc
        await super().setup(ctx)

    async def close(self) -> None:
        self._streaming = False
        self._db.close()
        await super().close()

    # ------------------------------------------------------------------ inbound

    async def stream(self) -> AsyncIterator[Message]:
        self._streaming = True
        last = await asyncio.to_thread(self._db.max_rowid)  # only messages that arrive from now on
        log.info("watching %s from ROWID %s", self._db.path, last)
        while self._streaming:
            rows = await asyncio.to_thread(self._db.messages_after, last)
            for row in rows:
                last = max(last, row.rowid)
                message = self._to_message(row)
                if message is not None:
                    yield message
            if not rows:
                await asyncio.sleep(self._poll_interval)

    def _space(self, row: MessageRow) -> Space:
        chat = row.chat_guid or (f"iMessage;-;{row.handle}" if row.handle else "unknown")
        return self.make_space(chat, type=SpaceType.GROUP if row.is_group else SpaceType.DM)

    def _user(self, row: MessageRow) -> User | None:
        if not row.handle:
            return None
        return self.make_user(row.handle, address=row.handle, service=_SERVICES.get(row.service or ""))

    def _to_message(self, row: MessageRow) -> Message | None:
        built = self._build(row)
        if built is not None:
            self._cache.set(built.id, built)
        if row.is_from_me or row.item_type != 0:
            return None  # own echoes and group-event rows are not surfaced
        return built

    def _build(self, row: MessageRow) -> Message | None:
        space = self._space(row)
        common: dict[str, Any] = dict(
            space=space,
            timestamp=row.date,
            direction=Direction.OUTBOUND if row.is_from_me else Direction.INBOUND,
            sender=self._user(row),
            metadata={"service": row.service, "rowid": row.rowid},
            raw=row,
        )
        if row.reaction_type:
            return self._reaction(row, common)
        parts = _ordered_parts(row.text, row.attachments)
        if not parts:
            return None
        if len(parts) == 1:
            return self.make_message(id=row.guid, content=_part_content(parts[0]), **common)
        items = [
            self.make_message(
                id=f"p:{i}/{row.guid}",
                content=_part_content(part),
                part_index=i,
                parent_id=row.guid,
                **common,
            )
            for i, part in enumerate(parts)
        ]
        return self.make_message(id=row.guid, content=Group(items), **common)

    def _reaction(self, row: MessageRow, common: dict[str, Any]) -> Message | None:
        kind = row.reaction_type
        if not 2000 <= kind <= 2005 or not row.reaction_target:
            return None  # removals (3000+), stickers and other associated rows
        tapback = _TAPBACKS.get(kind - 2000)
        target_guid = row.reaction_target.split("/", 1)[-1]  # "p:0/GUID" or "bp:GUID"
        target_guid = target_guid.removeprefix("bp:")
        target = self._cache.get(target_guid)
        if target is None:
            found = self._db.message_by_guid(target_guid)
            target = self._build(found) if found else None
        if tapback is None or target is None or isinstance(target.content, Reaction):
            return None
        return self.make_message(id=row.guid, content=Reaction(tapback.emoji, target), **common)

    # ------------------------------------------------------------------ outbound

    async def send(self, space: Space, content: Content) -> Message | None:
        match content:
            case Text():
                await applescript.send(space.id, content.text)
            case Markdown():
                await applescript.send(space.id, to_plain_text(content.markdown))
            case StreamText():
                await applescript.send(space.id, await content.collect())
            case RichLink() | App():
                await applescript.send(space.id, content.url)
            case Effect():
                return await self.send(space, content.content)  # effects need the private API
            case Reply():
                return await self.send(space, content.content)  # no threading via AppleScript
            case Attachment() | Voice():
                await self._send_bytes(space, await content.read(), content.name or "file")
            case Contact():
                await self._send_bytes(space, to_vcard(content).encode(), vcard_file_name(content))
            case Group():
                for item in content.items:
                    await self.send(space, item.content if isinstance(item, Message) else item)
            case Typing():
                return None  # accepted as a no-op
            case Custom():
                raise UnsupportedError.content("custom", str(self.platform))
            case _:
                raise UnsupportedError.content(
                    content.type.value, str(self.platform), "not available via AppleScript"
                )
        message = self.make_message(
            id=f"local-{uuid.uuid4()}",
            content=content,
            space=space,
            timestamp=utcnow(),
            direction=Direction.OUTBOUND,
            sender=self.make_user("me", is_agent=True),
        )
        self._cache.set(message.id, message)
        return message

    async def _send_bytes(self, space: Space, data: bytes, name: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / Path(name).name
            source.write_bytes(data)
            staged = await asyncio.to_thread(applescript.stage_file, source)
        await applescript.send(space.id, str(staged), is_file=True)

    # ------------------------------------------------------------------ resolution

    async def resolve_user(self, user_id: str) -> User:
        return self.make_user(user_id, address=user_id)

    async def create_space(self, users: Sequence[User | str], **params: Any) -> Space:
        addresses = [u.id if isinstance(u, User) else u for u in users]
        if len(addresses) != 1:
            raise UnsupportedError.action(
                "group creation", str(self.platform), "AppleScript can only start DMs"
            )
        return self.make_space(f"iMessage;-;{addresses[0]}", type=SpaceType.DM)

    async def get_space(self, space_id: str, **params: Any) -> Space:
        return self.make_space(space_id, type=SpaceType.GROUP if ";+;" in space_id else SpaceType.DM)

    async def get_message(self, space: Space, message_id: str) -> Message | None:
        cached = self._cache.get(message_id)
        if cached is not None:
            return cached
        row = await asyncio.to_thread(self._db.message_by_guid, message_id)
        return self._build(row) if row else None


def _ordered_parts(text: str | None, attachments: list[AttachmentRow]) -> list[tuple[str, Any]]:
    """Text segments and attachments in display order (U+FFFC marks attachment slots)."""
    parts: list[tuple[str, Any]] = []
    segments = (text or "").split("￼")
    for i, attachment in enumerate(attachments):
        if i < len(segments) and segments[i].strip():
            parts.append(("text", segments[i].strip()))
        parts.append(("attachment", attachment))
    rest = "".join(segments[len(attachments) :]).strip()
    if rest:
        parts.append(("text", rest))
    return parts


def _part_content(part: tuple[str, Any]) -> Content:
    kind, value = part
    if kind == "text":
        return Text(value)
    row: AttachmentRow = value

    async def read() -> bytes:
        return await asyncio.to_thread(row.path.read_bytes)

    mime = row.mime_type or "application/octet-stream"
    return Attachment(id=str(row.path), name=row.name, mime_type=mime, size=row.size, reader=read)

"""Content builders — the canonical way to construct outbound content.

Each builder validates its input at construction time (spectrum-ts semantics)
and returns a ``Content`` instance that any ``space.send(...)`` accepts. A bare
``str`` is always equivalent to ``text(str)``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import Any

from ..core.errors import ContentError
from ..models.content import (
    AddMember,
    App,
    Attachment,
    Avatar,
    Background,
    ByteReader,
    ByteStreamer,
    Contact,
    ContactCard,
    ContactField,
    ContactName,
    Content,
    ContentInput,
    Custom,
    DeltaExtractor,
    Edit,
    Effect,
    Group,
    LeaveSpace,
    Markdown,
    MiniApp,
    MiniAppLayout,
    Poll,
    PollChoice,
    Reaction,
    Read,
    RemoveMember,
    Rename,
    Reply,
    RichLink,
    StreamSource,
    StreamText,
    Text,
    Typing,
    Unsend,
    Voice,
)
from ..models.enums import AvatarActionKind, Direction, MessageEffect, StreamFormat, TypingState
from ..models.message import Message
from ..models.user import User
from .mime import guess_mime_type
from .vcard import from_vcard

CLEAR = "clear"
"""Reserved sentinel for ``avatar()`` / ``background()``. Use ``"./clear"`` for a file literally named clear."""

FileSource = str | os.PathLike[str] | bytes | bytearray


def coerce(value: ContentInput) -> Content:
    """``str`` -> ``Text``; ``Content`` passes through."""
    if isinstance(value, Content):
        return value
    if isinstance(value, str):
        return Text(value)
    raise TypeError(f"expected str or Content, got {type(value).__name__}")


# --------------------------------------------------------------------------- text


def text(value: str | StreamSource, *, extract: DeltaExtractor | None = None) -> Text | StreamText:
    """Plain text, or a live text stream when given an (async) iterable of chunks."""
    if isinstance(value, str):
        return Text(value)
    return StreamText(value, format=StreamFormat.PLAIN, extract=extract)


def markdown(value: str | StreamSource, *, extract: DeltaExtractor | None = None) -> Markdown | StreamText:
    """Styled text (outbound only). Streams render progressively where supported."""
    if isinstance(value, str):
        return Markdown(value)
    return StreamText(value, format=StreamFormat.MARKDOWN, extract=extract)


# --------------------------------------------------------------------------- files


def _is_url(value: object) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _file_reader(path: Path) -> ByteReader:
    async def read() -> bytes:
        import asyncio

        return await asyncio.to_thread(path.read_bytes)

    return read


def _url_reader(url: str) -> ByteReader:
    async def read() -> bytes:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http:
            response = await http.get(url)
            response.raise_for_status()
            return response.content

    return read


def _url_streamer(url: str) -> ByteStreamer:
    async def stream() -> AsyncIterator[bytes]:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http:
            async with http.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    yield chunk

    return stream


def _resolve_file(
    source: FileSource, name: str | None, mime_type: str | None, kind: str
) -> tuple[str, str, bytes | None, ByteReader | None, ByteStreamer | None, int | None]:
    data: bytes | None = None
    reader: ByteReader | None = None
    streamer: ByteStreamer | None = None
    size: int | None = None
    if isinstance(source, bytes | bytearray):
        data = bytes(source)
        if not name:
            raise ContentError(f"{kind}() from bytes requires name=")
    elif _is_url(source):
        url = str(source)
        name = name or (url.rsplit("/", 1)[-1].split("?", 1)[0] or "file")
        reader, streamer = _url_reader(url), _url_streamer(url)
    else:
        path = Path(os.fspath(source))
        if not path.is_file():
            raise ContentError(f"{kind}() file not found: {path}")
        name = name or path.name
        size = path.stat().st_size
        reader = _file_reader(path)
    mime = mime_type or guess_mime_type(name)
    if not mime:
        raise ContentError(f"cannot infer the MIME type of {name!r}; pass mime_type=")
    return name, mime, data, reader, streamer, size


def attachment(
    source: FileSource, *, name: str | None = None, mime_type: str | None = None, id: str | None = None
) -> Attachment:
    """File from a path, an ``http(s)`` URL or raw bytes. Bytes are read lazily at send time."""
    name, mime, data, reader, streamer, size = _resolve_file(source, name, mime_type, "attachment")
    return Attachment(
        id=id or str(uuid.uuid4()),
        name=name,
        mime_type=mime,
        size=size,
        data=data,
        reader=reader,
        streamer=streamer,
    )


def voice(
    source: FileSource,
    *,
    name: str | None = None,
    mime_type: str | None = None,
    duration: float | None = None,
    id: str | None = None,
) -> Voice:
    """Voice note (``duration`` in seconds helps waveform UIs)."""
    name, mime, data, reader, streamer, size = _resolve_file(source, name, mime_type, "voice")
    return Voice(
        id=id or str(uuid.uuid4()),
        name=name,
        mime_type=mime,
        duration=duration,
        size=size,
        data=data,
        reader=reader,
        streamer=streamer,
    )


# --------------------------------------------------------------------------- contacts / links


def contact(value: Contact | User | str | dict[str, Any], details: dict[str, Any] | None = None) -> Contact:
    """Contact card from a ``Contact``, a vCard string, a ``User`` (+ details) or a dict of fields."""
    if isinstance(value, Contact):
        return value
    if isinstance(value, str):
        if "BEGIN:VCARD" not in value.upper():
            raise ContentError("contact() string input must be a vCard")
        return from_vcard(value)
    fields: dict[str, Any] = dict(details or {})
    if isinstance(value, User):
        fields["user"] = value
    elif isinstance(value, dict):
        fields = {**value, **fields}
    else:
        raise TypeError(f"unsupported contact input {type(value).__name__}")
    name = fields.get("name")
    if isinstance(name, dict):
        fields["name"] = ContactName(**name)
    for key in ("phones", "emails"):
        fields[key] = [ContactField(**f) if isinstance(f, dict) else f for f in fields.get(key, ())]
    return Contact(**fields)


def richlink(url: str) -> RichLink:
    return RichLink(url)


def app(url: str, *, live: bool = False, layout: MiniAppLayout | dict[str, Any] | None = None) -> App:
    """Tappable app card (iMessage) / plain link elsewhere.

    ``live`` requests the extension's live UI. Without ``layout`` the card is built from the
    URL's Open Graph metadata at send time (title, description, preview image).
    """
    if isinstance(layout, dict):
        layout = MiniAppLayout(**layout)
    return App(url, live=live, layout=layout)


def poll(title: str, *options: str | PollChoice | Sequence[str | PollChoice]) -> Poll:
    """``poll("Lunch?", "Pizza", "Sushi")`` or ``poll("Lunch?", ["Pizza", "Sushi"])``."""
    flat: list[str | PollChoice] = []
    for option in options:
        if isinstance(option, str | PollChoice):
            flat.append(option)
        else:
            flat.extend(option)
    return Poll(title, flat)


def option(title: str) -> PollChoice:
    return PollChoice(title.strip())


def group(*items: ContentInput) -> Group:
    """Bundle several items into one visual unit (album). Groups don't nest."""
    return Group([coerce(i) for i in items])


def custom(raw: Any) -> Custom:
    return Custom(raw)


def effect(content: ContentInput, message_effect: MessageEffect | str) -> Effect:
    """Wrap text / markdown / an attachment with an iMessage bubble or screen effect."""
    return Effect(coerce(content), message_effect)


# --------------------------------------------------------------------------- message-targeted


def reply(content: ContentInput, target: Message) -> Reply:
    return Reply(coerce(content), target)


def edit(content: ContentInput, target: Message | None) -> Edit:
    if target is None:
        raise ContentError("edit() target is None")
    return Edit(coerce(content), target)


def reaction(emoji: str, target: Message) -> Reaction:
    return Reaction(emoji, target)


def unsend(target: Message | None) -> Unsend:
    if target is None:
        raise ContentError("unsend() target is None")
    if target.direction is not Direction.OUTBOUND:
        raise ContentError("only outbound messages can be unsent")
    return Unsend(target)


def read(target: Message) -> Read:
    if target.direction is not Direction.INBOUND:
        raise ContentError("only inbound messages can be marked read")
    return Read(target)


def typing(state: TypingState | str = TypingState.START) -> Typing:
    return Typing(state)


# --------------------------------------------------------------------------- chat management


def rename(display_name: str) -> Rename:
    return Rename(display_name)


def avatar(source: FileSource | None, *, mime_type: str | None = None) -> Avatar:
    """Group icon from a path / URL / bytes, or ``None`` / ``"clear"`` to remove it."""
    if source is None or source == CLEAR:
        return Avatar(AvatarActionKind.CLEAR)
    if isinstance(source, bytes | bytearray):
        if not mime_type:
            raise ContentError("avatar() from bytes requires mime_type=")
        return Avatar(AvatarActionKind.SET, mime_type=mime_type, data=bytes(source))
    _, mime, data, reader, _, _ = _resolve_file(source, None, mime_type, "avatar")
    return Avatar(AvatarActionKind.SET, mime_type=mime, data=data, reader=reader)


def _members(users: User | str | Iterable[User | str]) -> list[User | str]:
    if isinstance(users, str | User):
        return [users]
    return list(users)


def add_member(users: User | str | Iterable[User | str]) -> AddMember:
    members = _members(users)
    if not members:
        raise ContentError("add_member() requires at least one member")
    return AddMember(members)


def remove_member(users: User | str | Iterable[User | str]) -> RemoveMember:
    members = _members(users)
    if not members:
        raise ContentError("remove_member() requires at least one member")
    return RemoveMember(members)


def leave_space() -> LeaveSpace:
    return LeaveSpace()


# --------------------------------------------------------------------------- iMessage-only


async def background(source: FileSource | None, *, mime_type: str | None = None) -> Background:
    """iMessage chat background from a path / URL / bytes, or ``None`` / ``"clear"`` to remove it."""
    if source is None or source == CLEAR:
        return Background(None)
    if isinstance(source, bytes | bytearray):
        if not mime_type:
            raise ContentError("background() from bytes requires mime_type=")
        return Background(bytes(source), mime_type=mime_type)
    _, mime, data, reader, _, _ = _resolve_file(source, None, mime_type, "background")
    payload = data if data is not None else await reader()  # type: ignore[misc]
    return Background(payload, mime_type=mime)


def contact_card() -> ContactCard:
    """Share the agent account's own native iMessage contact card."""
    return ContactCard()


def mini_app(
    *,
    app_name: str,
    extension_bundle_id: str,
    team_id: str,
    url: str,
    layout: MiniAppLayout | dict[str, Any],
    app_store_id: int | None = None,
    live: bool = False,
) -> MiniApp:
    """Customized iMessage App card for an extension you own."""
    if isinstance(layout, dict):
        layout = MiniAppLayout(**layout)
    return MiniApp(
        app_name=app_name,
        extension_bundle_id=extension_bundle_id,
        team_id=team_id,
        url=url,
        layout=layout,
        app_store_id=app_store_id,
        live=live,
    )


# spectrum-ts naming aliases
addMember = add_member  # noqa: N816
removeMember = remove_member  # noqa: N816
leaveSpace = leave_space  # noqa: N816

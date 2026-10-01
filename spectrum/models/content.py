"""Content variants — the discriminated union carried by every ``Message``.

Each class sets ``type`` (a ``ContentType``) as a ``ClassVar``; branch on
``message.content.type`` or use ``isinstance``. Users normally build these
through the helpers in ``spectrum.content`` (``text()``, ``attachment()`` ...),
which add validation; constructing them directly is also supported.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ..core.errors import ContentError
from .base import Model
from .enums import (
    UNWRAPPABLE_CONTENT,
    AvatarActionKind,
    ContactFieldType,
    ContentType,
    MessageEffect,
    StreamFormat,
    TypingState,
)

if TYPE_CHECKING:
    from .message import Message
    from .user import User

ByteReader = Callable[[], Awaitable[bytes]]
ByteStreamer = Callable[[], AsyncIterator[bytes]]


def _require_str(name: str, value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContentError(f"{name} must be a str, got {type(value).__name__}")
    if not allow_empty and not value:
        raise ContentError(f"{name} must not be empty")
    return value


class Content(Model):
    """Abstract base of every content variant."""

    __slots__ = ()
    type: ClassVar[ContentType]

    @property
    def is_fire_and_forget(self) -> bool:
        return self.type.is_fire_and_forget


# --------------------------------------------------------------------------- text


@dataclass(init=False, repr=False, slots=True)
class Text(Content):
    type: ClassVar[ContentType] = ContentType.TEXT
    __repr_fields__ = ("text",)
    _text: str

    def __init__(self, text: str) -> None:
        self.text = text

    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str) -> None:
        self._text = _require_str("text", value, allow_empty=True)


@dataclass(init=False, repr=False, slots=True)
class Markdown(Content):
    """Outbound-only styled text (CommonMark + GFM strikethrough)."""

    type: ClassVar[ContentType] = ContentType.MARKDOWN
    __repr_fields__ = ("markdown",)
    _markdown: str

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown

    @property
    def markdown(self) -> str:
        return self._markdown

    @markdown.setter
    def markdown(self, value: str) -> None:
        self._markdown = _require_str("markdown", value)


StreamSource = AsyncIterable[Any] | Iterable[Any]
DeltaExtractor = Callable[[Any], str | None]


@dataclass(init=False, repr=False, slots=True)
class StreamText(Content):
    """Text produced incrementally (e.g. LLM tokens). A stream can be sent only once."""

    type: ClassVar[ContentType] = ContentType.STREAM_TEXT
    __repr_fields__ = ("format", "consumed")
    _source: StreamSource
    _format: StreamFormat
    _extract: DeltaExtractor | None
    _consumed: bool

    def __init__(
        self,
        source: StreamSource,
        *,
        format: StreamFormat = StreamFormat.PLAIN,
        extract: DeltaExtractor | None = None,
    ) -> None:
        if isinstance(source, str | bytes):
            raise ContentError("StreamText source must be an iterable of chunks, not a string")
        self._source = source
        self._format = StreamFormat(format)
        self._extract = extract
        self._consumed = False

    @property
    def format(self) -> StreamFormat:
        return self._format

    @property
    def consumed(self) -> bool:
        return self._consumed

    async def chunks(self) -> AsyncIterator[str]:
        """Yield text deltas. Raises if the stream was already consumed."""
        if self._consumed:
            raise ContentError("a text stream can only be sent once")
        self._consumed = True
        if isinstance(self._source, AsyncIterable):
            async for chunk in self._source:
                delta = self._delta(chunk)
                if delta:
                    yield delta
        else:
            for chunk in self._source:
                delta = self._delta(chunk)
                if delta:
                    yield delta

    def _delta(self, chunk: Any) -> str | None:
        if self._extract is not None:
            return self._extract(chunk)
        return _auto_extract(chunk)

    async def collect(self) -> str:
        return "".join([c async for c in self.chunks()])


def _auto_extract(chunk: Any) -> str | None:
    """Best-effort delta extraction for plain strings and common LLM SDK chunk shapes."""
    if chunk is None:
        return None
    if isinstance(chunk, str):
        return chunk
    get = chunk.get if isinstance(chunk, dict) else (lambda k, d=None: getattr(chunk, k, d))
    # Anthropic: content_block_delta -> delta.text
    delta = get("delta")
    if delta is not None:
        dget = delta.get if isinstance(delta, dict) else (lambda k, d=None: getattr(delta, k, d))
        text = dget("text")
        if isinstance(text, str):
            return text
        if isinstance(delta, str):  # OpenAI responses: response.output_text.delta
            return delta
    # OpenAI chat: choices[0].delta.content
    choices = get("choices")
    if choices:
        first = choices[0]
        fdelta = first.get("delta") if isinstance(first, dict) else getattr(first, "delta", None)
        if fdelta is not None:
            content = fdelta.get("content") if isinstance(fdelta, dict) else getattr(fdelta, "content", None)
            if isinstance(content, str):
                return content
    text = get("text")
    return text if isinstance(text, str) else None


# --------------------------------------------------------------------------- binary


class _Readable(Content):
    """Mixin for byte-bearing content with lazy ``read()`` / ``stream()``."""

    __slots__ = ()
    _data: bytes | None
    _reader: ByteReader | None
    _streamer: ByteStreamer | None

    @property
    def is_loaded(self) -> bool:
        return self._data is not None

    async def read(self) -> bytes:
        """Materialize the bytes (cached after the first call)."""
        if self._data is None:
            if self._reader is None:
                raise ContentError(f"{type(self).__name__} has no byte source (metadata-only)")
            self._data = await self._reader()
        return self._data

    async def stream(self) -> AsyncIterator[bytes]:
        """Stream the bytes. Prefer this for large files when the provider supports it."""
        if self._data is None and self._streamer is not None:
            async for chunk in self._streamer():
                yield chunk
            return
        yield await self.read()


@dataclass(init=False, repr=False, slots=True)
class Attachment(_Readable):
    type: ClassVar[ContentType] = ContentType.ATTACHMENT
    __repr_fields__ = ("id", "name", "mime_type", "size")
    _id: str
    _name: str
    _mime_type: str
    _size: int | None
    _data: bytes | None
    _reader: ByteReader | None
    _streamer: ByteStreamer | None

    def __init__(
        self,
        *,
        id: str,
        name: str,
        mime_type: str,
        size: int | None = None,
        data: bytes | None = None,
        reader: ByteReader | None = None,
        streamer: ByteStreamer | None = None,
    ) -> None:
        self._id = _require_str("id", id)
        self.name = name
        self.mime_type = mime_type
        self._size = size if size is not None else (len(data) if data is not None else None)
        self._data = data
        self._reader = reader
        self._streamer = streamer

    @property
    def id(self) -> str:
        """Stable id — provider-native (e.g. iMessage attachment GUID) for inbound files."""
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value: str) -> None:
        self._name = _require_str("name", value)

    @property
    def mime_type(self) -> str:
        return self._mime_type

    @mime_type.setter
    def mime_type(self, value: str) -> None:
        value = _require_str("mime_type", value)
        if "/" not in value:
            raise ContentError(f"invalid MIME type {value!r}")
        self._mime_type = value

    @property
    def size(self) -> int | None:
        return self._size

    @property
    def is_image(self) -> bool:
        return self._mime_type.startswith("image/")

    @property
    def is_audio(self) -> bool:
        return self._mime_type.startswith("audio/")

    @property
    def is_video(self) -> bool:
        return self._mime_type.startswith("video/")


@dataclass(init=False, repr=False, slots=True)
class Voice(_Readable):
    """A voice note. Platforms without voice notes downgrade to an audio attachment."""

    type: ClassVar[ContentType] = ContentType.VOICE
    __repr_fields__ = ("id", "name", "mime_type", "duration", "size")
    _id: str
    _name: str | None
    _mime_type: str
    _duration: float | None
    _size: int | None
    _data: bytes | None
    _reader: ByteReader | None
    _streamer: ByteStreamer | None

    def __init__(
        self,
        *,
        id: str,
        mime_type: str,
        name: str | None = None,
        duration: float | None = None,
        size: int | None = None,
        data: bytes | None = None,
        reader: ByteReader | None = None,
        streamer: ByteStreamer | None = None,
    ) -> None:
        self._id = _require_str("id", id)
        self._name = name
        if not mime_type.startswith("audio/"):
            raise ContentError(f"voice MIME type must be audio/*, got {mime_type!r}")
        self._mime_type = mime_type
        self.duration = duration
        self._size = size if size is not None else (len(data) if data is not None else None)
        self._data = data
        self._reader = reader
        self._streamer = streamer

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def mime_type(self) -> str:
        return self._mime_type

    @property
    def size(self) -> int | None:
        return self._size

    @property
    def duration(self) -> float | None:
        """Length in seconds."""
        return self._duration

    @duration.setter
    def duration(self, value: float | None) -> None:
        if value is not None and value < 0:
            raise ContentError("duration must be non-negative")
        self._duration = value


# --------------------------------------------------------------------------- contact


@dataclass(frozen=True, slots=True)
class ContactName:
    formatted: str | None = None
    first: str | None = None
    last: str | None = None
    middle: str | None = None
    prefix: str | None = None
    suffix: str | None = None

    @property
    def display(self) -> str | None:
        if self.formatted:
            return self.formatted
        parts = [p for p in (self.prefix, self.first, self.middle, self.last, self.suffix) if p]
        return " ".join(parts) or None


@dataclass(frozen=True, slots=True)
class ContactField:
    value: str
    type: ContactFieldType | None = None


@dataclass(frozen=True, slots=True)
class ContactAddress:
    street: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    type: ContactFieldType | None = None


@dataclass(frozen=True, slots=True)
class ContactOrg:
    name: str | None = None
    title: str | None = None
    department: str | None = None


@dataclass(init=False, repr=False, slots=True)
class Contact(Content):
    type: ClassVar[ContentType] = ContentType.CONTACT
    __repr_fields__ = ("name", "phones", "emails", "org")
    _name: ContactName | None
    _phones: list[ContactField]
    _emails: list[ContactField]
    _addresses: list[ContactAddress]
    _org: ContactOrg | None
    _urls: list[str]
    _birthday: str | None
    _note: str | None
    _photo: Attachment | None
    _user: User | None
    _raw: Any

    def __init__(
        self,
        *,
        name: ContactName | None = None,
        phones: Sequence[ContactField] = (),
        emails: Sequence[ContactField] = (),
        addresses: Sequence[ContactAddress] = (),
        org: ContactOrg | None = None,
        urls: Sequence[str] = (),
        birthday: str | None = None,
        note: str | None = None,
        photo: Attachment | None = None,
        user: User | None = None,
        raw: Any = None,
    ) -> None:
        self._name = name
        self._phones = list(phones)
        self._emails = list(emails)
        self._addresses = list(addresses)
        self._org = org
        self._urls = list(urls)
        self._birthday = birthday
        self._note = note
        self._photo = photo
        self._user = user
        self._raw = raw

    @property
    def name(self) -> ContactName | None:
        return self._name

    @name.setter
    def name(self, value: ContactName | None) -> None:
        self._name = value

    @property
    def phones(self) -> list[ContactField]:
        return self._phones

    @property
    def emails(self) -> list[ContactField]:
        return self._emails

    @property
    def addresses(self) -> list[ContactAddress]:
        return self._addresses

    @property
    def org(self) -> ContactOrg | None:
        return self._org

    @org.setter
    def org(self, value: ContactOrg | None) -> None:
        self._org = value

    @property
    def urls(self) -> list[str]:
        return self._urls

    @property
    def birthday(self) -> str | None:
        return self._birthday

    @property
    def note(self) -> str | None:
        return self._note

    @note.setter
    def note(self, value: str | None) -> None:
        self._note = value

    @property
    def photo(self) -> Attachment | None:
        return self._photo

    @property
    def user(self) -> User | None:
        return self._user

    @property
    def raw(self) -> Any:
        return self._raw


# --------------------------------------------------------------------------- simple payloads


@dataclass(init=False, repr=False, slots=True)
class RichLink(Content):
    """A URL the receiving platform should unfurl natively (no OG metadata is fetched)."""

    type: ClassVar[ContentType] = ContentType.RICHLINK
    __repr_fields__ = ("url",)
    _url: str

    def __init__(self, url: str) -> None:
        self.url = url

    @property
    def url(self) -> str:
        return self._url

    @url.setter
    def url(self, value: str) -> None:
        value = _require_str("url", value)
        if not value.startswith(("http://", "https://")):
            raise ContentError(f"rich link URL must be absolute http(s): {value!r}")
        self._url = value


@dataclass(init=False, repr=False, slots=True)
class App(Content):
    """URL presented as a tappable app card (iMessage) or a plain link elsewhere."""

    type: ClassVar[ContentType] = ContentType.APP
    __repr_fields__ = ("url", "live", "layout")
    _url: str
    _live: bool
    _layout: MiniAppLayout | None

    def __init__(self, url: str, *, live: bool = False, layout: MiniAppLayout | None = None) -> None:
        self._url = _require_str("url", url)
        self._live = bool(live)
        self._layout = layout

    @property
    def url(self) -> str:
        return self._url

    @property
    def live(self) -> bool:
        return self._live

    @live.setter
    def live(self, value: bool) -> None:
        self._live = bool(value)

    @property
    def layout(self) -> MiniAppLayout | None:
        """Card layout; ``None`` until supplied or resolved from the URL's link metadata."""
        return self._layout

    @layout.setter
    def layout(self, value: MiniAppLayout | None) -> None:
        self._layout = value

    async def resolve_layout(self) -> MiniAppLayout:
        """Supplied layout, or one derived from Open Graph metadata (fetched once, never raises)."""
        if self._layout is None:
            from ..content.linkmeta import layout_for_url

            layout = await layout_for_url(self._url)
            self._layout = layout
            return layout
        return self._layout


@dataclass(init=False, repr=False, slots=True)
class Custom(Content):
    """Provider-specific structured payload passed through untouched."""

    type: ClassVar[ContentType] = ContentType.CUSTOM
    __repr_fields__ = ("raw",)
    _raw: Any

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    @property
    def raw(self) -> Any:
        return self._raw


# --------------------------------------------------------------------------- wrappers


@dataclass(init=False, repr=False, slots=True)
class Effect(Content):
    """iMessage bubble/screen effect wrapping text, markdown or an attachment."""

    type: ClassVar[ContentType] = ContentType.EFFECT
    __repr_fields__ = ("effect", "content")
    _content: Content
    _effect: str

    def __init__(self, content: Content, effect: MessageEffect | str) -> None:
        if content.type not in (ContentType.TEXT, ContentType.MARKDOWN, ContentType.ATTACHMENT):
            raise ContentError("effect() can only wrap text, markdown or an attachment")
        self._content = content
        self._effect = str(effect)

    @property
    def content(self) -> Content:
        return self._content

    @property
    def effect(self) -> str:
        return self._effect


@dataclass(init=False, repr=False, slots=True)
class Reaction(Content):
    type: ClassVar[ContentType] = ContentType.REACTION
    __repr_fields__ = ("emoji", "target")
    _emoji: str
    _target: Message

    def __init__(self, emoji: str, target: Message) -> None:
        self._emoji = _require_str("emoji", str(emoji))
        if target.content.type is ContentType.REACTION:
            raise ContentError("cannot react to a reaction")
        self._target = target

    @property
    def emoji(self) -> str:
        return self._emoji

    @property
    def target(self) -> Message:
        return self._target


def _check_wrappable(builder: str, content: Content) -> Content:
    if content.type in UNWRAPPABLE_CONTENT:
        raise ContentError(f'{builder}() cannot wrap "{content.type}" content')
    return content


@dataclass(init=False, repr=False, slots=True)
class Reply(Content):
    type: ClassVar[ContentType] = ContentType.REPLY
    __repr_fields__ = ("content", "target")
    _content: Content
    _target: Message

    def __init__(self, content: Content, target: Message) -> None:
        self._content = _check_wrappable("reply", content)
        self._target = target

    @classmethod
    def inbound(cls, content: Content, target: Message) -> Reply:
        """Wire-side constructor: inbound replies may wrap content outbound builders reject (e.g. a group)."""
        instance = cls.__new__(cls)
        instance._content = content
        instance._target = target
        return instance

    @property
    def content(self) -> Content:
        return self._content

    @property
    def target(self) -> Message:
        return self._target


@dataclass(init=False, repr=False, slots=True)
class Edit(Content):
    type: ClassVar[ContentType] = ContentType.EDIT
    __repr_fields__ = ("content", "target")
    _content: Content
    _target: Message

    def __init__(self, content: Content, target: Message) -> None:
        self._content = _check_wrappable("edit", content)
        self._target = target

    @property
    def content(self) -> Content:
        return self._content

    @property
    def target(self) -> Message:
        return self._target


@dataclass(init=False, repr=False, slots=True)
class Unsend(Content):
    type: ClassVar[ContentType] = ContentType.UNSEND
    __repr_fields__ = ("target",)
    _target: Message

    def __init__(self, target: Message) -> None:
        self._target = target

    @property
    def target(self) -> Message:
        return self._target


@dataclass(init=False, repr=False, slots=True)
class Read(Content):
    """Outbound: mark the conversation read. Inbound: ``sender`` read ``target`` (your message)."""

    type: ClassVar[ContentType] = ContentType.READ
    __repr_fields__ = ("target",)
    _target: Message

    def __init__(self, target: Message) -> None:
        self._target = target

    @property
    def target(self) -> Message:
        return self._target


@dataclass(init=False, repr=False, slots=True)
class Typing(Content):
    type: ClassVar[ContentType] = ContentType.TYPING
    __repr_fields__ = ("state",)
    _state: TypingState

    def __init__(self, state: TypingState | str = TypingState.START) -> None:
        self.state = TypingState(state)

    @property
    def state(self) -> TypingState:
        return self._state

    @state.setter
    def state(self, value: TypingState | str) -> None:
        self._state = TypingState(value)


# --------------------------------------------------------------------------- group / poll


@dataclass(init=False, repr=False, slots=True)
class Group(Content):
    """Several items rendered as one visual unit (e.g. a photo album).

    Inbound groups hold ``Message`` items; outbound groups hold ``Content`` items.
    """

    type: ClassVar[ContentType] = ContentType.GROUP
    __repr_fields__ = ("items",)
    _items: tuple[Any, ...]

    def __init__(self, items: Sequence[Any]) -> None:
        if not items:
            raise ContentError("group() requires at least one item")
        for item in items:
            inner = getattr(item, "content", item) if not isinstance(item, Content) else item
            if inner.type is ContentType.GROUP:
                raise ContentError("groups cannot be nested")
            if inner.type is ContentType.REACTION:
                raise ContentError("reactions cannot be group members")
        self._items = tuple(items)

    @property
    def items(self) -> tuple[Any, ...]:
        return self._items


@dataclass(frozen=True, slots=True)
class PollChoice:
    title: str
    identifier: str | None = None


@dataclass(init=False, repr=False, slots=True)
class Poll(Content):
    type: ClassVar[ContentType] = ContentType.POLL
    __repr_fields__ = ("title", "options")
    _title: str
    _options: list[PollChoice]

    def __init__(self, title: str, options: Sequence[PollChoice | str]) -> None:
        self._title = _require_str("title", title.strip() if isinstance(title, str) else title)
        choices = [o if isinstance(o, PollChoice) else PollChoice(str(o).strip()) for o in options]
        if len(choices) < 2:
            raise ContentError("a poll needs at least two options")
        if any(not c.title for c in choices):
            raise ContentError("poll options must not be empty")
        self._options = choices

    @property
    def title(self) -> str:
        return self._title

    @property
    def options(self) -> list[PollChoice]:
        return self._options


@dataclass(init=False, repr=False, slots=True)
class PollVote(Content):
    """Inbound poll vote (``selected=False`` for an un-vote)."""

    type: ClassVar[ContentType] = ContentType.POLL_OPTION
    __repr_fields__ = ("title", "selected", "poll_id")
    _option: PollChoice
    _poll: Poll | None
    _poll_id: str
    _selected: bool

    def __init__(self, *, option: PollChoice, poll_id: str, selected: bool, poll: Poll | None = None) -> None:
        self._option = option
        self._poll_id = poll_id
        self._selected = selected
        self._poll = poll

    @property
    def option(self) -> PollChoice:
        return self._option

    @property
    def title(self) -> str:
        return self._option.title

    @property
    def poll(self) -> Poll | None:
        return self._poll

    @property
    def poll_id(self) -> str:
        return self._poll_id

    @property
    def selected(self) -> bool:
        return self._selected


# --------------------------------------------------------------------------- chat management


@dataclass(init=False, repr=False, slots=True)
class Rename(Content):
    type: ClassVar[ContentType] = ContentType.RENAME
    __repr_fields__ = ("display_name",)
    _display_name: str

    def __init__(self, display_name: str) -> None:
        self.display_name = display_name

    @property
    def display_name(self) -> str:
        return self._display_name

    @display_name.setter
    def display_name(self, value: str) -> None:
        self._display_name = _require_str("display_name", value)


@dataclass(init=False, repr=False, slots=True)
class Avatar(Content):
    """Set (``kind=SET`` with image bytes) or clear (``kind=CLEAR``) a group icon."""

    type: ClassVar[ContentType] = ContentType.AVATAR
    __repr_fields__ = ("kind", "mime_type")
    _kind: AvatarActionKind
    _mime_type: str | None
    _data: bytes | None
    _reader: ByteReader | None

    def __init__(
        self,
        kind: AvatarActionKind | str,
        *,
        mime_type: str | None = None,
        data: bytes | None = None,
        reader: ByteReader | None = None,
    ) -> None:
        self._kind = AvatarActionKind(kind)
        if self._kind is AvatarActionKind.SET and not mime_type:
            raise ContentError("setting an avatar requires a mime_type")
        self._mime_type = mime_type
        self._data = data
        self._reader = reader

    @property
    def kind(self) -> AvatarActionKind:
        return self._kind

    @property
    def mime_type(self) -> str | None:
        return self._mime_type

    async def read(self) -> bytes:
        if self._kind is AvatarActionKind.CLEAR:
            raise ContentError("a cleared avatar has no bytes")
        if self._data is None:
            if self._reader is None:
                raise ContentError("avatar is metadata-only here; use space.get_avatar()")
            self._data = await self._reader()
        return self._data


class _MemberChange(Content):
    __slots__ = ()
    _members: tuple[str, ...]

    @property
    def members(self) -> tuple[str, ...]:
        return self._members


def _member_ids(members: Iterable[Any]) -> tuple[str, ...]:
    ids = []
    for member in members:
        member_id = member if isinstance(member, str) else getattr(member, "id", None)
        if not isinstance(member_id, str) or not member_id:
            raise ContentError(f"invalid member {member!r}")
        ids.append(member_id)
    return tuple(ids)


@dataclass(init=False, repr=False, slots=True)
class AddMember(_MemberChange):
    type: ClassVar[ContentType] = ContentType.ADD_MEMBER
    __repr_fields__ = ("members",)
    _members: tuple[str, ...]

    def __init__(self, members: Iterable[Any]) -> None:
        self._members = _member_ids(members)


@dataclass(init=False, repr=False, slots=True)
class RemoveMember(_MemberChange):
    type: ClassVar[ContentType] = ContentType.REMOVE_MEMBER
    __repr_fields__ = ("members",)
    _members: tuple[str, ...]

    def __init__(self, members: Iterable[Any]) -> None:
        self._members = _member_ids(members)


@dataclass(repr=False, slots=True)
class LeaveSpace(Content):
    type: ClassVar[ContentType] = ContentType.LEAVE_SPACE


# --------------------------------------------------------------------------- iMessage-only


@dataclass(init=False, repr=False, slots=True)
class Background(Content):
    """iMessage chat background image (set) or removal (``data is None``)."""

    type: ClassVar[ContentType] = ContentType.BACKGROUND
    __repr_fields__ = ("cleared", "mime_type")
    _data: bytes | None
    _mime_type: str | None

    def __init__(self, data: bytes | None = None, *, mime_type: str | None = None) -> None:
        if data is not None and not mime_type:
            raise ContentError("a background image needs a mime_type")
        self._data = data
        self._mime_type = mime_type

    @property
    def cleared(self) -> bool:
        return self._data is None

    @property
    def data(self) -> bytes | None:
        return self._data

    @property
    def mime_type(self) -> str | None:
        return self._mime_type


@dataclass(repr=False, slots=True)
class ContactCard(Content):
    """Share the agent account's own native iMessage contact card."""

    type: ClassVar[ContentType] = ContentType.CONTACT_CARD


@dataclass(frozen=True, slots=True)
class MiniAppLayout:
    caption: str | None = None
    subcaption: str | None = None
    trailing_caption: str | None = None
    trailing_subcaption: str | None = None
    image: bytes | None = field(default=None, repr=False)
    image_title: str | None = None
    image_subtitle: str | None = None
    summary: str | None = None


@dataclass(init=False, repr=False, slots=True)
class MiniApp(Content):
    """Customized iMessage App card for an extension you own."""

    type: ClassVar[ContentType] = ContentType.MINI_APP
    __repr_fields__ = ("app_name", "extension_bundle_id", "url", "live")
    _app_name: str
    _extension_bundle_id: str
    _team_id: str
    _url: str
    _layout: MiniAppLayout
    _app_store_id: int | None
    _live: bool

    def __init__(
        self,
        *,
        app_name: str,
        extension_bundle_id: str,
        team_id: str,
        url: str,
        layout: MiniAppLayout,
        app_store_id: int | None = None,
        live: bool = False,
    ) -> None:
        if ":" in extension_bundle_id:
            raise ContentError("extension_bundle_id must not contain ':'")
        if len(team_id) != 10 or not team_id.isalnum() or team_id.upper() != team_id:
            raise ContentError("team_id must be a 10-character uppercase alphanumeric Apple Team ID")
        if not url.startswith(("http://", "https://")):
            raise ContentError("url must be an absolute http(s) URL")
        if app_store_id is not None and app_store_id <= 0:
            raise ContentError("app_store_id must be a positive integer")
        if (layout.image_title or layout.image_subtitle) and layout.image is None:
            raise ContentError("image_title / image_subtitle require image")
        self._app_name = _require_str("app_name", app_name)
        self._extension_bundle_id = extension_bundle_id
        self._team_id = team_id
        self._url = url
        self._layout = layout
        self._app_store_id = app_store_id
        self._live = live

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def extension_bundle_id(self) -> str:
        return self._extension_bundle_id

    @property
    def team_id(self) -> str:
        return self._team_id

    @property
    def url(self) -> str:
        return self._url

    @property
    def layout(self) -> MiniAppLayout:
        return self._layout

    @property
    def app_store_id(self) -> int | None:
        return self._app_store_id

    @property
    def live(self) -> bool:
        return self._live


ContentInput = str | Content

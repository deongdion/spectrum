"""Map ``photon.imessage.v1`` stream events to Spectrum ``Message`` objects.

Port of spectrum-ts ``remote/inbound.ts`` + ``reactions.ts`` + ``read-receipts.ts``
+ ``group-events.ts`` + ``polls.ts``. Key rules:

* the agent's own actions never surface (``is_from_me`` or actor == this line)
* multipart messages (text + attachments split on U+FFFC) become a ``Group`` whose
  items carry child ids ``p:<index>/<guid>``
* reactions / read receipts reference their target by guid; the target is resolved
  from the per-line cache, then ``GetMessage``; unresolvable events are dropped
* read receipts: in a DM the reader is the peer from the chat guid (the event
  actor is *our* line); in a group the actor is trusted only if it is neither
  our line nor the target's sender. Only receipts on *outbound* targets surface.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...content.mime import is_vcard
from ...content.vcard import from_vcard
from ...core.utils import utcnow
from ...models.content import (
    AddMember,
    App,
    Attachment,
    Avatar,
    Content,
    Custom,
    Group,
    LeaveSpace,
    MiniAppLayout,
    Poll,
    PollChoice,
    PollVote,
    Reaction,
    Read,
    RemoveMember,
    Rename,
    Reply,
    Text,
    Voice,
)
from ...models.enums import AddressService, AvatarActionKind, Direction, SpaceType, Tapback
from ...models.message import Message
from ...models.space import Space
from ...models.user import User
from .errors import IMessageError, NotFoundError
from .lines import LineHandle

if TYPE_CHECKING:
    from .provider import IMessage

log = logging.getLogger("spectrum.imessage.inbound")

OBJECT_REPLACEMENT = "￼"
CAF_MIME = "audio/x-caf"
CAF_UTI = "com.apple.coreaudio-format"
OCTET_STREAM = "application/octet-stream"

_SERVICES = {1: AddressService.IMESSAGE, 2: AddressService.SMS, 3: AddressService.RCS}
_TAPBACK_KINDS = {
    0: Tapback.LOVE,
    1: Tapback.LIKE,
    2: Tapback.DISLIKE,
    3: Tapback.LAUGH,
    4: Tapback.EMPHASIZE,
    5: Tapback.QUESTION,
}
REACTION_KIND_EMOJI = 6
_TRANSFER_STATES = {
    0: "pending",
    -1: "unavailable",
    1: "transferring",
    2: "failed",
    5: "finished",
    6: "unknown",
}
_ITEM_TYPES = {0: "normal", 1: "participantChange", 2: "groupNameChange", 3: "chatAction"}


# --------------------------------------------------------------------------- id helpers


def chat_type(chat_guid: str) -> SpaceType:
    return SpaceType.GROUP if ";+;" in chat_guid else SpaceType.DM


def dm_chat_guid(address: str) -> str:
    return f"any;-;{address}"


def dm_peer(chat_guid: str) -> str | None:
    marker = ";-;"
    if marker in chat_guid:
        peer = chat_guid.split(marker, 1)[1]
        return peer or None
    return None


def child_id(part_index: int, parent_guid: str) -> str:
    return f"p:{part_index}/{parent_guid}"


def parse_child_id(message_id: str) -> tuple[int, str] | None:
    if not message_id.startswith("p:") or "/" not in message_id:
        return None
    head, _, parent = message_id.partition("/")
    try:
        return int(head[2:]), parent
    except ValueError:
        return None


def to_datetime(ts: Any) -> datetime:
    if ts is None or (ts.seconds == 0 and ts.nanos == 0):
        return utcnow()
    return ts.ToDatetime(tzinfo=UTC)


def _opt(msg: Any, field: str) -> Any:
    return getattr(msg, field) if msg.HasField(field) else None


# --------------------------------------------------------------------------- attachments


def normalize_mime(info: Any) -> str:
    mime = info.mime_type or OCTET_STREAM
    is_caf = (info.uti or "").lower() == CAF_UTI or (info.file_name or "").lower().endswith(".caf")
    return CAF_MIME if mime.lower() == OCTET_STREAM and is_caf else mime


def audio_mime(info: Any) -> str | None:
    mime = normalize_mime(info)
    return mime if mime.lower().startswith("audio/") else None


def ordered_parts(text: str | None, attachments: list[Any]) -> list[tuple[str, Any]]:
    """Interleave text segments and attachments (U+FFFC marks attachment slots)."""
    parts: list[tuple[str, Any]] = []

    def add_text(segment: str | None) -> None:
        if segment and segment.strip():
            parts.append(("text", segment.strip()))

    if not text:
        return [("attachment", a) for a in attachments]
    if OBJECT_REPLACEMENT not in text:
        parts.extend(("attachment", a) for a in attachments)
        add_text(text)
        return parts
    segments = text.split(OBJECT_REPLACEMENT)
    for index, att in enumerate(attachments):
        add_text(segments[index] if index < len(segments) else None)
        parts.append(("attachment", att))
    add_text("".join(segments[len(attachments) :]))
    return parts


def message_metadata(m: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "is_sent": m.is_sent,
        "is_delivered": m.is_delivered,
        "is_delivered_quietly": m.is_delivered_quietly,
        "did_notify_recipient": m.did_notify_recipient,
        "is_delayed": m.is_delayed,
        "send_error_code": m.send_error_code,
        "is_auto_reply": m.is_auto_reply,
        "is_corrupt": m.is_corrupt,
        "is_expirable": m.is_expirable,
        "is_service_message": m.is_service_message,
        "is_spam": m.is_spam,
        "is_system_message": m.is_system_message,
        "is_audio_message": m.is_audio_message,
        "item_type": _ITEM_TYPES.get(m.item_type, str(m.item_type)),
        "chat_guids": list(m.chat_guids),
    }
    for name in (
        "date_read",
        "date_delivered",
        "date_edited",
        "date_retracted",
        "date_played",
        "date_expressive_send_played",
    ):
        if m.HasField(name):
            meta[name] = getattr(m, name).ToDatetime(tzinfo=UTC)
    for name in ("subject", "group_title", "part_count"):
        if m.HasField(name):
            meta[name] = getattr(m, name)
    content = m.content
    if content.HasField("text"):
        meta["native_text"] = content.text
    for name in ("balloon_bundle_id", "expressive_send_style_id"):
        if content.HasField(name):
            meta[name] = getattr(content, name)
    if content.formatting:
        meta["formatting"] = [
            {
                "type": f.type,
                "start": f.start,
                "length": f.length,
                **({"effect": f.effect_name} if f.HasField("effect_name") else {}),
            }
            for f in content.formatting
        ]
    if content.mentions:
        meta["mentions"] = [
            {"address": x.address, "start": x.start, "length": x.length} for x in content.mentions
        ]
    if content.attachments:
        meta["attachment_metadata"] = [
            {
                "guid": a.guid,
                "file_name": a.file_name,
                "mime_type": a.mime_type,
                "uti": a.uti,
                "total_bytes": a.total_bytes,
                "transfer_state": _TRANSFER_STATES.get(a.transfer_state, "unknown"),
                "is_sticker": a.is_sticker,
                "is_hidden": a.is_hidden,
                **({"original_guid": a.original_guid} if a.HasField("original_guid") else {}),
            }
            for a in content.attachments
        ]
    if m.applied_reactions:
        meta["applied_reactions"] = [
            {
                "message_guid": r.message_guid,
                "reaction": reaction_emoji(r.reaction),
                "is_from_me": r.is_from_me,
                "sender": r.sender.address if r.HasField("sender") else None,
            }
            for r in m.applied_reactions
        ]
    return meta


def app_layout(mini: Any) -> MiniAppLayout:
    """Decoded inbound card slots (``image_*`` dropped: inbound cards carry no image bytes)."""
    info = mini.layout if mini.HasField("layout") else None

    def slot(name: str) -> str | None:
        return (
            getattr(info, name) if info is not None and info.HasField(name) and getattr(info, name) else None
        )

    app_name = mini.app_name if mini.HasField("app_name") else None
    return MiniAppLayout(
        caption=slot("caption") or slot("summary") or app_name,
        subcaption=slot("subcaption"),
        trailing_caption=slot("trailing_caption"),
        trailing_subcaption=slot("trailing_subcaption"),
        summary=slot("summary"),
    )


def reaction_emoji(reaction: Any) -> str | None:
    if reaction.kind == REACTION_KIND_EMOJI:
        return reaction.emoji if reaction.HasField("emoji") else None
    tapback = _TAPBACK_KINDS.get(reaction.kind)
    return tapback.emoji.value if tapback else None


# --------------------------------------------------------------------------- mapper


class InboundMapper:
    """Builds Spectrum messages for one line. Stateless apart from the line caches."""

    def __init__(self, provider: IMessage, line: LineHandle) -> None:
        self._provider = provider
        self._line = line

    @property
    def line(self) -> LineHandle:
        return self._line

    # ------------------------------------------------------------------ primitives

    def space(self, chat_guid: str) -> Space:
        return self._provider.make_space(chat_guid, type=chat_type(chat_guid), phone=self._line.phone)

    def user(self, addr: Any | None) -> User | None:
        if addr is None or not addr.address:
            return None
        return self._provider.make_user(
            addr.address,
            address=addr.address,
            country=addr.country if addr.HasField("country") else None,
            service=_SERVICES.get(addr.service, AddressService.UNKNOWN),
        )

    def _attachment(self, info: Any, *, as_voice: bool) -> Content:
        rpc = self._line.rpc
        guid = info.guid

        async def reader() -> bytes:
            return await rpc.download_attachment(guid)

        def streamer() -> Any:
            return rpc.download_attachment_stream(guid)

        name = info.file_name or "attachment"
        if as_voice and (mime := audio_mime(info)):
            return Voice(
                id=guid,
                name=name,
                mime_type=mime,
                size=info.total_bytes or None,
                reader=reader,
                streamer=streamer,
            )
        return Attachment(
            id=guid,
            name=name,
            mime_type=normalize_mime(info),
            size=info.total_bytes or None,
            reader=reader,
            streamer=streamer,
        )

    async def _attachment_content(self, info: Any, *, as_voice: bool) -> Content:
        if is_vcard(info.mime_type, info.file_name):
            try:
                data = await self._line.rpc.download_attachment(info.guid)
                return from_vcard(data.decode("utf-8", errors="replace"))
            except Exception:
                log.warning(
                    "failed to parse vCard attachment %s; surfacing as attachment", info.guid, exc_info=True
                )
        return self._attachment(info, as_voice=as_voice)

    # ------------------------------------------------------------------ message rebuild

    async def rebuild(
        self,
        m: Any,
        chat_hint: str | None = None,
        *,
        timestamp: datetime | None = None,
        visited: frozenset[str] = frozenset(),
    ) -> Message:
        chat_guid = chat_hint or (m.chat_guids[0] if m.chat_guids else "")
        space = self.space(chat_guid)
        common: dict[str, Any] = dict(
            space=space,
            timestamp=timestamp or to_datetime(m.date_created if m.HasField("date_created") else None),
            direction=Direction.OUTBOUND if m.is_from_me else Direction.INBOUND,
            sender=self.user(m.sender) if m.HasField("sender") else None,
            metadata=message_metadata(m),
            raw=m,
        )
        message = await self._unwrapped(m, common)
        target_guid = (m.reply_target_guid if m.HasField("reply_target_guid") else None) or (
            m.thread_originator_guid if m.HasField("thread_originator_guid") else None
        )
        if target_guid:
            target = await self._reply_target(target_guid, m.guid, space, visited)
            message = self._provider.make_message(
                id=message.id, content=Reply.inbound(message.content, target), **common
            )
        self.cache(message)
        return message

    async def _unwrapped(self, m: Any, common: dict[str, Any]) -> Message:
        attachments = list(m.content.attachments)
        make = self._provider.make_message
        if not attachments:
            mini = m.content.mini_app if m.content.HasField("mini_app") else None
            if mini is not None and mini.HasField("url") and mini.url.startswith(("http://", "https://")):
                return make(
                    id=m.guid, content=App(mini.url, live=mini.live, layout=app_layout(mini)), **common
                )
            text = m.content.text if m.content.HasField("text") else ""
            content: Content = Text(text) if text else Custom({"imessage_type": "unsupported-message"})
            return make(id=m.guid, content=content, **common)

        voice_guid = None
        if m.is_audio_message:
            voice_guid = next((a.guid for a in attachments if audio_mime(a)), None)
        parts = ordered_parts(m.content.text if m.content.HasField("text") else None, attachments)
        if not parts:
            return make(id=m.guid, content=Custom({"imessage_type": "unsupported-message"}), **common)

        async def part_content(kind: str, value: Any) -> Content:
            if kind == "text":
                return Text(value)
            return await self._attachment_content(value, as_voice=value.guid == voice_guid)

        if len(parts) == 1:
            return make(id=m.guid, content=await part_content(*parts[0]), part_index=0, **common)
        items = [
            make(
                id=child_id(i, m.guid),
                content=await part_content(kind, value),
                part_index=i,
                parent_id=m.guid,
                **common,
            )
            for i, (kind, value) in enumerate(parts)
        ]
        return make(id=m.guid, content=Group(items), **common)

    async def _reply_target(
        self, target_guid: str, current_guid: str, space: Space, visited: frozenset[str]
    ) -> Message:
        if target_guid == current_guid or target_guid in visited:
            return self._stub(target_guid, space)
        cached = self._line.message_cache.get(target_guid)
        if cached is not None:
            return cached
        try:
            fetched = await self._line.rpc.get_message(target_guid)
            return await self.rebuild(fetched, space.id, visited=visited | {current_guid})
        except NotFoundError:
            return self._stub(target_guid, space)
        except IMessageError:
            log.warning("failed to resolve reply target %s", target_guid, exc_info=True)
            return self._stub(target_guid, space)

    def _stub(self, guid: str, space: Space) -> Message:
        return self._provider.make_message(
            id=guid,
            content=Custom({"imessage_type": "reply-target", "stub": True}),
            space=space,
            timestamp=utcnow(),
        )

    def cache(self, message: Message) -> None:
        cache = self._line.message_cache
        cache.set(message.id, message)
        content = message.content
        if isinstance(content, Reply):
            content = content.content
        if isinstance(content, Group):
            for item in content.items:
                if isinstance(item, Message):
                    cache.set(item.id, item)

    async def resolve_target(self, chat_guid: str, guid: str) -> Message | None:
        """Cache first, then one ``GetMessage``; any failure drops the event (returns ``None``)."""
        cached = self._line.message_cache.get(guid)
        if cached is not None:
            return cached
        try:
            return await self.rebuild(await self._line.rpc.get_message(guid), chat_guid)
        except Exception:
            log.debug("event target %s could not be resolved; dropping", guid, exc_info=True)
            return None

    # ------------------------------------------------------------------ message events

    async def message_event(self, sequence: int | None, ev: Any) -> list[Message]:
        kind = ev.WhichOneof("change")
        if kind == "message_received":
            m = ev.message_received.message
            if m.is_from_me:
                return []
            return [await self.rebuild(m, ev.chat_guid or None, timestamp=to_datetime(ev.occurred_at))]
        if kind == "reaction_added":
            actor = ev.actor if ev.HasField("actor") else None
            if ev.is_from_me or self._line.owns(actor.address if actor else None):
                return []
            return await self._reaction(sequence, ev, actor)
        if kind == "message_read":
            return await self._read_receipt(sequence, ev)
        return []

    async def _reaction(self, sequence: int | None, ev: Any, actor: Any | None) -> list[Message]:
        change = ev.reaction_added
        emoji = reaction_emoji(change.reaction)
        if not emoji or actor is None or not actor.address:
            return []
        target = await self.resolve_target(ev.chat_guid, change.message_guid)
        if target is None:
            return []
        part = change.target_part_index if change.HasField("target_part_index") else None
        if isinstance(target.content, Group):
            items = target.content.items
            index = part or 0
            if 0 <= index < len(items) and isinstance(items[index], Message):
                target = items[index]
        if target.content.type.value == "reaction":
            return []
        suffix = f":{part}" if part is not None else ""
        return [
            self._provider.make_message(
                id=f"{change.message_guid}:reaction:{sequence}{suffix}",
                content=Reaction(emoji, target),
                space=self.space(ev.chat_guid),
                timestamp=to_datetime(ev.occurred_at),
                sender=self.user(actor),
            )
        ]

    async def _read_receipt(self, sequence: int | None, ev: Any) -> list[Message]:
        change = ev.message_read
        actor = ev.actor if ev.HasField("actor") else None
        peer = dm_peer(ev.chat_guid)
        if peer is None and not (actor and actor.address):
            return []
        target = await self.resolve_target(ev.chat_guid, change.message_guid)
        if target is None or not target.is_outbound:
            return []
        if peer is not None:
            reader = self._provider.make_user(peer, address=peer)
        elif (
            actor
            and actor.address
            and actor.address != self._line.phone
            and actor.address != (target.sender.address if target.sender else None)
        ):
            reader = self.user(actor)
        else:
            return []
        read_at = change.read_at if change.HasField("read_at") else ev.occurred_at
        return [
            self._provider.make_message(
                id=f"{change.message_guid}:read:{sequence}",
                content=Read(target),
                space=self.space(ev.chat_guid),
                timestamp=to_datetime(read_at),
                sender=reader,
            )
        ]

    # ------------------------------------------------------------------ group events

    async def group_event(self, sequence: int | None, ev: Any) -> list[Message]:
        kind = ev.WhichOneof("change")
        if kind is None:
            return []
        change = getattr(ev, kind)
        actor = (
            change.participant if kind == "participant_left" else (ev.actor if ev.HasField("actor") else None)
        )
        if ev.is_from_me or self._line.owns(actor.address if actor else None):
            return []
        content: Content | None
        match kind:
            case "participant_added":
                content = AddMember([change.participant.address]) if change.participant.address else None
            case "participant_removed":
                content = RemoveMember([change.participant.address]) if change.participant.address else None
            case "participant_left":
                content = LeaveSpace()
            case "display_name_changed":
                content = Rename(change.display_name) if change.display_name else None
            case "icon_changed":
                content = await self._icon(ev.chat_guid)
            case "icon_removed":
                content = Avatar(AvatarActionKind.CLEAR)
            case _:
                content = None
        if content is None:
            return []
        return [
            self._provider.make_message(
                id=f"{ev.chat_guid}:group:{sequence}",
                content=content,
                space=self.space(ev.chat_guid),
                timestamp=to_datetime(ev.occurred_at),
                sender=self.user(actor),
            )
        ]

    async def _icon(self, chat_guid: str) -> Avatar | None:
        try:
            icon = await self._line.rpc.get_icon(chat_guid)
        except NotFoundError:
            return None
        except IMessageError:
            log.error("failed to fetch changed group icon for %s", chat_guid, exc_info=True)
            return None
        if not icon.data:
            return None
        return Avatar(AvatarActionKind.SET, mime_type=icon.mime_type or "image/jpeg", data=bytes(icon.data))

    # ------------------------------------------------------------------ poll events

    def _poll_created(self, sequence: int | None, ev: Any, actor: Any | None) -> list[Message]:
        """Someone else created a poll -> inbound ``Poll`` message (an extension over spectrum-ts,
        which only surfaces votes). The id is distinct from the poll's message guid so it never
        collides with a ``message_received`` echo of the same bubble."""
        change = ev.created
        try:
            content = Poll(
                change.title or "poll", [PollChoice(o.text, o.option_identifier) for o in change.options]
            )
        except Exception:
            log.debug("poll %s has an unrepresentable option list; dropping", ev.poll_message_guid)
            return []
        return [
            self._provider.make_message(
                id=f"{ev.poll_message_guid}:poll:created",
                content=content,
                space=self.space(ev.chat_guid),
                timestamp=to_datetime(ev.occurred_at),
                sender=self.user(actor),
                metadata={"poll_message_guid": ev.poll_message_guid, "sequence": sequence},
            )
        ]

    async def poll_event(self, sequence: int | None, ev: Any) -> list[Message]:
        kind = ev.WhichOneof("change")
        cache = self._line.poll_cache
        if kind in ("created", "option_added"):
            change = getattr(ev, kind)
            cache.set(ev.poll_message_guid, {"title": change.title, "options": list(change.options)})
        actor = ev.actor if ev.HasField("actor") else None
        if ev.is_from_me or self._line.owns(actor.address if actor else None):
            return []
        if kind == "created":
            return self._poll_created(sequence, ev, actor)
        if kind not in ("voted", "unvoted"):
            return []
        option_id = getattr(ev, kind).option_identifier
        if not option_id or actor is None or not actor.address:
            return []  # like spectrum-ts: a vote without a voter is noise
        info = cache.get(ev.poll_message_guid)
        if info is None or not any(o.option_identifier == option_id for o in info["options"]):
            try:
                poll = await self._line.rpc.get_poll(ev.poll_message_guid)
                info = {"title": poll.title, "options": list(poll.options)}
                cache.set(ev.poll_message_guid, info)
            except IMessageError:
                log.debug("poll %s could not be resolved; dropping vote", ev.poll_message_guid, exc_info=True)
                return []
        choice = next((o for o in info["options"] if o.option_identifier == option_id), None)
        if choice is None:
            return []
        choices = [PollChoice(o.text, o.option_identifier) for o in info["options"]]
        try:
            poll_content: Poll | None = Poll(info["title"] or "poll", choices)
        except Exception:
            poll_content = None
        return [
            self._provider.make_message(
                id=f"{ev.poll_message_guid}:poll:{sequence}",
                content=PollVote(
                    option=PollChoice(choice.text, choice.option_identifier),
                    poll_id=ev.poll_message_guid,
                    selected=kind == "voted",
                    poll=poll_content,
                ),
                space=self.space(ev.chat_guid),
                timestamp=to_datetime(ev.occurred_at),
                sender=self.user(actor),
            )
        ]

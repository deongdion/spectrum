"""Outbound dispatcher: one ``Content`` -> ``photon.imessage.v1`` calls.

Port of spectrum-ts ``remote/send.ts`` + the ``send`` handler in ``index.ts``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ...content.audio import ensure_m4a, m4a_name
from ...content.formatting import render
from ...content.vcard import to_vcard, vcard_file_name
from ...core.errors import UnsupportedError
from ...core.utils import utcnow
from ...models.content import (
    AddMember,
    App,
    Attachment,
    Avatar,
    Background,
    Contact,
    ContactCard,
    Content,
    Edit,
    Effect,
    Group,
    LeaveSpace,
    Markdown,
    MiniApp,
    Poll,
    Reaction,
    Read,
    RemoveMember,
    Rename,
    Reply,
    RichLink,
    StreamText,
    Text,
    Typing,
    Unsend,
    Voice,
)
from ...models.enums import (
    AvatarActionKind,
    ContentType,
    Direction,
    Platform,
    StreamFormat,
    Tapback,
    TypingState,
)
from ...models.message import Message
from ...models.space import Space
from ._proto.photon.imessage.v1 import message_service_pb2 as message_pb
from ._proto.photon.imessage.v1 import message_types_pb2 as types_pb
from .inbound import InboundMapper, child_id, message_metadata, parse_child_id, to_datetime
from .lines import LineHandle

if TYPE_CHECKING:
    from .provider import IMessage

PLATFORM = str(Platform.IMESSAGE)
GROUP_ITEM_TYPES = {
    ContentType.TEXT,
    ContentType.MARKDOWN,
    ContentType.ATTACHMENT,
    ContentType.CONTACT,
    ContentType.VOICE,
}
STREAM_INITIAL_THROTTLE = 1.0
STREAM_BACKOFF = 2.0
STREAM_MAX_INTERIM_EDITS = 4

SPECTRUM_MINI_APP = {
    "app_name": "Spectrum",
    "extension_bundle_id": "codes.photon.Spectrum.MessagesExtension",
    "team_id": "P8XT6232SL",
    "app_store_id": 6777616651,
}

_TAPBACK_KIND = {
    Tapback.LOVE: "MESSAGE_REACTION_KIND_LOVE",
    Tapback.LIKE: "MESSAGE_REACTION_KIND_LIKE",
    Tapback.DISLIKE: "MESSAGE_REACTION_KIND_DISLIKE",
    Tapback.LAUGH: "MESSAGE_REACTION_KIND_LAUGH",
    Tapback.EMPHASIZE: "MESSAGE_REACTION_KIND_EMPHASIZE",
    Tapback.QUESTION: "MESSAGE_REACTION_KIND_QUESTION",
}


def _unsupported(content_type: str, detail: str | None = None) -> UnsupportedError:
    return UnsupportedError.content(content_type, PLATFORM, detail)


def _formats(rendered_formatting: Any) -> list[types_pb.TextFormat]:
    return [types_pb.TextFormat(type=f.type, start=f.start, length=f.length) for f in rendered_formatting]


def to_reaction(emoji: str) -> types_pb.MessageReaction:
    tapback = Tapback.from_emoji(emoji)
    if tapback is not None:
        return types_pb.MessageReaction(kind=_TAPBACK_KIND[tapback])
    return types_pb.MessageReaction(kind="MESSAGE_REACTION_KIND_EMOJI", emoji=emoji)


def target_ref(message: Message) -> tuple[str, int | None]:
    """``(parent guid, part index)`` for a possibly-child message id."""
    if message.parent_id:
        return message.parent_id, message.part_index
    child = parse_child_id(message.id)
    if child:
        return child[1], child[0]
    return message.id, None


class OutboundSender:
    def __init__(self, provider: IMessage, line: LineHandle) -> None:
        self._provider = provider
        self._line = line
        self._rpc = line.rpc
        self._mapper = InboundMapper(provider, line)

    # ------------------------------------------------------------------ records

    def _record(self, space: Space, response_message: Any, content: Content, **extra: Any) -> Message:
        message = self._provider.make_message(
            id=response_message.guid,
            content=content,
            space=space,
            timestamp=to_datetime(
                response_message.date_created if response_message.HasField("date_created") else None
            ),
            direction=Direction.OUTBOUND,
            sender=self._provider.agent_user(self._line),
            metadata={**message_metadata(response_message), **extra},
            raw=response_message,
        )
        self._mapper.cache(message)
        return message

    # ------------------------------------------------------------------ dispatch

    async def send(self, space: Space, content: Content) -> Message | None:
        chat = space.id
        match content:
            case Reply():
                guid, part = target_ref(content.target)
                reply_to = types_pb.ReplyTarget(message_guid=guid)
                if part is not None:
                    reply_to.target_part_index = part
                return await self._send_content(space, content.content, reply_to=reply_to)
            case Reaction():
                guid, part = target_ref(content.target)
                response = await self._rpc.set_reaction(
                    chat, guid, to_reaction(content.emoji), True, part_index=part
                )
                return self._record(space, response.message, content)
            case Unsend():
                await self._unsend(space, content.target)
                return None
            case Edit():
                await self._edit(space, content)
                return None
            case Typing():
                await self._rpc.set_typing(chat, content.state is TypingState.START)
                return None
            case Read():
                await self._rpc.mark_read(chat)
                return None
            case Rename():
                self._require_group(space, "rename")
                await self._rpc.set_display_name(chat, content.display_name)
                return None
            case Avatar():
                self._require_group(space, "avatar")
                if content.kind is AvatarActionKind.CLEAR:
                    await self._rpc.remove_icon(chat)
                else:
                    await self._rpc.set_icon(chat, await content.read())
                return None
            case AddMember():
                self._require_group(
                    space, "addMember", "a DM cannot become a group; create one with create_space()"
                )
                await self._rpc.add_participants(chat, content.members)
                return None
            case RemoveMember():
                self._require_group(space, "removeMember")
                await self._rpc.remove_participants(chat, content.members)
                return None
            case LeaveSpace():
                self._require_group(space, "leaveSpace")
                await self._rpc.leave_group(chat)
                return None
            case Background():
                if content.data is None:
                    await self._rpc.remove_background(chat)
                else:
                    await self._rpc.set_background(chat, content.data)
                return None
            case ContactCard():
                await self._rpc.share_contact_info(chat)
                return None
            case StreamText():
                return await self._stream_text(space, content)
            case Group():
                return await self._group(space, content)
            case MiniApp() | App():
                request = await self._mini_app_request(chat, content)
                response = await self._rpc.send_mini_app(request)
                session = (
                    response.mini_app_card_session if response.HasField("mini_app_card_session") else None
                )
                return self._record(space, response.message, content, mini_app_card_session=session)
            case _:
                return await self._send_content(space, content)

    # ------------------------------------------------------------------ plain content

    async def _send_content(
        self, space: Space, content: Content, *, reply_to: Any = None, effect_id: str | None = None
    ) -> Message:
        chat = space.id
        rpc = self._rpc
        match content:
            case Effect():
                return await self._send_content(
                    space, content.content, reply_to=reply_to, effect_id=content.effect
                )
            case Text():
                response = await rpc.send_text(chat, content.text, reply_to=reply_to, effect_id=effect_id)
            case Markdown():
                rendered = render(content.markdown)
                if not rendered.text:
                    raise _unsupported("markdown", "renders to empty text")
                response = await rpc.send_text(
                    chat,
                    rendered.text,
                    reply_to=reply_to,
                    effect_id=effect_id,
                    formatting=_formats(rendered.formatting),
                )
            case RichLink():
                response = await rpc.send_text(chat, content.url, reply_to=reply_to, enable_link_preview=True)
            case Attachment():
                guid = await self._upload(content.name, await content.read())
                response = await rpc.send_attachment(chat, guid, reply_to=reply_to, effect_id=effect_id)
            case Voice():
                name, data = await self._voice_payload(content)
                guid = await self._upload(name, data)
                response = await rpc.send_attachment(chat, guid, reply_to=reply_to, is_audio_message=True)
            case Contact():
                guid = await self._upload(vcard_file_name(content), to_vcard(content).encode())
                response = await rpc.send_attachment(chat, guid, reply_to=reply_to)
            case Poll():
                if reply_to is not None:
                    raise _unsupported("poll", "polls cannot be sent as replies")
                poll = await rpc.create_poll(chat, content.title, [o.title for o in content.options])
                return self._provider.make_message(
                    id=poll.poll_message_guid,
                    content=content,
                    space=space,
                    timestamp=utcnow(),
                    direction=Direction.OUTBOUND,
                    sender=self._provider.agent_user(self._line),
                )
            case _:
                raise _unsupported(content.type.value)
        return self._record(space, response.message, content)

    @staticmethod
    async def _voice_payload(content: Voice) -> tuple[str, bytes]:
        """iMessage audio messages must be M4A: transcode with ffmpeg when needed."""
        result = await ensure_m4a(await content.read(), content.mime_type)
        name = m4a_name(content.name) if result.converted else (content.name or "voice.m4a")
        return name, result.data

    async def _upload(self, name: str, data: bytes) -> str:
        response = await self._rpc.upload_attachment(name, data)
        return str(response.attachment.guid)

    # ------------------------------------------------------------------ group (multipart)

    async def _group(self, space: Space, content: Group) -> Message:
        text_items = 0
        parts: list[types_pb.MessagePart] = []
        for index, item in enumerate(content.items):
            inner: Content = item.content if isinstance(item, Message) else item
            if inner.type not in GROUP_ITEM_TYPES:
                raise _unsupported("group", f'"{inner.type.value}" items are not supported inside a group')
            part = types_pb.MessagePart(bubble_index=index)
            match inner:
                case Text():
                    text_items += 1
                    part.text = inner.text
                case Markdown():
                    text_items += 1
                    rendered = render(inner.markdown)
                    part.text = rendered.text
                    part.formatting.extend(_formats(rendered.formatting))
                case Attachment():
                    part.attachment.CopyFrom(
                        types_pb.AttachmentRef(
                            attachment_guid=await self._upload(inner.name, await inner.read()),
                            attachment_name=inner.name,
                        )
                    )
                case Voice():
                    name, data = await self._voice_payload(inner)
                    part.attachment.CopyFrom(
                        types_pb.AttachmentRef(
                            attachment_guid=await self._upload(name, data), attachment_name=name
                        )
                    )
                case Contact():
                    name = vcard_file_name(inner)
                    part.attachment.CopyFrom(
                        types_pb.AttachmentRef(
                            attachment_guid=await self._upload(name, to_vcard(inner).encode()),
                            attachment_name=name,
                        )
                    )
            if text_items > 1:
                raise _unsupported("group", "groups can contain at most 1 text item")
            parts.append(part)
        response = await self._rpc.send_multipart(space.id, parts)
        parent = response.message
        timestamp = to_datetime(parent.date_created if parent.HasField("date_created") else None)
        agent = self._provider.agent_user(self._line)
        items = [
            self._provider.make_message(
                id=child_id(i, parent.guid),
                content=item.content if isinstance(item, Message) else item,
                space=space,
                timestamp=timestamp,
                direction=Direction.OUTBOUND,
                sender=agent,
                part_index=i,
                parent_id=parent.guid,
            )
            for i, item in enumerate(content.items)
        ]
        return self._record(space, parent, Group(items))

    # ------------------------------------------------------------------ streaming text

    async def _stream_text(self, space: Space, content: StreamText) -> Message:
        if content.format is StreamFormat.MARKDOWN:
            # No native progressive markdown on iMessage: render the full text once.
            full = await content.collect()
            if not full:
                raise _unsupported("streamText", "stream produced no text")
            return await self._send_content(space, Markdown(full))

        chat = space.id
        sent: Any = None
        full = ""
        last_sent = ""
        last_edit = 0.0
        edits = 0

        async def flush(text: str) -> None:
            nonlocal last_sent, last_edit, edits
            if sent is None or text == last_sent:
                return
            await self._rpc.edit_message(chat, sent.guid, text)
            last_sent, last_edit, edits = text, time.monotonic(), edits + 1

        async for delta in content.chunks():
            full += delta
            if sent is None:
                sent = (await self._rpc.send_text(chat, full)).message
                last_sent, last_edit = full, time.monotonic()
                continue
            gap = STREAM_INITIAL_THROTTLE * STREAM_BACKOFF**edits
            if edits < STREAM_MAX_INTERIM_EDITS and time.monotonic() - last_edit >= gap:
                await flush(full)
        if sent is None:
            raise _unsupported("streamText", "stream produced no text")
        await flush(full)
        message = self._record(space, sent, Text(full), native_text=full)
        return message

    # ------------------------------------------------------------------ edit / unsend

    async def _edit(self, space: Space, content: Edit) -> None:
        target = content.target
        inner = content.content
        if isinstance(inner, MiniApp | App):
            session = target.metadata.get("mini_app_card_session")
            if session is None:
                raise _unsupported(
                    "edit", "the target has no mini-app card session (keep the message returned by send)"
                )
            request = await self._mini_app_request(space.id, inner, session=session)
            response = await self._rpc.update_mini_app(request)
            if response.HasField("mini_app_card_session"):
                target.metadata["mini_app_card_session"] = response.mini_app_card_session
            return
        if isinstance(inner, Markdown):
            text = render(inner.markdown).text
        elif isinstance(inner, Text):
            text = inner.text
        else:
            raise _unsupported(inner.type.value, "only text content can be edited")
        guid, part = target_ref(target)
        await self._rpc.edit_message(space.id, guid, text, part_index=part)

    async def _unsend(self, space: Space, target: Message) -> None:
        if isinstance(target.content, Reaction):
            reaction = target.content
            guid, part = target_ref(reaction.target)
            await self._rpc.set_reaction(space.id, guid, to_reaction(reaction.emoji), False, part_index=part)
            return
        guid, part = target_ref(target)
        await self._rpc.unsend_message(space.id, guid, part_index=part)

    # ------------------------------------------------------------------ helpers

    def _require_group(self, space: Space, action: str, detail: str | None = None) -> None:
        if not space.is_group:
            raise _unsupported(action, detail or "only group chats support this")

    async def _mini_app_request(self, chat: str, content: MiniApp | App, *, session: Any = None) -> Any:
        if isinstance(content, App):
            # universal app cards render through Spectrum's own iMessage extension;
            # the layout comes from the URL's Open Graph metadata unless supplied
            identity: dict[str, Any] = dict(SPECTRUM_MINI_APP)
            layout = await content.resolve_layout()
            url, live = content.url, content.live
        else:
            identity = {
                "app_name": content.app_name,
                "extension_bundle_id": content.extension_bundle_id,
                "team_id": content.team_id,
                "app_store_id": content.app_store_id,
            }
            layout, url, live = content.layout, content.url, content.live
        pb_layout = message_pb.MiniAppLayout(
            **{
                k: v
                for k, v in {
                    "caption": layout.caption,
                    "subcaption": layout.subcaption,
                    "trailing_caption": layout.trailing_caption,
                    "trailing_subcaption": layout.trailing_subcaption,
                    "image": layout.image,
                    "image_title": layout.image_title,
                    "image_subtitle": layout.image_subtitle,
                    "summary": layout.summary,
                }.items()
                if v is not None
            }
        )
        common = {
            "team_id": identity["team_id"],
            "extension_bundle_id": identity["extension_bundle_id"],
            "app_name": identity["app_name"],
            "url": url,
            "layout": pb_layout,
            "live": live,
        }
        if identity.get("app_store_id"):
            common["app_store_id"] = identity["app_store_id"]
        if session is not None:
            return message_pb.UpdateCustomizedMiniAppMessageRequest(session=session, **common)
        return message_pb.SendCustomizedMiniAppMessageRequest(chat_guid=chat, **common)

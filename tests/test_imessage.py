"""End-to-end test of the IMessage provider against an in-process fake gRPC server."""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import pytest
from google.protobuf import empty_pb2

import spectrum
from spectrum.content.audio import ffmpeg_path, is_m4a
from spectrum.models import AddMember, Group, Poll, PollVote, Reaction, Read, Text
from spectrum.providers import IMessage, LineConfig
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    address_types_pb2 as addr_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    attachment_service_pb2 as attsvc_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    attachment_service_pb2_grpc as att_grpc,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    attachment_types_pb2 as att_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    chat_service_pb2 as chat_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    chat_service_pb2_grpc as chat_grpc,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    event_service_pb2 as event_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    event_service_pb2_grpc as event_grpc,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    group_service_pb2 as group_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    group_service_pb2_grpc as group_grpc,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    group_types_pb2 as gtypes_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    message_service_pb2 as msg_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    message_service_pb2_grpc as msg_grpc,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    message_types_pb2 as types_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    poll_service_pb2 as poll_pb,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    poll_service_pb2_grpc as poll_grpc,
)
from spectrum.providers.imessage._proto.photon.imessage.v1 import (
    poll_types_pb2 as ptypes_pb,
)

LINE = "+15550001"
PEER = "+15559999"
DM = f"any;-;{PEER}"
GROUP = "any;+;group-1"


def addr(address: str) -> addr_pb.SingleServiceAddressInfo:
    return addr_pb.SingleServiceAddressInfo(address=address, service=addr_pb.CHAT_SERVICE_TYPE_IMESSAGE)


class FakeServer:
    def __init__(self) -> None:
        self.message_streams: asyncio.Queue[asyncio.Queue[Any]] = asyncio.Queue()
        self.group_events: asyncio.Queue[Any] = asyncio.Queue()
        self.poll_events: asyncio.Queue[Any] = asyncio.Queue()
        self.uploads: dict[str, bytes] = {}
        self.calls: list[tuple[str, Any, dict[str, str]]] = []
        self.messages: dict[str, types_pb.Message] = {}
        self.catch_up_after: list[int] = []
        self.missed: list[msg_pb.SubscribeMessageEventsResponse] = []
        self.counter = 0

    def record(self, name: str, request: Any, context: grpc.aio.ServicerContext) -> None:
        metadata = {str(k): str(v) for k, v in (context.invocation_metadata() or ())}
        self.calls.append((name, request, metadata))

    def new_guid(self) -> str:
        self.counter += 1
        return f"OUT-{self.counter}"


def received(seq: int, m: types_pb.Message, chat: str = DM) -> msg_pb.SubscribeMessageEventsResponse:
    event = types_pb.MessageChangeEvent(chat_guid=chat, is_from_me=m.is_from_me)
    event.message_received.message.CopyFrom(m)
    return msg_pb.SubscribeMessageEventsResponse(sequence=seq, message_changed=event)


def inbound(
    guid: str, text: str, *, from_me: bool = False, attachments=(), chat: str = DM
) -> types_pb.Message:
    m = types_pb.Message(guid=guid, is_from_me=from_me, chat_guids=[chat])
    m.content.text = text
    m.content.attachments.extend(attachments)
    m.sender.CopyFrom(addr(LINE if from_me else PEER))
    m.date_created.GetCurrentTime()
    return m


def build_services(state: FakeServer) -> list[tuple[Any, Any]]:
    class Messages(msg_grpc.MessageServiceServicer):
        async def SendTextMessage(self, request, context):
            state.record("SendTextMessage", request, context)
            m = inbound(state.new_guid(), request.text, from_me=True)
            state.messages[m.guid] = m
            return msg_pb.MessageResponse(message=m)

        async def SendAttachmentMessage(self, request, context):
            state.record("SendAttachmentMessage", request, context)
            return msg_pb.MessageResponse(message=inbound(state.new_guid(), "", from_me=True))

        async def SendCustomizedMiniAppMessage(self, request, context):
            state.record("SendCustomizedMiniAppMessage", request, context)
            return msg_pb.MessageResponse(message=inbound(state.new_guid(), "", from_me=True))

        async def SetReaction(self, request, context):
            state.record("SetReaction", request, context)
            return msg_pb.MessageResponse(message=inbound(state.new_guid(), "", from_me=True))

        async def GetMessage(self, request, context):
            if request.message_guid not in state.messages:
                await context.abort(grpc.StatusCode.NOT_FOUND, "message not found")
            return msg_pb.GetMessageResponse(message=state.messages[request.message_guid])

        async def SubscribeMessageEvents(self, request, context):
            state.record("SubscribeMessageEvents", request, context)
            queue: asyncio.Queue[Any] = asyncio.Queue()
            await state.message_streams.put(queue)
            while (item := await queue.get()) is not None:
                yield item

    class Chats(chat_grpc.ChatServiceServicer):
        async def SetTyping(self, request, context):
            state.record("SetTyping", request, context)
            return empty_pb2.Empty()

        async def MarkChatRead(self, request, context):
            state.record("MarkChatRead", request, context)
            return empty_pb2.Empty()

    class Groups(group_grpc.GroupServiceServicer):
        async def SubscribeGroupEvents(self, request, context):
            while True:
                yield await state.group_events.get()

    class Polls(poll_grpc.PollServiceServicer):
        async def SubscribePollEvents(self, request, context):
            while True:
                yield await state.poll_events.get()

    class Attachments(att_grpc.AttachmentServiceServicer):
        async def UploadAttachment(self, request, context):
            state.record("UploadAttachment", request, context)
            guid = f"ATT-UP-{len(state.uploads) + 1}"
            state.uploads[guid] = request.data
            return attsvc_pb.UploadAttachmentResponse(
                attachment=att_pb.AttachmentInfo(guid=guid, file_name=request.file_name)
            )

    class Events(event_grpc.EventServiceServicer):
        async def CatchUpEvents(self, request, context):
            state.catch_up_after.append(request.after_sequence)
            for frame in state.missed:
                yield event_pb.CatchUpEventsResponse(
                    sequence=frame.sequence, message_changed=frame.message_changed
                )
            yield event_pb.CatchUpEventsResponse(complete=event_pb.CatchUpEventsComplete(head_sequence=99))

    return [
        (Messages(), msg_grpc.add_MessageServiceServicer_to_server),
        (Chats(), chat_grpc.add_ChatServiceServicer_to_server),
        (Groups(), group_grpc.add_GroupServiceServicer_to_server),
        (Polls(), poll_grpc.add_PollServiceServicer_to_server),
        (Attachments(), att_grpc.add_AttachmentServiceServicer_to_server),
        (Events(), event_grpc.add_EventServiceServicer_to_server),
    ]


@pytest.fixture
async def fake():
    state = FakeServer()
    server = grpc.aio.server()
    for servicer, add in build_services(state):
        add(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield state, port
    await server.stop(None)


async def wait_until(predicate, timeout: float = 5.0) -> None:
    async def loop() -> None:
        while not predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(loop(), timeout)


async def test_end_to_end(fake):
    state, port = fake
    provider = IMessage(lines=[LineConfig(f"127.0.0.1:{port}", "tkn", phone=LINE, tls=False)])
    client = spectrum.Client(providers=[provider])
    got: dict[str, list[spectrum.Message]] = {"message": [], "reaction": [], "read": [], "member": []}

    @client.event
    async def on_message(message: spectrum.Message):
        got["message"].append(message)
        if message.text == "ping":
            async with message.space.typing():
                await message.reply("pong")

    @client.event
    async def on_reaction(message):
        got["reaction"].append(message)

    @client.event
    async def on_read_receipt(message):
        got["read"].append(message)

    @client.event
    async def on_member_add(message):
        got["member"].append(message)

    runner = asyncio.create_task(client.start())
    stream = await asyncio.wait_for(state.message_streams.get(), 5)

    # 1) own echo is suppressed, inbound text triggers a reply
    await stream.put(received(1, inbound("ME-1", "echo", from_me=True)))
    await stream.put(received(2, inbound("IN-1", "ping")))
    await wait_until(lambda: any(c[0] == "SendTextMessage" for c in state.calls))
    assert [m.text for m in got["message"]] == ["ping"]

    name, request, metadata = next(c for c in state.calls if c[0] == "SendTextMessage")
    assert request.text == "pong" and request.chat_guid == DM and request.reply_to.message_guid == "IN-1"
    assert metadata["authorization"] == "Bearer tkn" and metadata.get("x-idempotency-key")
    assert [c[1].is_typing for c in state.calls if c[0] == "SetTyping"] == [True, False]
    ping = got["message"][0]
    assert ping.sender is not None and ping.sender.id == PEER
    assert ping.sender.service is spectrum.AddressService.IMESSAGE
    assert ping.space.is_dm and ping.space.phone == LINE

    # 2) peer reacts to our reply (resolved from the outbound cache)
    reply_guid = "OUT-1"  # first guid minted by the fake server
    ev = types_pb.MessageChangeEvent(chat_guid=DM, actor=addr(PEER))
    ev.reaction_added.message_guid = reply_guid
    ev.reaction_added.reaction.kind = types_pb.MESSAGE_REACTION_KIND_LAUGH
    await stream.put(msg_pb.SubscribeMessageEventsResponse(sequence=3, message_changed=ev))
    await wait_until(lambda: got["reaction"])
    reaction = got["reaction"][0].content
    assert isinstance(reaction, Reaction) and reaction.emoji == "😂"
    assert reaction.target.id == reply_guid and reaction.target.is_outbound
    # like spectrum-ts, the outbound record of a reply carries the inner content
    assert isinstance(reaction.target.content, Text) and reaction.target.content.text == "pong"

    # 3) read receipt in a DM: the reader is the peer even though the actor is our line
    ev = types_pb.MessageChangeEvent(chat_guid=DM, actor=addr(LINE), is_from_me=True)
    ev.message_read.message_guid = reply_guid
    await stream.put(msg_pb.SubscribeMessageEventsResponse(sequence=4, message_changed=ev))
    await wait_until(lambda: got["read"])
    receipt = got["read"][0]
    assert isinstance(receipt.content, Read) and receipt.sender is not None and receipt.sender.id == PEER

    # 4) multipart inbound -> group with child ids
    photo = att_pb.AttachmentInfo(guid="ATT-1", file_name="IMG.HEIC", mime_type="image/heic", total_bytes=3)
    await stream.put(received(5, inbound("IN-2", "look￼", attachments=[photo])))
    await wait_until(lambda: any(m.id == "IN-2" for m in got["message"]))
    album = next(m for m in got["message"] if m.id == "IN-2").content
    assert isinstance(album, Group)
    assert [i.id for i in album.items] == ["p:0/IN-2", "p:1/IN-2"]
    assert album.items[1].content.mime_type == "image/heic"

    # 5) group event on a dedicated line
    gev = gtypes_pb.GroupChangeEvent(chat_guid=GROUP, actor=addr(PEER))
    gev.participant_added.participant.CopyFrom(addr("+15557777"))
    await state.group_events.put(group_pb.SubscribeGroupEventsResponse(sequence=6, group_changed=gev))
    await wait_until(lambda: got["member"])
    member = got["member"][0]
    assert isinstance(member.content, AddMember) and member.content.members == ("+15557777",)
    assert member.space.is_group

    # 6) disconnect -> reconnect with catch-up from the last sequence; duplicates are dropped
    state.missed = [received(6, inbound("DUP", "dup")), received(7, inbound("IN-3", "missed while down"))]
    await stream.put(None)  # server ends the stream
    await asyncio.wait_for(state.message_streams.get(), 10)
    await wait_until(lambda: any(m.text == "missed while down" for m in got["message"]), timeout=10)
    assert state.catch_up_after == [6]
    assert not any(m.text == "dup" for m in got["message"])

    await client.close()
    await asyncio.wait_for(runner, 5)


async def test_unauthorized_maps_to_error_class(fake):
    _, port = fake
    from spectrum.providers.imessage import IMessageRpc, NotFoundError

    rpc = IMessageRpc(f"127.0.0.1:{port}", "tkn", tls=False)
    with pytest.raises(NotFoundError):
        await rpc.get_message("missing")
    await rpc.close()


def test_chat_service_request_shape():
    # guard against proto drift: fields the outbound path relies on
    assert "reply_to" in msg_pb.SendTextMessageRequest.DESCRIPTOR.fields_by_name
    assert "is_typing" in chat_pb.SetTypingRequest.DESCRIPTOR.fields_by_name


async def test_polls_app_cards_and_voice(fake):
    state, port = fake
    provider = IMessage(lines=[LineConfig(f"127.0.0.1:{port}", "tkn", phone=LINE, tls=False)])
    client = spectrum.Client(providers=[provider])
    polls: list[spectrum.Message] = []
    votes: list[spectrum.Message] = []

    @client.event
    async def on_poll(message):
        polls.append(message)

    @client.event
    async def on_poll_vote(message):
        votes.append(message)

    runner = asyncio.create_task(client.start())
    await asyncio.wait_for(state.message_streams.get(), 5)

    # someone else creates a poll, then votes on it
    options = [
        ptypes_pb.PollOption(text="Pizza", option_identifier="o1"),
        ptypes_pb.PollOption(text="Sushi", option_identifier="o2"),
    ]
    created = ptypes_pb.PollChangeEvent(chat_guid=DM, poll_message_guid="POLL-1", actor=addr(PEER))
    created.created.title = "Lunch?"
    created.created.options.extend(options)
    await state.poll_events.put(poll_pb.SubscribePollEventsResponse(sequence=10, poll_changed=created))
    voted = ptypes_pb.PollChangeEvent(chat_guid=DM, poll_message_guid="POLL-1", actor=addr(PEER))
    voted.voted.option_identifier = "o2"
    await state.poll_events.put(poll_pb.SubscribePollEventsResponse(sequence=11, poll_changed=voted))
    await wait_until(lambda: polls and votes)
    poll = polls[0].content
    assert (
        isinstance(poll, Poll)
        and poll.title == "Lunch?"
        and [o.title for o in poll.options] == ["Pizza", "Sushi"]
    )
    vote = votes[0].content
    assert isinstance(vote, PollVote) and vote.title == "Sushi" and vote.selected and vote.poll_id == "POLL-1"

    space = await provider.get_space(DM)

    # app card with a supplied layout goes out through Spectrum's own extension (no metadata fetch)
    await space.send(spectrum.app("https://example.com/order/1", layout={"caption": "Order #1"}))
    req = next(c[1] for c in state.calls if c[0] == "SendCustomizedMiniAppMessage")
    assert req.extension_bundle_id == "codes.photon.Spectrum.MessagesExtension"
    assert req.layout.caption == "Order #1" and req.url == "https://example.com/order/1"

    # wav voice note is transcoded to m4a before upload
    if ffmpeg_path():
        from tests.test_media import _wav

        await space.send(spectrum.voice(_wav(), name="note.wav", mime_type="audio/wav"))
        upload = next(c[1] for c in state.calls if c[0] == "UploadAttachment")
        assert upload.file_name == "note.m4a" and is_m4a(upload.data)
        sent = next(c[1] for c in state.calls if c[0] == "SendAttachmentMessage")
        assert sent.is_audio_message

    await client.close()
    await asyncio.wait_for(runner, 5)

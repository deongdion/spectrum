"""Async gRPC client for one iMessage line (``photon.imessage.v1``).

Python counterpart of ``@photon-ai/advanced-imessage``'s ``createClient``:

* ``authorization: Bearer <token>`` metadata (token resolved per call, so renewals apply)
* ``x-idempotency-key`` on mutating calls (one key per logical call, reused across retries)
* unary retries when the server marks the failure ``x-retryable: true``
* one forced token refresh + retry on ``UNAUTHENTICATED``
* errors mapped to ``IMessageError`` subclasses
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import grpc

from ...core.utils import jittered_backoff
from ._proto.photon.imessage.v1 import (
    address_service_pb2 as address_pb,
)
from ._proto.photon.imessage.v1 import (
    address_service_pb2_grpc as address_grpc,
)
from ._proto.photon.imessage.v1 import (
    attachment_service_pb2 as attachment_pb,
)
from ._proto.photon.imessage.v1 import (
    attachment_service_pb2_grpc as attachment_grpc,
)
from ._proto.photon.imessage.v1 import (
    chat_service_pb2 as chat_pb,
)
from ._proto.photon.imessage.v1 import (
    chat_service_pb2_grpc as chat_grpc,
)
from ._proto.photon.imessage.v1 import (
    event_service_pb2 as event_pb,
)
from ._proto.photon.imessage.v1 import (
    event_service_pb2_grpc as event_grpc,
)
from ._proto.photon.imessage.v1 import (
    group_service_pb2 as group_pb,
)
from ._proto.photon.imessage.v1 import (
    group_service_pb2_grpc as group_grpc,
)
from ._proto.photon.imessage.v1 import (
    message_service_pb2 as message_pb,
)
from ._proto.photon.imessage.v1 import (
    message_service_pb2_grpc as message_grpc,
)
from ._proto.photon.imessage.v1 import (
    message_types_pb2 as types_pb,
)
from ._proto.photon.imessage.v1 import (
    poll_service_pb2 as poll_pb,
)
from ._proto.photon.imessage.v1 import (
    poll_service_pb2_grpc as poll_grpc,
)
from .errors import AuthenticationError, IMessageError, from_rpc_error, is_retryable

log = logging.getLogger("spectrum.imessage.rpc")

TokenProvider = Callable[[], Awaitable[str]]

MAX_MESSAGE_BYTES = 128 * 1024 * 1024

_CHANNEL_OPTIONS: list[tuple[str, Any]] = [
    ("grpc.keepalive_time_ms", 30_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 0.2
    max_delay: float = 5.0


DEFAULT_RETRY = RetryPolicy()


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One frame from an event stream: ``kind`` is the oneof case name."""

    sequence: int | None
    kind: str
    event: Any


class IMessageRpc:
    def __init__(
        self,
        address: str,
        token: str | TokenProvider,
        *,
        tls: bool = True,
        timeout: float | None = 60.0,
        retry: RetryPolicy | None = DEFAULT_RETRY,
        auto_idempotency: bool = True,
        on_unauthenticated: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        address = address.removeprefix("https://").removeprefix("http://").rstrip("/")
        if ":" not in address:
            address += ":443"
        self._address = address
        self._token: TokenProvider = token if callable(token) else _static_token(token)
        self._timeout = timeout
        self._retry = retry
        self._auto_idempotency = auto_idempotency
        self._on_unauthenticated = on_unauthenticated
        if tls:
            self._channel = grpc.aio.secure_channel(
                address, grpc.ssl_channel_credentials(), options=_CHANNEL_OPTIONS
            )
        else:
            self._channel = grpc.aio.insecure_channel(address, options=_CHANNEL_OPTIONS)
        self.messages = message_grpc.MessageServiceStub(self._channel)
        self.chats = chat_grpc.ChatServiceStub(self._channel)
        self.groups = group_grpc.GroupServiceStub(self._channel)
        self.attachments = attachment_grpc.AttachmentServiceStub(self._channel)
        self.polls = poll_grpc.PollServiceStub(self._channel)
        self.addresses = address_grpc.AddressServiceStub(self._channel)
        self.events = event_grpc.EventServiceStub(self._channel)

    @property
    def address(self) -> str:
        return self._address

    async def close(self) -> None:
        await self._channel.close()

    # ------------------------------------------------------------------ core call helpers

    async def _metadata(self, idempotency_key: str | None) -> list[tuple[str, str]]:
        metadata = [("authorization", f"Bearer {await self._token()}")]
        if idempotency_key:
            metadata.append(("x-idempotency-key", idempotency_key))
        return metadata

    async def unary(self, method: Callable[..., Any], request: Any, *, mutating: bool = False) -> Any:
        key = str(uuid.uuid4()) if (mutating and self._auto_idempotency) else None
        attempts = self._retry.max_attempts if self._retry else 1
        refreshed = False
        attempt = 0
        while True:
            try:
                return await method(request, metadata=await self._metadata(key), timeout=self._timeout)
            except grpc.aio.AioRpcError as exc:
                if (
                    exc.code() is grpc.StatusCode.UNAUTHENTICATED
                    and self._on_unauthenticated
                    and not refreshed
                ):
                    refreshed = True
                    await self._on_unauthenticated()
                    continue
                attempt += 1
                if self._retry and is_retryable(exc) and attempt < attempts:
                    delay = min(
                        self._retry.max_delay,
                        jittered_backoff(attempt - 1, initial=self._retry.initial_delay),
                    )
                    log.debug("retrying %s in %.2fs", getattr(method, "_method", method), delay)
                    await asyncio.sleep(delay)
                    continue
                raise from_rpc_error(exc) from exc

    async def stream(self, method: Callable[..., Any], request: Any) -> AsyncIterator[Any]:
        """Server stream. Not retried automatically (use catch-up to recover)."""
        call = method(request, metadata=await self._metadata(None))
        try:
            async for frame in call:
                yield frame
        except grpc.aio.AioRpcError as exc:
            if exc.code() is grpc.StatusCode.CANCELLED:
                return
            error = from_rpc_error(exc)
            if isinstance(error, AuthenticationError) and self._on_unauthenticated:
                await self._on_unauthenticated()
            raise error from exc
        finally:
            call.cancel()

    async def _events(self, method: Callable[..., Any], request: Any) -> AsyncIterator[StreamEvent]:
        async for frame in self.stream(method, request):
            kind = frame.WhichOneof("payload")
            if kind is None or kind == "heartbeat":
                continue
            sequence = frame.sequence if frame.HasField("sequence") else None
            yield StreamEvent(sequence, kind, getattr(frame, kind))

    # ------------------------------------------------------------------ messages

    async def send_text(
        self,
        chat_guid: str,
        text: str,
        *,
        reply_to: types_pb.ReplyTarget | None = None,
        effect_id: str | None = None,
        formatting: Sequence[types_pb.TextFormat] = (),
        enable_link_preview: bool | None = None,
        subject: str | None = None,
        client_message_id: str | None = None,
    ) -> message_pb.MessageResponse:
        request = message_pb.SendTextMessageRequest(
            chat_guid=chat_guid, text=text, formatting=list(formatting)
        )
        if reply_to is not None:
            request.reply_to.CopyFrom(reply_to)
        if effect_id:
            request.effect_id = effect_id
        if enable_link_preview is not None:
            request.enable_link_preview = enable_link_preview
        if subject:
            request.subject = subject
        if client_message_id:
            request.client_message_id = client_message_id
        return await self.unary(self.messages.SendTextMessage, request, mutating=True)

    async def send_attachment(
        self,
        chat_guid: str,
        attachment_guid: str,
        *,
        reply_to: types_pb.ReplyTarget | None = None,
        effect_id: str | None = None,
        is_audio_message: bool = False,
        attachment_name: str | None = None,
    ) -> message_pb.MessageResponse:
        ref = types_pb.AttachmentRef(attachment_guid=attachment_guid)
        if attachment_name:
            ref.attachment_name = attachment_name
        request = message_pb.SendAttachmentMessageRequest(chat_guid=chat_guid, attachment=ref)
        if reply_to is not None:
            request.reply_to.CopyFrom(reply_to)
        if effect_id:
            request.effect_id = effect_id
        if is_audio_message:
            request.is_audio_message = True
        return await self.unary(self.messages.SendAttachmentMessage, request, mutating=True)

    async def send_multipart(
        self,
        chat_guid: str,
        parts: Sequence[types_pb.MessagePart],
        *,
        reply_to: types_pb.ReplyTarget | None = None,
        effect_id: str | None = None,
    ) -> message_pb.MessageResponse:
        request = message_pb.SendMultipartMessageRequest(chat_guid=chat_guid, parts=list(parts))
        if reply_to is not None:
            request.reply_to.CopyFrom(reply_to)
        if effect_id:
            request.effect_id = effect_id
        return await self.unary(self.messages.SendMultipartMessage, request, mutating=True)

    async def send_mini_app(
        self, request: message_pb.SendCustomizedMiniAppMessageRequest
    ) -> message_pb.MessageResponse:
        return await self.unary(self.messages.SendCustomizedMiniAppMessage, request, mutating=True)

    async def update_mini_app(
        self, request: message_pb.UpdateCustomizedMiniAppMessageRequest
    ) -> message_pb.MessageResponse:
        return await self.unary(self.messages.UpdateCustomizedMiniAppMessage, request, mutating=True)

    async def edit_message(
        self, chat_guid: str, message_guid: str, new_text: str, *, part_index: int | None = None
    ) -> message_pb.MessageResponse:
        target = message_pb.MessageTarget(chat_guid=chat_guid, message_guid=message_guid)
        if part_index is not None:
            target.target_part_index = part_index
        request = message_pb.EditMessageRequest(target=target, new_text=new_text)
        return await self.unary(self.messages.EditMessage, request, mutating=True)

    async def unsend_message(
        self, chat_guid: str, message_guid: str, *, part_index: int | None = None
    ) -> None:
        target = message_pb.MessageTarget(chat_guid=chat_guid, message_guid=message_guid)
        if part_index is not None:
            target.target_part_index = part_index
        await self.unary(
            self.messages.UnsendMessage, message_pb.UnsendMessageRequest(target=target), mutating=True
        )

    async def set_reaction(
        self,
        chat_guid: str,
        message_guid: str,
        reaction: types_pb.MessageReaction,
        is_set: bool,
        *,
        part_index: int | None = None,
    ) -> message_pb.MessageResponse:
        target = message_pb.MessageTarget(chat_guid=chat_guid, message_guid=message_guid)
        if part_index is not None:
            target.target_part_index = part_index
        request = message_pb.SetReactionRequest(target=target, reaction=reaction, is_set=is_set)
        return await self.unary(self.messages.SetReaction, request, mutating=True)

    async def get_message(self, message_guid: str) -> types_pb.Message:
        response = await self.unary(
            self.messages.GetMessage, message_pb.GetMessageRequest(message_guid=message_guid)
        )
        return response.message

    async def list_chat_messages(
        self, chat_guid: str, *, page_size: int = 50, page_token: str | None = None
    ) -> message_pb.ListChatMessagesResponse:
        request = message_pb.ListChatMessagesRequest(chat_guid=chat_guid, page_size=page_size)
        if page_token:
            request.page_token = page_token
        return await self.unary(self.messages.ListChatMessages, request)

    def subscribe_message_events(self, chat_guid: str | None = None) -> AsyncIterator[StreamEvent]:
        request = message_pb.SubscribeMessageEventsRequest()
        if chat_guid:
            request.chat_guid = chat_guid
        return self._events(self.messages.SubscribeMessageEvents, request)

    # ------------------------------------------------------------------ chats

    async def create_chat(self, addresses: Sequence[str]) -> chat_pb.CreateChatResponse:
        return await self.unary(
            self.chats.CreateChat, chat_pb.CreateChatRequest(addresses=list(addresses)), mutating=True
        )

    async def get_chat(self, chat_guid: str) -> Any:
        return (await self.unary(self.chats.GetChat, chat_pb.GetChatRequest(chat_guid=chat_guid))).chat

    async def mark_read(self, chat_guid: str) -> None:
        await self.unary(
            self.chats.MarkChatRead, chat_pb.MarkChatReadRequest(chat_guid=chat_guid), mutating=True
        )

    async def set_typing(self, chat_guid: str, is_typing: bool) -> None:
        await self.unary(
            self.chats.SetTyping, chat_pb.SetTypingRequest(chat_guid=chat_guid, is_typing=is_typing)
        )

    async def share_contact_info(self, chat_guid: str) -> None:
        await self.unary(
            self.chats.ShareContactInfo, chat_pb.ShareContactInfoRequest(chat_guid=chat_guid), mutating=True
        )

    async def set_background(self, chat_guid: str, data: bytes) -> None:
        await self.unary(
            self.chats.SetBackground,
            chat_pb.SetBackgroundRequest(chat_guid=chat_guid, data=data),
            mutating=True,
        )

    async def remove_background(self, chat_guid: str) -> None:
        await self.unary(
            self.chats.RemoveBackground, chat_pb.RemoveBackgroundRequest(chat_guid=chat_guid), mutating=True
        )

    # ------------------------------------------------------------------ groups

    async def set_display_name(self, chat_guid: str, display_name: str) -> None:
        request = group_pb.SetDisplayNameRequest(chat_guid=chat_guid, display_name=display_name)
        await self.unary(self.groups.SetDisplayName, request, mutating=True)

    async def add_participants(self, chat_guid: str, addresses: Sequence[str]) -> None:
        request = group_pb.AddParticipantsRequest(chat_guid=chat_guid, addresses=list(addresses))
        await self.unary(self.groups.AddParticipants, request, mutating=True)

    async def remove_participants(self, chat_guid: str, addresses: Sequence[str]) -> None:
        request = group_pb.RemoveParticipantsRequest(chat_guid=chat_guid, addresses=list(addresses))
        await self.unary(self.groups.RemoveParticipants, request, mutating=True)

    async def leave_group(self, chat_guid: str) -> None:
        await self.unary(
            self.groups.LeaveGroup, group_pb.LeaveGroupRequest(chat_guid=chat_guid), mutating=True
        )

    async def set_icon(self, chat_guid: str, data: bytes) -> None:
        await self.unary(
            self.groups.SetIcon, group_pb.SetIconRequest(chat_guid=chat_guid, data=data), mutating=True
        )

    async def remove_icon(self, chat_guid: str) -> None:
        await self.unary(
            self.groups.RemoveIcon, group_pb.RemoveIconRequest(chat_guid=chat_guid), mutating=True
        )

    async def get_icon(self, chat_guid: str) -> group_pb.GetIconResponse:
        return await self.unary(self.groups.GetIcon, group_pb.GetIconRequest(chat_guid=chat_guid))

    def subscribe_group_events(self, chat_guid: str | None = None) -> AsyncIterator[StreamEvent]:
        request = group_pb.SubscribeGroupEventsRequest()
        if chat_guid:
            request.chat_guid = chat_guid
        return self._events(self.groups.SubscribeGroupEvents, request)

    # ------------------------------------------------------------------ attachments

    async def upload_attachment(self, file_name: str, data: bytes) -> attachment_pb.UploadAttachmentResponse:
        if not data:
            raise IMessageError("attachment data must not be empty", code="invalidArgument")
        request = attachment_pb.UploadAttachmentRequest(file_name=file_name or "attachment", data=data)
        return await self.unary(self.attachments.UploadAttachment, request, mutating=True)

    async def get_attachment_info(self, attachment_guid: str) -> Any:
        request = attachment_pb.GetAttachmentInfoRequest(attachment_guid=attachment_guid)
        return (await self.unary(self.attachments.GetAttachmentInfo, request)).attachment

    async def download_attachment_stream(self, attachment_guid: str) -> AsyncIterator[bytes]:
        """Primary file bytes only (Live Photo companion frames are skipped)."""
        request = attachment_pb.DownloadAttachmentRequest(attachment_guid=attachment_guid)
        async for frame in self.stream(self.attachments.DownloadAttachment, request):
            if frame.WhichOneof("payload") == "primary_chunk":
                yield frame.primary_chunk

    async def download_attachment(self, attachment_guid: str) -> bytes:
        return b"".join([chunk async for chunk in self.download_attachment_stream(attachment_guid)])

    # ------------------------------------------------------------------ polls

    async def create_poll(self, chat_guid: str, title: str, options: Sequence[str]) -> Any:
        request = poll_pb.CreatePollRequest(chat_guid=chat_guid, title=title, options=list(options))
        return (await self.unary(self.polls.CreatePoll, request, mutating=True)).poll

    async def get_poll(self, poll_message_guid: str) -> Any:
        request = poll_pb.GetPollRequest(poll_message_guid=poll_message_guid)
        return (await self.unary(self.polls.GetPoll, request)).poll

    def subscribe_poll_events(self, poll_message_guid: str | None = None) -> AsyncIterator[StreamEvent]:
        request = poll_pb.SubscribePollEventsRequest()
        if poll_message_guid:
            request.poll_message_guid = poll_message_guid
        return self._events(self.polls.SubscribePollEvents, request)

    # ------------------------------------------------------------------ addresses / events

    async def is_imessage_available(self, address: str) -> bool:
        request = address_pb.GetIMessageAvailabilityRequest(address=address)
        response = await self.unary(self.addresses.GetIMessageAvailability, request)
        return bool(response.is_available)

    def catch_up(self, after_sequence: int | None = None) -> AsyncIterator[StreamEvent]:
        """Replay the durable event log. Ends with a ``kind == "complete"`` event."""
        request = event_pb.CatchUpEventsRequest()
        if after_sequence is not None:
            request.after_sequence = after_sequence
        return self._events(self.events.CatchUpEvents, request)


def _static_token(token: str) -> TokenProvider:
    async def provider() -> str:
        return token

    return provider

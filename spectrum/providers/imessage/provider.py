"""Spectrum Cloud iMessage provider (``@spectrum-ts/imessage`` port).

Streaming model, per line:

1. open live ``Subscribe*Events`` streams (messages, polls, and on dedicated lines groups)
2. after a disconnect, replay ``CatchUpEvents(after=<last sequence>)`` while the new
   live streams buffer, then drain the buffer
3. dedupe every event by its global ``sequence``

Lines that appear/disappear at token renewal start/stop their stream tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from ...core.errors import ConfigurationError, UnsupportedError
from ...core.utils import jittered_backoff
from ...models.content import Attachment, Content
from ...models.enums import MessageEffect, Platform, SpaceType
from ...models.message import Message
from ...models.space import AvatarData, Space
from ...models.user import User
from ..base import Provider, ProviderContext
from .errors import IMessageError, NotFoundError, ValidationError
from .inbound import InboundMapper, chat_type, dm_chat_guid, parse_child_id
from .lines import SHARED_PHONE, LineConfig, LineHandle, LineManager
from .outbound import OutboundSender
from .rpc import DEFAULT_RETRY, RetryPolicy, StreamEvent

log = logging.getLogger("spectrum.imessage")

_CATCH_UP_KINDS = {"message_changed", "group_changed", "poll_changed"}


class IMessage(Provider):
    """iMessage over Spectrum Cloud (or explicit / self-hosted lines).

    Args:
        lines: explicit ``LineConfig`` entries instead of cloud token discovery.
        group_events: subscribe to group change events. Defaults to dedicated lines only
            (shared-pool lines never receive them).
        poll_events: subscribe to poll vote events.
        catch_up: replay missed events after a reconnect.
        cache_size: per-line message cache used to resolve reaction / receipt targets.
    """

    platform = Platform.IMESSAGE
    effect = MessageEffect

    def __init__(
        self,
        *,
        lines: Sequence[LineConfig] | None = None,
        group_events: bool | None = None,
        poll_events: bool = True,
        catch_up: bool = True,
        cache_size: int = 2000,
        retry: RetryPolicy | None = DEFAULT_RETRY,
        timeout: float | None = 60.0,
        reconnect_max_delay: float = 30.0,
    ) -> None:
        super().__init__()
        self._explicit = list(lines or [])
        self._group_events = group_events
        self._poll_events = poll_events
        self._catch_up = catch_up
        self._cache_size = cache_size
        self._retry = retry
        self._timeout = timeout
        self._reconnect_max_delay = reconnect_max_delay
        self._manager: LineManager | None = None
        self._queue: asyncio.Queue[Message] | None = None
        self._line_tasks: dict[str, asyncio.Task[None]] = {}
        self._streaming = False

    # ------------------------------------------------------------------ properties

    @property
    def manager(self) -> LineManager:
        if self._manager is None:
            raise ConfigurationError("iMessage provider is not set up (call client.login())")
        return self._manager

    @property
    def phones(self) -> tuple[str, ...]:
        """Phone numbers of the connected lines (``("shared",)`` on pooled plans)."""
        return self.manager.phones

    @property
    def is_shared(self) -> bool:
        return self.manager.is_shared

    @property
    def catch_up(self) -> bool:
        return self._catch_up

    @catch_up.setter
    def catch_up(self, value: bool) -> None:
        self._catch_up = bool(value)

    @property
    def poll_events(self) -> bool:
        return self._poll_events

    @poll_events.setter
    def poll_events(self, value: bool) -> None:
        if self._streaming:
            raise RuntimeError("poll_events can only be changed before streaming starts")
        self._poll_events = bool(value)

    def _include_group_events(self) -> bool:
        if self._group_events is not None:
            return self._group_events and not self.is_shared
        return not self.is_shared

    # ------------------------------------------------------------------ lifecycle

    async def setup(self, ctx: ProviderContext) -> None:
        await super().setup(ctx)
        cloud = None if self._explicit else ctx.cloud
        if not self._explicit and cloud is None:
            raise ConfigurationError(
                "iMessage needs project credentials (SPECTRUM_PROJECT_ID / SPECTRUM_PROJECT_SECRET) "
                "or explicit lines=[LineConfig(...)]"
            )
        manager = LineManager(
            cloud=cloud,
            explicit=self._explicit,
            retry=self._retry,
            timeout=self._timeout,
            cache_size=self._cache_size,
        )
        manager.on_attach(self._line_attached)
        manager.on_detach(self._line_detached)
        self._manager = manager
        await manager.start()
        log.info("iMessage ready: %s line(s) %s", len(manager.lines), ", ".join(manager.phones))

    async def close(self) -> None:
        self._streaming = False
        for task in list(self._line_tasks.values()):
            task.cancel()
        for task in list(self._line_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._line_tasks.clear()
        if self._manager is not None:
            await self._manager.close()
            self._manager = None
        await super().close()

    # ------------------------------------------------------------------ streaming

    async def stream(self) -> AsyncIterator[Message]:
        self._queue = asyncio.Queue()
        self._streaming = True
        for line in self.manager.lines:
            self._start_line(line)
        try:
            while self._streaming:
                yield await self._queue.get()
        finally:
            self._streaming = False

    def _start_line(self, line: LineHandle) -> None:
        if line.key in self._line_tasks:
            return
        task = asyncio.create_task(self._run_line(line), name=f"spectrum:imessage:{line.phone}")
        self._line_tasks[line.key] = task

    async def _line_attached(self, line: LineHandle) -> None:
        if self._streaming:
            self._start_line(line)

    async def _line_detached(self, line: LineHandle) -> None:
        task = self._line_tasks.pop(line.key, None)
        if task:
            task.cancel()

    async def _run_line(self, line: LineHandle) -> None:
        mapper = InboundMapper(self, line)
        include_groups = self._include_group_events()
        attempt = 0
        while self._streaming:
            buffer: asyncio.Queue[StreamEvent | BaseException] = asyncio.Queue()
            streams = [line.rpc.subscribe_message_events()]
            if self._poll_events:
                streams.append(line.rpc.subscribe_poll_events())
            if include_groups:
                streams.append(line.rpc.subscribe_group_events())
            pumps = [asyncio.create_task(self._pump(s, buffer)) for s in streams]
            try:
                if self._catch_up and line.cursor is not None:
                    await self._replay(line, mapper, include_groups)
                attempt = 0
                while True:
                    item = await buffer.get()
                    if isinstance(item, BaseException):
                        raise item
                    await self._handle(line, mapper, item)
                    attempt = 0
            except asyncio.CancelledError:
                raise
            except ValidationError as exc:
                log.warning("line %s: cursor rejected (%s); resetting catch-up cursor", line.phone, exc.code)
                line.cursor = None
            except Exception as exc:
                attempt += 1
                delay = jittered_backoff(attempt, initial=1.0, maximum=self._reconnect_max_delay)
                log.warning("line %s stream error (%s); reconnecting in %.1fs", line.phone, exc, delay)
                await asyncio.sleep(delay)
            finally:
                for pump in pumps:
                    pump.cancel()

    @staticmethod
    async def _pump(stream: AsyncIterator[StreamEvent], buffer: asyncio.Queue[Any]) -> None:
        try:
            async for event in stream:
                await buffer.put(event)
            await buffer.put(ConnectionResetError("stream ended"))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await buffer.put(exc)

    async def _replay(self, line: LineHandle, mapper: InboundMapper, include_groups: bool) -> None:
        log.debug("line %s: catching up after sequence %s", line.phone, line.cursor)
        async for event in line.rpc.catch_up(line.cursor):
            if event.kind == "complete":
                break
            if event.kind not in _CATCH_UP_KINDS:
                continue
            if event.kind == "group_changed" and not include_groups:
                continue
            if event.kind == "poll_changed" and not self._poll_events:
                continue
            await self._handle(line, mapper, event)

    async def _handle(self, line: LineHandle, mapper: InboundMapper, event: StreamEvent) -> None:
        seq = event.sequence
        if seq is not None:
            if not line.seen_sequences.add(seq):
                return
            line.cursor = seq if line.cursor is None else max(line.cursor, seq)
        self._dispatch_raw(line, event)
        try:
            if event.kind == "message_changed":
                messages = await mapper.message_event(seq, event.event)
            elif event.kind == "group_changed":
                messages = await mapper.group_event(seq, event.event)
            elif event.kind == "poll_changed":
                messages = await mapper.poll_event(seq, event.event)
            else:
                return
        except IMessageError as exc:
            if exc.retryable:
                raise
            log.warning("skipping unmappable %s event %s: %s", event.kind, seq, exc)
            return
        except Exception:
            log.exception("skipping unmappable %s event %s", event.kind, seq)
            return
        assert self._queue is not None
        for message in messages:
            await self._queue.put(message)

    def _dispatch_raw(self, line: LineHandle, event: StreamEvent) -> None:
        dispatch = self.context.dispatch
        if dispatch is not None:
            dispatch("imessage_raw_event", line.phone, event)

    # ------------------------------------------------------------------ outbound

    def _line_for(self, space: Space) -> LineHandle:
        return self.manager.for_phone(None if space.phone in (None, SHARED_PHONE) else space.phone)

    async def send(self, space: Space, content: Content) -> Message | None:
        return await OutboundSender(self, self._line_for(space)).send(space, content)

    def agent_user(self, line: LineHandle) -> User:
        return self.make_user(line.phone, is_agent=True, address=None if line.is_shared else line.phone)

    # ------------------------------------------------------------------ resolution

    async def resolve_user(self, user_id: str) -> User:
        return self.make_user(user_id, address=user_id)

    user = resolve_user

    async def create_space(
        self, users: Sequence[User | str], *, phone: str | None = None, **params: Any
    ) -> Space:
        """DM (one user) or group (several users, dedicated lines only)."""
        addresses = [u.id if isinstance(u, User) else u for u in users]
        if not addresses:
            raise ValueError("create_space() requires at least one user")
        manager = self.manager
        if manager.is_shared:
            if len(addresses) > 1:
                raise UnsupportedError.action(
                    "space creation",
                    "iMessage (shared mode)",
                    "shared mode cannot create group chats - use a dedicated number, or get_space(chat_guid)",
                )
            return self.make_space(dm_chat_guid(addresses[0]), type=SpaceType.DM, phone=SHARED_PHONE)
        line = manager.for_phone(phone) if phone else manager.random_line()
        response = await line.rpc.create_chat(addresses)
        return self.make_space(
            response.chat.guid,
            type=SpaceType.GROUP if response.chat.is_group else SpaceType.DM,
            phone=line.phone,
        )

    async def get_space(self, space_id: str, *, phone: str | None = None, **params: Any) -> Space:
        """Reference an existing chat by GUID. With several dedicated lines ``phone`` is required."""
        manager = self.manager
        if manager.is_shared:
            resolved = SHARED_PHONE
        elif phone:
            resolved = phone
        elif len(manager.lines) == 1:
            resolved = manager.lines[0].phone
        else:
            raise ConfigurationError(
                f"get_space() needs phone= with multiple lines. Available: {', '.join(manager.phones)}"
            )
        return self.make_space(space_id, type=chat_type(space_id), phone=resolved)

    async def get_message(self, space: Space, message_id: str) -> Message | None:
        line = self._line_for(space)
        cached = line.message_cache.get(message_id)
        if cached is not None:
            return cached
        mapper = InboundMapper(self, line)
        child = parse_child_id(message_id)
        try:
            parent = await mapper.rebuild(
                await line.rpc.get_message(child[1] if child else message_id), space.id
            )
        except NotFoundError:
            return None
        if child is None:
            return parent
        return line.message_cache.get(message_id)

    async def get_members(self, space: Space) -> list[User]:
        self._require_group(space, "get_members")
        line = self._line_for(space)
        chat = await line.rpc.get_chat(space.id)
        inbound = InboundMapper(self, line)
        return [
            u for p in chat.participants if p.address != line.phone and (u := inbound.user(p)) is not None
        ]

    async def get_avatar(self, space: Space) -> AvatarData | None:
        self._require_group(space, "get_avatar")
        try:
            icon = await self._line_for(space).rpc.get_icon(space.id)
        except NotFoundError:
            return None
        if not icon.data:
            return None
        return AvatarData(bytes(icon.data), icon.mime_type or "image/jpeg")

    async def get_display_name(self, space: Space) -> str | None:
        chat = await self._line_for(space).rpc.get_chat(space.id)
        return chat.display_name if chat.HasField("display_name") and chat.display_name else None

    async def fetch_attachment(
        self, attachment_id: str, *, space: Space | None = None, phone: str | None = None
    ) -> Attachment | None:
        """Lazy attachment by iMessage GUID. Pass ``phone`` (or a space) with several dedicated lines."""
        line = self.manager.for_phone(
            phone or (space.phone if space and space.phone != SHARED_PHONE else None)
        )
        try:
            info = await line.rpc.get_attachment_info(attachment_id)
        except NotFoundError:
            return None
        content = InboundMapper(self, line)._attachment(info, as_voice=False)
        return content if isinstance(content, Attachment) else None

    get_attachment = fetch_attachment

    async def is_imessage_available(self, address: str, *, phone: str | None = None) -> bool:
        return await self.manager.for_phone(phone).rpc.is_imessage_available(address)

    # ------------------------------------------------------------------ iMessage-only sugar

    async def background(self, space: Space, source: Any, *, mime_type: str | None = None) -> None:
        """Set (path / URL / bytes) or clear (``None`` / ``"clear"``) the chat background."""
        from ...content.builders import background

        await space.send(await background(source, mime_type=mime_type))

    async def share_contact_card(self, space: Space) -> None:
        from ...content.builders import contact_card

        await space.send(contact_card())

    @staticmethod
    def _require_group(space: Space, action: str) -> None:
        if space.type is not SpaceType.GROUP:
            raise UnsupportedError.action(action, "iMessage", "only group chats support this")


__all__ = ["IMessage", "LineConfig"]

"""``Client`` — the discord.py-style entry point.

    import spectrum
    from spectrum.providers import IMessage

    client = spectrum.Client(providers=[IMessage()])

    @client.event
    async def on_ready():
        print("ready on", client.imessage.phones)

    @client.event
    async def on_message(message: spectrum.Message):
        if message.text == "ping":
            await message.reply("pong")

    client.run()

Events (``on_<name>``):

=====================  ===============================================================
``on_ready()``         providers connected and streaming
``on_message(m)``      every inbound message, any content type
``on_reaction(m)``     ``m.content`` is ``Reaction`` (also emitted as ``on_message``)
``on_read_receipt(m)`` someone read your message (``m.content.target``)
``on_poll(m)``         someone else created a poll (``m.content`` is ``Poll``)
``on_poll_vote(m)``    poll (un)vote
``on_member_add(m)`` / ``on_member_remove(m)`` / ``on_member_leave(m)``
``on_space_rename(m)`` / ``on_space_avatar(m)``
``on_imessage_raw_event(phone, event)``  every raw iMessage stream event
``on_provider_error(provider, exc)``     a provider stream failed
``on_error(event, exc, *args)``          a handler raised (default: logged)
``on_close()``          client shut down
=====================  ===============================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, TypeVar, overload

from .cloud.client import CloudClient
from .cloud.types import ProjectInfo
from .core.config import WEBHOOK_SECRET_ENV, Credentials, resolve
from .core.dispatcher import Coro, EventDispatcher
from .core.errors import ClientClosedError, CloudError, ConfigurationError
from .core.utils import LRUCache
from .models.content import ContentInput
from .models.enums import Platform
from .models.message import Message
from .models.space import Space
from .models.user import User
from .providers.base import Provider, ProviderContext
from .webhook.handler import WebhookHandler, WebhookResponse
from .webhook.parser import WebhookParser

log = logging.getLogger("spectrum.client")

P = TypeVar("P", bound=Provider)
CoroT = TypeVar("CoroT", bound=Coro)


class Client:
    """Spectrum client.

    Args:
        providers: platform providers to run (e.g. ``[IMessage()]``).
        project_id / project_secret: Spectrum project credentials. Fallback to
            ``SPECTRUM_PROJECT_ID`` / ``SPECTRUM_PROJECT_SECRET`` (or ``PHOTON_*``).
        webhook_secret: signing secret for ``handle_webhook``. Fallback ``SPECTRUM_WEBHOOK_SECRET``.
        dedupe_size: how many recent message ids to remember for at-least-once dedupe.
    """

    def __init__(
        self,
        *,
        providers: Sequence[Provider],
        project_id: str | None = None,
        project_secret: str | None = None,
        webhook_secret: str | None = None,
        cloud_url: str | None = None,
        dedupe_size: int = 10_000,
    ) -> None:
        if not providers:
            raise ConfigurationError("at least one provider is required")
        platforms = [str(p.platform) for p in providers]
        if len(set(platforms)) != len(platforms):
            raise ConfigurationError(f"duplicate providers for platform(s): {platforms}")
        self._providers: dict[str, Provider] = {str(p.platform): p for p in providers}
        self._project_id = project_id
        self._project_secret = project_secret
        self._cloud_url = cloud_url
        self._dispatcher = EventDispatcher()
        self._seen: LRUCache[str, bool] = LRUCache(dedupe_size)
        self._webhook = WebhookHandler(
            resolve(webhook_secret, WEBHOOK_SECRET_ENV), WebhookParser(self._provider_for)
        )
        self._credentials: Credentials | None = None
        self._cloud: CloudClient | None = None
        self._project: ProjectInfo | None = None
        self._pumps: list[asyncio.Task[None]] = []
        self._subscribers: list[asyncio.Queue[Message | None]] = []
        self._logged_in = False
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._closing = False

    # ================================================================== properties

    @property
    def project_id(self) -> str | None:
        return self._credentials.project_id if self._credentials else self._project_id

    @project_id.setter
    def project_id(self, value: str | None) -> None:
        self._require_not_logged_in("project_id")
        self._project_id = value

    @property
    def project_secret(self) -> str | None:
        """Write-only in practice: reads return a mask so the secret never leaks into logs."""
        return "***" if (self._project_secret or self._credentials) else None

    @project_secret.setter
    def project_secret(self, value: str | None) -> None:
        self._require_not_logged_in("project_secret")
        self._project_secret = value

    @property
    def webhook_secret(self) -> str | None:
        return "***" if self._webhook.secret else None

    @webhook_secret.setter
    def webhook_secret(self, value: str | None) -> None:
        self._webhook.secret = value

    @property
    def providers(self) -> tuple[Provider, ...]:
        return tuple(self._providers.values())

    @property
    def cloud(self) -> CloudClient:
        """Spectrum Cloud management API client (available after ``login()`` with credentials)."""
        if self._cloud is None:
            raise ConfigurationError("no Spectrum Cloud client: project credentials missing or not logged in")
        return self._cloud

    @property
    def project(self) -> ProjectInfo | None:
        return self._project

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def imessage(self) -> Any:
        """The registered ``IMessage`` provider (typed helpers: ``create_space``, ``get_attachment`` ...)."""
        from .providers.imessage import IMessage

        return self.get_provider(IMessage)

    @overload
    def get_provider(self, key: type[P]) -> P: ...
    @overload
    def get_provider(self, key: Platform | str) -> Provider: ...

    def get_provider(self, key: Any) -> Any:
        if isinstance(key, type):
            for provider in self._providers.values():
                if isinstance(provider, key):
                    return provider
            raise ConfigurationError(f"no {key.__name__} provider registered")
        provider = self._providers.get(str(key))
        if provider is None:
            raise ConfigurationError(f"no provider registered for platform {key!s}")
        return provider

    def _provider_for(self, platform: Platform | str) -> Provider | None:
        provider = self._providers.get(str(platform))
        return provider if provider is not None and provider.is_ready else None

    # ================================================================== events

    def event(self, coro: CoroT) -> CoroT:
        """Register *the* handler for the event named by the function (``on_message`` ...)."""
        return self._dispatcher.event(coro)

    def listen(self, name: str | None = None) -> Callable[[CoroT], CoroT]:
        """Register an additional listener (several may coexist)."""
        return self._dispatcher.listen(name)

    def add_listener(self, func: Coro, name: str | None = None) -> None:
        self._dispatcher.add_listener(func, name)

    def remove_listener(self, func: Coro, name: str | None = None) -> None:
        self._dispatcher.remove_listener(func, name)

    async def wait_for(
        self, event: str, *, check: Callable[..., bool] | None = None, timeout: float | None = None
    ) -> Any:
        """Await the next ``event`` whose arguments satisfy ``check``.

        ``reply = await client.wait_for("message", check=lambda m: m.space == space, timeout=60)``
        """
        return await self._dispatcher.wait_for(event, check=check, timeout=timeout)

    def dispatch(self, event: str, *args: Any) -> None:
        self._dispatcher.dispatch(event, *args)

    async def messages(self) -> AsyncIterator[Message]:
        """spectrum-ts style iteration: ``async for message in client.messages(): ...``"""
        queue: asyncio.Queue[Message | None] = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            self._subscribers.remove(queue)

    # ================================================================== lifecycle

    async def login(self) -> None:
        """Resolve credentials, create the cloud client and set up every provider (no streaming)."""
        if self._closing or self.is_closed:
            raise ClientClosedError("client is closed")
        if self._logged_in:
            return
        self._credentials = Credentials.resolve(self._project_id, self._project_secret)
        if self._credentials is not None:
            self._cloud = CloudClient(self._credentials, base_url=self._cloud_url)
            try:
                self._project = await self._cloud.get_project()
                log.info("logged in to project %r", self._project.name)
            except CloudError as exc:
                if exc.status in (401, 403, 404):
                    await self._cloud.close()
                    raise ConfigurationError(
                        f"Spectrum Cloud rejected the project credentials: {exc}"
                    ) from exc
                log.warning("could not fetch project metadata: %s", exc)
        ctx = ProviderContext(self._credentials, self._cloud, self._project, self.dispatch)
        set_up: list[Provider] = []
        try:
            for provider in self._providers.values():
                await provider.setup(ctx)
                set_up.append(provider)
        except BaseException:
            for provider in set_up:
                with contextlib.suppress(Exception):
                    await provider.close()
            if self._cloud:
                await self._cloud.close()
            raise
        self._logged_in = True

    async def connect(self) -> None:
        """Open every provider stream, emit ``on_ready`` and run until ``close()``."""
        if not self._logged_in:
            raise ConfigurationError("call login() before connect()")
        for provider in self._providers.values():
            self._pumps.append(
                asyncio.create_task(self._pump(provider), name=f"spectrum:pump:{provider.platform}")
            )
        self._ready.set()
        self.dispatch("ready")
        await self._closed.wait()

    async def start(self) -> None:
        await self.login()
        await self.connect()

    async def close(self) -> None:
        if self._closing:
            await self._closed.wait()
            return
        self._closing = True
        for pump in self._pumps:
            pump.cancel()
        for pump in self._pumps:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        self._pumps.clear()
        for provider in self._providers.values():
            try:
                await provider.close()
            except Exception:
                log.exception("error closing %r", provider)
        if self._cloud:
            await self._cloud.close()
        for queue in self._subscribers:
            queue.put_nowait(None)
        self.dispatch("close")
        await self._dispatcher.drain()
        self._dispatcher.cancel_waiters()
        self._ready.clear()
        self._closed.set()

    async def __aenter__(self) -> Client:
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def run(self, *, log_level: int | None = logging.INFO, log_format: str | None = None) -> None:
        """Blocking entry point: configure logging, start, and close cleanly on Ctrl+C / SIGTERM."""
        if log_level is not None:
            logging.basicConfig(
                level=log_level, format=log_format or "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
            )

        async def runner() -> None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError, RuntimeError):  # not available on Windows
                    loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.close()))
            try:
                await self.start()
            finally:
                if not self.is_closed:
                    await self.close()

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(runner())

    # ================================================================== inbound

    async def _pump(self, provider: Provider) -> None:
        try:
            async for message in provider.stream():
                self._receive(message)
            log.info("%s stream ended", provider.platform)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("%s stream failed", provider.platform)
            self.dispatch("provider_error", provider, exc)
        if not self._closing and all(p.done() for p in self._pumps if p is not asyncio.current_task()):
            asyncio.ensure_future(self.close())

    def _receive(self, message: Message) -> None:
        if not self._seen.add(f"{message.platform}:{message.id}"):
            log.debug("dropping duplicate message %s", message.id)
            return
        for queue in self._subscribers:
            queue.put_nowait(message)
        self.dispatch("message", message)
        extra = message.content.type.event_name
        if extra:
            self.dispatch(extra, message)

    async def handle_webhook(
        self, body: bytes, headers: Mapping[str, str] | list[tuple[str, str]]
    ) -> WebhookResponse:
        """Verify + parse a native Spectrum webhook delivery and dispatch it as ``on_message``.

        Pass the **raw** request body. Returns the HTTP response to send back. Handlers run
        after this returns (fire-and-forget), so acknowledge immediately.
        """
        result = self._webhook.process(body, headers)
        event = result.event
        if event is not None and event.message is not None and not result.duplicate:
            self._receive(event.message)
        elif event is not None and event.message is None:
            self.dispatch("webhook_event", event)
        return result.response

    # ================================================================== conveniences

    async def fetch_user(self, platform: Platform | str, user_id: str) -> User:
        return await self.get_provider(platform).resolve_user(user_id)

    async def create_space(
        self, platform: Platform | str, users: User | str | Sequence[User | str], **params: Any
    ) -> Space:
        """Start a conversation (``params`` are provider-specific, e.g. iMessage ``phone=``)."""
        members = [users] if isinstance(users, User | str) else list(users)
        return await self.get_provider(platform).create_space(members, **params)

    async def fetch_space(self, platform: Platform | str, space_id: str, **params: Any) -> Space:
        return await self.get_provider(platform).get_space(space_id, **params)

    async def send(self, space: Space, *contents: ContentInput) -> Message | None | list[Message]:
        return await space.send(*contents)

    async def responding(self, space: Space, fn: Callable[[], Any]) -> Any:
        return await space.responding(fn)

    def _require_not_logged_in(self, name: str) -> None:
        if self._logged_in:
            raise ConfigurationError(f"{name} cannot change after login()")

    def __repr__(self) -> str:
        return f"<Client providers={list(self._providers)} logged_in={self._logged_in} ready={self.is_ready}>"


Spectrum = Client
"""spectrum-ts naming alias."""

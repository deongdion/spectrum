"""Event dispatching.

* ``@client.event`` registers *the* handler for ``on_<name>`` (replaces any previous one).
* ``@client.listen("on_<name>")`` adds an extra listener; any number may coexist.
* ``await client.wait_for("<name>", check=..., timeout=...)`` awaits the next matching event.
* Every handler runs in its own task, so a slow handler never blocks the stream.
  Exceptions are routed to ``on_error(event_name, exc, *args)``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

log = logging.getLogger("spectrum.dispatch")

Coro = Callable[..., Awaitable[Any]]
CoroT = TypeVar("CoroT", bound=Coro)


def _event_name(name: str) -> str:
    """``"message"`` / ``"on_message"`` -> ``"message"``."""
    return name[3:] if name.startswith("on_") else name


class EventDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[str, Coro] = {}
        self._listeners: dict[str, list[Coro]] = {}
        self._waiters: dict[str, list[tuple[asyncio.Future[Any], Callable[..., bool] | None]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ registration

    def event(self, coro: CoroT) -> CoroT:
        """Decorator registering the single handler for the event named by the function."""
        if not inspect.iscoroutinefunction(coro):
            raise TypeError("event handlers must be coroutine functions (async def)")
        if not coro.__name__.startswith("on_"):
            raise ValueError(f"event handler names must start with 'on_' (got {coro.__name__!r})")
        self._handlers[_event_name(coro.__name__)] = coro
        log.debug("registered handler %s", coro.__name__)
        return coro

    def listen(self, name: str | None = None) -> Callable[[CoroT], CoroT]:
        """Decorator adding an extra listener. ``name`` defaults to the function name."""

        def decorator(coro: CoroT) -> CoroT:
            if not inspect.iscoroutinefunction(coro):
                raise TypeError("listeners must be coroutine functions (async def)")
            event = _event_name(name or coro.__name__)
            self._listeners.setdefault(event, []).append(coro)
            return coro

        return decorator

    def add_listener(self, func: Coro, name: str | None = None) -> None:
        self.listen(name)(func)

    def remove_listener(self, func: Coro, name: str | None = None) -> None:
        event = _event_name(name or func.__name__)
        listeners = self._listeners.get(event, [])
        if func in listeners:
            listeners.remove(func)

    def has_handler(self, name: str) -> bool:
        event = _event_name(name)
        return event in self._handlers or bool(self._listeners.get(event)) or bool(self._waiters.get(event))

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, name: str, *args: Any) -> None:
        event = _event_name(name)
        log.debug("dispatch %s", event)

        waiters = self._waiters.get(event)
        if waiters:
            remaining = []
            for future, check in waiters:
                if future.done():
                    continue
                try:
                    matched = check(*args) if check else True
                except Exception as exc:  # a broken check fails its own waiter only
                    future.set_exception(exc)
                    continue
                if matched:
                    future.set_result(args[0] if len(args) == 1 else (args or None))
                else:
                    remaining.append((future, check))
            self._waiters[event] = remaining

        handler = self._handlers.get(event)
        if handler is not None:
            self._schedule(handler, event, args)
        for listener in self._listeners.get(event, ()):
            self._schedule(listener, event, args)

    async def wait_for(
        self, name: str, *, check: Callable[..., bool] | None = None, timeout: float | None = None
    ) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(_event_name(name), []).append((future, check))
        return await asyncio.wait_for(future, timeout)

    def _schedule(self, coro: Coro, event: str, args: tuple[Any, ...]) -> None:
        task = asyncio.get_running_loop().create_task(self._run(coro, event, args), name=f"spectrum:{event}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, coro: Coro, event: str, args: tuple[Any, ...]) -> None:
        try:
            await coro(*args)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if event == "error":
                log.exception("unhandled exception inside on_error")
                return
            await self._handle_error(event, exc, *args)

    async def _handle_error(self, event: str, exc: Exception, *args: Any) -> None:
        handler = self._handlers.get("error")
        listeners = self._listeners.get("error", [])
        if handler is None and not listeners:
            log.error("ignoring exception in on_%s", event, exc_info=exc)
            return
        for func in ([handler] if handler else []) + listeners:
            try:
                await func(event, exc, *args)
            except Exception:
                log.exception("unhandled exception inside on_error")

    async def drain(self, timeout: float | None = 5.0) -> None:
        """Wait for in-flight handler tasks (used during graceful shutdown)."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    def cancel_waiters(self) -> None:
        for waiters in self._waiters.values():
            for future, _ in waiters:
                if not future.done():
                    future.cancel()
        self._waiters.clear()

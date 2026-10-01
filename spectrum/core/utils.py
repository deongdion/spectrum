"""Small shared helpers."""

from __future__ import annotations

import asyncio
import random
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")


class LRUCache(Generic[K, V]):
    """Bounded insertion/access-ordered mapping."""

    def __init__(self, maxsize: int = 1000) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._data: OrderedDict[K, V] = OrderedDict()

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def get(self, key: K) -> V | None:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def add(self, key: K) -> bool:
        """Set-like insert for dedupe caches. Returns ``False`` if the key was already present."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self.set(key, True)  # type: ignore[arg-type]
        return True

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def jittered_backoff(attempt: int, *, initial: float = 1.0, maximum: float = 30.0) -> float:
    """Full-jitter exponential backoff in seconds."""
    return random.uniform(0, min(maximum, initial * (2**attempt)))


async def merge_async_iterators(iterators: list[AsyncIterator[T]]) -> AsyncIterator[T]:
    """Interleave several async iterators in arrival order."""
    queue: asyncio.Queue[tuple[int, T | BaseException | None]] = asyncio.Queue()

    async def pump(index: int, it: AsyncIterator[T]) -> None:
        try:
            async for item in it:
                await queue.put((index, item))
        except BaseException as exc:  # forwarded to the consumer
            await queue.put((index, exc))
            return
        await queue.put((index, None))

    tasks = [asyncio.create_task(pump(i, it)) for i, it in enumerate(iterators)]
    remaining = len(tasks)
    try:
        while remaining:
            _, item = await queue.get()
            if item is None:
                remaining -= 1
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item
    finally:
        for task in tasks:
            task.cancel()


async def maybe_await(value: T | Awaitable[T]) -> T:
    if isinstance(value, Awaitable):
        return await value  # type: ignore[no-any-return]
    return value


def ensure_callable_async(func: Callable[..., T | Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    async def wrapper(*args: object, **kwargs: object) -> T:
        return await maybe_await(func(*args, **kwargs))

    return wrapper

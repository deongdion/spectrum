"""Test-only provider: scripted inbound texts, records outbound content."""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator

from spectrum.core.utils import utcnow
from spectrum.models import Content, Direction, Message, Space, SpaceType, Text
from spectrum.providers import Provider


class ScriptedProvider(Provider):
    platform = "test"

    def __init__(self, inbound: list[str] | None = None) -> None:
        super().__init__()
        self.inbound = list(inbound or [])
        self.sent: list[Content] = []
        self._ids = itertools.count(1)

    async def stream(self) -> AsyncIterator[Message]:
        space = self.make_space("chat-1", type=SpaceType.DM)
        for text in self.inbound:
            yield self.make_message(
                id=f"in-{next(self._ids)}",
                content=Text(text),
                space=space,
                timestamp=utcnow(),
                sender=self.make_user("user"),
            )

    async def send(self, space: Space, content: Content) -> Message | None:
        self.sent.append(content)
        return self.make_message(
            id=f"out-{next(self._ids)}",
            content=content,
            space=space,
            timestamp=utcnow(),
            direction=Direction.OUTBOUND,
        )

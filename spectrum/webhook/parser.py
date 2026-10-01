"""Deserialize a native Spectrum webhook body into ``(Space, Message)``.

The wire format is a JSON projection of the SDK objects (function fields removed,
binary content metadata-only, message targets slimmed to refs). When the client
has a provider for the delivery's platform, the returned objects are bound to
it, so ``space.send()`` works and ``attachment.read()`` fetches the bytes lazily.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..core.errors import WebhookError
from ..core.utils import parse_iso8601, utcnow
from ..models.content import (
    AddMember,
    Attachment,
    Avatar,
    Contact,
    ContactField,
    ContactName,
    Content,
    Custom,
    Group,
    LeaveSpace,
    Reaction,
    Read,
    RemoveMember,
    Rename,
    RichLink,
    Text,
    Voice,
)
from ..models.enums import AvatarActionKind, ContactFieldType, Direction, Platform, SpaceType
from ..models.message import Message
from ..models.space import Space
from ..models.user import User

if TYPE_CHECKING:
    from ..providers.base import Provider

ProviderLookup = Callable[[Platform | str], "Provider | None"]


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    event: str
    space: Space | None
    message: Message | None
    payload: dict[str, Any]


def _platform(value: Any) -> Platform | str:
    return Platform.parse(str(value or "unknown"))


class WebhookParser:
    def __init__(self, lookup: ProviderLookup | None = None) -> None:
        self._lookup = lookup or (lambda _platform: None)

    def parse(self, body: bytes | str | dict[str, Any]) -> WebhookEvent:
        if isinstance(body, dict):
            payload = body
        else:
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise WebhookError("body is not valid JSON") from exc
        if not isinstance(payload, dict) or "event" not in payload:
            raise WebhookError("unexpected payload shape")
        event = str(payload["event"])
        if event != "messages":
            return WebhookEvent(event, None, None, payload)
        space = self.space(payload.get("space") or (payload.get("message") or {}).get("space") or {})
        message = self.message(payload.get("message") or {}, space)
        return WebhookEvent(event, space, message, payload)

    # ------------------------------------------------------------------ objects

    def space(self, data: dict[str, Any]) -> Space:
        platform = _platform(data.get("platform"))
        extras = {k: v for k, v in data.items() if k not in ("id", "platform", "type", "phone")}
        kind = data.get("type")
        return Space(
            str(data.get("id", "")),
            platform,
            type=SpaceType(kind) if kind in ("dm", "group") else None,
            phone=data.get("phone"),
            extras=extras,
            provider=self._lookup(platform),
        )

    def user(self, data: dict[str, Any] | None) -> User | None:
        if not data or not data.get("id"):
            return None
        extras = {
            k: v for k, v in data.items() if k not in ("id", "platform", "address", "country", "service")
        }
        return User(
            str(data["id"]),
            _platform(data.get("platform")),
            address=data.get("address"),
            country=data.get("country"),
            service=data.get("service"),
            extras=extras,
        )

    def message(self, data: dict[str, Any], space: Space) -> Message:
        return Message(
            id=str(data.get("id", "")),
            content=self.content(data.get("content") or {}, space),
            space=space,
            timestamp=parse_iso8601(data.get("timestamp")) or utcnow(),
            direction=data.get("direction") or Direction.INBOUND,
            sender=self.user(data.get("sender")),
            platform=_platform(data.get("platform") or space.platform),
            raw=data,
        )

    def _ref(self, data: dict[str, Any], space: Space) -> Message:
        """Slim target ref -> stub ``Message`` (content = ``contentPreview`` as text when present)."""
        preview = data.get("contentPreview")
        return Message(
            id=str(data.get("id", "")),
            content=Text(preview) if preview else Custom({"ref": True}),
            space=space,
            timestamp=_ts(data.get("timestamp")),
            direction=Direction.OUTBOUND,
            sender=self.user(data.get("sender")),
            platform=_platform(data.get("platform") or space.platform),
            raw=data,
        )

    def _reader(self, attachment_id: str, space: Space) -> Callable[[], Any] | None:
        provider = space.provider if space.is_bound else None
        if provider is None:
            return None

        async def read() -> bytes:
            attachment = await provider.fetch_attachment(attachment_id, space=space)
            if attachment is None:
                raise WebhookError(f"attachment {attachment_id} is no longer available")
            return await attachment.read()

        return read

    def content(self, data: dict[str, Any], space: Space) -> Content:
        kind = data.get("type")
        match kind:
            case "text":
                return Text(str(data.get("text", "")))
            case "attachment":
                attachment_id = str(data.get("id", ""))
                mime = str(data.get("mimeType") or "application/octet-stream")
                if mime.startswith("audio/") and data.get("voice"):
                    return Voice(
                        id=attachment_id,
                        mime_type=mime,
                        name=data.get("name"),
                        size=data.get("size"),
                        reader=self._reader(attachment_id, space),
                    )
                return Attachment(
                    id=attachment_id or "unknown",
                    name=str(data.get("name") or "attachment"),
                    mime_type=mime,
                    size=data.get("size"),
                    reader=self._reader(attachment_id, space),
                )
            case "richlink":
                return RichLink(str(data["url"]))
            case "reaction":
                return Reaction(str(data.get("emoji", "")), self._ref(data.get("target") or {}, space))
            case "read":
                return Read(self._ref(data.get("target") or {}, space))
            case "group":
                return Group([self.message(item, space) for item in data.get("items") or []])
            case "contact":
                name = data.get("name") or {}
                return Contact(
                    name=ContactName(
                        **{
                            k: name.get(k)
                            for k in ("formatted", "first", "last", "middle", "prefix", "suffix")
                        }
                    ),
                    phones=[
                        ContactField(p["value"], _field_type(p.get("type"))) for p in data.get("phones") or []
                    ],
                    emails=[
                        ContactField(e["value"], _field_type(e.get("type"))) for e in data.get("emails") or []
                    ],
                    raw=data.get("raw"),
                )
            case "rename":
                return Rename(str(data.get("displayName", "")))
            case "avatar":
                action = data.get("action") or {}
                if action.get("kind") == "clear":
                    return Avatar(AvatarActionKind.CLEAR)
                return Avatar(AvatarActionKind.SET, mime_type=action.get("mimeType") or "image/jpeg")
            case "addMember":
                return AddMember(data.get("members") or [])
            case "removeMember":
                return RemoveMember(data.get("members") or [])
            case "leaveSpace":
                return LeaveSpace()
            case _:
                return Custom(data)


def _ts(value: Any) -> datetime:
    return (parse_iso8601(value) if isinstance(value, str) else None) or utcnow()


def _field_type(value: Any) -> ContactFieldType | None:
    try:
        return ContactFieldType(value) if value else None
    except ValueError:
        return None

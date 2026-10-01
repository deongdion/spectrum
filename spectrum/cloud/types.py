"""Response DTOs for the Spectrum Cloud HTTP API (frozen dataclasses)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..core.utils import parse_iso8601


class LineMode(StrEnum):
    SHARED = "shared"
    DEDICATED = "dedicated"


class WebhookSchema(StrEnum):
    NORMALIZED_V1 = "normalized-events.v1"
    RAW_INBOUND_V1 = "raw-inbound.v1"


class WebhookStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class UserKind(StrEnum):
    SHARED = "shared"
    DEDICATED = "dedicated"


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    first_name: str
    last_name: str
    avatar_url: str | None
    imessage_synced: bool


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    name: str
    slug: str | None
    profile: ProjectProfile | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ProjectInfo:
        profile = data.get("profile")
        return cls(
            name=data.get("name", ""),
            slug=data.get("slug"),
            profile=ProjectProfile(
                first_name=profile.get("firstName", ""),
                last_name=profile.get("lastName", ""),
                avatar_url=profile.get("avatarUrl"),
                imessage_synced=bool(profile.get("imessageSynced")),
            )
            if profile
            else None,
        )


@dataclass(frozen=True, slots=True)
class IMessageTokens:
    """``POST /projects/{id}/imessage/tokens``.

    Shared projects get a single ``token``; dedicated projects get one token per
    line instance in ``auth`` (instance id -> token) with ``numbers`` (instance id -> E.164).
    """

    mode: LineMode
    expires_in: int
    token: str | None = None
    auth: dict[str, str] = field(default_factory=dict)
    numbers: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"IMessageTokens(mode={self.mode.value!r}, expires_in={self.expires_in}, "
            f"lines={sorted(self.numbers.values()) if self.numbers else 'shared'})"
        )

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> IMessageTokens:
        mode = LineMode(data["type"])
        if mode is LineMode.SHARED:
            return cls(mode=mode, expires_in=int(data["expiresIn"]), token=data["token"])
        if "auth" not in data:
            raise ValueError("malformed dedicated token payload: missing 'auth'")
        return cls(
            mode=mode,
            expires_in=int(data["expiresIn"]),
            auth=dict(data["auth"]),
            numbers=dict(data.get("numbers") or {}),
        )


@dataclass(frozen=True, slots=True)
class ScopedToken:
    token: str
    expires_in: int

    def __repr__(self) -> str:
        return f"ScopedToken(expires_in={self.expires_in})"


@dataclass(frozen=True, slots=True)
class Webhook:
    id: str
    webhook_url: str
    schema_version: WebhookSchema | None
    event_types: tuple[str, ...]
    enabled: bool
    status: WebhookStatus | None
    created_at: datetime | None
    updated_at: datetime | None
    failure_notification_email: str | None = None
    disabled_reason: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Webhook:
        return cls(
            id=data["id"],
            webhook_url=data["webhookUrl"],
            schema_version=WebhookSchema(data["schemaVersion"]) if data.get("schemaVersion") else None,
            event_types=tuple(data.get("eventTypes") or ()),
            enabled=bool(data.get("enabled", True)),
            status=WebhookStatus(data["status"]) if data.get("status") else None,
            created_at=parse_iso8601(data.get("createdAt")),
            updated_at=parse_iso8601(data.get("updatedAt")),
            failure_notification_email=data.get("failureNotificationEmail"),
            disabled_reason=data.get("disabledReason"),
        )


@dataclass(frozen=True, slots=True)
class WebhookRegistration:
    """Registration result. Both secrets are returned exactly once — store them immediately."""

    webhook: Webhook
    signing_secret: str
    standard_signing_secret: str | None

    def __repr__(self) -> str:
        return f"WebhookRegistration(webhook={self.webhook!r}, signing_secret='***')"


@dataclass(frozen=True, slots=True)
class Line:
    id: str
    platform: str
    phone_number: str | None
    status: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Line:
        return cls(
            id=data.get("id") or data.get("phoneNumberId", ""),
            platform=data.get("platform", ""),
            phone_number=data.get("phoneNumber") or data.get("displayPhoneNumber"),
            status=data.get("status"),
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class ProjectUser:
    id: str
    kind: UserKind
    phone_number: str
    assigned_phone_number: str | None
    first_name: str | None
    last_name: str | None
    email: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ProjectUser:
        return cls(
            id=data["id"],
            kind=UserKind(data["type"]),
            phone_number=data["phoneNumber"],
            assigned_phone_number=data.get("assignedPhoneNumber"),
            first_name=data.get("firstName"),
            last_name=data.get("lastName"),
            email=data.get("email"),
        )

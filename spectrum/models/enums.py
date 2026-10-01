"""Enumerations used across the public API.

All enums are ``StrEnum`` so they compare equal to the raw wire strings
(``ContentType.TEXT == "text"``) and serialize without conversion.
"""

from __future__ import annotations

import re
from enum import StrEnum

_PLATFORM_SEPARATORS = re.compile(r"[\s-]+")


class Platform(StrEnum):
    IMESSAGE = "imessage"
    LOCAL_IMESSAGE = "local_imessage"
    WHATSAPP_BUSINESS = "whatsapp_business"
    TELEGRAM = "telegram"
    SLACK = "slack"
    TERMINAL = "terminal"

    @classmethod
    def parse(cls, value: str) -> Platform | str:
        """Normalize ``"iMessage"`` / ``"WhatsApp Business"`` style labels.

        Unknown (custom) platform ids are returned unchanged as ``str``.
        """
        key = _PLATFORM_SEPARATORS.sub("_", value.strip().lower())
        try:
            return cls(key)
        except ValueError:
            return value


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ContentType(StrEnum):
    TEXT = "text"
    MARKDOWN = "markdown"
    STREAM_TEXT = "streamText"
    ATTACHMENT = "attachment"
    VOICE = "voice"
    CONTACT = "contact"
    RICHLINK = "richlink"
    EFFECT = "effect"
    REACTION = "reaction"
    POLL = "poll"
    POLL_OPTION = "poll_option"
    GROUP = "group"
    REPLY = "reply"
    EDIT = "edit"
    UNSEND = "unsend"
    READ = "read"
    TYPING = "typing"
    RENAME = "rename"
    AVATAR = "avatar"
    ADD_MEMBER = "addMember"
    REMOVE_MEMBER = "removeMember"
    LEAVE_SPACE = "leaveSpace"
    APP = "app"
    CUSTOM = "custom"
    # iMessage-only outbound content
    BACKGROUND = "background"
    CONTACT_CARD = "contactCard"
    MINI_APP = "customizedMiniApp"

    @property
    def event_name(self) -> str | None:
        """Extra ``on_<event>`` dispatched alongside ``on_message`` for this content type."""
        return _CONTENT_EVENTS.get(self)

    @property
    def is_fire_and_forget(self) -> bool:
        """Sends of this type resolve to ``None`` instead of a ``Message``."""
        return self in _FIRE_AND_FORGET


_CONTENT_EVENTS: dict[ContentType, str] = {
    ContentType.REACTION: "reaction",
    ContentType.POLL: "poll",
    ContentType.READ: "read_receipt",
    ContentType.POLL_OPTION: "poll_vote",
    ContentType.ADD_MEMBER: "member_add",
    ContentType.REMOVE_MEMBER: "member_remove",
    ContentType.LEAVE_SPACE: "member_leave",
    ContentType.RENAME: "space_rename",
    ContentType.AVATAR: "space_avatar",
    ContentType.EDIT: "message_edit",
    ContentType.UNSEND: "message_unsend",
}

_FIRE_AND_FORGET = frozenset(
    {
        ContentType.EDIT,
        ContentType.UNSEND,
        ContentType.READ,
        ContentType.TYPING,
        ContentType.RENAME,
        ContentType.AVATAR,
        ContentType.ADD_MEMBER,
        ContentType.REMOVE_MEMBER,
        ContentType.LEAVE_SPACE,
        ContentType.BACKGROUND,
        ContentType.CONTACT_CARD,
    }
)

# Content types that ``reply()`` / ``edit()`` refuse to wrap.
UNWRAPPABLE_CONTENT = frozenset(
    {
        ContentType.REPLY,
        ContentType.EDIT,
        ContentType.REACTION,
        ContentType.GROUP,
        ContentType.TYPING,
        ContentType.RENAME,
        ContentType.AVATAR,
        ContentType.ADD_MEMBER,
        ContentType.REMOVE_MEMBER,
        ContentType.LEAVE_SPACE,
        ContentType.UNSEND,
        ContentType.READ,
    }
)


class SpaceType(StrEnum):
    DM = "dm"
    GROUP = "group"


class TypingState(StrEnum):
    START = "start"
    STOP = "stop"


class StreamFormat(StrEnum):
    PLAIN = "plain"
    MARKDOWN = "markdown"


class AddressService(StrEnum):
    IMESSAGE = "iMessage"
    SMS = "SMS"
    RCS = "RCS"
    UNKNOWN = "unknown"


class AvatarActionKind(StrEnum):
    SET = "set"
    CLEAR = "clear"


class Emoji(StrEnum):
    """Universal aliases that iMessage maps to native tapbacks."""

    LOVE = "❤️"
    LIKE = "👍"
    DISLIKE = "👎"
    LAUGH = "😂"
    EMPHASIZE = "‼️"
    QUESTION = "❓"


class Tapback(StrEnum):
    LOVE = "love"
    LIKE = "like"
    DISLIKE = "dislike"
    LAUGH = "laugh"
    EMPHASIZE = "emphasize"
    QUESTION = "question"

    @property
    def emoji(self) -> Emoji:
        return Emoji[self.name]

    @classmethod
    def from_emoji(cls, emoji: str) -> Tapback | None:
        for member in Emoji:
            if member.value == emoji:
                return cls[member.name]
        return None


class MessageEffect(StrEnum):
    """iMessage bubble / screen effect identifiers."""

    # bubble effects
    SLAM = "com.apple.MobileSMS.expressivesend.impact"
    LOUD = "com.apple.MobileSMS.expressivesend.loud"
    GENTLE = "com.apple.MobileSMS.expressivesend.gentle"
    INVISIBLE = "com.apple.MobileSMS.expressivesend.invisibleink"
    # screen effects
    CONFETTI = "com.apple.messages.effect.CKConfettiEffect"
    FIREWORKS = "com.apple.messages.effect.CKFireworksEffect"
    BALLOONS = "com.apple.messages.effect.CKBalloonEffect"
    HEART = "com.apple.messages.effect.CKHeartEffect"
    LASERS = "com.apple.messages.effect.CKLasersEffect"
    CELEBRATION = "com.apple.messages.effect.CKHappyBirthdayEffect"
    SPARKLES = "com.apple.messages.effect.CKSparklesEffect"
    SPOTLIGHT = "com.apple.messages.effect.CKSpotlightEffect"
    ECHO = "com.apple.messages.effect.CKEchoEffect"

    @property
    def is_screen_effect(self) -> bool:
        return self.value.startswith("com.apple.messages.effect.")


class ContactFieldType(StrEnum):
    MOBILE = "mobile"
    HOME = "home"
    WORK = "work"
    OTHER = "other"

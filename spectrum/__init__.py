"""spectrum — async Python SDK for Photon Spectrum.

Layout::

    spectrum/
      lib.py          Client (events, lifecycle, webhooks)
      core/           errors, configuration, event dispatcher, utils
      models/         Enums + dataclass models: Message, Space, User, Content variants
      content/        builders (text(), attachment(), reply() ...), markdown, vCard, MIME
      cloud/          Spectrum Cloud HTTP API client + token renewal
      providers/      Provider base, IMessage (gRPC)
      webhook/        signature verification, payload parser, aiohttp helper
"""

from . import content
from .content import (
    add_member,
    app,
    attachment,
    avatar,
    contact,
    custom,
    edit,
    effect,
    group,
    leave_space,
    markdown,
    option,
    poll,
    reaction,
    read,
    remove_member,
    rename,
    reply,
    richlink,
    text,
    typing,
    unsend,
    voice,
)
from .core import (
    ClientClosedError,
    CloudError,
    ConfigurationError,
    ContentError,
    SpectrumError,
    UnsupportedError,
    WebhookError,
)
from .lib import Client, Spectrum
from .models import (
    AddressService,
    Attachment,
    Content,
    ContentType,
    Direction,
    Emoji,
    Message,
    MessageEffect,
    Platform,
    Space,
    SpaceType,
    Tapback,
    TypingState,
    User,
)

__version__ = "0.1.0"

__all__ = [
    "AddressService",
    "Attachment",
    "Client",
    "ClientClosedError",
    "CloudError",
    "ConfigurationError",
    "Content",
    "ContentError",
    "ContentType",
    "Direction",
    "Emoji",
    "Message",
    "MessageEffect",
    "Platform",
    "Space",
    "SpaceType",
    "Spectrum",
    "SpectrumError",
    "Tapback",
    "TypingState",
    "UnsupportedError",
    "User",
    "WebhookError",
    "__version__",
    "add_member",
    "app",
    "attachment",
    "avatar",
    "contact",
    "content",
    "custom",
    "edit",
    "effect",
    "group",
    "leave_space",
    "markdown",
    "option",
    "poll",
    "reaction",
    "read",
    "remove_member",
    "rename",
    "reply",
    "richlink",
    "text",
    "typing",
    "unsend",
    "voice",
]

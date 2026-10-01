"""Exception hierarchy shared by every layer of the SDK."""

from __future__ import annotations

from typing import Any


class SpectrumError(Exception):
    """Base class for every error raised by this SDK."""


class ConfigurationError(SpectrumError):
    """Missing or invalid client / provider configuration."""


class ContentError(SpectrumError, ValueError):
    """A content builder rejected its input (raised at build time)."""


class UnsupportedError(SpectrumError):
    """The platform cannot perform the requested content type or action."""

    def __init__(self, message: str, *, platform: str | None = None, content_type: str | None = None) -> None:
        super().__init__(message)
        self.platform = platform
        self.content_type = content_type

    @classmethod
    def content(cls, content_type: str, platform: str, detail: str | None = None) -> UnsupportedError:
        suffix = f": {detail}" if detail else ""
        return cls(
            f'{platform} does not support "{content_type}" content{suffix}',
            platform=platform,
            content_type=content_type,
        )

    @classmethod
    def action(cls, action: str, platform: str, detail: str | None = None) -> UnsupportedError:
        suffix = f": {detail}" if detail else ""
        return cls(f"{platform} does not support {action}{suffix}", platform=platform)


class CloudError(SpectrumError):
    """Non-2xx (or ``succeed: false``) response from the Spectrum Cloud HTTP API."""

    def __init__(self, status: int, message: str, code: str | None = None, body: Any = None) -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.code = code
        self.body = body


class WebhookError(SpectrumError):
    """Base class for webhook delivery problems. ``status`` is the HTTP code to answer with."""

    status: int = 400


class WebhookSignatureError(WebhookError):
    status = 401


class WebhookHeaderError(WebhookError):
    status = 400


class ClientClosedError(SpectrumError):
    """Operation attempted on a client that has been closed."""

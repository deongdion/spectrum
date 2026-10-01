"""iMessage gRPC error model (port of ``@photon-ai/advanced-imessage`` errors).

The server reports the stable error code in the ``error-code`` trailer,
retryability in ``x-retryable`` and structured context in ``error-context-*``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import grpc

from ...core.errors import SpectrumError


class ErrorCode(StrEnum):
    # authentication
    UNAUTHENTICATED = "unauthenticated"
    TOKEN_EXPIRED = "tokenExpired"
    TOKEN_BLOCKED = "tokenBlocked"
    UNAUTHORIZED = "unauthorized"
    # rate limits
    DAILY_LIMIT_EXCEEDED = "dailyLimitExceeded"
    RECIPIENT_LIMIT_EXCEEDED = "recipientLimitExceeded"
    UPLOAD_RATE_EXCEEDED = "uploadRateExceeded"
    CONTENT_DUPLICATE_EXCEEDED = "contentDuplicateExceeded"
    RECIPIENT_COOLING_DOWN = "recipientCoolingDown"
    RECIPIENT_LOCKED = "recipientLocked"
    SEND_RECEIVE_RATIO_EXCEEDED = "sendReceiveRatioExceeded"
    # duplicates
    DUPLICATE_MESSAGE = "duplicateMessage"
    # not found
    CHAT_NOT_FOUND = "chatNotFound"
    MESSAGE_NOT_FOUND = "messageNotFound"
    ATTACHMENT_NOT_FOUND = "attachmentNotFound"
    ADDRESS_NOT_FOUND = "addressNotFound"
    SHARED_FRIEND_LOCATION_NOT_FOUND = "sharedFriendLocationNotFound"
    GROUP_ICON_NOT_FOUND = "groupIconNotFound"
    POLL_NOT_FOUND = "pollNotFound"
    # validation
    INVALID_ARGUMENT = "invalidArgument"
    PRECONDITION_FAILED = "preconditionFailed"
    OPERATION_NOT_SUPPORTED = "operationNotSupported"
    ATTACHMENT_NOT_READY = "attachmentNotReady"
    PRIVATE_API_UNAVAILABLE = "privateApiUnavailable"
    # infrastructure
    SERVICE_UNAVAILABLE = "serviceUnavailable"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internalError"
    DATABASE_ERROR = "databaseError"
    NETWORK_ERROR = "networkError"


class IMessageError(SpectrumError):
    """Base iMessage RPC error. Branch on the subclass, then on ``code``; never parse ``message``."""

    def __init__(
        self,
        message: str,
        *,
        code: str = ErrorCode.INTERNAL_ERROR,
        retryable: bool = False,
        grpc_code: grpc.StatusCode | None = None,
        context: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self._code = code
        self._retryable = retryable
        self._grpc_code = grpc_code
        self._context = dict(context or {})

    @property
    def code(self) -> ErrorCode | str:
        """Stable error code. Unknown server codes are returned as raw strings."""
        try:
            return ErrorCode(self._code)
        except ValueError:
            return self._code

    @property
    def retryable(self) -> bool:
        return self._retryable

    @property
    def grpc_code(self) -> grpc.StatusCode | None:
        return self._grpc_code

    @property
    def context(self) -> dict[str, str]:
        return self._context

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self._code!r}, retryable={self._retryable}, message={str(self)!r})"
        )


class AuthenticationError(IMessageError):
    pass


class NotFoundError(IMessageError):
    pass


class RateLimitError(IMessageError):
    pass


class ValidationError(IMessageError):
    pass


class ConnectionError(IMessageError):  # noqa: A001 - mirrors the upstream SDK name
    pass


_BY_STATUS: dict[grpc.StatusCode, type[IMessageError]] = {
    grpc.StatusCode.UNAUTHENTICATED: AuthenticationError,
    grpc.StatusCode.PERMISSION_DENIED: AuthenticationError,
    grpc.StatusCode.NOT_FOUND: NotFoundError,
    grpc.StatusCode.RESOURCE_EXHAUSTED: RateLimitError,
    grpc.StatusCode.INVALID_ARGUMENT: ValidationError,
    grpc.StatusCode.FAILED_PRECONDITION: ValidationError,
    grpc.StatusCode.UNAVAILABLE: ConnectionError,
    grpc.StatusCode.DEADLINE_EXCEEDED: ConnectionError,
}


def _trailers(error: grpc.aio.AioRpcError) -> list[tuple[str, Any]]:
    try:
        return list(error.trailing_metadata() or ())
    except Exception:
        return []


def trailer_value(error: grpc.aio.AioRpcError, key: str) -> str | None:
    for k, v in _trailers(error):
        if k == key:
            return v.decode() if isinstance(v, bytes) else str(v)
    return None


def is_retryable(error: grpc.aio.AioRpcError) -> bool:
    return trailer_value(error, "x-retryable") == "true"


def from_rpc_error(error: BaseException) -> IMessageError:
    if isinstance(error, IMessageError):
        return error
    if not isinstance(error, grpc.aio.AioRpcError):
        return IMessageError(str(error))
    context = {
        k.removeprefix("error-context-"): (v.decode() if isinstance(v, bytes) else str(v))
        for k, v in _trailers(error)
        if k.startswith("error-context-")
    }
    status = error.code()
    if status in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
        default_code = ErrorCode.SERVICE_UNAVAILABLE
    else:
        default_code = ErrorCode.INTERNAL_ERROR
    cls = _BY_STATUS.get(status, IMessageError)
    return cls(
        error.details() or status.name,
        code=trailer_value(error, "error-code") or default_code,
        retryable=is_retryable(error),
        grpc_code=status,
        context=context,
    )

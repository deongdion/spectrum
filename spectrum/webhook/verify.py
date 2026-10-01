"""Webhook signature verification.

Two schemes are delivered side by side:

* **Spectrum (legacy)** — ``X-Spectrum-Signature: v0=<hex>`` where
  ``hex = HMAC-SHA256(signingSecret, "v0:" + X-Spectrum-Timestamp + ":" + rawBody)``
* **Standard Webhooks** — ``webhook-signature: v1,<base64> [v1,<base64> ...]`` over
  ``webhook-id + "." + webhook-timestamp + "." + rawBody`` keyed by the base64 part of
  the ``whsec_...`` secret.

Both reject timestamps more than ``tolerance`` seconds from now (replay protection)
and compare in constant time. Always verify the *raw* body bytes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..core.errors import WebhookHeaderError, WebhookSignatureError

DEFAULT_TOLERANCE = 300


class SignatureScheme(StrEnum):
    SPECTRUM = "spectrum"
    STANDARD = "standard"


@dataclass(frozen=True, slots=True)
class VerifiedDelivery:
    scheme: SignatureScheme
    timestamp: int
    event: str | None
    webhook_id: str | None
    """``X-Spectrum-Webhook-Id`` (registered webhook) or ``webhook-id`` (event id, Standard scheme)."""


class _Headers:
    """Case-insensitive view over any header mapping (dict, multidict, list of pairs)."""

    def __init__(self, headers: Mapping[str, str] | list[tuple[str, str]]) -> None:
        items = headers.items() if isinstance(headers, Mapping) else headers
        self._data = {
            (k.decode() if isinstance(k, bytes) else k).lower(): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in items
        }

    def get(self, name: str) -> str | None:
        return self._data.get(name.lower())


def _check_timestamp(raw: str, tolerance: int, now: float | None) -> int:
    try:
        timestamp = int(raw)
    except ValueError as exc:
        raise WebhookHeaderError("invalid timestamp header") from exc
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        raise WebhookSignatureError("stale timestamp")
    return timestamp


def sign_spectrum(secret: str, timestamp: int | str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(), b"v0:" + str(timestamp).encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def _standard_key(secret: str) -> bytes:
    raw = secret.removeprefix("whsec_")
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        return raw.encode()


def sign_standard(secret: str, message_id: str, timestamp: int | str, body: bytes) -> str:
    payload = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(_standard_key(secret), payload, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def verify_spectrum(
    secret: str,
    body: bytes,
    headers: Mapping[str, str] | list[tuple[str, str]],
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> VerifiedDelivery:
    h = _Headers(headers)
    timestamp = h.get("x-spectrum-timestamp")
    signature = h.get("x-spectrum-signature")
    if not timestamp or not signature:
        raise WebhookHeaderError("missing X-Spectrum-Timestamp / X-Spectrum-Signature")
    ts = _check_timestamp(timestamp, tolerance, now)
    expected = sign_spectrum(secret, timestamp, body)
    if not hmac.compare_digest(expected.encode(), signature.strip().encode()):
        raise WebhookSignatureError("bad signature")
    return VerifiedDelivery(
        SignatureScheme.SPECTRUM, ts, h.get("x-spectrum-event"), h.get("x-spectrum-webhook-id")
    )


def verify_standard(
    secret: str,
    body: bytes,
    headers: Mapping[str, str] | list[tuple[str, str]],
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> VerifiedDelivery:
    h = _Headers(headers)
    message_id = h.get("webhook-id")
    timestamp = h.get("webhook-timestamp")
    signatures = h.get("webhook-signature")
    if not message_id or not timestamp or not signatures:
        raise WebhookHeaderError("missing webhook-id / webhook-timestamp / webhook-signature")
    ts = _check_timestamp(timestamp, tolerance, now)
    expected = sign_standard(secret, message_id, timestamp, body).encode()
    for candidate in signatures.split():
        if candidate.startswith("v1,") and hmac.compare_digest(expected, candidate.encode()):
            return VerifiedDelivery(SignatureScheme.STANDARD, ts, h.get("x-spectrum-event"), message_id)
    raise WebhookSignatureError("bad signature")


def verify(
    secret: str,
    body: bytes,
    headers: Mapping[str, str] | list[tuple[str, str]],
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    now: float | None = None,
) -> VerifiedDelivery:
    """Pick the scheme from the secret: ``whsec_...`` -> Standard Webhooks, otherwise Spectrum."""
    if secret.startswith("whsec_"):
        return verify_standard(secret, body, headers, tolerance=tolerance, now=now)
    return verify_spectrum(secret, body, headers, tolerance=tolerance, now=now)

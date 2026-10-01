import asyncio
import base64
import json
import time

import pytest

import spectrum
from spectrum.core.errors import WebhookHeaderError, WebhookSignatureError
from spectrum.webhook import sign_spectrum, sign_standard, verify

from .fakes import ScriptedProvider

BODY = json.dumps(
    {
        "event": "messages",
        "space": {"id": "any;-;+15550100", "platform": "iMessage", "type": "dm", "phone": "+15551234567"},
        "message": {
            "id": "spc-msg-1",
            "platform": "iMessage",
            "direction": "inbound",
            "timestamp": "2026-05-14T19:06:32.000Z",
            "sender": {"id": "+15550100", "platform": "iMessage"},
            "content": {
                "type": "reaction",
                "emoji": "❤️",
                "target": {
                    "id": "spc-msg-0",
                    "platform": "iMessage",
                    "timestamp": "2026-05-14T19:00:00Z",
                    "contentPreview": "hey",
                },
            },
        },
    }
).encode()


def _spectrum_headers(secret: str, body: bytes = BODY, ts: int | None = None) -> dict[str, str]:
    ts = ts or int(time.time())
    return {
        "X-Spectrum-Event": "messages",
        "X-Spectrum-Webhook-Id": "wh-1",
        "X-Spectrum-Timestamp": str(ts),
        "X-Spectrum-Signature": sign_spectrum(secret, ts, body),
    }


def test_spectrum_signature_roundtrip_and_failures():
    secret = "a" * 64
    delivery = verify(secret, BODY, _spectrum_headers(secret))
    assert delivery.webhook_id == "wh-1" and delivery.event == "messages"
    with pytest.raises(WebhookSignatureError):
        verify(secret, BODY + b" ", _spectrum_headers(secret))
    with pytest.raises(WebhookSignatureError):
        verify(secret, BODY, _spectrum_headers(secret, ts=int(time.time()) - 600))
    with pytest.raises(WebhookHeaderError):
        verify(secret, BODY, {})


def test_standard_webhooks_signature():
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    ts = str(int(time.time()))
    headers = {
        "webhook-id": "evt_1",
        "webhook-timestamp": ts,
        "webhook-signature": "v1,bogus " + sign_standard(secret, "evt_1", ts, BODY),
    }
    assert verify(secret, BODY, headers).webhook_id == "evt_1"


async def test_client_handle_webhook_dispatches_and_dedupes():
    secret = "b" * 64
    client = spectrum.Client(providers=[ScriptedProvider()], webhook_secret=secret)
    reactions: list[spectrum.Message] = []
    got = asyncio.Event()

    @client.event
    async def on_reaction(message):
        reactions.append(message)
        got.set()

    response = await client.handle_webhook(BODY, _spectrum_headers(secret))
    assert response.status == 200
    await asyncio.wait_for(got.wait(), 2)
    again = await client.handle_webhook(BODY, _spectrum_headers(secret))
    await asyncio.sleep(0.05)
    assert again.status == 200 and len(reactions) == 1

    message = reactions[0]
    assert message.platform is spectrum.Platform.IMESSAGE
    assert message.space.phone == "+15551234567" and message.space.is_dm
    content = message.content
    assert isinstance(content, spectrum.models.Reaction)
    assert content.emoji == "❤️"
    assert content.target.text == "hey"

    bad = await client.handle_webhook(BODY, {**_spectrum_headers(secret), "X-Spectrum-Signature": "v0=00"})
    assert bad.status == 401


async def test_missing_secret_answers_500():
    client = spectrum.Client(providers=[ScriptedProvider()])
    client.webhook_secret = None
    assert (await client.handle_webhook(BODY, {})).status == 500

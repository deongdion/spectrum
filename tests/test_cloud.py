import json

import httpx
import pytest

from spectrum.cloud import CloudClient, LineMode
from spectrum.core.config import Credentials
from spectrum.core.errors import CloudError

CREDS = Credentials("11111111-1111-4111-8111-111111111111", "secret")


def _client(handler) -> CloudClient:
    http = httpx.AsyncClient(
        base_url="https://spectrum.test",
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth(CREDS.project_id, CREDS.project_secret),
    )
    return CloudClient(CREDS, base_url="https://spectrum.test", http=http)


async def test_issue_dedicated_tokens_and_basic_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/projects/{CREDS.project_id}/imessage/tokens"
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(
            200,
            json={
                "succeed": True,
                "data": {
                    "type": "dedicated",
                    "auth": {"inst1": "tok"},
                    "numbers": {"inst1": "+15550001"},
                    "expiresIn": 3600,
                },
            },
        )

    tokens = await _client(handler).issue_imessage_tokens()
    assert tokens.mode is LineMode.DEDICATED and tokens.numbers == {"inst1": "+15550001"}
    assert "tok" not in repr(tokens)


async def test_error_envelope_maps_to_cloud_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, content=json.dumps({"succeed": False, "message": "already registered"}))

    with pytest.raises(CloudError) as info:
        await _client(handler).register_webhook("https://example.com/hook")
    assert info.value.status == 409 and "already registered" in str(info.value)

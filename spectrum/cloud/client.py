"""Async client for the Spectrum Cloud management API (``https://spectrum.photon.codes``).

Authentication is HTTP Basic ``projectId:projectSecret``. Every response is a
``{"succeed": bool, "data" | "message": ...}`` envelope; the HTTP status is the
source of truth.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..core.config import DEFAULT_CLOUD_URL, Credentials
from ..core.errors import CloudError
from .types import (
    IMessageTokens,
    Line,
    LineMode,
    ProjectInfo,
    ProjectUser,
    ScopedToken,
    Webhook,
    WebhookRegistration,
    WebhookSchema,
)

log = logging.getLogger("spectrum.cloud")

USER_AGENT = "spectrum-py/0.1.0"


class CloudClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._base_url = (base_url or os.environ.get("SPECTRUM_CLOUD_URL") or DEFAULT_CLOUD_URL).rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            auth=httpx.BasicAuth(credentials.project_id, credentials.project_secret),
            headers={"User-Agent": USER_AGENT},
        )

    @property
    def project_id(self) -> str:
        return self._credentials.project_id

    @property
    def base_url(self) -> str:
        return self._base_url

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> CloudClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ transport

    async def request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        response = await self._http.request(method, path, json=json, params=params)
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            message = (body or {}).get("message") if isinstance(body, dict) else None
            code = (body or {}).get("code") if isinstance(body, dict) else None
            raise CloudError(
                response.status_code, message or response.reason_phrase or "request failed", code, body
            )
        if not isinstance(body, dict) or not body.get("succeed"):
            message = body.get("message") if isinstance(body, dict) else "malformed response"
            raise CloudError(response.status_code, message or "succeed=false", None, body)
        return body.get("data")

    def _p(self, suffix: str = "") -> str:
        return f"/projects/{self.project_id}/{suffix}"

    # ------------------------------------------------------------------ project

    async def get_project(self) -> ProjectInfo:
        return ProjectInfo.from_api(await self.request("GET", self._p()))

    async def get_imessage_mode(self) -> LineMode:
        data = await self.request("GET", self._p("imessage/"))
        return LineMode(data["type"])

    async def get_platforms(self) -> Any:
        return await self.request("GET", self._p("platforms/"))

    async def toggle_platform(self, platform: str, enabled: bool) -> Any:
        return await self.request(
            "PATCH", self._p("platforms/"), json={"platform": platform, "enabled": enabled}
        )

    # ------------------------------------------------------------------ runtime tokens

    async def issue_imessage_tokens(self) -> IMessageTokens:
        return IMessageTokens.from_api(await self.request("POST", self._p("imessage/tokens")))

    async def issue_fusor_token(self) -> ScopedToken:
        data = await self.request("POST", self._p("fusor/token"))
        return ScopedToken(data["token"], int(data["expiresIn"]))

    async def issue_voice_token(self) -> ScopedToken:
        data = await self.request("POST", self._p("voice/tokens"))
        return ScopedToken(data["token"], int(data["expiresIn"]))

    # ------------------------------------------------------------------ webhooks

    async def list_webhooks(self) -> list[Webhook]:
        data = await self.request("GET", self._p("webhooks/"))
        items = data if isinstance(data, list) else (data or {}).get("webhooks", [])
        return [Webhook.from_api(w) for w in items]

    async def register_webhook(
        self,
        url: str,
        *,
        schema_version: WebhookSchema = WebhookSchema.NORMALIZED_V1,
        failure_notification_email: str | None = None,
    ) -> WebhookRegistration:
        """Register a public HTTPS endpoint. The returned secrets are shown only once."""
        body: dict[str, Any] = {
            "webhookUrl": url,
            "schemaVersion": schema_version.value,
            "eventTypes": ["message.received"],
        }
        if failure_notification_email:
            body["failureNotificationEmail"] = failure_notification_email
        data = await self.request("POST", self._p("webhooks/"), json=body)
        return WebhookRegistration(
            webhook=Webhook.from_api(data),
            signing_secret=data["signingSecret"],
            standard_signing_secret=data.get("standardSigningSecret"),
        )

    async def delete_webhook(self, webhook_id: str) -> None:
        await self.request("DELETE", self._p(f"webhooks/{webhook_id}"))

    async def rotate_webhook_secret(self, webhook_id: str, *, overlap_seconds: int = 86_400) -> str:
        """Rotate the Standard Webhooks (``whsec_``) secret; returns the new secret."""
        data = await self.request(
            "POST", self._p(f"webhooks/{webhook_id}/secret/rotate"), json={"overlapSeconds": overlap_seconds}
        )
        return str(data["standardSigningSecret"])

    async def list_webhook_egress_ips(self) -> Any:
        return await self.request("GET", self._p("webhooks/egress-ips"))

    # ------------------------------------------------------------------ lines / users

    async def list_lines(self, platform: str | None = None) -> list[Line]:
        data = await self.request(
            "GET", self._p("lines/"), params={"platform": platform} if platform else None
        )
        return [Line.from_api(item) for item in (data or {}).get("lines", [])]

    async def list_users(self) -> list[ProjectUser]:
        data = await self.request("GET", self._p("users/"))
        items = data if isinstance(data, list) else (data or {}).get("users", [])
        return [ProjectUser.from_api(u) for u in items]

    async def create_user(
        self,
        phone_number: str,
        *,
        assigned_phone_number: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
    ) -> ProjectUser:
        """Register a user. On Free/Pro (shared pool) only registered users can be messaged."""
        body: dict[str, Any] = {
            "type": "dedicated" if assigned_phone_number else "shared",
            "phoneNumber": phone_number,
        }
        if assigned_phone_number:
            body["assignedPhoneNumber"] = assigned_phone_number
        for key, value in (("firstName", first_name), ("lastName", last_name), ("email", email)):
            if value is not None:
                body[key] = value
        return ProjectUser.from_api(await self.request("POST", self._p("users/"), json=body))

    async def delete_user(self, user_id: str) -> None:
        await self.request("DELETE", self._p(f"users/{user_id}/"))

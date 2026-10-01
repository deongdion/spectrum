"""Optional aiohttp integration (``pip install aiohttp``).

``create_app(client)`` mounts ``POST <path>`` and feeds deliveries through
``client.handle_webhook``; handlers then fire as normal ``on_message`` events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

    from ..lib import Client


def create_app(client: Client, *, path: str = "/spectrum/webhook") -> web.Application:
    from aiohttp import web

    async def handle(request: web.Request) -> web.Response:
        body = await request.read()  # raw bytes: re-serialized JSON would break the HMAC
        result = await client.handle_webhook(body, list(request.headers.items()))
        return web.Response(status=result.status, body=result.body, headers=result.headers)

    async def on_startup(_: web.Application) -> None:
        if not client.is_logged_in:
            await client.login()

    async def on_cleanup(_: web.Application) -> None:
        await client.close()

    app = web.Application()
    app.router.add_post(path, handle)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def run(client: Client, *, host: str = "0.0.0.0", port: int = 8080, path: str = "/spectrum/webhook") -> None:
    """Blocking helper: serve the webhook endpoint until interrupted."""
    from aiohttp import web

    web.run_app(create_app(client, path=path), host=host, port=port)

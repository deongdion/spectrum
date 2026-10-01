import asyncio

import pytest

import spectrum
from spectrum.models import Reply, Text, Typing

from .fakes import ScriptedProvider


async def test_on_message_and_reply():
    provider = ScriptedProvider(["ping", "hello"])
    client = spectrum.Client(providers=[provider])
    seen: list[str] = []
    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    @client.event
    async def on_message(message: spectrum.Message):
        seen.append(message.text or "")
        if message.text == "ping":
            async with message.space.typing():
                await message.reply("pong")

    await asyncio.wait_for(client.start(), timeout=5)  # returns once the scripted stream ends
    assert ready.is_set()
    assert seen == ["ping", "hello"]
    kinds = [type(c) for c in provider.sent]
    assert kinds == [Typing, Reply, Typing]
    reply = provider.sent[1]
    assert isinstance(reply, Reply) and reply.content == Text("pong") and reply.target.text == "ping"
    assert client.is_closed


async def test_listen_wait_for_and_error_routing():
    client = spectrum.Client(providers=[ScriptedProvider(["a", "b"])])
    extra: list[str] = []
    errors: list[str] = []

    @client.listen("on_message")
    async def also(message):
        extra.append(message.text)

    @client.event
    async def on_message(message):
        if message.text == "a":
            raise RuntimeError("boom")

    @client.event
    async def on_error(event, exc, *args):
        errors.append(f"{event}:{exc}")

    await client.login()
    waiter = asyncio.ensure_future(client.wait_for("message", check=lambda m: m.text == "b", timeout=5))
    runner = asyncio.ensure_future(client.connect())
    got = await waiter
    await asyncio.wait_for(runner, timeout=5)
    assert got.text == "b"
    assert extra == ["a", "b"]
    assert errors == ["message:boom"]


def test_event_decorator_validation():
    client = spectrum.Client(providers=[ScriptedProvider()])
    with pytest.raises(TypeError):
        client.event(lambda m: None)  # type: ignore[arg-type]

    async def message(_):  # missing on_ prefix
        pass

    with pytest.raises(ValueError):
        client.event(message)


def test_credentials_are_masked():
    client = spectrum.Client(providers=[ScriptedProvider()], project_secret="s3cret")
    assert client.project_secret == "***"
    client.project_id = "abc"
    assert client.project_id == "abc"


def test_duplicate_providers_rejected():
    with pytest.raises(spectrum.ConfigurationError):
        spectrum.Client(providers=[ScriptedProvider(), ScriptedProvider()])

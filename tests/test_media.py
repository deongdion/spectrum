"""Voice m4a normalization and app-card link metadata."""

import io
import math
import struct
import wave

import httpx
import pytest

from spectrum.content.audio import ensure_m4a, ffmpeg_path, is_m4a, m4a_name
from spectrum.content.linkmeta import build_layout, layout_for_url, parse_metadata
from spectrum.models import App, MiniAppLayout

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def _wav(seconds: float = 0.5, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return buf.getvalue()


async def test_m4a_passthrough_by_mime_and_brand():
    fake_m4a = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20
    assert is_m4a(fake_m4a)
    assert (await ensure_m4a(fake_m4a, "application/octet-stream")).converted is False
    assert (await ensure_m4a(b"whatever", "audio/x-m4a")).converted is False
    assert m4a_name("note.wav") == "note.m4a" and m4a_name(None) == "voice.m4a"


@pytest.mark.skipif(ffmpeg_path() is None, reason="ffmpeg not installed")
async def test_wav_is_transcoded_with_ffmpeg():
    result = await ensure_m4a(_wav(), "audio/wav")
    assert result.converted and is_m4a(result.data)
    assert result.duration is not None and 0.3 < result.duration < 0.8


def test_parse_metadata_prefers_open_graph():
    html = """<html><head><title>Fallback</title>
      <meta property="og:title" content=" Spectrum  Docs ">
      <meta name="twitter:description" content="Agents everywhere">
      <meta property="og:site_name" content="Photon">
      <meta property="og:image" content="/img/card.png"></head></html>"""
    meta = parse_metadata(html, "https://photon.codes/docs/intro")
    assert meta.title == "Spectrum Docs"
    assert meta.summary == "Agents everywhere"
    assert meta.site_name == "Photon"
    assert meta.image_url == "https://photon.codes/img/card.png"


def _transport(page_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "wsrv.nl":
            assert request.url.params["output"] == "jpg"
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        html = '<meta property="og:title" content="Order #123"><meta property="og:image" content="https://cdn.x/i.png">'
        return httpx.Response(page_status, text=html, headers={"content-type": "text/html; charset=utf-8"})

    return httpx.MockTransport(handler)


async def test_layout_for_url_with_image():
    async with httpx.AsyncClient(transport=_transport()) as http:
        layout = await layout_for_url("https://shop.example.com/o/123", http=http)
    assert layout.caption == "Order #123" and layout.image == JPEG
    assert layout.image_title == "Order #123" and layout.summary == "Order #123"


async def test_layout_falls_back_to_host():
    async with httpx.AsyncClient(transport=_transport(page_status=500)) as http:
        layout = await layout_for_url("https://www.example.com/x", http=http)
    assert layout == build_layout(type(parse_metadata("", ""))(), "https://www.example.com/x", None)
    assert layout.caption == "example.com" and layout.image is None


async def test_app_uses_supplied_layout_without_fetching():
    card = App("https://example.com", layout=MiniAppLayout(caption="Hi"))
    assert (await card.resolve_layout()).caption == "Hi"

"""Link metadata for ``app()`` cards (port of spectrum-ts ``fetchLinkMetadata`` + ``buildLayout``).

Open Graph / Twitter card tags supply the layout: title -> caption,
description -> subcaption, site name -> image overlay, og:image -> JPEG preview
(transcoded through the same wsrv.nl proxy spectrum-ts uses). Failures never
raise: they fall back to a host-only caption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

import httpx

from ..models.content import MiniAppLayout

log = logging.getLogger("spectrum.linkmeta")

TIMEOUT = 5.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15 spectrum-python/richlink"
)
JPEG_PROXY = "https://wsrv.nl/"
MAX_IMAGE_WIDTH = 1200
MAX_HTML_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class LinkMetadata:
    title: str | None = None
    summary: str | None = None
    site_name: str | None = None
    image_url: str | None = None


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None and data.strip():
            self.title = data


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def parse_metadata(html: str, base_url: str) -> LinkMetadata:
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML: use whatever was collected
        pass
    m = parser.meta
    image = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or m.get("twitter:image:src")
    return LinkMetadata(
        title=_clean(m.get("og:title")) or _clean(m.get("twitter:title")) or _clean(parser.title),
        summary=_clean(m.get("og:description")) or _clean(m.get("twitter:description")),
        site_name=_clean(m.get("og:site_name")),
        image_url=urljoin(base_url, image.strip()) if image and image.strip() else None,
    )


def site_host(url: str) -> str:
    host = urlparse(url).hostname
    return host.removeprefix("www.") if host else url


def jpeg_proxy_url(image_url: str) -> str:
    return f"{JPEG_PROXY}?url={quote(image_url, safe='')}&output=jpg&w={MAX_IMAGE_WIDTH}"


def is_jpeg(data: bytes) -> bool:
    return len(data) > 2 and data[0] == 0xFF and data[1] == 0xD8


async def fetch_metadata(url: str, *, http: httpx.AsyncClient | None = None) -> LinkMetadata:
    owns = http is None
    client = http or httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    try:
        response = await client.get(url)
        if response.status_code >= 400 or "html" not in response.headers.get("content-type", "html"):
            return LinkMetadata()
        return parse_metadata(
            response.content[:MAX_HTML_BYTES].decode(response.encoding or "utf-8", "replace"),
            str(response.url),
        )
    except Exception as exc:
        log.debug("link metadata fetch failed for %s: %s", url, exc)
        return LinkMetadata()
    finally:
        if owns:
            await client.aclose()


async def fetch_jpeg(image_url: str, *, http: httpx.AsyncClient | None = None) -> bytes | None:
    owns = http is None
    client = http or httpx.AsyncClient(
        follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}
    )
    try:
        response = await client.get(jpeg_proxy_url(image_url))
        return response.content if response.status_code < 400 and is_jpeg(response.content) else None
    except Exception as exc:
        log.debug("link preview image fetch failed for %s: %s", image_url, exc)
        return None
    finally:
        if owns:
            await client.aclose()


def build_layout(metadata: LinkMetadata, url: str, image: bytes | None) -> MiniAppLayout:
    title = metadata.title or metadata.site_name or site_host(url)
    if image:
        return MiniAppLayout(
            caption=title,
            subcaption=metadata.summary,
            image=image,
            image_title=metadata.site_name or title,
            summary=title,
        )
    return MiniAppLayout(caption=title, subcaption=metadata.summary, summary=title)


async def layout_for_url(url: str, *, http: httpx.AsyncClient | None = None) -> MiniAppLayout:
    metadata = await fetch_metadata(url, http=http)
    image = await fetch_jpeg(metadata.image_url, http=http) if metadata.image_url else None
    return build_layout(metadata, url, image)

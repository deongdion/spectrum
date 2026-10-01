"""Markdown -> iMessage styled text.

iMessage (remote mode) carries styling as ranges over the plain text, with
``start`` / ``length`` measured in **UTF-16 code units**. This renderer covers
the subset spectrum-ts supports: headings, emphasis, strong, strikethrough,
inline code, links, images, lists, block quotes, fenced code and GFM tables.
Unsupported syntax degrades to readable plain text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BOLD = "bold"
ITALIC = "italic"
STRIKETHROUGH = "strikethrough"
UNDERLINE = "underline"


@dataclass(slots=True)
class Span:
    text: str
    styles: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class TextFormat:
    type: str
    start: int
    length: int


@dataclass(frozen=True, slots=True)
class RenderedText:
    text: str
    formatting: tuple[TextFormat, ...]


# order matters: longest / most specific delimiters first
_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|!\[(?P<img_alt>[^\]]*)\]\((?P<img_url>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<link_url>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|(?P<strong_delim>\*\*|__)(?P<strong_body>.+?)(?P=strong_delim)"
    r"|~~(?P<strike_body>.+?)~~"
    r"|\*(?P<em_star>[^*\s](?:.*?[^*\s])?)\*"
    r"|(?<![A-Za-z0-9])_(?P<em_under>[^_\s](?:.*?[^_\s])?)_(?![A-Za-z0-9])"
    r"|<(?P<autolink>https?://[^>\s]+)>"
)


def _inline(text: str, styles: frozenset[str] = frozenset()) -> list[Span]:
    spans: list[Span] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            spans.append(Span(_unescape(text[pos : m.start()]), styles))
        if m.group("code") is not None:
            spans.append(Span(m.group("code_body"), styles))
        elif m.group("img_url") is not None:
            alt = m.group("img_alt") or "image"
            spans.append(Span(f"{alt} ({m.group('img_url')})", styles))
        elif m.group("link_url") is not None:
            label, url = m.group("link_text"), m.group("link_url")
            spans.extend(_inline(label, styles))
            if label.strip() != url:
                spans.append(Span(f" ({url})", styles))
        elif m.group("strong_body") is not None:
            spans.extend(_inline(m.group("strong_body"), styles | {BOLD}))
        elif m.group("strike_body") is not None:
            spans.extend(_inline(m.group("strike_body"), styles | {STRIKETHROUGH}))
        elif m.group("em_star") is not None:
            spans.extend(_inline(m.group("em_star"), styles | {ITALIC}))
        elif m.group("em_under") is not None:
            spans.extend(_inline(m.group("em_under"), styles | {ITALIC}))
        elif m.group("autolink") is not None:
            spans.append(Span(m.group("autolink"), styles))
        pos = m.end()
    if pos < len(text):
        spans.append(Span(_unescape(text[pos:]), styles))
    return spans


_ESCAPABLE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~|>])")


def _unescape(text: str) -> str:
    return _ESCAPABLE.sub(r"\1", text)


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?:\[( |x|X)\]\s+)?(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _block_lines(markdown: str) -> list[list[Span]]:
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[list[Span]] = []
    in_fence = False
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append(_inline(" ".join(s.strip() for s in paragraph)))
            paragraph.clear()

    for line in lines:
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            out.append([Span(line)])
            continue
        if not line.strip():
            flush()
            out.append([])
            continue
        if _TABLE_SEP.match(line) or _HR.match(line):
            flush()
            continue
        if m := _HEADING.match(line):
            flush()
            out.append(_inline(m.group(2), frozenset({BOLD})))
            continue
        if m := _BULLET.match(line):
            flush()
            indent = "  " * (len(m.group(1)) // 2)
            box = {" ": "☐ ", "x": "☑ ", "X": "☑ "}.get(m.group(2) or "", "")
            out.append([Span(f"{indent}• {box}"), *_inline(m.group(3))])
            continue
        if m := _ORDERED.match(line):
            flush()
            indent = "  " * (len(m.group(1)) // 2)
            out.append([Span(f"{indent}{m.group(2)}. "), *_inline(m.group(3))])
            continue
        if m := _QUOTE.match(line):
            flush()
            out.append([Span("│ "), *_inline(m.group(1), frozenset({ITALIC}))])
            continue
        if line.strip().startswith("|") and line.strip().endswith("|"):
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            row: list[Span] = []
            for i, cell in enumerate(cells):
                if i:
                    row.append(Span(" | "))
                row.extend(_inline(cell))
            out.append(row)
            continue
        if line.endswith("  ") or line.endswith("\\"):  # hard line break
            paragraph.append(line.rstrip("\\ "))
            flush()
            continue
        paragraph.append(line)
    flush()
    return out


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def render(markdown: str) -> RenderedText:
    """Render markdown to plain text plus UTF-16 formatting ranges."""
    blocks = _block_lines(markdown)
    # collapse runs of blank lines and trim leading/trailing blanks
    lines: list[list[Span]] = []
    for block in blocks:
        if not block and (not lines or not lines[-1]):
            continue
        lines.append(block)
    while lines and not lines[-1]:
        lines.pop()

    text_parts: list[str] = []
    ranges: dict[str, list[tuple[int, int]]] = {}
    offset = 0
    for i, line in enumerate(lines):
        if i:
            text_parts.append("\n")
            offset += 1
        for span in line:
            if not span.text:
                continue
            length = _utf16_len(span.text)
            for style in span.styles:
                spans = ranges.setdefault(style, [])
                if spans and spans[-1][0] + spans[-1][1] == offset:
                    spans[-1] = (spans[-1][0], spans[-1][1] + length)  # merge adjacent
                else:
                    spans.append((offset, length))
            text_parts.append(span.text)
            offset += length

    formatting = tuple(
        TextFormat(style, start, length)
        for style in (BOLD, ITALIC, UNDERLINE, STRIKETHROUGH)
        for start, length in ranges.get(style, ())
    )
    return RenderedText("".join(text_parts), formatting)


def to_plain_text(markdown: str) -> str:
    """Readable fallback for platforms without styled text."""
    return render(markdown).text

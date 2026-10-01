from datetime import UTC, datetime

import pytest

import spectrum
from spectrum.content import render
from spectrum.core.errors import ContentError
from spectrum.models import (
    Attachment,
    ContentType,
    Direction,
    Group,
    Message,
    Platform,
    Reaction,
    Space,
    Tapback,
    Text,
)


def _msg(direction: Direction = Direction.INBOUND, content=None) -> Message:
    return Message(
        id="m1",
        content=content or Text("hi"),
        space=Space("s1", Platform.IMESSAGE),
        timestamp=datetime.now(UTC),
        direction=direction,
    )


def test_string_is_text():
    assert spectrum.content.coerce("hello") == Text("hello")


def test_top_level_builders_are_callables():
    # regression: a submodule named like a builder used to shadow the function
    for name in spectrum.content.__all__:
        value = getattr(spectrum.content, name)
        assert not isinstance(value, type(spectrum)), f"spectrum.content.{name} is a module"
    assert spectrum.markdown("**x**").type is ContentType.MARKDOWN


def test_text_setter_validates():
    t = Text("a")
    t.text = "b"
    assert t.text == "b"
    with pytest.raises(ContentError):
        t.text = 1  # type: ignore[assignment]


def test_reply_rejects_unwrappable():
    target = _msg()
    with pytest.raises(ContentError):
        spectrum.reply(spectrum.reaction("❤️", target), target)
    assert spectrum.reply("ok", target).content == Text("ok")


def test_unsend_and_read_direction_rules():
    with pytest.raises(ContentError):
        spectrum.unsend(_msg(Direction.INBOUND))
    with pytest.raises(ContentError):
        spectrum.read(_msg(Direction.OUTBOUND))
    assert spectrum.unsend(_msg(Direction.OUTBOUND)).type is ContentType.UNSEND


def test_cannot_react_to_reaction():
    reaction_msg = _msg(content=Reaction("👍", _msg()))
    with pytest.raises(ContentError):
        spectrum.reaction("❤️", reaction_msg)


def test_group_rules():
    with pytest.raises(ContentError):
        Group([spectrum.group("a", "b")])
    assert len(spectrum.group("a", "b").items) == 2


def test_poll_needs_two_options():
    with pytest.raises(ContentError):
        spectrum.poll("Lunch?", "Pizza")
    p = spectrum.poll("Lunch?", ["Pizza", "Sushi"])
    assert [o.title for o in p.options] == ["Pizza", "Sushi"]


async def test_attachment_from_bytes_and_path(tmp_path):
    a = spectrum.attachment(b"data", name="report.pdf")
    assert isinstance(a, Attachment) and a.mime_type == "application/pdf" and await a.read() == b"data"
    f = tmp_path / "photo.heic"
    f.write_bytes(b"x" * 10)
    b = spectrum.attachment(f)
    assert (b.name, b.mime_type, b.size) == ("photo.heic", "image/heic", 10)
    assert await b.read() == b"x" * 10
    with pytest.raises(ContentError):
        spectrum.attachment(b"data", name="noext")


def test_markdown_render_utf16_offsets():
    rendered = render("# Title\n\n**bold** 😀 _it_ ~~gone~~ [link](https://x.y)")
    assert rendered.text == "Title\n\nbold 😀 it gone link (https://x.y)"
    fmt = {(f.type, f.start, f.length) for f in rendered.formatting}
    # "😀" is two UTF-16 code units, so ranges after it shift by one extra unit
    assert fmt == {("bold", 0, 5), ("bold", 7, 4), ("italic", 15, 2), ("strikethrough", 18, 4)}


def test_tapback_mapping():
    assert Tapback.from_emoji("😂") is Tapback.LAUGH
    assert Tapback.LOVE.emoji == "❤️"
    assert Tapback.from_emoji("🔥") is None


def test_platform_parse():
    assert Platform.parse("iMessage") is Platform.IMESSAGE
    assert Platform.parse("WhatsApp Business") is Platform.WHATSAPP_BUSINESS
    assert Platform.parse("my_custom") == "my_custom"


def test_vcard_roundtrip():
    c = spectrum.contact(
        {"name": {"first": "Ada", "last": "Lovelace"}, "phones": [{"value": "+15551234567"}]}
    )
    parsed = spectrum.content.from_vcard(spectrum.content.to_vcard(c))
    assert parsed.name is not None
    assert parsed.name.first == "Ada" and parsed.phones[0].value == "+15551234567"

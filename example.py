"""Example iMessage bot that shows off what Spectrum can send.

Put your project credentials in .env (see .env.example), then run:

    python example.py

Text the bot's number "help" to see every command. On Free/Pro plans your phone
number must be registered under Users in the dashboard.
"""

import asyncio
import io
import math
import struct
import wave
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from dotenv import load_dotenv

import spectrum
from spectrum import Emoji, MessageEffect
from spectrum.content import contact_card
from spectrum.providers import IMessage

load_dotenv()

client = spectrum.Client(providers=[IMessage()])

PHOTOS = [
    "https://picsum.photos/id/1015/800/600.jpg",
    "https://picsum.photos/id/1043/800/600.jpg",
    "https://picsum.photos/id/1018/800/600.jpg",
]
WALLPAPER = "https://picsum.photos/id/29/1170/2532.jpg"

Handler = Callable[[spectrum.Message, str], Awaitable[None]]
COMMANDS: dict[str, tuple[str, Handler]] = {}


def command(name: str, usage: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        COMMANDS[name] = (usage, handler)
        return handler

    return register


# --------------------------------------------------------------------------- basics


@command("help", "list commands")
async def help_(message: spectrum.Message, _: str) -> None:
    lines = [f"• {name} — {usage}" for name, (usage, _) in COMMANDS.items()]
    await message.space.send(spectrum.markdown("**Commands**\n" + "\n".join(lines)))


@command("ping", "tapback + threaded reply")
async def ping(message: spectrum.Message, _: str) -> None:
    await message.react(Emoji.LIKE)
    await message.reply("pong")


@command("ask", "ask a question and wait for the answer")
async def ask(message: spectrum.Message, _: str) -> None:
    await message.space.send("What's your name?")
    # read receipts and reactions are messages too: wait for the next *text* from this person
    try:
        answer = await client.wait_for(
            "message",
            check=lambda m: (
                m.type is spectrum.ContentType.TEXT
                and m.space == message.space
                and m.sender == message.sender
            ),
            timeout=60,
            consume=True,  # the answer goes only here, not to on_message as well
        )
    except TimeoutError:
        await message.space.send("No answer — maybe next time 👋")
        return
    await answer.reply(f"Nice to meet you, {answer.text}!")


@command("format", "bold / italic / strikethrough / lists")
async def format_(message: spectrum.Message, _: str) -> None:
    await message.space.send(
        spectrum.markdown(
            "# Formatting\n"
            "**bold**, _italic_, ~~strikethrough~~ and `code`\n\n"
            "- bullet one\n- bullet two\n\n"
            "> quoted text\n\n"
            "[Spectrum docs](https://photon.codes/docs)"
        )
    )


# --------------------------------------------------------------------------- effects


@command("party", "confetti screen effect")
async def party(message: spectrum.Message, _: str) -> None:
    await message.space.send(spectrum.effect("🎉", MessageEffect.CONFETTI))


@command("effect", "effect <name>: " + ", ".join(e.name.lower() for e in MessageEffect))
async def effect(message: spectrum.Message, args: str) -> None:
    name = args.strip().upper() or "FIREWORKS"
    if name not in MessageEffect.__members__:
        await message.reply("Unknown effect. Try: " + ", ".join(e.name.lower() for e in MessageEffect))
        return
    chosen = MessageEffect[name]
    await message.space.send(spectrum.effect(f"{chosen.name.lower()} ✨", chosen))


# --------------------------------------------------------------------------- media


@command("photo", "send a photo")
async def photo(message: spectrum.Message, _: str) -> None:
    await message.space.send(spectrum.attachment(PHOTOS[0], name="mountains.jpg"))


@command("album", "several photos as one album")
async def album(message: spectrum.Message, _: str) -> None:
    photos = [spectrum.attachment(url, name=f"photo-{i}.jpg") for i, url in enumerate(PHOTOS, 1)]
    await message.space.send(spectrum.group(*photos))


@command("voice", "a generated voice note (needs ffmpeg)")
async def voice(message: spectrum.Message, _: str) -> None:
    await message.space.send(spectrum.voice(chime(), name="chime.wav", mime_type="audio/wav", duration=1.2))


@command("map", "map <place>: a location as an Apple Maps preview")
async def map_(message: spectrum.Message, args: str) -> None:
    place = args.strip()
    if place:
        url = f"https://maps.apple.com/?q={quote(place)}"
    else:
        url = "https://maps.apple.com/?q=Gyeongbokgung%20Palace&ll=37.5796,126.9770"
    await message.space.send(spectrum.richlink(url))


@command("link", "a link with a rich preview")
async def link(message: spectrum.Message, _: str) -> None:
    await message.space.send(spectrum.richlink("https://github.com/deongdion/spectrum"))


@command("app", "a tappable app card built from the page's metadata")
async def app(message: spectrum.Message, _: str) -> None:
    await message.space.send(spectrum.app("https://github.com/deongdion/spectrum"))


# --------------------------------------------------------------------------- structured content


@command("poll", "a native iMessage poll")
async def poll(message: spectrum.Message, _: str) -> None:
    await message.space.send(spectrum.poll("Lunch?", "Pizza 🍕", "Sushi 🍣", "Tacos 🌮"))


@command("contact", "a contact card (vCard)")
async def contact(message: spectrum.Message, _: str) -> None:
    await message.space.send(
        spectrum.contact(
            {
                "name": {"first": "Ada", "last": "Lovelace"},
                "phones": [{"value": "+15551234567", "type": "mobile"}],
                "emails": [{"value": "ada@example.com", "type": "work"}],
                "note": "Sent by a Spectrum bot",
            }
        )
    )


@command("card", "the bot's own contact card")
async def card(message: spectrum.Message, _: str) -> None:
    await message.space.send(contact_card())


# --------------------------------------------------------------------------- live updates


@command("stream", "text that streams in and is edited in place")
async def stream(message: spectrum.Message, _: str) -> None:
    async def words():
        for word in "Streaming replies arrive word by word while the bubble is edited in place.".split():
            yield word + " "
            await asyncio.sleep(0.4)

    await message.space.send(spectrum.text(words()))


@command("edit", "send a message, then edit it")
async def edit(message: spectrum.Message, _: str) -> None:
    sent = await message.space.send("Working on it…")
    await asyncio.sleep(2)
    if sent:
        await sent.edit("Done ✅")


@command("unsend", "send a message, then take it back")
async def unsend(message: spectrum.Message, _: str) -> None:
    sent = await message.space.send("This message will disappear in 3 seconds 👻")
    await asyncio.sleep(3)
    if sent:
        await sent.unsend()


@command("typing", "show the typing indicator for a few seconds")
async def typing(message: spectrum.Message, _: str) -> None:
    async with message.space.typing():
        await asyncio.sleep(3)
    await message.space.send("…and done typing.")


@command("background", "background [off]: set or clear the chat background")
async def background(message: spectrum.Message, args: str) -> None:
    source = None if args.strip().lower() in {"off", "clear"} else WALLPAPER
    await client.imessage.background(message.space, source)


# --------------------------------------------------------------------------- events


@client.event
async def on_ready():
    print("ready — iMessage lines:", client.imessage.phones)


@client.event
async def on_message(message: spectrum.Message):
    if message.type is not spectrum.ContentType.TEXT:
        return
    text = (message.text or "").strip()
    print(f"<- {message.sender}: {text}")
    await message.read()

    name, _, args = text.partition(" ")
    entry = COMMANDS.get(name.lower())
    if entry is None:
        async with message.space.typing():
            await message.space.send(
                spectrum.markdown(f"**echo:** {text}\n\nText _help_ to see what I can do.")
            )
        return
    await entry[1](message, args)


@client.event
async def on_reaction(message: spectrum.Message):
    reaction = message.content
    assert isinstance(reaction, spectrum.models.Reaction)
    print(f"{message.sender} reacted {reaction.emoji} to {reaction.target.id}")


@client.event
async def on_read_receipt(message: spectrum.Message):
    receipt = message.content
    assert isinstance(receipt, spectrum.models.Read)
    print(f"{message.sender} read {receipt.target.id}")


@client.event
async def on_poll_vote(message: spectrum.Message):
    vote = message.content
    assert isinstance(vote, spectrum.models.PollVote)
    verb = "voted for" if vote.selected else "took back their vote for"
    await message.space.send(f"{message.sender} {verb} {vote.title}")


@client.event
async def on_error(event: str, exc: Exception, *args):
    print(f"error in on_{event}: {exc!r}")


def chime(rate: int = 22050) -> bytes:
    """A short C-E-G chime as WAV bytes (converted to m4a on send)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = bytearray()
        for frequency in (523.25, 659.25, 783.99):
            for i in range(int(rate * 0.4)):
                fade = 1 - i / (rate * 0.4)
                frames += struct.pack("<h", int(12000 * fade * math.sin(2 * math.pi * frequency * i / rate)))
        out.writeframes(bytes(frames))
    return buffer.getvalue()


if __name__ == "__main__":
    client.run()

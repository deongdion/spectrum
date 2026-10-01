"""Spectrum Python SDK example — a real iMessage bot via Spectrum Cloud.

Put your project credentials in .env:

    SPECTRUM_PROJECT_ID=...        (PHOTON_PROJECT_ID also works)
    SPECTRUM_PROJECT_SECRET=...    (PHOTON_PROJECT_SECRET also works)

then run:

    .venv/Scripts/python example.py

Text the bot's number from your phone:
    ping   -> 👍 tapback + threaded "pong"
    party  -> confetti effect
    ask    -> asks a question and waits for the answer (wait_for)
    other  -> markdown echo with a typing indicator

On Free/Pro (shared line) your phone number must be registered under Users in the dashboard.
"""

from dotenv import load_dotenv

import spectrum
from spectrum import Emoji, MessageEffect
from spectrum.providers import IMessage

load_dotenv()

client = spectrum.Client(providers=[IMessage()])


@client.event
async def on_ready():
    print("ready — iMessage lines:", client.imessage.phones)


@client.event
async def on_message(message: spectrum.Message):
    if message.type is not spectrum.ContentType.TEXT:
        return
    text = message.text or ""
    print(f"<- {message.sender}: {text}")
    command = text.strip().lower()

    if command == "ping":
        await message.react(Emoji.LIKE)
        await message.reply("pong")
    elif command == "party":
        await message.space.send(spectrum.effect("🎉", MessageEffect.CONFETTI))
    elif command == "ask":
        await message.space.send("What's your name?")
        # read receipts and reactions are messages too: wait for the next *text* from this person
        answer = await client.wait_for(
            "message",
            check=lambda m: (
                m.type is spectrum.ContentType.TEXT
                and m.space == message.space
                and m.sender == message.sender
            ),
            timeout=60,
        )
        await answer.reply(f"Nice to meet you, {answer.text}!")
    else:
        async with message.space.typing():
            await message.space.send(spectrum.markdown(f"**echo:** {text}"))


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
async def on_error(event: str, exc: Exception, *args):
    print(f"error in on_{event}: {exc!r}")


if __name__ == "__main__":
    client.run()

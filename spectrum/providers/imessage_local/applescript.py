"""Send through the Messages app with AppleScript (``osascript``).

Values are passed as script arguments (``argv``), never interpolated into the script text.
The first send triggers macOS's "Terminal wants to control Messages" prompt: allow it once.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from ...core.errors import SpectrumError

# Messages can only read files it has access to; copying into ~/Pictures avoids silent failures.
OUTBOX = Path("~/Pictures/spectrum-outbox").expanduser()

_SEND_TO_CHAT = """
on run argv
    set chatGuid to item 1 of argv
    set payload to item 2 of argv
    set isFile to item 3 of argv
    tell application "Messages"
        set targetChat to chat id chatGuid
        if isFile is "1" then
            send (POSIX file payload) to targetChat
        else
            send payload to targetChat
        end if
    end tell
end run
"""

_SEND_TO_ADDRESS = """
on run argv
    set address to item 1 of argv
    set payload to item 2 of argv
    set isFile to item 3 of argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        set target to participant address of svc
        if isFile is "1" then
            send (POSIX file payload) to target
        else
            send payload to target
        end if
    end tell
end run
"""


class AppleScriptError(SpectrumError):
    pass


async def _osascript(script: str, *args: str, timeout: float = 30.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise AppleScriptError("osascript timed out") from None
    if proc.returncode != 0:
        raise AppleScriptError(err.decode("utf-8", "replace").strip() or f"osascript exit {proc.returncode}")
    return out.decode("utf-8", "replace").strip()


def stage_file(path: Path) -> Path:
    """Copy a file into the outbox so Messages is allowed to read it."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    target = OUTBOX / f"{uuid.uuid4().hex[:8]}-{path.name}"
    shutil.copyfile(path, target)
    return target


async def send(chat_guid: str, payload: str, *, is_file: bool = False) -> None:
    """Send text (or a file path) to a chat; for an unseen DM fall back to the address."""
    flag = "1" if is_file else "0"
    try:
        await _osascript(_SEND_TO_CHAT, chat_guid, payload, flag)
    except AppleScriptError:
        address = dm_address(chat_guid)
        if address is None:
            raise
        await _osascript(_SEND_TO_ADDRESS, address, payload, flag)


def dm_address(chat_guid: str) -> str | None:
    """``iMessage;-;+15551234567`` -> ``+15551234567`` (``;+;`` group guids return None)."""
    if ";-;" not in chat_guid:
        return None
    return chat_guid.split(";-;", 1)[1] or None

"""Voice-note normalization: iMessage audio messages must be M4A (AAC in an MP4/iPod container).

Port of spectrum-ts ``ensureM4a``: input that is already M4A (by MIME type or
``ftyp`` brand) passes through; anything else is transcoded with ``ffmpeg``.
The binary is taken from ``SPECTRUM_FFMPEG_PATH`` or found on ``PATH``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ContentError

M4A_BRANDS = frozenset({"M4A ", "M4B ", "M4P ", "mp42", "mp41", "isom", "iso2"})
M4A_MIME_TYPES = frozenset({"audio/mp4", "audio/mp4a-latm", "audio/x-m4a", "audio/aac", "audio/aacp"})
FFMPEG_MISSING = (
    "voice content: input is not m4a/aac and ffmpeg is unavailable. "
    "Install ffmpeg (e.g. `winget install ffmpeg`) or set SPECTRUM_FFMPEG_PATH."
)
_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?")


@dataclass(frozen=True, slots=True)
class AudioResult:
    data: bytes
    converted: bool
    duration: float | None = None


def is_m4a(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12].decode("ascii", "replace") in M4A_BRANDS


def is_m4a_mime_type(mime_type: str) -> bool:
    return mime_type.lower() in M4A_MIME_TYPES


def ffmpeg_path() -> str | None:
    return os.environ.get("SPECTRUM_FFMPEG_PATH") or shutil.which("ffmpeg")


def parse_duration(stderr: str) -> float | None:
    match = _DURATION.search(stderr)
    if not match:
        return None
    hh, mm, ss, frac = match.groups()
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + float(f"0.{frac or 0}")


async def transcode_to_m4a(data: bytes, *, timeout: float = 120.0) -> AudioResult:
    binary = ffmpeg_path()
    if not binary:
        raise ContentError(FFMPEG_MISSING)
    with tempfile.TemporaryDirectory(prefix="spectrum-voice-") as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out.m4a"
        src.write_bytes(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "-y",
                "-i",
                str(src),
                "-f",
                "ipod",
                "-c:a",
                "aac",
                str(dst),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ContentError(FFMPEG_MISSING) from exc
        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            raise ContentError("ffmpeg conversion timed out") from None
        stderr = stderr_bytes.decode("utf-8", "replace")
        if proc.returncode != 0:
            raise ContentError(f"ffmpeg conversion failed (exit {proc.returncode}): {stderr[-500:]}")
        return AudioResult(dst.read_bytes(), True, parse_duration(stderr))


async def ensure_m4a(data: bytes, mime_type: str) -> AudioResult:
    """Return M4A bytes, transcoding only when needed."""
    if is_m4a_mime_type(mime_type) or is_m4a(data):
        return AudioResult(data, False)
    return await transcode_to_m4a(data)


def m4a_name(name: str | None) -> str:
    if not name:
        return "voice.m4a"
    return str(Path(name).with_suffix(".m4a"))

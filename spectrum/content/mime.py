"""MIME type inference with the Apple-specific types ``mimetypes`` lacks."""

from __future__ import annotations

import mimetypes
from pathlib import PurePath

_EXTRA = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".webp": "image/webp",
    ".m4a": "audio/mp4",
    ".caf": "audio/x-caf",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".mov": "video/quicktime",
    ".vcf": "text/vcard",
    ".pkpass": "application/vnd.apple.pkpass",
}

VCARD_MIME_TYPES = frozenset(
    {"text/vcard", "text/x-vcard", "text/directory", "application/vcard", "application/x-vcard"}
)


def guess_mime_type(name: str) -> str | None:
    suffix = PurePath(name).suffix.lower()
    if suffix in _EXTRA:
        return _EXTRA[suffix]
    guessed, _ = mimetypes.guess_type(name, strict=False)
    return guessed


def normalize_mime_type(mime_type: str) -> str:
    return (mime_type.split(";")[0] or "").strip().lower()


def is_vcard(mime_type: str | None, file_name: str | None) -> bool:
    if mime_type and normalize_mime_type(mime_type) in VCARD_MIME_TYPES:
        return True
    return bool(file_name and file_name.lower().endswith(".vcf"))


def extension_for(mime_type: str) -> str:
    for ext, value in _EXTRA.items():
        if value == mime_type:
            return ext
    return mimetypes.guess_extension(mime_type) or ".bin"

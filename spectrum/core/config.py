"""Explicit-wins-over-environment configuration resolution.

Mirrors spectrum-ts: an explicit value always wins, otherwise the conventional
environment variable is read. Provider fields follow
``SPECTRUM_<PLATFORM>_<FIELD>`` (e.g. ``SPECTRUM_TELEGRAM_BOT_TOKEN``).
``PHOTON_*`` names are accepted as a fallback for the project credentials.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

PROJECT_ID_ENV = ("SPECTRUM_PROJECT_ID", "PHOTON_PROJECT_ID")
PROJECT_SECRET_ENV = ("SPECTRUM_PROJECT_SECRET", "PHOTON_PROJECT_SECRET")
WEBHOOK_SECRET_ENV = ("SPECTRUM_WEBHOOK_SECRET",)

DEFAULT_CLOUD_URL = "https://spectrum.photon.codes"

_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def env_first(names: tuple[str, ...] | str) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in (names,) if isinstance(names, str) else names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def resolve(explicit: str | None, names: tuple[str, ...] | str) -> str | None:
    return explicit if explicit else env_first(names)


def provider_env_name(platform: str, field: str) -> str:
    """``("whatsapp_business", "phoneNumberId")`` -> ``SPECTRUM_WHATSAPP_BUSINESS_PHONE_NUMBER_ID``."""
    platform_part = _SEPARATORS.sub("_", platform).strip("_").upper()
    field_part = _SEPARATORS.sub("_", _CAMEL.sub("_", field)).strip("_").upper()
    return f"SPECTRUM_{platform_part}_{field_part}"


def provider_setting(explicit: str | None, platform: str, field: str) -> str | None:
    return resolve(explicit, provider_env_name(platform, field))


@dataclass(frozen=True, slots=True)
class Credentials:
    """Resolved project credentials. ``secret`` is never included in ``repr``."""

    project_id: str
    project_secret: str

    def __repr__(self) -> str:
        return f"Credentials(project_id={self.project_id!r}, project_secret='***')"

    @classmethod
    def resolve(cls, project_id: str | None = None, project_secret: str | None = None) -> Credentials | None:
        pid = resolve(project_id, PROJECT_ID_ENV)
        secret = resolve(project_secret, PROJECT_SECRET_ENV)
        if pid and secret:
            return cls(pid, secret)
        return None

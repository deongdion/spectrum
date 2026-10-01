"""A participant on a platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import Model
from .enums import AddressService, Platform


@dataclass(init=False, repr=False, eq=False, slots=True)
class User(Model):
    """Platform user. ``id`` is the canonical platform handle (iMessage: E.164 or email).

    Two users are equal when platform and id match.
    """

    __repr_fields__ = ("id", "platform", "service", "is_agent")
    _id: str
    _platform: Platform | str
    _is_agent: bool
    _address: str | None
    _country: str | None
    _service: AddressService | None
    _extras: dict[str, Any]

    def __init__(
        self,
        id: str,
        platform: Platform | str,
        *,
        is_agent: bool = False,
        address: str | None = None,
        country: str | None = None,
        service: AddressService | str | None = None,
        extras: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(id, str):
            raise TypeError("user id must be a str")
        self._id = id
        self._platform = platform
        self._is_agent = is_agent
        self._address = address
        self._country = country
        self._service = _parse_service(service)
        self._extras = dict(extras or {})

    @property
    def id(self) -> str:
        return self._id

    @property
    def platform(self) -> Platform | str:
        return self._platform

    @property
    def is_agent(self) -> bool:
        """``True`` for the agent's own account (spectrum-ts ``kind: "agent"``)."""
        return self._is_agent

    @property
    def address(self) -> str | None:
        """iMessage: phone number or email the user is reachable at."""
        return self._address

    @property
    def country(self) -> str | None:
        return self._country

    @property
    def service(self) -> AddressService | None:
        """iMessage: the service the user is reachable on (iMessage / SMS / RCS)."""
        return self._service

    @property
    def extras(self) -> dict[str, Any]:
        """Untyped provider-specific fields."""
        return self._extras

    @property
    def mention(self) -> str:
        return self._id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, User) and other._id == self._id and other._platform == self._platform

    def __hash__(self) -> int:
        return hash((str(self._platform), self._id))

    def __str__(self) -> str:
        return self._id


def _parse_service(value: AddressService | str | None) -> AddressService | None:
    if value is None or isinstance(value, AddressService):
        return value
    for member in AddressService:
        if member.value.lower() == str(value).lower():
            return member
    return AddressService.UNKNOWN

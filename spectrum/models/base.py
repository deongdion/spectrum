"""Common base for domain models.

Models are ``@dataclass(init=False, repr=False)`` classes whose state lives in
underscore-prefixed fields and is exposed through ``@property`` getters (and
validating setters where mutation makes sense). Each subclass lists the public
property names in ``__repr_fields__`` so ``repr()`` shows the public API, not
the private storage.
"""

from __future__ import annotations

from typing import Any, ClassVar


class Model:
    __slots__ = ()
    __repr_fields__: ClassVar[tuple[str, ...]] = ()

    def __repr__(self) -> str:
        parts = []
        for name in self.__repr_fields__:
            value = getattr(self, name, None)
            if value is None:
                continue
            parts.append(f"{name}={_short_repr(value)}")
        return f"{type(self).__name__}({', '.join(parts)})"

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection of the public fields (callables and bytes dropped)."""
        out: dict[str, Any] = {}
        for name in self.__repr_fields__:
            value = getattr(self, name, None)
            if value is None or callable(value) or isinstance(value, bytes | bytearray):
                continue
            out[name] = _to_jsonable(value)
        return out


def _short_repr(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _to_jsonable(value: Any) -> Any:
    from datetime import datetime
    from enum import Enum

    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value

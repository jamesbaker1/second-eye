"""Just enough of pydantic to declare plain records, for a container without it.

Used only when the real package cannot be imported (see scripts/_bootstrap.py).
No validation and no coercion: inside the container every record is built by
this product's own code from values it already trusts. Anything that arrives
from outside is validated by the real pydantic, on the server that receives it.
"""

from __future__ import annotations

import copy

_MISSING = object()


class _FieldInfo:
    def __init__(self, default, default_factory):
        self.default = default
        self.default_factory = default_factory


def Field(default=_MISSING, *, default_factory=None, **_ignored):
    return _FieldInfo(default, default_factory)


def field_validator(*_args, **_kwargs):
    return lambda function: function


def model_validator(*_args, **_kwargs):
    return lambda function: function


class BaseModel:
    def __init__(self, **data):
        for klass in reversed(type(self).__mro__):
            for name in getattr(klass, "__annotations__", {}):
                if name.startswith("_"):
                    continue
                if name in data:
                    value = data[name]
                else:
                    default = getattr(type(self), name, _MISSING)
                    if isinstance(default, _FieldInfo):
                        default = (default.default_factory()
                                   if default.default_factory else default.default)
                    if default is _MISSING:
                        raise TypeError(f"{type(self).__name__} needs {name!r}")
                    value = copy.deepcopy(default)
                setattr(self, name, value)

    def model_dump(self) -> dict:
        return {
            name: getattr(self, name)
            for klass in reversed(type(self).__mro__)
            for name in getattr(klass, "__annotations__", {})
            if not name.startswith("_")
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.model_dump()!r})"

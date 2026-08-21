"""Stdlib-only JSON serde for swarm dataclasses.

Rules: enums serialize as their string value; unknown keys in from_dict are
dropped (forward compat); missing keys fall back to field defaults; canonical
JSON (sorted keys, tight separators) is the only form ever hashed or hashed.
Never pickle across the network.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from enum import Enum
from typing import Any, Dict, Type, TypeVar

T = TypeVar("T")


def to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> bytes:
    return json.dumps(to_dict(obj), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def dumps(obj: Any) -> str:
    return json.dumps(to_dict(obj))


def _coerce(value: Any, annotation: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _coerce(value, non_none[0])
        return value
    if origin in (list, typing.List):
        item_t = args[0] if args else Any
        return [_coerce(v, item_t) for v in value]
    if origin in (dict, typing.Dict):
        val_t = args[1] if len(args) == 2 else Any
        return {k: _coerce(v, val_t) for k, v in value.items()}
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return from_dict(annotation, value)
    return value


def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
    if not isinstance(data, dict):
        raise TypeError(
            f"from_dict({cls.__name__}) expected dict, got {type(data).__name__}"
        )
    hints = typing.get_type_hints(cls)
    kwargs: Dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], hints.get(f.name, Any))
    return cls(**kwargs)


def loads(cls: Type[T], text: str) -> T:
    return from_dict(cls, json.loads(text))

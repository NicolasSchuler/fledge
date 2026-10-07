"""Strict TOML conversion for grouped, immutable checker options."""

from __future__ import annotations

import types
from dataclasses import fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

from .models import PreparationError


def read_options(kind: type, value: object, location: str) -> Any:
    if isinstance(value, kind):
        return value
    if not isinstance(value, dict):
        raise PreparationError(f"{location} must be a settings table")
    if not is_dataclass(kind):
        raise PreparationError(f"{location} requires a dataclass option schema")
    known = {item.name for item in fields(kind)}
    unknown = set(value) - known
    if unknown:
        raise PreparationError(
            f"Unknown {location} setting(s): {', '.join(sorted(map(str, unknown)))}"
        )
    hints = get_type_hints(kind)
    converted = {
        name: _convert(hints[name], item, f"{location}.{name}") for name, item in value.items()
    }
    try:
        return kind(**converted)
    except (ValueError, TypeError, PreparationError) as error:
        raise PreparationError(f"Invalid {location} settings: {error}") from error


def _convert(annotation: Any, value: object, location: str) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in {Union, types.UnionType}:
        if value is None and type(None) in args:
            return None
        variants = [item for item in args if item is not type(None)]
        for variant in variants:
            try:
                return _convert(variant, value, location)
            except PreparationError:
                pass
        raise PreparationError(f"{location} has an invalid value type")
    if isinstance(annotation, type) and is_dataclass(annotation):
        return read_options(annotation, value, location)
    if origin is tuple:
        if isinstance(value, dict) and len(args) == 2 and args[-1] is Ellipsis:
            # Named policy pairs such as required_fields.article=["title","author"].
            inner = get_args(args[0])
            if get_origin(args[0]) is tuple and len(inner) == 2 and inner[0] is str:
                if any(not isinstance(key, str) for key in value):
                    raise PreparationError(f"{location} requires string keys")
                value = tuple(sorted(value.items()))
        if not isinstance(value, (list, tuple)):
            raise PreparationError(f"{location} must be an array")
        if len(args) == 2 and args[-1] is Ellipsis:
            return tuple(
                _convert(args[0], item, f"{location}[{index}]") for index, item in enumerate(value)
            )
        if len(args) != len(value):
            raise PreparationError(f"{location} requires {len(args)} array items")
        return tuple(
            _convert(kind, item, f"{location}[{index}]")
            for index, (kind, item) in enumerate(zip(args, value, strict=True))
        )
    if annotation is bool and type(value) is not bool:
        raise PreparationError(f"{location} must be a boolean")
    if annotation is int and type(value) is not int:
        raise PreparationError(f"{location} must be an integer")
    if annotation is float and (type(value) not in {int, float}):
        raise PreparationError(f"{location} must be a number")
    if annotation is str and not isinstance(value, str):
        raise PreparationError(f"{location} must be a string")
    return value

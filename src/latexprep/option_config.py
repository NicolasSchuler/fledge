"""Strict TOML conversion for grouped, immutable checker options."""

from __future__ import annotations

import re
import types
from dataclasses import fields, is_dataclass
from typing import Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

from .errors import PreparationError, SettingsError

T = TypeVar("T")


def _settings_error(location: str, message: str) -> SettingsError:
    """An error about the value at ``location``, named by the top-level setting that holds it."""
    return SettingsError(message, (re.split(r"[.\[]", location, maxsplit=1)[0],))


def read_options(kind: type[T], value: object, location: str) -> T:
    """Convert one TOML table into the option dataclass ``kind``; reuse an instance as is."""
    if isinstance(value, kind):
        return value
    if not isinstance(value, dict):
        raise _settings_error(location, f"{location} must be a settings table")
    if not is_dataclass(kind):
        raise _settings_error(location, f"{location} requires a dataclass option schema")
    known = {item.name for item in fields(kind)}
    unknown = set(value) - known
    if unknown:
        raise _settings_error(
            location, f"Unknown {location} setting(s): {', '.join(sorted(map(str, unknown)))}"
        )
    hints = get_type_hints(kind)
    converted = {
        name: _convert(hints[name], item, f"{location}.{name}") for name, item in value.items()
    }
    try:
        return kind(**converted)
    except (ValueError, TypeError, PreparationError) as error:
        raise _settings_error(location, f"Invalid {location} settings: {error}") from error


def require_booleans(options: object, *names: str) -> None:
    """Reject option fields that hold anything but ``True`` or ``False``, such as ``1``."""
    for name in names:
        if type(getattr(options, name)) is not bool:
            raise SettingsError(f"{name} must be a boolean", (name,))


def _convert(annotation: Any, value: object, location: str) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in {Union, types.UnionType}:
        if value is None and type(None) in args:
            return None
        variants = [item for item in args if item is not type(None)]
        if len(variants) == 1:
            # An optional value keeps its variant's message, such as the allowed choices.
            return _convert(variants[0], value, location)
        for variant in variants:
            try:
                return _convert(variant, value, location)
            except PreparationError:
                pass
        raise _settings_error(location, f"{location} has an invalid value type")
    if origin is Literal:
        # Compare types too: TOML true must not match a literal 1, nor 1 a literal True.
        if not any(type(value) is type(choice) and value == choice for choice in args):
            raise _settings_error(
                location, f"{location} must be one of {', '.join(map(str, args))}"
            )
        return value
    if isinstance(annotation, type) and is_dataclass(annotation):
        return read_options(annotation, value, location)
    if origin is tuple:
        if isinstance(value, dict) and len(args) == 2 and args[-1] is Ellipsis:
            # Named policy pairs such as required_fields.article=["title","author"].
            inner = get_args(args[0])
            if get_origin(args[0]) is tuple and len(inner) == 2 and inner[0] is str:
                if any(not isinstance(key, str) for key in value):
                    raise _settings_error(location, f"{location} requires string keys")
                value = tuple(sorted(value.items()))
        if not isinstance(value, (list, tuple)):
            raise _settings_error(location, f"{location} must be an array")
        if len(args) == 2 and args[-1] is Ellipsis:
            return tuple(
                _convert(args[0], item, f"{location}[{index}]") for index, item in enumerate(value)
            )
        if len(args) != len(value):
            raise _settings_error(location, f"{location} requires {len(args)} array items")
        return tuple(
            _convert(kind, item, f"{location}[{index}]")
            for index, (kind, item) in enumerate(zip(args, value, strict=True))
        )
    if annotation is bool and type(value) is not bool:
        raise _settings_error(location, f"{location} must be a boolean")
    if annotation is int and type(value) is not int:
        raise _settings_error(location, f"{location} must be an integer")
    if annotation is float and (type(value) not in {int, float}):
        raise _settings_error(location, f"{location} must be a number")
    if annotation is str and not isinstance(value, str):
        raise _settings_error(location, f"{location} must be a string")
    return value

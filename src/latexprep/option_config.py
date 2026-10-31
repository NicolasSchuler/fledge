"""Strict TOML conversion for grouped, immutable checker options, and the checks they share.

``read_options`` converts one TOML table into an option dataclass from its annotations;
``read_top_level_options`` does the same for options that TOML spells as top-level keys.
Both end by constructing the dataclass, so its ``__post_init__`` applies the same rules to
TOML values and to programmatic construction.

A ``__post_init__`` validates with the ``require_*`` functions and ``validate_scalars``
below. Every message names the setting as a settings file spells it, gives the allowed
range or choices, and calls a TOML array a "list". ``SettingsError.settings`` carries the
key, so the command line can say where to change it. A dataclass states the range of its
numeric fields in a ``FIELD_BOUNDS`` class variable, which the TOML conversion shares.
"""

from __future__ import annotations

import difflib
import math
import re
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from functools import cache
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NamedTuple,
    NoReturn,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from .errors import PreparationError, SettingsError

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

T = TypeVar("T")
# Longest value quoted back in a message, so a long invalid entry cannot flood the report.
_MAX_QUOTED_CHARACTERS = 60


@dataclass(frozen=True)
class Bounds:
    """The numbers a setting accepts: inclusive limits, or a minimum that excludes itself.

    Attributes:
        minimum: The lowest accepted number, or ``None`` for no lower limit.
        maximum: The highest accepted number, or ``None`` for no upper limit.
        exclusive_minimum: Whether ``minimum`` itself is rejected.
    """

    minimum: float | None = None
    maximum: float | None = None
    # With a minimum of 0 this means "positive"; without it, the minimum itself is allowed.
    exclusive_minimum: bool = False


# Shared limits that several option classes use.
POSITIVE_INTEGER = Bounds(minimum=1)
NONNEGATIVE_INTEGER = Bounds(minimum=0)
POSITIVE_NUMBER = Bounds(minimum=0, exclusive_minimum=True)
NONNEGATIVE_NUMBER = Bounds(minimum=0)
# A share of a total, such as a pixel ratio.
FRACTION = Bounds(minimum=0, maximum=1)
# A share that must be above zero.
NONZERO_FRACTION = Bounds(minimum=0, maximum=1, exclusive_minimum=True)
# An 8-bit colour channel or gray value.
EIGHT_BIT_MAXIMUM = 255
EIGHT_BIT_VALUE = Bounds(minimum=0, maximum=EIGHT_BIT_MAXIMUM)


def _settings_error(location: str, message: str) -> SettingsError:
    """Return an error about the value at ``location``, keyed by its top-level setting.

    Args:
        location: The path of the value, such as ``table.name[0]``; the part before the
            first ``.`` or ``[`` is the top-level setting that the error carries.
        message: The error message.

    Returns:
        The error, for the caller to raise.
    """
    return SettingsError(message, (re.split(r"[.\[]", location, maxsplit=1)[0],))


# --- Validation kit -----------------------------------------------------------------


def _bound(value: float) -> str:
    """Format a limit as a person writes it: 1048576 and 0.1, never 1.04858e+06 or 5.0."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _quoted(value: object) -> str:
    """Return the ``repr`` of ``value``, cut to ``_MAX_QUOTED_CHARACTERS`` with an ellipsis."""
    text = repr(value)
    return text if len(text) <= _MAX_QUOTED_CHARACTERS else text[: _MAX_QUOTED_CHARACTERS - 1] + "…"


def _reject(name: str, requirement: str) -> NoReturn:
    """Raise an error saying that the setting ``name`` must be ``requirement``.

    Args:
        name: The setting, which the error also carries as its key.
        requirement: What the value must be; completes "name must be ...".

    Raises:
        SettingsError: Always.
    """
    raise SettingsError(f"{name} must be {requirement}", (name,))


def _integer_requirement(bounds: Bounds) -> str:
    """Describe the integers that ``bounds`` accepts, as in "a positive integer"."""
    low, high = bounds.minimum, bounds.maximum
    if low is not None and high is not None:
        return f"an integer from {_bound(low)} to {_bound(high)}"
    if low == 1:
        return "a positive integer"
    if low == 0:
        return "a nonnegative integer"
    if low is not None:
        return f"an integer of at least {_bound(low)}"
    if high is not None:
        return f"an integer of at most {_bound(high)}"
    return "an integer"


def _number_requirement(bounds: Bounds) -> str:
    """Describe the numbers that ``bounds`` accepts, as in "a nonnegative number"."""
    low, high, exclusive = bounds.minimum, bounds.maximum, bounds.exclusive_minimum
    if low is not None and high is not None:
        if exclusive:
            return f"a number greater than {_bound(low)} and at most {_bound(high)}"
        return f"a number from {_bound(low)} to {_bound(high)}"
    if low is not None:
        if low == 0:
            return "a positive number" if exclusive else "a nonnegative number"
        if exclusive:
            return f"a number greater than {_bound(low)}"
        return f"a number of at least {_bound(low)}"
    if high is not None:
        return f"a number of at most {_bound(high)}"
    return "a finite number"


def _check_integer(name: str, value: object, bounds: Bounds, *, allow_none: bool) -> None:
    """Reject anything but an integer within ``bounds``; ``True`` is not an integer.

    Args:
        name: The setting named in the message.
        value: The value to check.
        bounds: The accepted limits.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``value`` is not an integer within ``bounds``.
    """
    if value is None and allow_none:
        return
    if (
        type(value) is not int
        or (bounds.minimum is not None and value < bounds.minimum)
        or (bounds.maximum is not None and value > bounds.maximum)
    ):
        _reject(name, _integer_requirement(bounds))


def _check_number(name: str, value: object, bounds: Bounds, *, allow_none: bool) -> None:
    """Reject anything but a finite integer or float within ``bounds``; not a boolean.

    Args:
        name: The setting named in the message.
        value: The value to check.
        bounds: The accepted limits.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``value`` is not a finite number within ``bounds``.
    """
    if value is None and allow_none:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (bounds.minimum is not None and value < bounds.minimum)
        or (bounds.exclusive_minimum and value == bounds.minimum)
        or (bounds.maximum is not None and value > bounds.maximum)
    ):
        _reject(name, _number_requirement(bounds))


def require_int(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    allow_none: bool = False,
) -> None:
    """Reject anything but an integer within the limits; ``True`` is not an integer.

    Args:
        name: The setting named in the message.
        value: The value to check.
        minimum: The lowest accepted integer, or ``None`` for no lower limit.
        maximum: The highest accepted integer, or ``None`` for no upper limit.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``value`` is not an integer within the limits.
    """
    _check_integer(name, value, Bounds(minimum, maximum), allow_none=allow_none)


def require_number(
    name: str,
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
    allow_none: bool = False,
) -> None:
    """Reject anything but a finite integer or float within the limits; not ``True`` or NaN.

    Args:
        name: The setting named in the message.
        value: The value to check.
        minimum: The lowest accepted number, or ``None`` for no lower limit.
        maximum: The highest accepted number, or ``None`` for no upper limit.
        exclusive_minimum: Whether ``minimum`` itself is rejected.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``value`` is not a finite number within the limits.
    """
    _check_number(name, value, Bounds(minimum, maximum, exclusive_minimum), allow_none=allow_none)


def require_choice(
    name: str, value: object, choices: Iterable[object], *, allow_none: bool = False
) -> None:
    """Reject a value that is not one of ``choices``, such as the arguments of a Literal.

    Types are compared too: neither ``True`` nor ``1`` matches the other.

    Args:
        name: The setting named in the message.
        value: The value to check.
        choices: The accepted values, listed in the message in their given order.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``value`` is not one of ``choices``.
    """
    if value is None and allow_none:
        return
    allowed = tuple(choices)
    if not any(type(value) is type(choice) and value == choice for choice in allowed):
        _reject(name, f"one of {', '.join(map(str, allowed))}")


def require_text(
    name: str,
    value: object,
    *,
    expected: str = "a nonempty string",
    max_length: int | None = None,
    pattern: re.Pattern[str] | None = None,
    allow_blank: bool = False,
    allow_none: bool = False,
) -> None:
    """Reject text that is blank, contains NUL, does not match ``pattern`` or is too long.

    Args:
        name: The setting named in the message.
        value: The value to check; anything but a string is rejected.
        expected: Completes "name must be ..." and describes what the pattern accepts.
        max_length: The most characters the text may have, or ``None`` for no limit.
        pattern: A pattern the whole text must match, or ``None``.
        allow_blank: Whether an empty or all-space string is accepted too.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``value`` is not acceptable text, or it is longer than ``max_length``.
    """
    if value is None and allow_none:
        return
    if (
        not isinstance(value, str)
        or not (allow_blank or value.strip())
        or "\0" in value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _reject(name, expected)
    if max_length is not None and len(value) > max_length:
        _reject(name, f"at most {max_length} characters long")


def _ordered(choices: Iterable[str]) -> tuple[str, ...]:
    """Return the choices in their given order; an unordered set is sorted for a stable message."""
    return tuple(sorted(choices)) if isinstance(choices, (set, frozenset)) else tuple(choices)


def _require_at_most(name: str, items: Sequence[object], max_items: int | None) -> None:
    """Reject more than ``max_items`` entries.

    Args:
        name: The setting named in the message.
        items: The entries of the setting.
        max_items: The most entries allowed, or ``None`` for no limit.

    Raises:
        SettingsError: ``items`` has more than ``max_items`` entries.
    """
    if max_items is not None and len(items) > max_items:
        raise SettingsError(f"{name} may list at most {max_items} entries", (name,))


def _require_tuple(name: str, values: object, kind: str) -> tuple[Any, ...]:
    """Return ``values`` if it is a tuple, which is what a TOML array becomes.

    Args:
        name: The setting named in the message.
        values: The value to check.
        kind: What the entries must be; completes "a list of ...".

    Returns:
        ``values`` itself.

    Raises:
        SettingsError: ``values`` is not a tuple; a list gets a message of its own.
    """
    if isinstance(values, list):
        raise SettingsError(f"{name} must be given as a tuple, not a list", (name,))
    if not isinstance(values, tuple):
        _reject(name, f"a list of {kind}")
    return values


class TextFormat(NamedTuple):
    """What an entry of a string list must look like, and how a message describes that.

    Attributes:
        pattern: The pattern that a whole entry must match.
        description: The accepted entries as a message names them; completes "entry ...
            must be ...", as in "a lowercase entry type".
    """

    pattern: re.Pattern[str]
    description: str


def require_unique_strings(
    name: str,
    values: object,
    *,
    max_items: int | None = None,
    max_length: int | None = None,
    allow_surrounding_whitespace: bool = False,
    choices: Iterable[str] | None = None,
    item_format: TextFormat | None = None,
    allow_none: bool = False,
) -> None:
    """Reject anything but a tuple of distinct nonempty strings, which TOML spells as a list.

    Args:
        name: The setting named in the messages.
        values: The value to check.
        max_items: The most entries allowed, or ``None`` for no limit.
        max_length: The most characters an entry may have, or ``None`` for no limit.
        allow_surrounding_whitespace: Whether an entry may start or end with whitespace.
        choices: A closed set that limits the entries, or ``None``.
        item_format: A pattern that limits the entries, as in "a lowercase entry type", or
            ``None``.
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``values`` is not such a tuple, or an entry breaks one of the limits.
    """
    if values is None and allow_none:
        return
    values = _require_tuple(name, values, "nonempty strings")
    allowed = None if choices is None else _ordered(choices)
    for value in values:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            _reject(name, "a list of nonempty strings")
        if not allow_surrounding_whitespace and value != value.strip():
            raise SettingsError(
                f"{name} entry {_quoted(value)} must not start or end with whitespace", (name,)
            )
        if max_length is not None and len(value) > max_length:
            raise SettingsError(
                f"{name} entries may be at most {max_length} characters long", (name,)
            )
        if allowed is not None and value not in allowed:
            raise SettingsError(
                f"{name} entry {_quoted(value)} must be one of {', '.join(allowed)}", (name,)
            )
        if item_format is not None and item_format.pattern.fullmatch(value) is None:
            raise SettingsError(
                f"{name} entry {_quoted(value)} must be {item_format.description}", (name,)
            )
    _require_at_most(name, values, max_items)
    repeated = sorted({value for value in values if values.count(value) > 1})
    if repeated:
        raise SettingsError(
            f"{name} lists {', '.join(map(_quoted, repeated))} more than once", (name,)
        )


def require_records(
    name: str,
    values: object,
    record_type: type,
    *,
    max_items: int | None = None,
    distinct_by: Callable[[Any], object] | None = None,
    distinct_description: str = "entry",
) -> None:
    """Reject anything but a tuple of ``record_type`` instances, optionally all distinct.

    Args:
        name: The setting named in the messages.
        values: The value to check.
        record_type: The class every entry must be an instance of.
        max_items: The most entries allowed, or ``None`` for no limit.
        distinct_by: Maps a record to what must differ between records, or ``None`` to
            allow repeats.
        distinct_description: Names what ``distinct_by`` returns, for the message.

    Raises:
        SettingsError: ``values`` is not such a tuple, has too many entries, or repeats
            what ``distinct_by`` returns.
    """
    values = _require_tuple(name, values, f"{record_type.__name__} records")
    if any(not isinstance(value, record_type) for value in values):
        _reject(name, f"a list of {record_type.__name__} records")
    _require_at_most(name, values, max_items)
    if distinct_by is not None:
        keys = [distinct_by(value) for value in values]
        if len(set(keys)) != len(keys):
            raise SettingsError(
                f"{name} must not list the same {distinct_description} more than once", (name,)
            )


def require_integers(
    name: str,
    values: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    max_items: int | None = None,
) -> None:
    """Reject anything but a tuple of integers within the limits, such as page numbers.

    Args:
        name: The setting named in the messages; an entry is named ``name[index]``.
        values: The value to check.
        minimum: The lowest accepted integer, or ``None`` for no lower limit.
        maximum: The highest accepted integer, or ``None`` for no upper limit.
        max_items: The most entries allowed, or ``None`` for no limit.

    Raises:
        SettingsError: ``values`` is not a tuple, has too many entries, or holds an entry
            that is not an integer within the limits.
    """
    values = _require_tuple(name, values, "integers")
    _require_at_most(name, values, max_items)
    for index, value in enumerate(values):
        _check_integer(f"{name}[{index}]", value, Bounds(minimum, maximum), allow_none=False)


def require_numbers(
    name: str,
    values: object,
    *,
    count: int,
    minimum: float | None = None,
    maximum: float | None = None,
    description: str = "",
    allow_none: bool = False,
) -> None:
    """Reject anything but a tuple of exactly ``count`` numbers within the limits.

    Args:
        name: The setting named in the messages; an entry is named ``name[index]``.
        values: The value to check.
        count: The number of entries required.
        minimum: The lowest accepted number, or ``None`` for no lower limit.
        maximum: The highest accepted number, or ``None`` for no upper limit.
        description: Names the numbers for the message, as in "(left, top, right, bottom)".
        allow_none: Whether ``None`` is accepted too.

    Raises:
        SettingsError: ``values`` is not a tuple of ``count`` entries, or an entry is not a
            finite number within the limits.
    """
    if values is None and allow_none:
        return
    values = _require_tuple(name, values, f"{count} numbers")
    if len(values) != count:
        _reject(name, f"a list of {count} numbers {description}".rstrip())
    for index, value in enumerate(values):
        _check_number(f"{name}[{index}]", value, Bounds(minimum, maximum), allow_none=False)


# Validates one item of a pair; called with the item's name and value.
ItemCheck = Callable[[str, object], None]


def require_pairs(
    name: str,
    rows: object,
    *,
    first: ItemCheck,
    second: ItemCheck,
    max_items: int | None = None,
    unique_first: bool = True,
) -> None:
    """Reject anything but a tuple of two-item tuples whose items pass ``first`` and ``second``.

    The checks are called as ``check("name[0][1]", item)``, an index path that matches the
    messages of the TOML conversion.

    Args:
        name: The setting named in the messages.
        rows: The value to check.
        first: The check of the first item of every pair.
        second: The check of the second item of every pair.
        max_items: The most pairs allowed, or ``None`` for no limit.
        unique_first: Whether the first items must differ.

    Raises:
        SettingsError: ``rows`` is not a tuple of two-item tuples, has too many pairs, or
            repeats a first item that must be unique. ``first`` and ``second`` raise their
            own errors.
    """
    rows = _require_tuple(name, rows, "two-item lists")
    if any(not isinstance(row, tuple) or len(row) != 2 for row in rows):
        _reject(name, "a list of two-item lists")
    _require_at_most(name, rows, max_items)
    heads: list[object] = []
    for index, (head, tail) in enumerate(rows):
        first(f"{name}[{index}][0]", head)
        second(f"{name}[{index}][1]", tail)
        if unique_first and head in heads:
            raise SettingsError(f"{name} lists {_quoted(head)} more than once", (name,))
        heads.append(head)


@cache
def _scalar_fields(
    kind: type[DataclassInstance],
) -> tuple[tuple[str, str, tuple[object, ...], bool], ...]:
    """Return each bool, int, float and Literal field as (name, tag, choices, optional).

    ``X | None`` counts as ``X`` with ``optional`` set; any other union is left out.
    """
    hints = get_type_hints(kind)
    scalars = []
    for item in fields(kind):
        annotation, optional = hints[item.name], False
        if get_origin(annotation) in {Union, types.UnionType}:
            variants = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(variants) != 1:
                continue
            optional = len(variants) < len(get_args(annotation))
            annotation = variants[0]
        if annotation is bool:
            scalars.append((item.name, "boolean", (), optional))
        elif annotation is int:
            scalars.append((item.name, "integer", (), optional))
        elif annotation is float:
            scalars.append((item.name, "number", (), optional))
        elif get_origin(annotation) is Literal:
            scalars.append((item.name, "choice", get_args(annotation), optional))
    return tuple(scalars)


def validate_scalars(options: DataclassInstance) -> None:
    """Check each boolean, integer, number and Literal field against its annotation.

    ``X | None`` fields may be ``None``. Numbers must lie within the limits that the
    dataclass lists for them in ``FIELD_BOUNDS``; a field without an entry is unbounded.

    Args:
        options: The option dataclass instance to check.

    Raises:
        SettingsError: A field does not match its annotation or its limits.
    """
    kind = type(options)
    limits: Mapping[str, Bounds] = getattr(kind, "FIELD_BOUNDS", {})
    for name, tag, choices, optional in _scalar_fields(kind):
        value = getattr(options, name)
        if tag == "boolean":
            if type(value) is not bool and not (optional and value is None):
                _reject(name, "a boolean (true or false)")
        elif tag == "integer":
            _check_integer(name, value, limits.get(name, Bounds()), allow_none=optional)
        elif tag == "number":
            _check_number(name, value, limits.get(name, Bounds()), allow_none=optional)
        else:
            require_choice(name, value, choices, allow_none=optional)


# --- Unknown names --------------------------------------------------------------------


def closest_names(names: Iterable[str], known: Iterable[str]) -> dict[str, str]:
    """Return the known name that resembles each of ``names``, for the names that resemble one.

    Args:
        names: The names to match.
        known: The valid names.

    Returns:
        The closest known name, by :func:`difflib.get_close_matches`, of each name that
        resembles one.
    """
    candidates = sorted(known)
    return {
        name: close[0]
        for name in names
        if (close := difflib.get_close_matches(name, candidates, n=1))
    }


def with_suggestions(names: Iterable[str], guesses: Mapping[str, str]) -> str:
    """List the names for a message, each followed by "(did you mean ...?)" if ``guesses`` has one.

    Args:
        names: The names to list.
        guesses: The suggestion for some of the names, as :func:`closest_names` returns.

    Returns:
        The names, separated by commas.
    """
    return ", ".join(
        f"{name} (did you mean {guesses[name]}?)" if name in guesses else name for name in names
    )


# --- TOML conversion ------------------------------------------------------------------


def _reject_unknown_keys(table: Mapping[object, object], known: set[str], location: str) -> None:
    """Raise for the keys of ``table`` that are not in ``known``, suggesting the nearest names.

    Args:
        table: The TOML table to check.
        known: The valid keys.
        location: The table as the messages name it.

    Raises:
        SettingsError: ``table`` has a key that is not in ``known``.
    """
    unknown = sorted(map(str, set(table) - known))
    if unknown:
        guesses = closest_names(unknown, known)
        raise _settings_error(
            location,
            f"Unknown {location} setting(s): {with_suggestions(unknown, guesses)}. "
            f"Valid {location} settings: {', '.join(sorted(known))}.",
        )


def read_options(kind: type[T], value: object, location: str) -> T:
    """Convert one TOML table into the option dataclass ``kind``; reuse an instance as is.

    Args:
        kind: The option dataclass.
        value: The TOML table, or an instance of ``kind``.
        location: The table as the messages name it; nested tables extend it with a dot.

    Returns:
        The option object.

    Raises:
        SettingsError: ``value`` is not a table, ``kind`` is not a dataclass, a key is
            unknown, or a value is invalid, including when the dataclass rejects it.
    """
    if isinstance(value, kind):
        return value
    if not isinstance(value, dict):
        raise _settings_error(location, f"{location} must be a settings table")
    if not is_dataclass(kind):
        raise _settings_error(location, f"{location} requires a dataclass option schema")
    _reject_unknown_keys(value, {item.name for item in fields(kind)}, location)
    hints = get_type_hints(kind)
    limits: Mapping[str, Bounds] = getattr(kind, "FIELD_BOUNDS", {})
    converted = {
        name: _convert(hints[name], item, f"{location}.{name}", limits.get(name))
        for name, item in value.items()
    }
    try:
        return kind(**converted)
    except (ValueError, TypeError, PreparationError) as error:
        raise _settings_error(location, f"Invalid {location} settings: {error}") from error


def read_option_groups(kinds: Sequence[type], value: object, location: str) -> tuple[Any, ...]:
    """Convert one TOML table into several option dataclasses that each own some of its keys.

    A module keeps the options of its own feature, but a settings file may write them in a
    table that another feature named. Every key belongs to exactly one of ``kinds``; an
    unknown key is reported against the keys of all of them, as ``read_options`` does for
    one.

    Args:
        kinds: The option dataclasses that share the table.
        value: The TOML table.
        location: The table as the messages name it.

    Returns:
        One option object per kind, in the order of ``kinds``.

    Raises:
        SettingsError: ``value`` is not a table, a kind is not a dataclass, a key is unknown,
            or a value is invalid.
        TypeError: Two of ``kinds`` declare the same option.
    """
    if not isinstance(value, dict):
        raise _settings_error(location, f"{location} must be a settings table")
    owners: dict[str, type] = {}
    for kind in kinds:
        if not is_dataclass(kind):
            raise _settings_error(location, f"{location} requires a dataclass option schema")
        for item in fields(kind):
            if item.name in owners:
                raise TypeError(
                    f"{item.name} is an option of both {owners[item.name].__name__} "
                    f"and {kind.__name__}"
                )
            owners[item.name] = kind
    _reject_unknown_keys(value, set(owners), location)
    return tuple(
        read_options(
            kind, {name: item for name, item in value.items() if owners[name] is kind}, location
        )
        for kind in kinds
    )


def read_top_level_options(kind: type[T], values: Mapping[str, object]) -> T:
    """Convert top-level TOML settings into the option dataclass ``kind`` that holds them.

    Each key is its own location, as in ``abstract_min_words must be ...``.

    Args:
        kind: The option dataclass.
        values: The top-level settings, which the caller has already limited to the keys
            of ``kind``.

    Returns:
        The option object.

    Raises:
        SettingsError: A value is invalid, including when the dataclass rejects it.
    """
    hints = get_type_hints(kind)
    limits: Mapping[str, Bounds] = getattr(kind, "FIELD_BOUNDS", {})
    converted = {
        name: _convert(hints[name], item, name, limits.get(name)) for name, item in values.items()
    }
    try:
        return kind(**converted)
    except SettingsError:
        raise
    except (ValueError, TypeError, PreparationError) as error:
        raise SettingsError(str(error)) from error


def _scalar_error(location: str, check: Callable[[], None]) -> None:
    """Run a kit check on the value at ``location``, naming the top-level setting at fault.

    Args:
        location: The path of the value.
        check: The check to run.

    Raises:
        SettingsError: The check failed; the error carries the top-level setting of
            ``location``.
    """
    try:
        check()
    except SettingsError as error:
        raise _settings_error(location, str(error)) from None


def _convert(annotation: Any, value: object, location: str, bounds: Bounds | None = None) -> object:
    """Convert the TOML ``value`` at ``location`` to what ``annotation`` declares.

    Args:
        annotation: The type of the field or item.
        value: The TOML value.
        location: The path of the value, for the messages.
        bounds: The limits of a numeric value, from the dataclass's ``FIELD_BOUNDS``, or
            ``None``.

    Returns:
        The converted value.

    Raises:
        SettingsError: ``value`` does not fit ``annotation``.
    """
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in {Union, types.UnionType}:
        return _convert_union(args, value, location, bounds)
    if origin is Literal:
        _scalar_error(location, lambda: require_choice(location, value, args))
        return value
    if isinstance(annotation, type) and is_dataclass(annotation):
        return read_options(annotation, value, location)
    if origin is tuple:
        return _convert_tuple(args, value, location)
    return _convert_scalar(annotation, value, location, bounds)


def _convert_union(
    variants: tuple[Any, ...], value: object, location: str, bounds: Bounds | None
) -> object:
    """Convert to ``X | None`` (``None`` stays ``None``) or to the first variant that fits.

    Args:
        variants: The types of the union.
        value: The TOML value.
        location: The path of the value, for the messages.
        bounds: The limits of a numeric value; applied only to ``X | None``.

    Returns:
        The converted value.

    Raises:
        SettingsError: No variant fits ``value``.
    """
    if value is None and type(None) in variants:
        return None
    candidates = [item for item in variants if item is not type(None)]
    if len(candidates) == 1:
        # An optional value keeps its variant's message, such as the allowed choices.
        return _convert(candidates[0], value, location, bounds)
    for variant in candidates:
        try:
            return _convert(variant, value, location)
        except PreparationError:
            pass
    raise _settings_error(location, f"{location} has an invalid value type")


def _named_pairs(args: tuple[Any, ...], value: object, location: str) -> object:
    """Turn a TOML table into sorted (name, value) pairs where the annotation asks for pairs.

    That is named policy pairs such as ``required_fields.article = ["title", "author"]``.

    Args:
        args: The type arguments of the tuple annotation.
        value: The TOML value.
        location: The path of the value, for the messages.

    Returns:
        The sorted pairs, or any other value as it is.

    Raises:
        SettingsError: A key of the table is not a string.
    """
    if not isinstance(value, dict) or len(args) != 2 or args[-1] is not Ellipsis:
        return value
    inner = get_args(args[0])
    if get_origin(args[0]) is tuple and len(inner) == 2 and inner[0] is str:
        if any(not isinstance(key, str) for key in value):
            raise _settings_error(location, f"{location} requires string keys")
        return tuple(sorted(value.items()))
    return value


def _convert_tuple(args: tuple[Any, ...], value: object, location: str) -> object:
    """Convert a TOML list (or table of pairs) to the tuple form ``args`` describes.

    Args:
        args: The type arguments of the tuple annotation.
        value: The TOML value.
        location: The path of the value, for the messages.

    Returns:
        The converted tuple.

    Raises:
        SettingsError: ``value`` is not a list, has the wrong number of items for a
            fixed-length tuple, or holds an invalid item.
    """
    value = _named_pairs(args, value, location)
    if not isinstance(value, (list, tuple)):
        raise _settings_error(location, f"{location} must be a list")
    if len(args) == 2 and args[-1] is Ellipsis:
        return tuple(
            _convert(args[0], item, f"{location}[{index}]") for index, item in enumerate(value)
        )
    if len(args) != len(value):
        raise _settings_error(location, f"{location} requires {len(args)} list items")
    return tuple(
        _convert(kind, item, f"{location}[{index}]")
        for index, (kind, item) in enumerate(zip(args, value, strict=True))
    )


def _convert_scalar(annotation: Any, value: object, location: str, bounds: Bounds | None) -> object:
    """Check a bool, int, float or str against its annotation; other types pass unchanged.

    Args:
        annotation: The type of the field or item.
        value: The TOML value.
        location: The path of the value, for the messages.
        bounds: The limits of a numeric value, or ``None`` for none.

    Returns:
        ``value`` itself.

    Raises:
        SettingsError: ``value`` does not match ``annotation`` or lies outside ``bounds``.
    """
    if annotation is bool and type(value) is not bool:
        raise _settings_error(location, f"{location} must be a boolean")
    if annotation is int:
        _scalar_error(
            location,
            lambda: _check_integer(location, value, bounds or Bounds(), allow_none=False),
        )
    if annotation is float:
        _scalar_error(
            location, lambda: _check_number(location, value, bounds or Bounds(), allow_none=False)
        )
    if annotation is str and not isinstance(value, str):
        raise _settings_error(location, f"{location} must be a string")
    return value

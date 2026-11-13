r"""The warnings chktex printed, read strictly from the output format Fledge gives it.

Fledge asks chktex for one line per warning: ``TOKEN|number|line|column|kind|message``.
``TOKEN`` is new for every lint, so text that a project makes chktex print, such as a
``UserWarn`` pattern with a new line in it, cannot pass for a warning. A line counts only
with the token, numbers in range and a kind chktex uses; every other line that is not
empty is counted as unreadable. The message is the last field, so a ``|`` in it is kept,
and it is shortened and made one safe line. Reading is linear in the size of the output,
and keeps a bounded number of warnings while counting all of them.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from .text_safety import escape_single_line_text

FIELD_SEPARATOR = "|"
# The kinds chktex prints for ``%k``; ``-e`` and ``-m`` turn a warning into the others.
_KINDS = frozenset({"Warning", "Error", "Message"})
# chktex numbers its warnings from 1; three digits leave room for versions to come.
_MAX_NUMBER_DIGITS = 3
# Lines and columns of a source file within the source size limit.
_MAX_POSITION_DIGITS = 9
# The characters of a message that a finding keeps.
MAX_MESSAGE_CHARACTERS = 300
_FIELDS = 6


def new_run_token() -> str:
    """Return a random token that marks the warning lines of one lint."""
    return secrets.token_hex(8)


def output_format(token: str) -> str:
    """Return the ``-f`` format that makes chktex print each warning on one line for ``token``.

    The line holds the warning number, line, column, kind and message, in that order.
    """
    return FIELD_SEPARATOR.join((token, "%n", "%l", "%c", "%k", "%m")) + "\n"


@dataclass(frozen=True, slots=True)
class ChktexWarning:
    """One warning chktex printed.

    Attributes:
        number: chktex's number of the warning, which ``% chktex N`` names.
        line: The line of the warning, counted from 1.
        column: The column chktex gives for it.
        kind: ``Warning``, ``Error`` or ``Message``, as chktex's settings make it.
        message: chktex's message, shortened and on one safe line.
    """

    number: int
    line: int
    column: int
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class ChktexOutput:
    """The warnings of one chktex run.

    Attributes:
        warnings: The first warnings, at most as many as were asked for.
        total: How many warnings chktex printed, including those not kept.
        unreadable_lines: How many lines were neither empty nor a warning.
    """

    warnings: tuple[ChktexWarning, ...]
    total: int
    unreadable_lines: int


def _number(text: str, digits: int, smallest: int) -> int | None:
    """Return the decimal number ``text`` when it has at most ``digits`` digits and is large enough.

    Args:
        text: The field.
        digits: The most digits the field may have.
        smallest: The smallest value accepted.

    Returns:
        The number, or ``None`` when the field is not one.
    """
    if not (0 < len(text) <= digits and text.isascii() and text.isdecimal()):
        return None
    value = int(text)
    return value if value >= smallest else None


def _message(text: str) -> str:
    """Shorten a message to ``MAX_MESSAGE_CHARACTERS`` and show its unsafe characters."""
    shortened = text if len(text) <= MAX_MESSAGE_CHARACTERS else text[:MAX_MESSAGE_CHARACTERS] + "…"
    return escape_single_line_text(shortened.strip())


def read_warning(line: str, token: str) -> ChktexWarning | None:
    """Read one line of chktex's output, or return ``None`` when it is not a warning line.

    Args:
        line: The line, without its line break.
        token: The token of the lint.

    Returns:
        The warning, or ``None``.
    """
    fields = line.split(FIELD_SEPARATOR, _FIELDS - 1)
    if len(fields) != _FIELDS or fields[0] != token or fields[4] not in _KINDS:
        return None
    number = _number(fields[1], _MAX_NUMBER_DIGITS, 1)
    position = _number(fields[2], _MAX_POSITION_DIGITS, 1)
    column = _number(fields[3], _MAX_POSITION_DIGITS, 0)
    if number is None or position is None or column is None:
        return None
    return ChktexWarning(number, position, column, fields[4], _message(fields[5]))


def read_output(stdout: str, token: str, *, keep: int) -> ChktexOutput:
    """Read every warning line of one run's standard output.

    Args:
        stdout: What chktex printed on its standard output.
        token: The token of the lint.
        keep: The most warnings to keep; the rest are only counted.

    Returns:
        The kept warnings in chktex's order, how many there were, and how many lines could
        not be read.
    """
    warnings: list[ChktexWarning] = []
    total = unreadable = 0
    for line in stdout.split("\n"):
        if not line:
            continue
        warning = read_warning(line, token)
        if warning is None:
            unreadable += 1
            continue
        total += 1
        if len(warnings) < keep:
            warnings.append(warning)
    return ChktexOutput(tuple(warnings), total, unreadable)

r"""The names that a definition builds at run time instead of writing them out.

``\expandafter\def\csname tabfn@#1\endcsname{…}``, the kernel's ``\@namedef{name}`` and
etoolbox's ``\csdef{name}`` define a command whose name is text, so the definitions that
:func:`tex_spans.defined_names` reads do not list it. The manuscript policies
(``manuscript_policy_kernel``) need to know which names such a definition may define and
where its replacement text lies, to tell a command of the class or a package from a macro
of the paper that may typeset a label or a caption.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from .tex_commands import PRIMITIVE_BODY_DEFINITIONS
from .tex_lexer import Command, read_group
from .tex_spans import command_end

# A macro parameter in a replacement text: #1 to #9.
PARAMETER = re.compile(r"#([1-9])")


class BuiltName(NamedTuple):
    r"""A name that a definition builds instead of writing it as a control sequence.

    Attributes:
        pattern: The names the definition may define, matched against whole names.
        literal: The name itself when it is written out, as in ``\csdef{figlabel}``.
        body: The span of the replacement text when the definition takes one (``\def``
            or kin applied to ``\csname``, ``\@namedef``, ``\csdef``) and only parameters
            stand in front of the text.
    """

    pattern: re.Pattern[str]
    literal: str | None
    body: tuple[int, int] | None


# Definitions that take the name as text: the kernel's \@namedef{name} and etoolbox's
# \csdef{name}, \cslet{name} and kin.
_NAMED_DEFINITIONS = frozenset(
    {"@namedef", "csdef", "csgdef", "csedef", "csxdef", "cslet", "csletcs"}
)
# Those of them that copy another command instead of taking a replacement text.
_NAMED_ALIASES = frozenset({"cslet", "csletcs"})
_ANY_NAME = re.compile(".*", re.DOTALL)
# How many distinct patterns of built names are kept; beyond that, any name may be built.
_MAX_BUILT_PATTERNS = 64


def built_names(text: str, commands: Sequence[Command]) -> list[BuiltName]:
    r"""Return the names that definitions among ``commands`` build instead of writing them.

    ``\expandafter\def\csname tabfn@#1\endcsname`` may define any name that starts with
    ``tabfn@``; a name built from other commands may be any name. ``\@namedef{name}`` and
    etoolbox's ``\csdef{name}`` define the name they are given.

    Args:
        text: The text the commands were scanned from.
        commands: The commands of ``text``, in order.

    Returns:
        One entry per such definition.
    """
    names = []
    for index, command in enumerate(commands):
        body = None
        if command.name in _NAMED_DEFINITIONS:
            built = command.arguments[0].value if command.arguments else "\\"
            if command.arguments and command.name not in _NAMED_ALIASES:
                body = _replacement_span(text, command.arguments[0].end + 1)
        elif command.name == "csname" and command.named_by:
            following = commands[index + 1] if index + 1 < len(commands) else None
            closed = following is not None and following.name == "endcsname"
            built = text[command_end(command) : following.start] if closed else "\\"
            if closed and command.named_by in PRIMITIVE_BODY_DEFINITIONS:
                body = _replacement_span(text, following.name_end)
        else:
            continue
        built = built.strip()
        if "\\" in built or "{" in built:
            names.append(BuiltName(_ANY_NAME, None, body))
            continue
        parts = PARAMETER.split(built)[::2]
        pattern = re.compile(".*".join(re.escape(part) for part in parts), re.DOTALL)
        names.append(BuiltName(pattern, built if len(parts) == 1 else None, body))
    return names


def _replacement_span(text: str, pos: int) -> tuple[int, int] | None:
    r"""Return the span of the replacement text that follows a ``\def``'s name at ``pos``.

    TeX reads the parameter text up to the first brace; the span is read only when that
    text holds nothing but parameters, so that no delimiter is mistaken for the text.
    """
    brace = text.find("{", pos)
    if brace < 0 or text[pos:brace].strip(" \t\r\n#0123456789"):
        return None
    group = read_group(text, brace)
    return (group.start, group.end) if group else None


def patterns(names: Sequence[BuiltName]) -> tuple[re.Pattern[str], ...]:
    """Return the distinct patterns of built names, or one for any name beyond the bound."""
    patterns = {name.pattern.pattern: name.pattern for name in names}
    if len(patterns) > _MAX_BUILT_PATTERNS or _ANY_NAME.pattern in patterns:
        return (_ANY_NAME,)
    return tuple(patterns.values())

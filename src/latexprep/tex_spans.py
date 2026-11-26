r"""Offset spans of the lexical subset: definitions, conditionals and where commands run.

The lexer (``tex_lexer``) reads commands and their groups; this module answers
questions about ranges of the scanned text. Which commands a definition body or a
``\if…``/``\fi`` conditional encloses, whether an offset lies in such a span, and
whether a command runs where it stands rather than in the argument of a macro that
may drop or repeat it. Every reader of source text shares these answers.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from .tex_commands import (
    ARGUMENT_SPECIFICATION_DEFINITIONS,
    COMMAND_DEFINITION_COMMANDS,
    DEFINITION_COMMANDS,
    ENVIRONMENT_DEFINITION_COMMANDS,
    MACRO_DEFINITION_COMMANDS,
    NAMED_OPERAND_COUNTS,
    PRIMITIVE_BODY_DEFINITIONS,
    REGISTER_DEFINITIONS,
    TYPESET_ARGUMENT_COMMANDS,
)
from .tex_lexer import CONTROL_SEQUENCE, Argument, Command, end_of_line, read_group

# Package conditionals that take braced branches instead of ending with \fi.
ARGUMENT_CONDITIONALS = frozenset(
    {
        "ifthenelse",
        "iftoggle",
        "ifbool",
        "ifboolexpr",
        "ifdef",
        "ifundef",
        "ifdefempty",
        "ifdefvoid",
        "ifdefstring",
        "ifcsdef",
        "ifcsundef",
        "ifcsvoid",
        "ifstrequal",
        "ifstrempty",
        "ifblank",
        "ifnumcomp",
        "ifdimcomp",
        "ifpackageloaded",
        "ifclassloaded",
    }
)

# The longest replacement text read for one definition; a longer one is not read, so the
# name stays unexpanded and the reading stays bounded however definitions nest.
_MAX_MACRO_BODY = 4096
# The longest parameter text read between a defined name and its body, as in
# \def\x#1#2{…}; a longer one is not read, so the name stays unexpanded.
_MAX_PARAMETER_TEXT = 256
# Commands nested deeper than this many brace groups are not placed: no real manuscript
# nests so deeply, and the walk over the groups stays bounded.
MAX_PLACED_DEPTH = 64
# Primitive conditionals whose test reads text, numbers or boxes after the opener, so the
# first branch has no lexical start.
_TEXT_TEST_CONDITIONALS = frozenset(
    "if ifcat ifnum ifdim ifodd ifcase ifvoid ifhbox ifvbox ifeof ifcsname ifincsname "
    "iffontchar".split()
)


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort ``(start, end)`` spans and join those that overlap or touch."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def command_end(command: Command) -> int:
    """Return the offset just after a command's last recorded argument or option.

    A command without arguments or options ends at the end of its name.
    """
    return max(
        (argument.end + 1 for argument in (*command.arguments, *command.options)),
        default=command.name_end,
    )


def _definition_end(text: str, command: Command) -> int:
    r"""Return the offset after a definition's name, parameters and replacement body.

    A command outside ``DEFINITION_COMMANDS`` ends at :func:`command_end`. For the
    primitive-style definitions in it, the defined name (two names for ``\let`` and
    ``\futurelet``) and a braced body that opens on the same line are included.

    Args:
        text: The text the command was scanned from.
        command: The definition command.

    Returns:
        The offset just after the braced body when one opens on the same line and closes,
        otherwise just after the last defined name read, or :func:`command_end` for a
        command outside ``DEFINITION_COMMANDS``.
    """
    stop = command_end(command)
    if command.name not in DEFINITION_COMMANDS:
        return stop
    # Definitions written with TeX primitives take their name and replacement text
    # without braces around the name, so the lexer records no arguments for them.
    line_end = end_of_line(text, stop)
    cursor = stop
    # \let\a\b and \def\x#1{…} place the defined name outside any group.
    for _ in range(2 if command.name in {"let", "futurelet"} else 1):
        token = CONTROL_SEQUENCE.match(text, cursor)
        if token is None:
            break
        cursor = stop = token.end()
    opening = text.find("{", cursor, line_end)
    if opening >= 0:
        body = read_group(text, opening)
        if body is not None:
            return body.end + 1
    return stop


def definition_spans(text: str, commands: Sequence[Command]) -> list[tuple[int, int]]:
    r"""Return merged offset ranges covering definition commands and their bodies.

    Args:
        text: The text the commands were scanned from.
        commands: The commands of ``text``; those in ``MACRO_DEFINITION_COMMANDS`` count.

    Returns:
        The merged ``(start, end)`` ranges, from each definition command to the end of its
        body. ``\renewcommand\x[1]{…}`` names ``\x`` without braces, so the lexer records
        the parameters and body as the groups of ``\x``; the span then ends after them.
    """
    spans = []
    for index, command in enumerate(commands):
        if command.name not in MACRO_DEFINITION_COMMANDS:
            continue
        end = _definition_end(text, command)
        following = commands[index + 1] if index + 1 < len(commands) else None
        if (
            command.name in COMMAND_DEFINITION_COMMANDS
            and following is not None
            and following.named_by == command.name
        ):
            # The parameter options and the first group, the body; a later group is text.
            body = following.arguments[:1]
            end = max([end, *(item.end + 1 for item in (*following.options, *body))])
        spans.append((command.start, end))
    return merge_spans(spans)


class MacroDefinition(NamedTuple):
    r"""One definition of a command or environment, and where its replacement text lies.

    Attributes:
        name: The defined command without its backslash (``capslot``), or the environment
            as ``begin{name}``, the owner of its groups (see ``tex_lexer.Group``).
        start: The offset where the replacement text, or the aliased token, begins.
        end: The offset just after it.
        parameters: How many braced arguments a use reads: ``\newcommand{\x}[2]`` reads
            two, and one with an optional first argument (``[2][default]``) one.
    """

    name: str
    start: int
    end: int
    parameters: int = 0


def macro_definitions(text: str, commands: Sequence[Command]) -> list[MacroDefinition]:
    r"""Return the commands and environments that the definitions in ``commands`` define.

    ``\newcommand{\x}[1]{body}``, ``\renewcommand\x{body}`` and ``\def\x#1{body}``
    define ``x`` with that body, which for ``\def`` is the first brace group after the
    parameter text, as TeX reads it; ``\let\x\y`` defines ``x`` as the token ``\y``.
    ``\newenvironment{e}{begin}{end}`` and tcolorbox's ``\newtcolorbox{e}{options}``
    define ``begin{e}`` with their remaining arguments, and ``\newlength{\x}`` and
    ``\newsavebox{\x}`` define the register ``x`` with no replacement text. A definition
    whose name or body
    the lexer cannot delimit is left out, so the name stays unexpanded.

    Args:
        text: The text the commands were scanned from.
        commands: The commands of ``text``, in order.

    Returns:
        The definitions, in source order.
    """
    return [
        definition
        for index in range(len(commands))
        if (definition := _definition_at(text, commands, index)) is not None
    ]


def _definition_at(text: str, commands: Sequence[Command], index: int) -> MacroDefinition | None:
    r"""Return what ``commands[index]`` defines, or ``None`` when it defines nothing readable.

    xparse definitions (``\NewDocumentCommand{\x}{m}{body}``), whose second group is an
    argument specification, and ``\def`` with delimited parameters are not read.

    Args:
        text: The text the commands were scanned from.
        commands: The commands of ``text``, in order.
        index: The index of the command that may define something.

    Returns:
        The definition, as :func:`macro_definitions` describes it.
    """
    command = commands[index]
    following = commands[index + 1] if index + 1 < len(commands) else None
    named = following if following is not None and following.named_by == command.name else None
    arguments = command.arguments
    if command.name in ENVIRONMENT_DEFINITION_COMMANDS and len(arguments) > 1:
        name = f"begin{{{arguments[0].value.strip()}}}"
        return MacroDefinition(
            name, arguments[1].start, arguments[-1].end, _parameter_count(command.options)
        )
    if command.name in REGISTER_DEFINITIONS and arguments:
        # \newlength{\x} makes \x a register; using it prints nothing itself.
        token = CONTROL_SEQUENCE.fullmatch(arguments[0].value.strip())
        return MacroDefinition(token.group(1), command.start, command.start) if token else None
    if named is None or command.name in ARGUMENT_SPECIFICATION_DEFINITIONS:
        return None
    if command.name in COMMAND_DEFINITION_COMMANDS:
        return _command_definition(command, named)
    if command.name == "let":
        alias = commands[index + 2] if index + 2 < len(commands) else None
        # \global\let\x\x, which TikZ code uses to export a value, changes nothing.
        if alias is not None and alias.named_by == "let" and alias.name != named.name:
            return MacroDefinition(named.name, alias.start, alias.name_end)
        return None
    if command.name in PRIMITIVE_BODY_DEFINITIONS:
        return _primitive_definition(text, named)
    return None


def _command_definition(command: Command, named: Command) -> MacroDefinition | None:
    r"""Read ``\newcommand{\x}[n]{body}`` or ``\renewcommand\x[n]{body}``.

    Args:
        command: The definition command.
        named: The defined name.

    Returns:
        The definition, or ``None`` without a body.
    """
    if named.arguments:
        # Unbraced, \renewcommand\x[1]{body}: the lexer gives \x the groups.
        body, options = named.arguments[0], named.options
    elif len(command.arguments) > 1:
        body, options = command.arguments[1], command.options
    else:
        return None
    return MacroDefinition(named.name, body.start, body.end, _parameter_count(options))


def _primitive_definition(text: str, named: Command) -> MacroDefinition | None:
    r"""Read ``\def\x#1#2{body}``, whose body is the first brace group after the name.

    Args:
        text: The scanned text.
        named: The defined name.

    Returns:
        The definition, or ``None`` when its parameters are delimited or its body is not
        found within the bounds.
    """
    opening = text.find("{", named.name_end, named.name_end + _MAX_PARAMETER_TEXT)
    if opening < 0:
        return None
    parameters = text[named.name_end : opening]
    # Only undelimited parameters, #1#2: a delimited one may read any text up to its end.
    if parameters.replace(" ", "").replace("#", "").strip("123456789"):
        return None
    body = read_group(text, opening, limit=_MAX_MACRO_BODY)
    if body is None:
        return None
    return MacroDefinition(named.name, body.start, body.end, parameters.count("#"))


def _parameter_count(options: Sequence[Argument]) -> int:
    """Return how many braced arguments a use of a ``[n][default]`` definition reads."""
    if not options or not options[0].value.strip().isdigit():
        return 0
    count = int(options[0].value.strip())
    return max(0, count - 1) if len(options) > 1 else count


def conditional_spans(commands: list[Command]) -> list[tuple[int, int]]:
    r"""Return offset ranges enclosed by primitive-style ``\if…`` … ``\fi`` conditionals.

    A conditional name defined by ``\newif`` or ``\def`` is not itself executed,
    and an opener without a matching ``\fi`` (such as a branch-taking package
    conditional) is ignored rather than extended to the end of the file.

    Args:
        commands: The commands of a text, in order.

    Returns:
        The merged ``(start, end)`` ranges, from the offset of an ``\if…`` to the offset of
        its ``\fi``.
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for command in commands:
        if command.named:
            continue
        if command.name == "fi":
            if stack:
                start = stack.pop()
                spans.append((start, command.start))
        elif opens_conditional(command):
            stack.append(command.start)
    return merge_spans(spans)


def opens_conditional(command: Command) -> bool:
    r"""Return whether a command opens a primitive-style ``\if…`` … ``\fi`` conditional.

    A conditional that a definition or test only names, and a package conditional that
    takes braced branches (``\ifthenelse{…}{…}{…}``), do not.
    """
    return (
        not command.named
        and command.name.startswith("if")
        and command.name not in ARGUMENT_CONDITIONALS
    )


def is_source_terminator(command: Command) -> bool:
    r"""Return whether the command may stop its file or the document."""
    return not command.named and (command.name == "endinput" or command.ends("document"))


def stops_source(command: Command, conditionals: Sequence[tuple[int, int]]) -> bool:
    """Return whether a literal source terminator stands outside an uncertain context."""
    return (
        is_source_terminator(command)
        and not command.depth
        and not in_conditional(conditionals, command.start)
    )


def conditional_branches(
    commands: Sequence[Command], index: int
) -> tuple[list[tuple[int, int]], int] | None:
    r"""Return the branches of the conditional that ``commands[index]`` opens, and its end.

    A branch starts after the test: after the opener for a switch such as
    ``\ifcameraready`` or ``\iftrue``, after the tokens it names for ``\ifx`` and
    ``\ifdefined``. A test that reads text or numbers, such as ``\ifnum``, has no
    known end, so its branches are not returned.

    Args:
        commands: The commands of a text, in order.
        index: The index of the opening command.

    Returns:
        The ``(start, end)`` offsets of the branch before ``\else`` and of the one after
        it, if any, and the offset just after the matching ``\fi``; ``None`` for a test
        with operands that are not control sequences, an ``\or`` branch or a missing
        ``\fi``.
    """
    opener = commands[index]
    if opener.name in _TEXT_TEST_CONDITIONALS:
        return None
    position, depth, branches = index + 1, 0, []
    while position < len(commands) and commands[position].named_by == opener.name:
        position += 1
    # \ifx\a X compares a character the lexer does not see: its branch has no known start.
    if position - index - 1 != NAMED_OPERAND_COUNTS.get(opener.name, 0):
        return None
    start = commands[position - 1].name_end
    for command in commands[position:]:
        if opens_conditional(command):
            depth += 1
        elif command.name == "fi" and depth:
            depth -= 1
        elif command.name == "or" and not depth:
            return None
        elif command.name in {"else", "fi"} and not depth:
            branches.append((start, command.start))
            if command.name == "fi":
                return branches, command.name_end
            start = command.name_end
    return None


def runs_in_place(
    command: Command,
    definitions: Sequence[tuple[int, int]],
    defined: frozenset[str] = frozenset(),
) -> bool:
    r"""Return whether a command runs where it stands, once, as text outside any group does.

    That holds when every brace group around it is a bare group, an argument of one of
    ``TYPESET_ARGUMENT_COMMANDS`` or a group after ``\begin`` of an environment, as in
    ``\textbf{\cite{x}}`` or ``\subfloat{\includegraphics{a}}``, and no definition body
    encloses it. A command or environment that the project defines (``defined``) may drop,
    repeat or delay its arguments, as an unknown macro may, and so may a command nested
    deeper than ``MAX_PLACED_DEPTH`` groups.

    Args:
        command: The command to place.
        definitions: The merged :func:`definition_spans` of its source.
        defined: The commands and environments the project defines (:func:`defined_names`).

    Returns:
        Whether the command runs in place.
    """
    if command.depth > MAX_PLACED_DEPTH or in_half_open_span(definitions, command.start):
        return False
    group = command.group
    while group is not None:
        owner = group.owner
        if owner is not None and (
            owner in defined
            or not (owner in TYPESET_ARGUMENT_COMMANDS or owner.startswith("begin{"))
        ):
            return False
        group = group.parent
    return True


def defined_names(commands: Iterable[Command]) -> set[str]:
    r"""Return the commands and environments that definitions among ``commands`` define.

    A command is named as ``sout`` for ``\renewcommand{\sout}``, ``\def\sout`` or
    ``\let\sout``; an environment as ``begin{hideme}``, the owner of its groups (see
    ``tex_lexer.Group``). Names a conditional test only compares are not defined.

    Args:
        commands: The commands of one or more sources.

    Returns:
        The defined names.
    """
    names = set()
    for command in commands:
        if command.named_by in MACRO_DEFINITION_COMMANDS:
            names.add(command.name)
        elif "environment" in command.name.casefold() and command.arguments:
            names.add(f"begin{{{command.arguments[0].value.strip()}}}")
    return names


# The point-in-span tests below share one lookup over merged, sorted spans but
# differ in whether each boundary belongs to the span; callers depend on that.


def _span_starting_at_or_before(
    spans: Sequence[tuple[int, int]], offset: int
) -> tuple[int, int] | None:
    """Return the last span that starts at or before ``offset``.

    Args:
        spans: Merged, sorted ``(start, end)`` spans.
        offset: The offset to look up.

    Returns:
        The span, or ``None`` when every span starts after ``offset``.
    """
    index = bisect.bisect_right(spans, (offset, float("inf"))) - 1
    return spans[index] if index >= 0 else None


def in_conditional(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    r"""Return whether ``offset`` lies strictly inside one of the merged ``conditional_spans``.

    The opening ``\if…`` (at a span's start) and the closing ``\fi`` (at its end)
    are outside, so only enclosed commands count as conditional.

    Args:
        spans: The merged spans from :func:`conditional_spans`.
        offset: The offset to test.

    Returns:
        ``True`` when ``start < offset < end`` for one of the spans.
    """
    span = _span_starting_at_or_before(spans, offset)
    return span is not None and span[0] < offset < span[1]


def in_half_open_span(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    """Return whether ``start <= offset < end`` for one of the merged ``spans``.

    Args:
        spans: Merged, sorted ``(start, end)`` spans.
        offset: The offset to test.

    Returns:
        ``True`` when one of the spans holds ``offset``, its end excluded.
    """
    span = _span_starting_at_or_before(spans, offset)
    return span is not None and span[0] <= offset < span[1]


def in_closed_span(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    """Return whether ``start <= offset <= end`` for one of the merged ``spans``.

    Args:
        spans: Merged, sorted ``(start, end)`` spans.
        offset: The offset to test.

    Returns:
        ``True`` when one of the spans holds ``offset``, its end included.
    """
    span = _span_starting_at_or_before(spans, offset)
    return span is not None and span[0] <= offset <= span[1]

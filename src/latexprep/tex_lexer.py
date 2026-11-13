"""Bounded lexical subset of LaTeX shared by source inspection, checks and transformations.

This is not a TeX tokenizer. It recognizes control sequences, balanced brace and
bracket groups, comments and common verbatim-like regions without expanding
macros or tracking category codes. Offsets always refer to the scanned text, and
masking keeps every offset and line break in place.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, NamedTuple

from .cancellation import cancellation_point
from .limits import CANCELLATION_STRIDE_CHARS, KIB, MIB
from .tex_commands import (
    COMMAND_DEFINITION_COMMANDS,
    NAMED_OPERAND_COUNTS,
    PRIMITIVE_BODY_DEFINITIONS,
    STRINGIFYING_COMMANDS,
)

# Bounds shared by every reader of the lexical subset.
MAX_SOURCE_BYTES = 8 * MIB
MAX_TOTAL_SOURCE_BYTES = 32 * MIB
MAX_COMMANDS = 100_000
MAX_ARGUMENT_CHARS = 64 * KIB
# Groups read after one command: its brace arguments and bracket options together.
_MAX_GROUPS_PER_COMMAND = 6
# Arguments may together span this many times the scanned text, because commands nest.
_ARGUMENT_BUDGET_FACTOR = 4

# The owner of a brace group after ``<…>`` or ``(…)``: an argument of a command with
# delimited arguments, such as beamer's ``\only<2>{…}``, never a bare group.
DELIMITED_OWNER = "<delimited>"
# A control word, with ``@`` as a letter as in package code, or a control symbol
# on the same line.
CONTROL_SEQUENCE = re.compile(r"\\([A-Za-z@]+|[^\r\n])")
# The file name after a bare ``\input name``, which has no braces.
_BARE_INPUT_NAME = re.compile(r"[^\s\\{}\[\]]+")
# Environments whose body is read verbatim up to the matching \end.
VERBATIM_ENVIRONMENTS = frozenset(
    {
        "verbatim",
        "verbatim*",
        "Verbatim",
        "BVerbatim",
        "LVerbatim",
        "lstlisting",
        "minted",
        "comment",
    }
)
# The ``\end`` that closes each verbatim environment, spaces allowed as TeX allows them.
_VERBATIM_END = {
    name: re.compile(r"\\end\s*\{\s*" + re.escape(name) + r"\s*\}")
    for name in VERBATIM_ENVIRONMENTS
}
# Commands whose argument is read verbatim, either braced or between a repeated delimiter.
_VERBATIM_COMMANDS = frozenset({"verb", "lstinline", "mintinline", "url", "nolinkurl", "path"})
# The url package's \path takes a braced or ``|``-delimited argument. TikZ shares the name
# for \path[options] (a) -- (b), whose arguments are ordinary markup, not literal text.
_URL_PATH_OPENERS = "{|"


ParseLimit = Literal[
    "unclosed_group",
    "oversized_group",
    "argument_budget",
    "command_count",
    "expanded_size",
]

_PARSE_LIMIT_DESCRIPTIONS: dict[ParseLimit, str] = {
    "unclosed_group": "a brace group is never closed",
    "oversized_group": f"a brace group is longer than {MAX_ARGUMENT_CHARS // KIB} KiB",
    "argument_budget": "the command arguments together exceed the supported total size",
    "command_count": f"the text has more than {MAX_COMMANDS:,} commands",
    "expanded_size": f"the expanded text is larger than {MAX_TOTAL_SOURCE_BYTES // MIB} MiB",
}


class ParseLimitExceeded(Exception):
    """The lexical subset cannot establish bounded, complete argument spans.

    The message describes the limit and the character offset at which it was reached.

    Attributes:
        reason: The name of the limit that was reached.
        offset: The character offset in the scanned text where the limit was reached, so a
            caller can report the line.
    """

    def __init__(self, reason: ParseLimit, offset: int = 0) -> None:
        super().__init__(f"{_PARSE_LIMIT_DESCRIPTIONS[reason]} (at character {offset})")
        self.reason = reason
        self.offset = offset


@dataclass(frozen=True, slots=True)
class Argument:
    """A brace or bracket group of the scanned text.

    Attributes:
        start: The offset at which ``value`` begins, just inside the opening delimiter.
        end: The offset just after ``value``, that of the closing delimiter.
        value: The text inside the delimiters.
    """

    start: int
    end: int
    value: str

    def stripped(self) -> Argument:
        """Return the value without surrounding whitespace, with offsets that still match it.

        Returns:
            A new argument whose ``value`` is stripped and whose ``start`` and ``end`` delimit
            the stripped value in the scanned text.
        """
        start = self.start + len(self.value) - len(self.value.lstrip())
        value = self.value.strip()
        return Argument(start, start + len(value), value)


@dataclass(frozen=True, slots=True)
class Group:
    r"""One open brace group and the group around it, shared by the commands inside.

    The owner is the command whose argument the group is: ``textbf`` for
    ``\textbf{…}``, ``begin{figure}`` for a group after ``\begin{figure}``, the
    definition (``def``, ``renewcommand``) for the parameters and body of a defined name,
    and ``None`` for a bare group. ``DELIMITED_OWNER`` marks a group after ``<…>`` or
    ``(…)``, which a command with such delimited arguments takes.

    Attributes:
        owner: The command that takes the group, or ``None``.
        parent: The group around this one, or ``None`` at the top level.
        depth: How many groups are open inside and including this one.
    """

    owner: str | None
    parent: Group | None
    depth: int


@dataclass(frozen=True, slots=True)
class Command:
    r"""A control sequence with the groups that directly follow it.

    The lexer cannot know a command's signature, so every following brace group
    is recorded as a possible argument and every bracket group as a possible option.

    Attributes:
        name: The control-sequence name without the backslash.
        start: The offset of the backslash.
        line: The 1-based line of the backslash.
        depth: The number of brace groups open at the command, never below zero.
        arguments: The brace groups that follow the command, in order.
        options: The bracket groups that follow the command, in order.
        group: The innermost open brace group, linked to the groups around it, or
            ``None`` at the top level; :attr:`groups` lists their owners.
        named_by: The definition or conditional test that names this control sequence
            instead of running it, as ``def`` in ``\def\x`` or ``ifx`` in ``\ifx\x\y``;
            ``None`` for a command that runs.
    """

    name: str
    start: int
    line: int
    depth: int
    arguments: tuple[Argument, ...]
    options: tuple[Argument, ...]
    group: Group | None = None
    named_by: str | None = None

    @property
    def groups(self) -> tuple[str | None, ...]:
        """The owner of each open brace group, outermost first (see :class:`Group`)."""
        owners = []
        group = self.group
        while group is not None:
            owners.append(group.owner)
            group = group.parent
        return tuple(reversed(owners))

    @property
    def named(self) -> bool:
        """Whether a definition or conditional test names this token instead of running it."""
        return self.named_by is not None

    @property
    def name_end(self) -> int:
        """Offset just after the control-sequence name, before any star or group."""
        return self.start + len(self.name) + 1

    def begins(self, environment: str, *, exact: bool = False) -> bool:
        r"""Return whether this is ``\begin{environment}``.

        Args:
            environment: The environment name to compare with the first argument.
            exact: Whether spaces around the name inside the braces count; they are ignored
                unless it is set.

        Returns:
            ``True`` when the command is ``\begin`` and its first argument names
            ``environment``.
        """
        return self.name == "begin" and self._names_environment(environment, exact=exact)

    def ends(self, environment: str, *, exact: bool = False) -> bool:
        r"""Return whether this is ``\end{environment}``; ``exact`` as for :meth:`begins`.

        Args:
            environment: The environment name to compare with the first argument.
            exact: Whether spaces around the name inside the braces count.

        Returns:
            ``True`` when the command is ``\end`` and its first argument names ``environment``.
        """
        return self.name == "end" and self._names_environment(environment, exact=exact)

    def _names_environment(self, environment: str, *, exact: bool) -> bool:
        """Return whether the command's first argument is the name ``environment``.

        A command without arguments names no environment.

        Args:
            environment: The environment name to compare with.
            exact: Whether to compare the argument as written instead of stripped of spaces.

        Returns:
            ``True`` when the first argument equals ``environment``.
        """
        if not self.arguments:
            return False
        value = self.arguments[0].value
        return (value if exact else value.strip()) == environment


RegionKind = Literal["comment", "literal"]


class Region(NamedTuple):
    """A comment from ``%`` to the end of its line, or a verbatim-like literal span.

    Attributes:
        start: The offset at which the region begins.
        end: The offset just after the region; a comment's ``end`` excludes the line
            terminator, including a carriage return.
        kind: ``"comment"`` or ``"literal"``.
    """

    start: int
    end: int
    kind: RegionKind


class RegionScan(NamedTuple):
    """The comment and literal regions of a text, and the literal regions that never close.

    Attributes:
        regions: The comment and literal regions, in the order they occur.
        unclosed: The offsets of the literal regions without a closing delimiter; they
            extend to the end of the text (or line), so later commands cannot be inspected
            reliably.
    """

    regions: list[Region]
    unclosed: list[int]


def skip_space(text: str, pos: int) -> int:
    """Return the first offset at or after ``pos`` that is not whitespace.

    Args:
        text: The text to scan.
        pos: The offset to start at.

    Returns:
        The offset of the first non-whitespace character, or the text's length.
    """
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def skip_trivia(text: str, pos: int) -> int:
    """Skip whitespace and whole comment lines.

    Args:
        text: The text to scan.
        pos: The offset to start at.

    Returns:
        The offset of the first character that is not whitespace and does not start a
        comment, or the text's length. A comment is skipped up to and including its line
        feed.
    """
    while True:
        pos = skip_space(text, pos)
        if pos < len(text) and text[pos] == "%":
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end + 1
        else:
            return pos


def _brace_depth_after(char: str, depth: int) -> int:
    """Return the brace nesting depth once ``char`` has been read, for bracket groups.

    Args:
        char: The character just read.
        depth: The brace nesting depth before ``char``.

    Returns:
        ``depth`` plus one after ``{``, minus one after ``}``, and unchanged otherwise.
    """
    if char == "{":
        return depth + 1
    if char == "}":
        return depth - 1
    return depth


def read_group(
    text: str,
    pos: int,
    opening: str = "{",
    closing: str = "}",
    *,
    limit: int = MAX_ARGUMENT_CHARS,
) -> Argument | None:
    """Read a balanced group starting at ``pos``.

    A bracket group ignores brackets nested inside braces, as in ``[{a]}]``.

    Args:
        text: The text to read from.
        pos: The offset of the opening delimiter.
        opening: The character that opens the group.
        closing: The character that closes the group.
        limit: The largest number of characters the group may span; a longer group counts
            as oversized.

    Returns:
        The group with its value and offsets, or ``None`` if it is absent, unclosed or
        oversized.
    """
    if pos >= len(text) or text[pos] != opening:
        return None
    depth, cursor = 1, pos + 1
    brace_depth = 0
    while cursor < len(text) and cursor - pos <= limit:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if opening == "[":
            brace_depth = _brace_depth_after(char, brace_depth)
            if brace_depth:
                cursor += 1
                continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return Argument(pos + 1, cursor, text[pos + 1 : cursor])
        cursor += 1
    return None


def _is_url_path_form(text: str, pos: int) -> bool:
    r"""Return whether the argument after ``\path`` at ``pos`` is the url package's literal form.

    That form opens with ``{`` or ``|`` after any whitespace and comment lines.

    Args:
        text: The text to scan.
        pos: The offset just after ``\path``.

    Returns:
        ``True`` when the first character after the whitespace and comments is ``{`` or
        ``|``.
    """
    opener = skip_trivia(text, pos)
    return opener < len(text) and text[opener] in _URL_PATH_OPENERS


class _LiteralEnd(NamedTuple):
    """The end of a verbatim-like region.

    Attributes:
        end: The offset just after the region.
        closed: Whether the region was closed by its own delimiter.
    """

    end: int
    closed: bool


def _verbatim_environment(text: str, pos: int) -> _LiteralEnd | None:
    r"""Find the end of a verbatim environment whose ``\begin`` command ends at ``pos``.

    Args:
        text: The text to scan.
        pos: The offset just after the ``\begin`` control sequence.

    Returns:
        Where the environment ends (after its ``\end``) and whether it was closed; an
        unclosed one runs to the end of the text. ``None`` when the command does not begin
        a verbatim environment.
    """
    environment = read_group(text, skip_trivia(text, pos))
    closing_pattern = _VERBATIM_END.get(environment.value.strip()) if environment else None
    if environment is None or closing_pattern is None:
        return None
    closing = closing_pattern.search(text, environment.end + 1)
    return _LiteralEnd(closing.end(), True) if closing else _LiteralEnd(len(text), False)


def _skip_verbatim_options(text: str, pos: int, command: str) -> int:
    r"""Skip the optional argument (and a ``\mintinline`` language) before a literal argument.

    Args:
        text: The text to scan.
        pos: The offset after the command name and its star, if any.
        command: The name of the command whose argument follows.

    Returns:
        The offset at which the literal argument is expected.
    """
    cursor = skip_trivia(text, pos)
    option = read_group(text, cursor, "[", "]")
    if option:
        cursor = skip_trivia(text, option.end + 1)
    if command == "mintinline":
        language = read_group(text, cursor)
        if language:
            cursor = skip_trivia(text, language.end + 1)
    return cursor


def _verbatim_command(text: str, name: str, pos: int) -> _LiteralEnd | None:
    r"""Find the end of the literal argument of ``\verb``, ``\url`` and kin.

    The argument is braced, or runs to the next copy of its first character on the line;
    ``\verb`` takes only the delimited form.

    Args:
        text: The text to scan.
        name: The name of the command.
        pos: The offset at which the command's name ends.

    Returns:
        Where the argument ends and whether it was closed; an unclosed argument runs to
        the end of the text (braced) or of the line (delimited). ``None`` when no argument
        follows, because the text ends or whitespace comes next.
    """
    cursor = pos + 1 if text.startswith("*", pos) else pos
    if name != "verb":
        cursor = _skip_verbatim_options(text, cursor, name)
    if cursor >= len(text) or text[cursor].isspace():
        return None
    if text[cursor] == "{" and name != "verb":
        argument = read_group(text, cursor)
        return _LiteralEnd(argument.end + 1, True) if argument else _LiteralEnd(len(text), False)
    line_end = end_of_line(text, cursor)
    close = text.find(text[cursor], cursor + 1, line_end)
    return _LiteralEnd(close + 1, True) if close >= 0 else _LiteralEnd(line_end, False)


def _literal_after(text: str, name: str, pos: int) -> _LiteralEnd | None:
    """Find the literal region that the control sequence ``name``, ending at ``pos``, opens.

    Args:
        text: The text to scan.
        name: The control-sequence name without the backslash.
        pos: The offset just after the control sequence.

    Returns:
        Where the region ends and whether it was closed, or ``None`` when ``name`` opens no
        literal region here.
    """
    if name == "begin":
        return _verbatim_environment(text, pos)
    if name in _VERBATIM_COMMANDS and (name != "path" or _is_url_path_form(text, pos)):
        return _verbatim_command(text, name, pos)
    return None


def end_of_line(text: str, pos: int) -> int:
    """Return the offset of the line break that ends the line holding ``pos``, or the text's end.

    Args:
        text: The text to scan.
        pos: An offset on the line.

    Returns:
        The offset of the next line break at or after ``pos``, or ``len(text)`` when there
        is none.
    """
    end = text.find("\n", pos)
    return len(text) if end < 0 else end


def source_regions(text: str) -> RegionScan:
    """Return comment and literal regions and unclosed literal offsets, without TeX expansion."""
    regions: list[Region] = []
    unclosed: list[int] = []
    pos = 0
    while pos < len(text):
        if pos % CANCELLATION_STRIDE_CHARS == 0:
            cancellation_point()
        if text[pos] == "%":
            end = end_of_line(text, pos)
            regions.append(Region(pos, end - (text[end - 1 : end] == "\r"), "comment"))
            pos = end
            continue
        token = CONTROL_SEQUENCE.match(text, pos) if text[pos] == "\\" else None
        if token is None:
            pos += 1
            continue
        literal = _literal_after(text, token.group(1), token.end())
        if literal is None:
            pos = token.end()
            continue
        regions.append(Region(pos, literal.end, "literal"))
        if not literal.closed:
            unclosed.append(pos)
        pos = literal.end
    return RegionScan(regions, unclosed)


def blank_regions(text: str, regions: Iterable[Region]) -> str:
    """Replace every character inside ``regions`` except line breaks with a space.

    Args:
        text: The text to blank.
        regions: The regions to blank, with offsets into ``text``.

    Returns:
        ``text`` with the regions blanked; offsets and line breaks are unchanged.
    """
    buffer = list(text)
    for start, end, _ in regions:
        buffer[start:end] = [char if char in "\r\n" else " " for char in buffer[start:end]]
    return "".join(buffer)


def mask_literals(text: str) -> str:
    """Blank comments and common literal regions while retaining offsets/newlines."""
    return blank_regions(text, source_regions(text).regions)


def _has_blank_line(text: str, start: int, end: int, comments: Sequence[int]) -> bool:
    """Return whether ``text[start:end]``, a gap of whitespace, contains a blank line.

    A blank line ends a paragraph. A line that only held a comment does not: the
    comment swallows its line break, so TeX reads straight through it.

    Args:
        text: The text that holds the gap.
        start: The offset at which the gap begins.
        end: The offset just after the gap.
        comments: The sorted offsets at which the comments of the original text begin.

    Returns:
        ``True`` when a line that starts and ends inside the gap holds no comment.
    """
    line_start = text.find("\n", start, end) + 1
    if line_start == 0:
        return False
    while (line_end := text.find("\n", line_start, end)) >= 0:
        index = bisect.bisect_left(comments, line_start)
        if index == len(comments) or comments[index] >= line_end:
            return True
        line_start = line_end + 1
    return False


def _unreadable_group_reason(text: str, pos: int) -> ParseLimit:
    """Return why the brace group at ``pos`` cannot be read: longer than the bound, or never closed.

    Args:
        text: The text that holds the group.
        pos: The offset of the opening brace.

    Returns:
        ``"oversized_group"`` when the group closes once the bound is lifted, otherwise
        ``"unclosed_group"``.
    """
    return "oversized_group" if read_group(text, pos, limit=len(text)) else "unclosed_group"


def comment_starts(regions: Iterable[Region]) -> list[int]:
    """Return the sorted offsets at which the comments among ``regions`` begin."""
    return [region.start for region in regions if region.kind == "comment"]


def _bare_input_name(masked: str, pos: int) -> list[Argument]:
    r"""Return the file name of ``\input name``, written without braces, as a one-item list.

    Args:
        masked: The literal-masked text.
        pos: The offset just after ``\input``.

    Returns:
        The name as the only argument, or an empty list when no name follows.
    """
    start = skip_space(masked, pos)
    match = _BARE_INPUT_NAME.match(masked, start)
    return [Argument(start, match.end(), match.group())] if match else []


class _Groups(NamedTuple):
    """The groups that follow a command.

    Attributes:
        arguments: The brace groups, in order.
        options: The bracket groups, in order.
    """

    arguments: list[Argument]
    options: list[Argument]


def _read_following_groups(masked: str, cursor: int, comments: Sequence[int]) -> _Groups:
    """Read the brace groups (arguments) and bracket groups (options) that follow a command.

    Reading stops at a paragraph break (see ``_has_blank_line``), at text that opens no
    group, or after ``_MAX_GROUPS_PER_COMMAND`` groups. An unmatched bracket is ordinary
    text and only ends the command's groups.

    Args:
        masked: The literal-masked text.
        cursor: The offset just after the command name and its star, if any.
        comments: The sorted offsets of the comments blanked to produce ``masked``.

    Returns:
        The arguments and options, each in order.

    Raises:
        ParseLimitExceeded: A brace group that follows the command is unclosed or longer
            than the bound, which leaves every later argument span unreadable.
    """
    arguments: list[Argument] = []
    options: list[Argument] = []
    for _ in range(_MAX_GROUPS_PER_COMMAND):
        skipped = skip_space(masked, cursor)
        # A paragraph break ends argument scanning: the next group starts
        # new text instead of completing this command.
        if _has_blank_line(masked, cursor, skipped, comments):
            break
        cursor = skipped
        option = read_group(masked, cursor, "[", "]")
        argument = read_group(masked, cursor)
        if option:
            options.append(option)
            cursor = option.end + 1
        elif argument:
            arguments.append(argument)
            cursor = argument.end + 1
        else:
            # An unbalanced brace leaves every later argument span
            # unreadable, but an unmatched bracket is ordinary text such
            # as "[0, 1)" and only ends this command's arguments.
            if cursor < len(masked) and masked[cursor] == "{":
                raise ParseLimitExceeded(_unreadable_group_reason(masked, cursor), cursor)
            break
    return _Groups(arguments, options)


def scan_commands(masked: str, comments: Sequence[int] = ()) -> list[Command]:
    """Return the commands of literal-masked text with their following groups.

    Without ``comments`` a line that held only a comment is taken for a blank line, which
    ends argument scanning.

    Args:
        masked: The text with its comments and literal regions blanked.
        comments: The sorted offsets of the comments blanked to produce ``masked``.

    Returns:
        The commands in the order they occur.

    Raises:
        ParseLimitExceeded: An unbalanced brace leaves later argument spans unreadable, the
            arguments exceed their bounded total size, or the text holds more than
            ``MAX_COMMANDS`` commands.
    """
    result: list[Command] = []
    state = _ScanState(masked, comments)
    pos = 0
    line = 1
    argument_chars = 0
    argument_budget = min(MAX_SOURCE_BYTES, len(masked) * _ARGUMENT_BUDGET_FACTOR)
    while pos < len(masked):
        if pos % CANCELLATION_STRIDE_CHARS == 0:
            cancellation_point()
        char = masked[pos]
        if char == "\n":
            line += 1
        elif char == "{":
            state.open_group(pos)
        elif char == "}":
            state.close_group(pos)
        elif char == "\\" and (token := CONTROL_SEQUENCE.match(masked, pos)):
            name_end = token.end()
            command = _read_command(masked, token.group(1), pos, line, comments, state)
            argument_chars += sum(
                len(group.value) for group in (*command.arguments, *command.options)
            )
            if argument_chars > argument_budget:
                raise ParseLimitExceeded("argument_budget", pos)
            command = state.command(command, name_end + masked.startswith("*", name_end))
            if command is not None:
                result.append(command)
            if len(result) > MAX_COMMANDS:
                raise ParseLimitExceeded("command_count", pos)
            pos = name_end
            continue
        pos += 1
    return result


def _read_command(
    masked: str, name: str, pos: int, line: int, comments: Sequence[int], state: _ScanState
) -> Command:
    """Read the command named ``name`` at ``pos`` with its following groups.

    Args:
        masked: The literal-masked text.
        name: The control-sequence name.
        pos: The offset of the backslash.
        line: The 1-based line of the backslash.
        comments: The sorted offsets of the comments blanked to produce ``masked``.
        state: The scan state, whose open groups give the command's depth.

    Returns:
        The command with its arguments and options, before naming and groups are added.
    """
    name_end = pos + len(name) + 1
    groups = _read_following_groups(masked, name_end + masked.startswith("*", name_end), comments)
    arguments = groups.arguments
    if not arguments and name == "input":
        arguments.extend(_bare_input_name(masked, name_end))
    return Command(name, pos, line, state.depth, tuple(arguments), tuple(groups.options))


class _ScanState:
    r"""What :func:`scan_commands` tracks between commands: open groups and named tokens.

    Every open brace group is recorded with the command whose argument it is, so that a
    reader can tell ``\textbf{\cite{x}}`` from the argument of an unknown macro. Groups
    are linked to their parent, so each costs constant memory however deep it is. A
    token that a definition or conditional test names is marked, the groups that follow
    a defined name belong to its definition, and a token that ``STRINGIFYING_COMMANDS``
    turn into characters is dropped.

    Args:
        masked: The literal-masked text being scanned.
        comments: The sorted offsets of the comments blanked to produce ``masked``.

    Attributes:
        group: The innermost open brace group, or ``None`` at the top level.
    """

    def __init__(self, masked: str, comments: Sequence[int]) -> None:
        self._masked = masked
        self._comments = comments
        self.group: Group | None = None
        # The offset of the opening brace of each argument not yet entered, by owner.
        self._argument_braces: dict[int, str] = {}
        # Where the next named token must start, how many remain, and what names them.
        self._operand_at = -1
        self._operands_left = 0
        self._naming = ""
        self._stringified_at = -1
        # The definition whose body the next brace group opens: TeX reads the parameter
        # text of \def\x#1 up to the first brace, across line breaks.
        self._body_of = ""

    @property
    def depth(self) -> int:
        """How many brace groups are open."""
        return self.group.depth if self.group else 0

    def open_group(self, pos: int) -> None:
        """Enter the brace group that opens at ``pos``."""
        owner = self._argument_braces.pop(pos, None)
        if self._body_of:
            owner, self._body_of = self._body_of, ""
        elif owner is None and self._after_delimited_argument(pos):
            owner = DELIMITED_OWNER
        self.group = Group(owner, self.group, self.depth + 1)

    def close_group(self, pos: int) -> None:
        """Leave the group that the brace at ``pos`` closes; a stray brace changes nothing.

        A brace group right after an argument, beyond those the lexer records for its
        command, is another argument of the same command.
        """
        if self.group is None:
            return
        owner, self.group = self.group.owner, self.group.parent
        following = skip_space(self._masked, pos + 1)
        if (
            owner is not None
            and self._masked.startswith("{", following)
            and following not in self._argument_braces
            and not _has_blank_line(self._masked, pos + 1, following, self._comments)
        ):
            self._argument_braces[following] = owner

    def _after_delimited_argument(self, pos: int) -> bool:
        r"""Return whether the group at ``pos`` follows ``<…>`` or ``(…)``, as ``\only<2>{…}``."""
        before = pos - 1
        while before >= 0 and self._masked[before] in " \t":
            before -= 1
        return before >= 0 and self._masked[before] in ">)"

    def command(self, command: Command, token_end: int) -> Command | None:
        """Complete one scanned command, or return ``None`` for a stringified token.

        Args:
            command: The command as read, without its groups or naming.
            token_end: The offset just after its name and star.

        Returns:
            The command with its enclosing group and what names it, if anything.
        """
        if command.start == self._stringified_at:
            self._stringified_at = -1
            return None
        named = command.start == self._operand_at and self._operands_left > 0
        named_by = self._naming if named else None
        self._operands_left = self._operands_left - 1 if named else 0
        self._operand_at = self._after_operand(token_end) if self._operands_left else -1
        if not named:
            self._expect_operands(command, token_end)
        owner = command.name
        if named_by is not None:
            # The parameters and body of a defined name belong to its definition.
            owner = named_by
            if named_by in PRIMITIVE_BODY_DEFINITIONS:
                self._body_of = named_by
        elif command.name == "begin" and command.arguments:
            owner = f"begin{{{command.arguments[0].value.strip()}}}"
        for argument in command.arguments:
            self._argument_braces[argument.start - 1] = owner
        return replace(command, group=self.group, named_by=named_by)

    def _expect_operands(self, command: Command, token_end: int) -> None:
        """Note the tokens that ``command`` names or stringifies, if it is such a command."""
        following = skip_space(self._masked, token_end)
        if command.name in STRINGIFYING_COMMANDS:
            # Before a blank line, \string turns the paragraph break into characters.
            if not _has_blank_line(self._masked, token_end, following, self._comments):
                self._stringified_at = following
            return
        count = NAMED_OPERAND_COUNTS.get(command.name, 0)
        if not count:
            return
        self._operands_left = count
        self._naming = command.name
        if command.name in COMMAND_DEFINITION_COMMANDS and command.arguments:
            # \newcommand{\x}: the defined name is alone in the first brace group.
            self._operands_left = 1
            self._operand_at = command.arguments[0].stripped().start
        else:
            self._operand_at = following

    def _after_operand(self, token_end: int) -> int:
        r"""Return where the next named token may start; ``\let\a=\b`` allows an ``=``."""
        pos = skip_space(self._masked, token_end)
        if self._masked.startswith("=", pos):
            pos = skip_space(self._masked, pos + 1)
        return pos


def scan_source_commands(text: str) -> list[Command]:
    """Return the commands of source text, masking its comments and literal regions first.

    Args:
        text: The TeX source.

    Returns:
        The commands in the order they occur.

    Raises:
        ParseLimitExceeded: As :func:`scan_commands` raises it.
    """
    regions = source_regions(text).regions
    return scan_commands(blank_regions(text, regions), comment_starts(regions))


def split_argument_items(argument: Argument, *, comma_separated: bool = True) -> list[Argument]:
    """Return the stripped items of a comma-separated argument, or the whole stripped argument.

    Args:
        argument: The argument to split.
        comma_separated: Whether to split the argument at commas; otherwise it is returned
            whole.

    Returns:
        The items, each stripped and with offsets that still match it.
    """
    if not comma_separated:
        return [argument.stripped()]
    items: list[Argument] = []
    cursor = argument.start
    for value in argument.value.split(","):
        items.append(Argument(cursor, cursor + len(value), value).stripped())
        cursor += len(value) + 1
    return items

r"""A project's ``.chktexrc``: read it within bounds and refuse what is unsafe for chktex.

chktex reads a ``.chktexrc`` from its working folder. Fledge copies a project's
configuration there only after reading it the way chktex 1.7 reads it (``ReadWord``,
``MapChars`` and ``ProcessRC`` in chktex's ``Resource.c``) and finding nothing that could
make chktex read another file, write a file, report under another file name, replace the
output format Fledge reads, or stop before it checks anything. Those effects come from
options in the ``CmdLine`` setting (``-l``, ``-S``, ``-o``, ``-p``, ``-V``, ``-d``, ``-g``,
``-h``, ``-i``, ``-W``) and from folders outside the project in ``TeXInputs``. So
``CmdLine`` may hold only options from a short list, which choose the warnings chktex
gives, each written on its own; and ``TeXInputs`` only folders inside the project.
Fledge turns chktex's own ``\input`` following off, so ``TeXInputs`` never takes effect
in its runs anyway.

The reader follows chktex's grammar closely enough that both see the same settings, and
refuses whatever the two might read differently: NUL and other control characters, lines
long enough for chktex to split, a quoted value left open, an escape that chktex rejects
or turns into NUL, and every syntax error. Each step is linear in the size of the file.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import NoReturn

from .file_safety import FileTooLargeError, byte_count_text, read_regular_file
from .limits import KIB

# The name chktex looks for in its working folder, as Fledge looks for it beside the main file.
CONFIGURATION_NAME = ".chktexrc"
# chktex's own configuration is about 24 KiB; a project's is a fraction of that.
MAX_CONFIGURATION_BYTES = 64 * KIB
# chktex reads a configuration into a 4096-byte line buffer and takes the rest of a longer
# line as a line of its own. Well below that, chktex and this reader see the same lines.
MAX_LINE_CHARACTERS = 1000
# What C's isspace accepts among the characters a line may hold.
_WHITESPACE = frozenset(" \t\r")
# The control characters a line may hold: the tab, and the carriage return of a CRLF file.
_ALLOWED_CONTROLS = frozenset("\t\r")
_ASCII_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_QUOTE = '"'
_ESCAPE = "!"
_COMMENT = "#"
# The characters an escape names directly; ``!n`` is a new line, ``!!`` the escape itself.
_SIMPLE_ESCAPES = {
    '"': '"',
    "!": "!",
    "#": "#",
    "n": "\n",
    "r": "\r",
    "b": "\b",
    "t": "\t",
    "f": "\f",
    "{": "{",
    "}": "}",
    "[": "[",
    "]": "]",
    "=": "=",
    " ": " ",
}
# Digits of the numeric escapes: ``!xHH``, ``!dDDD`` and ``!OOO``.
_HEX_DIGITS = "0123456789abcdefABCDEF"
_DECIMAL_DIGITS = "0123456789"
_OCTAL_DIGITS = "01234567"
_LARGEST_CHARACTER = 255
_BYTE_ORDER_MARK = b"\xef\xbb\xbf"
_EDIT_FORMAT = "Correct it as the chktexrc format in the chktex manual describes."


class _Token(StrEnum):
    """The tokens of a chktex configuration; each value is how a refusal names it.

    Attributes:
        END: The end of the file.
        OPEN: ``{``, which opens a list.
        CLOSE: ``}``, which closes a list.
        BRACKET_OPEN: ``[``, which opens a case-insensitive list.
        BRACKET_CLOSE: ``]``, which closes a case-insensitive list.
        EQUAL: ``=``, which replaces a setting's value instead of adding to it.
        WORD: The name of a setting.
        ITEM: A value, or an item of a list.
    """

    END = "the end of the file"
    OPEN = "`{`"
    CLOSE = "`}`"
    BRACKET_OPEN = "`[`"
    BRACKET_CLOSE = "`]`"
    EQUAL = "`=`"
    WORD = "a setting name"
    ITEM = "a value"


# chktex reads these characters as structure only where it expects one of the tokens
# they belong with; elsewhere they start an ordinary value.
_AFTER_NAME = {"{": _Token.OPEN, "[": _Token.BRACKET_OPEN, "=": _Token.EQUAL}
_LIST_END = {"}": _Token.CLOSE, "]": _Token.BRACKET_CLOSE}
_AFTER_NAME_TOKENS = frozenset(_AFTER_NAME.values())
_LIST_END_TOKENS = frozenset(_LIST_END.values())
_OPENERS = {_Token.OPEN: _Token.CLOSE, _Token.BRACKET_OPEN: _Token.BRACKET_CLOSE}


@dataclass(frozen=True, slots=True)
class _Setting:
    """What one chktex setting accepts.

    Attributes:
        name: The setting's name as chktex spells it; chktex compares it without case.
        has_list: Whether the setting takes a ``{ ... }`` list.
        has_case_list: Whether the setting takes a ``[ ... ]`` case-insensitive list.
        has_value: Whether the setting takes a single value after ``=``.
    """

    name: str
    has_list: bool = False
    has_case_list: bool = False
    has_value: bool = False


# chktex 1.7's settings (``RESOURCE_INFO`` in ``Resource.h``). chktex stops reading a
# configuration at any other name, so an unknown one is refused rather than guessed at.
_SETTINGS = {
    setting.name.lower(): setting
    for setting in (
        *(
            _Setting(name, has_list=True, has_case_list=True)
            for name in ("Silent", "Abbrev", "UserWarn")
        ),
        *(
            _Setting(name, has_list=True)
            for name in (
                "Linker",
                "IJAccent",
                "Italic",
                "ItalCmd",
                "PostLink",
                "WipeArg",
                "VerbEnvir",
                "MathEnvir",
                "MathCmd",
                "TextCmd",
                "MathRoman",
                "HyphDash",
                "NumDash",
                "WordDash",
                "DashExcpt",
                "CenterDots",
                "LowDots",
                "OutFormat",
                "Primitives",
                "NotPreSpaced",
                "NonItalic",
                "NoCharNext",
                "CmdLine",
                "TeXInputs",
                "UserWarnRegex",
                "TextEnvir",
            )
        ),
        *(
            _Setting(name, has_value=True)
            for name in ("VerbClear", "QuoteStyle", "TabSize", "CmdSpaceStyle")
        ),
    )
}
_COMMAND_LINE = "cmdline"
_SEARCH_FOLDERS = "texinputs"

# The options CmdLine may hold, each written on its own: they choose which warnings chktex
# gives and how it reads the document, or set an output format that Fledge's own options
# replace. A warning number or ``all`` follows -w, -e, -m and -n.
_WARNING_CHOICE = r"(?:[0-9]{1,3}|[Aa][Ll][Ll])"
_ACCEPTED_OPTION = re.compile(
    "|".join(
        (
            r"-[Lqr]",
            r"-[xHIbt][01]?",
            r"-v[0-9]?",
            rf"-[wemn]{_WARNING_CHOICE}",
            r"--(?:nolinesupp|quiet|reset)",
            r"--(?:wipeverb|headererr|inputfiles|backup|tictoc)(?:=[01])?",
            r"--verbosity(?:=[0-9])?",
            rf"--(?:warnon|erroron|msgon|nowarn)={_WARNING_CHOICE}",
            r"-[fs](?s:.)+",
            r"--(?:format|splitchar)=(?s:.)*",
        )
    )
)
_WARNING_CHOICE_ARGUMENT = re.compile(_WARNING_CHOICE)
# Options whose argument may be the next item: a warning number or ``all``, or any text.
_TAKES_WARNING_CHOICE = frozenset(
    {"-w", "-e", "-m", "-n", "--warnon", "--erroron", "--msgon", "--nowarn"}
)
_TAKES_ANY_ARGUMENT = frozenset({"-f", "-s", "--format", "--splitchar"})
# chktex's long options by the short option they stand for, to name an abbreviated one.
_LONG_OPTIONS = {
    "help": "h",
    "localrc": "l",
    "output": "o",
    "warnon": "w",
    "erroron": "e",
    "msgon": "m",
    "nowarn": "n",
    "nolinesupp": "L",
    "verbosity": "v",
    "pipeverb": "V",
    "debug": "d",
    "reset": "r",
    "set": "S",
    "quiet": "q",
    "license": "i",
    "splitchar": "s",
    "format": "f",
    "pseudoname": "p",
    "inputfiles": "I",
    "backup": "b",
    "globalrc": "g",
    "wipeverb": "x",
    "tictoc": "t",
    "headererr": "H",
    "version": "W",
}
# What each refused option would do in Fledge's run.
_REFUSED_OPTIONS = {
    "l": "reads another configuration file, possibly one outside the project",
    "S": "applies settings that Fledge cannot check",
    "o": "writes chktex's report to a file instead of passing it to Fledge",
    "p": "reports the warnings under another file name",
    "V": "replaces the output format that Fledge reads",
    "d": "prints chktex's internal settings beside its warnings",
    "g": "drops chktex's built-in settings, on which Fledge's run relies",
    "h": "stops chktex before it checks anything",
    "i": "stops chktex before it checks anything",
    "W": "stops chktex before it checks anything",
}
_ACCEPTED_OPTIONS_TEXT = (
    "-wN, -eN, -mN or -nN (N a warning number or all), -L, -H, -x, -I, -q, -r, -b, -t, "
    "-v, -f and -s, each written on its own"
)


@dataclass(frozen=True, slots=True)
class ConfigurationRefusal:
    """Why Fledge does not let chktex read a project's ``.chktexrc``.

    Attributes:
        line: The line of the file that holds the problem, or ``None`` for the whole file.
        reason: What the file does, as a clause that follows its name.
        remedy: What to change in the file.
    """

    line: int | None
    reason: str
    remedy: str


@dataclass(frozen=True, slots=True)
class ProjectConfiguration:
    """The ``.chktexrc`` beside a main file, as Fledge found and judged it.

    Attributes:
        path: The project-relative name of the file Fledge looked for.
        content: The file's bytes, which chktex may read; ``None`` when there is no file
            or it is refused.
        refusal: Why the file is refused, or ``None``.
    """

    path: str
    content: bytes | None = None
    refusal: ConfigurationRefusal | None = None


class _ConfigurationRefused(Exception):
    """Stops reading a configuration at its first refused construct.

    Args:
        refusal: Why the configuration is refused.
    """

    def __init__(self, refusal: ConfigurationRefusal) -> None:
        super().__init__(refusal.reason)
        self.refusal = refusal


def _ascii_lower(character: str) -> str:
    """Lower an ASCII letter as C's ``tolower`` does in the C locale; keep anything else."""
    return character.lower() if character in _ASCII_LETTERS else character


def _configuration_lines(text: str) -> list[str]:
    """Split a configuration into the lines chktex reads, each without trailing white space.

    Args:
        text: The configuration, decoded one character per byte.

    Returns:
        The lines.

    Raises:
        _ConfigurationRefused: A line is longer than ``MAX_LINE_CHARACTERS`` or holds a
            control character other than a tab or a carriage return.
    """
    lines = text.split("\n")
    for number, line in enumerate(lines, start=1):
        if len(line) > MAX_LINE_CHARACTERS:
            raise _ConfigurationRefused(
                ConfigurationRefusal(
                    number,
                    f"has a line longer than {MAX_LINE_CHARACTERS} characters, which chktex "
                    "would read as several lines",
                    "Break the line into shorter ones.",
                )
            )
        unusual = next(
            (
                character
                for character in line
                if (character < " " or character == "\x7f") and character not in _ALLOWED_CONTROLS
            ),
            None,
        )
        if unusual is not None:
            raise _ConfigurationRefused(
                ConfigurationRefusal(
                    number,
                    f"holds the control character U+{ord(unusual):04X}, which chktex and "
                    "Fledge could read differently",
                    "Remove the control character; write an escape such as !n instead.",
                )
            )
    return [line.rstrip(" \t\r") for line in lines]


class _ConfigurationReader:
    """chktex's tokenizer (``ReadWord``) over the lines of one configuration.

    Args:
        lines: The lines, as :func:`_configuration_lines` returns them.
    """

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = lines
        self._index = -1
        self._text = ""
        self._position = 0

    @property
    def line(self) -> int:
        """The number of the line the reader is on, counted from 1."""
        return max(self._index + 1, 1)

    def refuse(self, reason: str, remedy: str = _EDIT_FORMAT) -> NoReturn:
        """Stop reading with a refusal at the current line.

        Args:
            reason: What the file does, as a clause that follows its name.
            remedy: What to change in the file.

        Raises:
            _ConfigurationRefused: Always.
        """
        raise _ConfigurationRefused(ConfigurationRefusal(self.line, reason, remedy))

    def token(self, expected: frozenset[_Token]) -> tuple[_Token, str]:
        """Read the next token, as chktex reads it when it expects one of ``expected``.

        Args:
            expected: The tokens the parser accepts next; they decide whether a brace,
                bracket or ``=`` is structure, and whether letters form a setting name.

        Returns:
            The token and its text: the name of a setting, a value with its escapes
            replaced, or the structure character.
        """
        while True:
            if self._position >= len(self._text):
                if not self._next_line():
                    return _Token.END, ""
                continue
            while self._position < len(self._text) and self._text[self._position] in _WHITESPACE:
                self._position += 1
            if self._position >= len(self._text) or self._text[self._position] == _COMMENT:
                # chktex ignores the rest of a line from a comment character on.
                self._position = len(self._text)
                continue
            return self._token_at(self._text[self._position], expected)

    def _next_line(self) -> bool:
        """Move to the next line; return whether there was one."""
        if self._index + 1 >= len(self._lines):
            return False
        self._index += 1
        self._text = self._lines[self._index]
        self._position = 0
        return True

    def _token_at(self, character: str, expected: frozenset[_Token]) -> tuple[_Token, str]:
        """Read the token that starts with ``character`` at the current position.

        Args:
            character: The first character of the token.
            expected: The tokens the parser accepts next.

        Returns:
            The token and its text.
        """
        if character == _QUOTE:
            return _Token.ITEM, self._quoted_value()
        if character in _AFTER_NAME and expected & _AFTER_NAME_TOKENS:
            self._position += 1
            return _AFTER_NAME[character], character
        if character in _LIST_END and expected & _LIST_END_TOKENS:
            self._position += 1
            return _LIST_END[character], character
        if _Token.WORD in expected:
            return _Token.WORD, self._name()
        return _Token.ITEM, self._value()

    def _name(self) -> str:
        """Read a setting name: the ASCII letters at the current position.

        Raises:
            _ConfigurationRefused: No letter starts here.
        """
        start = self._position
        while self._position < len(self._text) and self._text[self._position] in _ASCII_LETTERS:
            self._position += 1
        if self._position == start:
            self.refuse(f"holds `{self._text[start]}` where a setting name belongs")
        return self._text[start : self._position]

    def _value(self) -> str:
        """Read an unquoted value, which ends at white space or at a comment.

        chktex ignores the rest of the line from a comment character on, even inside a
        value.
        """
        characters: list[str] = []
        while self._position < len(self._text):
            character = self._text[self._position]
            if character == _COMMENT:
                self._position = len(self._text)
                break
            self._position += 1
            if character == _ESCAPE:
                characters.append(self._escaped())
            elif character in _WHITESPACE:
                break
            else:
                characters.append(character)
        return "".join(characters)

    def _quoted_value(self) -> str:
        """Read a value in double quotes, which must end on its own line.

        Raises:
            _ConfigurationRefused: The line ends before the closing quote; chktex would then
                read on past the end of the line.
        """
        self._position += 1
        characters: list[str] = []
        while True:
            if self._position >= len(self._text):
                self.refuse(
                    "leaves a quoted value open at the end of a line",
                    'Close the value with " on the same line.',
                )
            character = self._text[self._position]
            self._position += 1
            if character == _QUOTE:
                return "".join(characters)
            characters.append(self._escaped() if character == _ESCAPE else character)

    def _escaped(self) -> str:
        """Read the escape after an escape character and return the character it stands for.

        Raises:
            _ConfigurationRefused: The escape is unknown or incomplete, or stands for NUL or
                for a value beyond one byte; chktex would warn and drop the character, or
                read past the end of the line.
        """
        if self._position >= len(self._text):
            self.refuse(
                "ends a line with the escape character !",
                "Write !! for an exclamation mark.",
            )
        character = self._text[self._position]
        self._position += 1
        lowered = _ascii_lower(character)
        if lowered in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[lowered]
        if lowered == "x":
            value = self._number(_HEX_DIGITS, 2, 16, 0)
        elif lowered == "d":
            value = self._number(_DECIMAL_DIGITS, 3, 10, 0)
        elif character in _OCTAL_DIGITS:
            value = self._number(_OCTAL_DIGITS, 2, 8, int(character))
        else:
            self.refuse(
                f"uses the unknown escape !{character}",
                "Write !! for an exclamation mark, or use an escape the chktexrc format lists.",
            )
        if not 0 < value <= _LARGEST_CHARACTER:
            self.refuse(
                "uses an escape that stands for NUL or for no single character",
                "Use an escape for a character from 1 to 255.",
            )
        return chr(value)

    def _number(self, digits: str, count: int, base: int, start: int) -> int:
        """Read the ``count`` digits of a numeric escape and return its value.

        Args:
            digits: The digits valid in ``base``.
            count: How many digits must follow.
            base: The base of the number.
            start: The value of a digit already read, for octal escapes.

        Returns:
            The value of the escape.

        Raises:
            _ConfigurationRefused: Fewer than ``count`` digits follow.
        """
        value = start
        for _ in range(count):
            if self._position >= len(self._text) or self._text[self._position] not in digits:
                self.refuse(
                    "has a numeric escape with too few digits",
                    "Write numeric escapes in full, such as !x2d, !d045 or !055.",
                )
            value = value * base + int(self._text[self._position], base)
            self._position += 1
        return value


class _Item:
    """One value that a configuration adds to a list, and its line.

    Args:
        text: The value with its escapes replaced.
        line: The line it is on.

    Attributes:
        text: The value with its escapes replaced.
        line: The line it is on.
    """

    __slots__ = ("line", "text")

    def __init__(self, text: str, line: int) -> None:
        self.text = text
        self.line = line


class _SettingsParser:
    """chktex's parser (``ProcessRC``): which values each setting receives.

    Every value added to ``CmdLine`` or ``TeXInputs`` is kept, including those that a later
    ``=`` replaces, so the checks look at a superset of what chktex finally applies.

    Args:
        reader: The tokenizer over the configuration.

    Attributes:
        command_line: The values added to ``CmdLine``.
        search_folders: The values added to ``TeXInputs``.
    """

    def __init__(self, reader: _ConfigurationReader) -> None:
        self._reader = reader
        self._expected = frozenset({_Token.WORD, _Token.END})
        self._setting = _Setting("")
        self._single_value = False
        self.command_line: list[_Item] = []
        self.search_folders: list[_Item] = []

    def parse(self) -> None:
        """Read every setting of the configuration.

        Raises:
            _ConfigurationRefused: A token is where chktex would not accept it, or a setting
                is unknown.
        """
        while True:
            token, text = self._reader.token(self._expected)
            if token not in self._expected:
                self._reader.refuse(
                    f"has {token.value} where chktex expects "
                    f"{' or '.join(sorted(item.value for item in self._expected))}"
                )
            if token is _Token.END:
                return
            self._accept(token, text)

    def _accept(self, token: _Token, text: str) -> None:
        """Apply one accepted token, as ``ProcessRC`` does.

        Args:
            token: The token.
            text: Its text.
        """
        setting = self._setting
        if token is _Token.WORD:
            self._name(text)
        elif token is _Token.EQUAL:
            self._single_value = True
            self._expected = self._openers(setting) | (
                {_Token.ITEM} if setting.has_value else frozenset()
            )
        elif token is _Token.ITEM:
            self._item(text)
        elif token in _OPENERS:
            self._single_value = False
            self._expected = frozenset({_Token.ITEM, _OPENERS[token]})
        else:
            self._expected = self._openers(setting) | {_Token.EQUAL, _Token.WORD, _Token.END}

    def _name(self, name: str) -> None:
        """Start the setting ``name``.

        Raises:
            _ConfigurationRefused: chktex has no setting of that name.
        """
        setting = _SETTINGS.get("".join(map(_ascii_lower, name)))
        if setting is None:
            self._reader.refuse(
                f"names the setting {name}, which chktex does not have",
                "Correct the name or remove the setting; chktex stops reading a "
                "configuration at a name it does not know.",
            )
        self._setting = setting
        self._expected = self._openers(setting) | {_Token.EQUAL}

    def _item(self, text: str) -> None:
        """Take a value: the single value after ``=``, or one more item of a list."""
        if self._single_value:
            self._single_value = False
            self._expected = frozenset({_Token.WORD, _Token.END})
            return
        key = self._setting.name.lower()
        if key == _COMMAND_LINE:
            self.command_line.append(_Item(text, self._reader.line))
        elif key == _SEARCH_FOLDERS:
            self.search_folders.append(_Item(text, self._reader.line))

    @staticmethod
    def _openers(setting: _Setting) -> frozenset[_Token]:
        """Return the list openers that ``setting`` accepts."""
        return frozenset(
            token
            for token, accepted in (
                (_Token.OPEN, setting.has_list),
                (_Token.BRACKET_OPEN, setting.has_case_list),
            )
            if accepted
        )


def _option_letter(option: str) -> str | None:
    """Return the short option that a command-line item starts, or ``None`` for no option.

    A long option counts when its name is a unique prefix of one of chktex's, as
    ``getopt_long`` reads it.
    """
    if option.startswith("--"):
        name = option[2:].partition("=")[0]
        matches = {letter for long, letter in _LONG_OPTIONS.items() if long.startswith(name)}
        return matches.pop() if name and len(matches) == 1 else None
    if len(option) > 1 and option.startswith("-"):
        return option[1]
    return None


def _option_refusal(item: _Item) -> ConfigurationRefusal:
    """Explain why one ``CmdLine`` item is refused.

    Args:
        item: The item, which is not an accepted option.

    Returns:
        The refusal, naming what the option would do where Fledge knows it.
    """
    letter = _option_letter(item.text)
    shown = item.text if len(item.text) <= 40 else item.text[:40] + "…"
    if letter in _REFUSED_OPTIONS:
        reason = f"sets {shown} in CmdLine, an option that {_REFUSED_OPTIONS[letter]}"
    elif letter is None:
        reason = f"has {shown!r} in CmdLine, which is not a chktex option"
    else:
        reason = (
            f"has {shown} in CmdLine, which is not one option written on its own in a "
            "form Fledge accepts"
        )
    return ConfigurationRefusal(
        item.line,
        reason,
        f"Remove it from CmdLine. Fledge accepts there {_ACCEPTED_OPTIONS_TEXT}.",
    )


def _check_command_line(items: Sequence[_Item]) -> None:
    """Accept the ``CmdLine`` items only when each is an accepted option or its argument.

    Args:
        items: The items, in order; an option's argument may be the next item, as
            ``getopt`` reads it.

    Raises:
        _ConfigurationRefused: An item is not an accepted option, or an option misses its
            argument or has one that is not accepted.
    """
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if item.text in _TAKES_WARNING_CHOICE:
            argument = items[index] if index < len(items) else None
            index += 1
            if argument is None or not _WARNING_CHOICE_ARGUMENT.fullmatch(argument.text):
                raise _ConfigurationRefused(
                    ConfigurationRefusal(
                        item.line,
                        f"gives {item.text} in CmdLine no warning number or all after it",
                        "Write the option with its argument, such as -n2 or -nall.",
                    )
                )
        elif item.text in _TAKES_ANY_ARGUMENT:
            index += 1
            if index > len(items):
                # getopt rejects the option, and chktex exits before it checks anything.
                raise _ConfigurationRefused(
                    ConfigurationRefusal(
                        item.line,
                        f"ends CmdLine with {item.text}, whose argument is missing",
                        "Give the option its argument, or remove it.",
                    )
                )
        elif not _ACCEPTED_OPTION.fullmatch(item.text):
            raise _ConfigurationRefused(_option_refusal(item))


def _check_search_folders(items: Sequence[_Item]) -> None:
    """Accept the ``TeXInputs`` folders only when each is relative and stays in the project.

    Raises:
        _ConfigurationRefused: A folder is absolute, starts from a home folder or climbs
            out with ``..``.
    """
    for item in items:
        parts = item.text.replace("\\", "/").split("/")
        if item.text.startswith(("/", "\\", "~")) or ".." in parts:
            raise _ConfigurationRefused(
                ConfigurationRefusal(
                    item.line,
                    f"names the folder {item.text!r} in TeXInputs, which can lie outside "
                    "the project",
                    "Name only folders inside the project, relative to the main file; "
                    "Fledge does not let chktex follow \\input itself, so TeXInputs has no "
                    "effect in its run.",
                )
            )


def judge_configuration(data: bytes) -> ConfigurationRefusal | None:
    """Read a ``.chktexrc`` as chktex would and judge whether chktex may read it.

    Args:
        data: The bytes of the file.

    Returns:
        Why the file is refused, or ``None`` when chktex may read it.
    """
    if data.startswith(_BYTE_ORDER_MARK):
        return ConfigurationRefusal(
            1,
            "starts with a byte-order mark, at which chktex stops reading the file",
            "Save the file without a byte-order mark.",
        )
    if b"\0" in data:
        return ConfigurationRefusal(
            data[: data.index(b"\0")].count(b"\n") + 1,
            "holds a NUL byte, after which chktex ignores the rest of its line",
            "Remove the NUL byte.",
        )
    try:
        parser = _SettingsParser(_ConfigurationReader(_configuration_lines(data.decode("latin-1"))))
        parser.parse()
        _check_command_line(parser.command_line)
        _check_search_folders(parser.search_folders)
    except _ConfigurationRefused as refused:
        return refused.refusal
    return None


def read_project_configuration(root: Path, main: str) -> ProjectConfiguration:
    """Find, read and judge the ``.chktexrc`` beside ``main``, where chktex would look for it.

    Args:
        root: The project root.
        main: The main file, relative to ``root``.

    Returns:
        The configuration: no content when there is no file, the bytes when chktex may
        read them, or a refusal for a file that is unreadable, a link, too large or unsafe.
    """
    name = str(PurePosixPath(main).parent / CONFIGURATION_NAME)
    try:
        data = read_regular_file(root / name, MAX_CONFIGURATION_BYTES)
    except FileNotFoundError:
        return ProjectConfiguration(name)
    except FileTooLargeError:
        reason = f"is larger than {byte_count_text(MAX_CONFIGURATION_BYTES)}"
        return ProjectConfiguration(
            name, refusal=ConfigurationRefusal(None, reason, "Shorten the file.")
        )
    except OSError as error:
        cause = (error.strerror or type(error).__name__).lower()
        return ProjectConfiguration(
            name,
            refusal=ConfigurationRefusal(
                None, f"cannot be read safely ({cause})", "Make it a regular file."
            ),
        )
    refusal = judge_configuration(data)
    if refusal is not None:
        return ProjectConfiguration(name, refusal=refusal)
    return ProjectConfiguration(name, content=data)
